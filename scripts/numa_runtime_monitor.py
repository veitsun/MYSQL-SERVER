#!/usr/bin/env python3
"""Runtime NUMA access locality monitor.

Unlike /proc/vmstat numa_local/numa_other (which only count page *allocation*
locality), this script uses two data sources that reflect actual runtime
memory access patterns:

  1. /sys/devices/system/node/nodeX/numastat
       local_node : node X memory accessed by CPUs *on* node X
       other_node : node X memory accessed by CPUs on *other* nodes
     → other_node delta / (local_node + other_node) delta = remote access %

  2. /proc/vmstat  numa_hint_faults / numa_hint_faults_local
     AutoNUMA periodically unmaps pages; when a thread accesses them, the
     kernel checks whether the page is local.  These fault counts are a
     second, independent proxy for access locality (requires
     kernel.numa_balancing = 1).
     → (hint_faults - hint_faults_local) / hint_faults = remote hint %

Both sources are system-wide (not per-process).  In a dedicated benchmark
where mysqld is the dominant workload the numbers closely reflect mysqld's
behaviour.

Usage:
    python3 scripts/numa_runtime_monitor.py --pid <mysqld-pid> [options]
    python3 scripts/numa_runtime_monitor.py --pid-file <path/to/mysql.pid>

    # one-shot snapshot
    python3 scripts/numa_runtime_monitor.py --pid 12345 --once

    # continuous monitoring, 5-second interval
    python3 scripts/numa_runtime_monitor.py --pid 12345 --interval 5

    # run for 60 seconds, write CSV, auto-plot on exit
    python3 scripts/numa_runtime_monitor.py --pid 12345 \
        --interval 5 --duration 60 --output ./numa_profile/runtime.csv
"""

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# /sys/devices/system/node/nodeX/numastat helpers
# ---------------------------------------------------------------------------

NODE_STAT_KEYS = ["numa_hit", "numa_miss", "local_node", "other_node"]


def read_node_numastats() -> Dict[int, Dict[str, int]]:
    """Return {node_id: {key: value}} for all available NUMA nodes."""
    base = "/sys/devices/system/node"
    result: Dict[int, Dict[str, int]] = {}
    try:
        entries = os.listdir(base)
    except OSError:
        return result
    for entry in sorted(entries):
        if not entry.startswith("node"):
            continue
        try:
            node_id = int(entry[4:])
        except ValueError:
            continue
        stat_path = os.path.join(base, entry, "numastat")
        stats: Dict[str, int] = {}
        try:
            with open(stat_path) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and parts[0] in NODE_STAT_KEYS:
                        stats[parts[0]] = int(parts[1])
        except OSError:
            continue
        result[node_id] = stats
    return result


def aggregate_node_deltas(
        before: Dict[int, Dict[str, int]],
        after: Dict[int, Dict[str, int]]) -> Dict[str, int]:
    """Sum deltas of each key across all nodes."""
    totals: Dict[str, int] = defaultdict(int)
    for node in after:
        b = before.get(node, {})
        for k in NODE_STAT_KEYS:
            totals[k] += after[node].get(k, 0) - b.get(k, 0)
    return dict(totals)


def remote_ratio_from_node_delta(delta: Dict[str, int]) -> float:
    local = delta.get("local_node", 0)
    other = delta.get("other_node", 0)
    total = local + other
    return other / total * 100 if total > 0 else 0.0


def print_node_stats_report(delta: Dict[str, int],
                             after: Dict[int, Dict[str, int]],
                             elapsed: float) -> None:
    remote_pct = remote_ratio_from_node_delta(delta)
    print(f"\n=== /sys node numastat (delta over {elapsed:.1f}s) ===")
    print(f"  local_node  : {delta.get('local_node', 0):>14,}  "
          f"(memory accessed by local CPU)")
    print(f"  other_node  : {delta.get('other_node', 0):>14,}  "
          f"(memory accessed by REMOTE CPU)  ← cross-NUMA")
    print(f"  numa_hit    : {delta.get('numa_hit', 0):>14,}  "
          f"(alloc succeeded on target node)")
    print(f"  numa_miss   : {delta.get('numa_miss', 0):>14,}  "
          f"(alloc fell back to other node)")
    print(f"  remote access %: {remote_pct:>10.2f}%")
    print()
    print("  Per-node breakdown (absolute counts):")
    for node in sorted(after):
        s = after[node]
        total = s.get("local_node", 0) + s.get("other_node", 0)
        node_remote = (s.get("other_node", 0) / total * 100
                       if total > 0 else 0.0)
        print(f"    Node {node}: local={s.get('local_node', 0):>12,}  "
              f"other={s.get('other_node', 0):>12,}  "
              f"remote%={node_remote:5.1f}%")


# ---------------------------------------------------------------------------
# /proc/vmstat AutoNUMA hint-fault helpers
# ---------------------------------------------------------------------------

HINT_KEYS = ["numa_hint_faults", "numa_hint_faults_local",
             "numa_pages_migrated"]


def read_hint_stats() -> Dict[str, int]:
    stats: Dict[str, int] = {}
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] in HINT_KEYS:
                    stats[parts[0]] = int(parts[1])
    except OSError as e:
        print(f"[warn] cannot read /proc/vmstat: {e}", file=sys.stderr)
    return stats


def hint_delta(before: Dict[str, int],
               after: Dict[str, int]) -> Dict[str, int]:
    return {k: after.get(k, 0) - before.get(k, 0) for k in HINT_KEYS}


def remote_ratio_from_hint_delta(delta: Dict[str, int]) -> float:
    total = delta.get("numa_hint_faults", 0)
    local = delta.get("numa_hint_faults_local", 0)
    remote = total - local
    return remote / total * 100 if total > 0 else 0.0


def print_hint_report(delta: Dict[str, int], elapsed: float) -> None:
    total = delta.get("numa_hint_faults", 0)
    local = delta.get("numa_hint_faults_local", 0)
    remote = total - local
    remote_pct = remote_ratio_from_hint_delta(delta)
    migrated = delta.get("numa_pages_migrated", 0)

    print(f"\n=== AutoNUMA hint faults (delta over {elapsed:.1f}s) ===")
    print(f"  hint_faults       : {total:>12,}  (pages probed for locality)")
    print(f"  hint_faults_local : {local:>12,}  (page already on local node)")
    print(f"  hint_faults_remote: {remote:>12,}  (page on remote node)")
    print(f"  pages_migrated    : {migrated:>12,}  (pages moved closer to CPU)")
    print(f"  remote access %   : {remote_pct:>10.2f}%")
    if total == 0:
        print("  [note] no hint faults this interval — "
              "check kernel.numa_balancing=1")


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def plot_remote_ratios(timestamps: List[float],
                       node_remote: List[float],
                       hint_remote: List[float],
                       output_path: Optional[str]) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(13, 5))
        ax.plot(timestamps, node_remote, color="tab:red", linewidth=1.5,
                marker="o", markersize=3, label="node numastat remote %")
        if any(v > 0 for v in hint_remote):
            ax.plot(timestamps, hint_remote, color="tab:blue", linewidth=1.5,
                    marker="s", markersize=3, linestyle="--",
                    label="AutoNUMA hint remote %")

        if node_remote:
            avg = sum(node_remote) / len(node_remote)
            ax.axhline(y=avg, color="gray", linestyle=":", linewidth=1,
                       label=f"avg node remote {avg:.1f}%")

        ax.set_xlabel("Elapsed time (s)")
        ax.set_ylabel("Remote NUMA access (%)")
        ax.set_title("Runtime NUMA Remote Access Ratio Over Time")
        all_vals = node_remote + hint_remote
        ax.set_ylim(0, max(max(all_vals) * 1.15, 5) if all_vals else 5)
        ax.legend()
        ax.grid(True, alpha=0.3)
        plt.tight_layout()

        plot_path = (output_path.rsplit(".", 1)[0] + "_plot.png"
                     if output_path else "numa_runtime_plot.png")
        plt.savefig(plot_path, dpi=150)
        plt.close()
        print(f"\n[plot] saved to {plot_path}")

    except ImportError:
        if not node_remote:
            return
        width = min(len(node_remote), 70)
        height = 16
        max_val = max(max(node_remote), 1)
        print("\n--- Runtime Remote NUMA Access % over time (ASCII) ---")
        # downsample if too many points
        step = max(1, len(node_remote) // width)
        data = node_remote[::step]
        ts_data = timestamps[::step]
        for row in range(height, -1, -1):
            threshold = max_val * row / height
            line = f"{threshold:5.1f}% |"
            for v in data:
                line += "#" if v >= threshold else " "
            print(line)
        print("       +" + "-" * len(data))
        print(f"        0s{' ' * max(0, len(data) - 4)}{ts_data[-1]:.0f}s")
        avg = sum(node_remote) / len(node_remote)
        print(f"  avg={avg:.2f}%  max={max(node_remote):.2f}%  "
              f"min={min(node_remote):.2f}%")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def resolve_pid(args) -> int:
    if args.pid:
        return args.pid
    if args.pid_file:
        with open(args.pid_file) as f:
            return int(f.read().strip())
    raise ValueError("provide --pid or --pid-file")


def run_once(pid: int) -> None:
    node_stats = read_node_numastats()
    hint_stats = read_hint_stats()

    print(f"=== One-shot NUMA runtime snapshot (PID {pid}) ===\n")

    print("--- /sys node numastat (absolute) ---")
    for node in sorted(node_stats):
        s = node_stats[node]
        total = s.get("local_node", 0) + s.get("other_node", 0)
        remote_pct = (s.get("other_node", 0) / total * 100
                      if total > 0 else 0.0)
        print(f"  Node {node}: local={s.get('local_node', 0):>12,}  "
              f"other={s.get('other_node', 0):>12,}  "
              f"remote%={remote_pct:5.1f}%")

    print("\n--- /proc/vmstat AutoNUMA hint faults (absolute) ---")
    for k in HINT_KEYS:
        print(f"  {k:<30s}: {hint_stats.get(k, 0):>12,}")


def run_loop(pid: int, interval: float,
             duration: Optional[float] = None,
             output: Optional[str] = None) -> None:
    stop_msg = f"duration={duration}s" if duration else "Ctrl-C to stop"
    print(f"Monitoring PID {pid}, interval={interval}s, {stop_msg}.\n")
    print("Data source: /sys/devices/system/node/nodeX/numastat  "
          "(runtime cross-NUMA access counts)\n")

    prev_node = read_node_numastats()
    prev_hint = read_hint_stats()
    start_time = time.monotonic()
    prev_time = start_time

    ts_elapsed: List[float] = []
    ts_node_remote: List[float] = []
    ts_hint_remote: List[float] = []

    csv_file = None
    writer = None
    if output:
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        csv_file = open(output, "w", newline="")
        writer = csv.writer(csv_file)
        writer.writerow(["timestamp", "elapsed_s",
                         "local_node", "other_node", "node_remote_pct",
                         "hint_faults", "hint_local", "hint_remote_pct",
                         "pages_migrated"])

    try:
        while True:
            time.sleep(interval)
            now = time.monotonic()
            elapsed = now - prev_time
            elapsed_total = now - start_time

            cur_node = read_node_numastats()
            cur_hint = read_hint_stats()

            node_delta = aggregate_node_deltas(prev_node, cur_node)
            h_delta = hint_delta(prev_hint, cur_hint)

            node_remote_pct = remote_ratio_from_node_delta(node_delta)
            hint_remote_pct = remote_ratio_from_hint_delta(h_delta)

            print("\n" + "=" * 72)
            print(f"  Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                  f"PID: {pid}  Elapsed: {elapsed_total:.0f}s")
            print("=" * 72)

            print_node_stats_report(node_delta, cur_node, elapsed)
            print_hint_report(h_delta, elapsed)

            ts_elapsed.append(elapsed_total)
            ts_node_remote.append(node_remote_pct)
            ts_hint_remote.append(hint_remote_pct)

            if writer:
                writer.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{elapsed_total:.1f}",
                    node_delta.get("local_node", 0),
                    node_delta.get("other_node", 0),
                    f"{node_remote_pct:.4f}",
                    h_delta.get("numa_hint_faults", 0),
                    h_delta.get("numa_hint_faults_local", 0),
                    f"{hint_remote_pct:.4f}",
                    h_delta.get("numa_pages_migrated", 0),
                ])
                csv_file.flush()

            prev_node = cur_node
            prev_hint = cur_hint
            prev_time = now

            if duration and elapsed_total >= duration:
                print(f"\n[info] duration {duration}s reached, stopping.")
                break

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if csv_file:
            csv_file.close()
            print(f"[info] CSV saved to {output}")

    if len(ts_elapsed) >= 2:
        plot_remote_ratios(ts_elapsed, ts_node_remote, ts_hint_remote, output)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor runtime NUMA cross-node access ratio for mysqld.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pid", type=int, help="mysqld PID")
    group.add_argument("--pid-file", metavar="PATH",
                       help="path to mysql.pid file")

    parser.add_argument("--once", action="store_true",
                        help="print one snapshot and exit")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="sampling interval in seconds (default: 5)")
    parser.add_argument("--duration", type=float, default=None,
                        help="stop after this many seconds and plot results")
    parser.add_argument("--output", metavar="PATH", default=None,
                        help="write per-sample CSV to this file; "
                             "plot image saved as <name>_plot.png")

    args = parser.parse_args()

    try:
        pid = resolve_pid(args)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(f"/proc/{pid}"):
        print(f"error: PID {pid} not found in /proc", file=sys.stderr)
        sys.exit(1)

    if args.once:
        run_once(pid)
    else:
        run_loop(pid, args.interval,
                 duration=args.duration, output=args.output)


if __name__ == "__main__":
    main()
