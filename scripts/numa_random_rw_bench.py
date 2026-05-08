#!/usr/bin/env python3
"""Random read/write benchmark for an existing InnoDB table.

This script is meant to act like a lightweight sysbench replacement for the
prepared table:

  numa_demo.t(id BIGINT PK AUTO_INCREMENT, c1 VARCHAR(1000), c2 INT)

It deliberately uses the existing `mysql` CLI, so it does not need any
external Python MySQL driver.
"""

import argparse
import os
import random
import shlex
import signal
import string
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple


def quote_ident(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_mysql_cmd(args: argparse.Namespace, database: Optional[str] = None) -> List[str]:
    cmd = [args.mysql_bin, "--no-defaults", "--batch", "--raw", "--skip-column-names", "--silent"]

    if args.user:
        cmd.append(f"--user={args.user}")
    if args.password:
        cmd.append(f"--password={args.password}")
    if args.socket:
        cmd.append(f"--socket={args.socket}")
    if args.host:
        cmd.append(f"--host={args.host}")
    if args.port:
        cmd.append(f"--port={args.port}")

    if database:
        cmd.append(database)

    return cmd


def run_mysql_once(
    args: argparse.Namespace, sql: str, database: Optional[str] = None
) -> str:
    cmd = build_mysql_cmd(args, database)
    cmd.extend(["-e", sql])
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            "mysql command failed with code {}.\ncmd={}\nstderr={}".format(
                res.returncode, " ".join(shlex.quote(part) for part in cmd), res.stderr.strip()
            )
        )
    return res.stdout


@dataclass
class WorkerStats:
    transactions: int = 0
    statements: int = 0
    read_txs: int = 0
    write_txs: int = 0
    errors: int = 0
    latency_ms: float = 0.0


class HighWaterMark:
    def __init__(self, initial_value: int):
        self._value = initial_value
        self._lock = threading.Lock()

    def get(self) -> int:
        with self._lock:
            return self._value

    def set_if_greater(self, new_value: int) -> None:
        with self._lock:
            if new_value > self._value:
                self._value = new_value

    def bump(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


def build_random_payload(rng: random.Random, width: int, tag: str) -> str:
    prefix = "{}{:010d}".format(tag, rng.randint(0, 9_999_999_999))
    if len(prefix) >= width:
        return prefix[:width]
    charset = string.ascii_lowercase + string.digits
    suffix = "".join(rng.choice(charset) for _ in range(width - len(prefix)))
    return prefix + suffix


def pick_existing_id(rng: random.Random, high_water_mark: HighWaterMark) -> int:
    max_id = max(1, high_water_mark.get())
    return rng.randint(1, max_id)


def build_read_sql(
    rng: random.Random, args: argparse.Namespace, table_name: str, high_water_mark: HighWaterMark
) -> Tuple[str, int]:
    statements = []
    for _ in range(args.point_selects):
        row_id = pick_existing_id(rng, high_water_mark)
        statements.append("SELECT c2 FROM {} WHERE id = {}".format(table_name, row_id))

    start_id = pick_existing_id(rng, high_water_mark)
    end_id = start_id + max(0, args.range_size - 1)
    statements.append(
        "SELECT COUNT(*), COALESCE(SUM(c2), 0) FROM {} WHERE id BETWEEN {} AND {}".format(
            table_name, start_id, end_id
        )
    )

    return ";\n".join(statements) + ";", len(statements)


def build_write_sql(
    rng: random.Random,
    args: argparse.Namespace,
    table_name: str,
    high_water_mark: HighWaterMark,
    insert_floor_id: int,
) -> Tuple[str, int]:
    op = rng.random()

    if op < args.update_pct:
        row_id = pick_existing_id(rng, high_water_mark)
        delta = rng.randint(1, 1000)
        sql = "START TRANSACTION; UPDATE {} SET c2 = c2 + {} WHERE id = {}; COMMIT;".format(
            table_name, delta, row_id
        )
        return sql, 3
    elif op < args.update_pct + args.insert_pct:
        payload = build_random_payload(rng, args.c1_size, "ins")
        c2 = rng.randint(1, 2_000_000_000)
        sql = "START TRANSACTION; INSERT INTO {}(c1, c2) VALUES ({}, {}); COMMIT;".format(
            table_name, sql_string(payload), c2
        )
        return sql, 3
    else:
        row_id = rng.randint(insert_floor_id, max(insert_floor_id, high_water_mark.get()))
        sql = "START TRANSACTION; DELETE FROM {} WHERE id = {}; COMMIT;".format(
            table_name, row_id
        )
        return sql, 3


def worker_main(
    worker_id: int,
    args: argparse.Namespace,
    table_name: str,
    high_water_mark: HighWaterMark,
    insert_floor_id: int,
    stop_event: threading.Event,
    stats: WorkerStats,
) -> None:
    rng = random.Random(args.seed + worker_id)
    while not stop_event.is_set():
        if args.events > 0 and stats.transactions >= args.events:
            break

        start = time.perf_counter()
        try:
            if rng.random() < args.read_ratio:
                sql, stmt_count = build_read_sql(rng, args, table_name, high_water_mark)
                run_mysql_once(args, sql, args.database)
                stats.read_txs += 1
            else:
                sql, stmt_count = build_write_sql(
                    rng, args, table_name, high_water_mark, insert_floor_id
                )
                run_mysql_once(args, sql, args.database)
                if "INSERT INTO" in sql:
                    high_water_mark.bump()
                stats.write_txs += 1

            stats.statements += stmt_count
            stats.transactions += 1
        except Exception:
            stats.errors += 1
            time.sleep(0.05)
        finally:
            stats.latency_ms += (time.perf_counter() - start) * 1000.0


def fetch_table_state(args: argparse.Namespace) -> Tuple[int, int]:
    sql = "SELECT COALESCE(MAX(id), 0), COUNT(*) FROM {}.{}".format(
        quote_ident(args.database), quote_ident(args.table)
    )
    out = run_mysql_once(args, sql)
    parts = out.strip().split("\t")
    if len(parts) != 2:
        raise RuntimeError("unexpected metadata query result: {!r}".format(out))
    return int(parts[0]), int(parts[1])


def print_report(
    elapsed: float, delta_elapsed: float, stats_snapshot: WorkerStats, delta_stats: WorkerStats
) -> None:
    tps = delta_stats.transactions / delta_elapsed if delta_elapsed > 0 else 0.0
    qps = delta_stats.statements / delta_elapsed if delta_elapsed > 0 else 0.0
    avg_lat = (
        delta_stats.latency_ms / delta_stats.transactions if delta_stats.transactions > 0 else 0.0
    )
    print(
        "[{:7.1f}s] tx={} q={} reads={} writes={} errs={} tps={:.1f} qps={:.1f} avg_lat={:.2f}ms".format(
            elapsed,
            stats_snapshot.transactions,
            stats_snapshot.statements,
            stats_snapshot.read_txs,
            stats_snapshot.write_txs,
            stats_snapshot.errors,
            tps,
            qps,
            avg_lat,
        ),
        flush=True,
    )


def sum_stats(items: List[WorkerStats]) -> WorkerStats:
    total = WorkerStats()
    for item in items:
        total.transactions += item.transactions
        total.statements += item.statements
        total.read_txs += item.read_txs
        total.write_txs += item.write_txs
        total.errors += item.errors
        total.latency_ms += item.latency_ms
    return total


def copy_stats(item: WorkerStats) -> WorkerStats:
    return WorkerStats(
        transactions=item.transactions,
        statements=item.statements,
        read_txs=item.read_txs,
        write_txs=item.write_txs,
        errors=item.errors,
        latency_ms=item.latency_ms,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Random read/write benchmark for an existing MySQL/InnoDB table."
    )
    parser.add_argument("--mysql-bin", default="build/runtime_output_directory/mysql")
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default=os.environ.get("MYSQL_PWD", ""))
    parser.add_argument("--socket", default=os.environ.get("SOCKET", ""))
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--database", default="numa_demo")
    parser.add_argument("--table", default="t")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--time", type=int, default=60)
    parser.add_argument("--events", type=int, default=0, help="max transactions per worker; 0 means unlimited")
    parser.add_argument("--report-interval", type=int, default=5)
    parser.add_argument("--read-ratio", type=float, default=0.8, help="fraction of transactions that are reads")
    parser.add_argument("--point-selects", type=int, default=8)
    parser.add_argument("--range-size", type=int, default=100)
    parser.add_argument("--update-pct", type=float, default=0.70)
    parser.add_argument("--insert-pct", type=float, default=0.20)
    parser.add_argument("--delete-pct", type=float, default=0.10)
    parser.add_argument("--c1-size", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260507)
    args = parser.parse_args()

    if not 0.0 <= args.read_ratio <= 1.0:
        parser.error("--read-ratio must be between 0 and 1")

    total_write_mix = args.update_pct + args.insert_pct + args.delete_pct
    if abs(total_write_mix - 1.0) > 1e-9:
        parser.error("--update-pct + --insert-pct + --delete-pct must equal 1.0")

    if not args.socket and not args.host:
        parser.error("either --socket or --host must be provided")

    if args.time <= 0 and args.events <= 0:
        parser.error("either --time > 0 or --events > 0 is required")

    return args


def main() -> int:
    args = parse_args()
    table_name = "{}.{}".format(quote_ident(args.database), quote_ident(args.table))

    max_id, row_count = fetch_table_state(args)
    if max_id <= 0 or row_count <= 0:
        raise RuntimeError("table {} appears to be empty".format(table_name))

    print(
        "Starting benchmark: table={}, initial_rows={}, initial_max_id={}, threads={}, duration={}s".format(
            table_name, row_count, max_id, args.threads, args.time
        ),
        flush=True,
    )

    stop_event = threading.Event()
    worker_stats = [WorkerStats() for _ in range(args.threads)]
    high_water_mark = HighWaterMark(max_id)
    insert_floor_id = max_id + 1
    threads = []

    def handle_signal(signum, _frame) -> None:
        print("Received signal {}, stopping...".format(signum), flush=True)
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    for worker_id in range(args.threads):
        thread = threading.Thread(
            target=worker_main,
            args=(
                worker_id,
                args,
                table_name,
                high_water_mark,
                insert_floor_id,
                stop_event,
                worker_stats[worker_id],
            ),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    start_time = time.time()
    prev_snapshot = WorkerStats()

    try:
        while not stop_event.is_set():
            time.sleep(max(1, args.report_interval))
            elapsed = time.time() - start_time
            snapshot = sum_stats(worker_stats)
            delta = WorkerStats(
                transactions=snapshot.transactions - prev_snapshot.transactions,
                statements=snapshot.statements - prev_snapshot.statements,
                read_txs=snapshot.read_txs - prev_snapshot.read_txs,
                write_txs=snapshot.write_txs - prev_snapshot.write_txs,
                errors=snapshot.errors - prev_snapshot.errors,
                latency_ms=snapshot.latency_ms - prev_snapshot.latency_ms,
            )
            print_report(elapsed, max(0.001, args.report_interval), snapshot, delta)
            prev_snapshot = copy_stats(snapshot)

            all_done = all(
                args.events > 0 and stats.transactions >= args.events for stats in worker_stats
            )
            if all_done:
                stop_event.set()
            if args.time > 0 and elapsed >= args.time:
                stop_event.set()
    finally:
        stop_event.set()
        for thread in threads:
            thread.join()

    total_elapsed = max(0.001, time.time() - start_time)
    final_stats = sum_stats(worker_stats)
    avg_lat = (
        final_stats.latency_ms / final_stats.transactions if final_stats.transactions > 0 else 0.0
    )

    print(
        "Finished: elapsed={:.1f}s tx={} q={} reads={} writes={} errs={} tps={:.1f} qps={:.1f} avg_lat={:.2f}ms final_max_id={}".format(
            total_elapsed,
            final_stats.transactions,
            final_stats.statements,
            final_stats.read_txs,
            final_stats.write_txs,
            final_stats.errors,
            final_stats.transactions / total_elapsed,
            final_stats.statements / total_elapsed,
            avg_lat,
            high_water_mark.get(),
        ),
        flush=True,
    )

    return 0 if final_stats.errors == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        sys.exit(2)
