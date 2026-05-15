#!/usr/bin/env python3
"""NUMA access statistics monitor for mysqld.

Two data sources:
  1. /proc/PID/numa_maps  — physical page distribution of each memory segment
     (shows WHERE memory physically lives, not who accessed it)
  2. /proc/vmstat         — kernel NUMA access counters (delta mode)
     numa_local  : accesses where thread and page are on the same node
     numa_other  : accesses where thread and page are on different nodes

Usage:
    python3 scripts/numa_stat_monitor.py --pid <mysqld-pid> [options]
    python3 scripts/numa_stat_monitor.py --pid-file <path/to/mysql.pid> [options]

    # one-shot snapshot (memory placement only)
    python3 scripts/numa_stat_monitor.py --pid 12345 --once

    # continuous monitoring, 5-second interval
    python3 scripts/numa_stat_monitor.py --pid 12345 --interval 5

    # show buffer-pool segments only (large anonymous mappings > threshold)
    python3 scripts/numa_stat_monitor.py --pid 12345 --bp-only --bp-min-mb 64
"""

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# /proc/vmstat helpers
# ---------------------------------------------------------------------------

VMSTAT_KEYS = ["numa_hit", "numa_miss", "numa_foreign",
               "numa_interleave", "numa_local", "numa_other"]


def read_vmstat() -> Dict[str, int]:
    stats: Dict[str, int] = {}
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] in VMSTAT_KEYS:
                    stats[parts[0]] = int(parts[1])
    except OSError as e:
        print(f"[warn] cannot read /proc/vmstat: {e}", file=sys.stderr)
    return stats


def vmstat_delta(before: Dict[str, int],
                 after: Dict[str, int]) -> Dict[str, int]:
    return {k: after.get(k, 0) - before.get(k, 0) for k in VMSTAT_KEYS}


def print_vmstat_report(delta: Dict[str, int], elapsed: float) -> None:
    total_access = delta.get("numa_local", 0) + delta.get("numa_other", 0)
    remote_pct = (delta["numa_other"] / total_access * 100
                  if total_access > 0 else 0.0)

    print("\n=== /proc/vmstat NUMA counters (delta over {:.1f}s) ===".format(elapsed))
    print(f"  numa_hit        : {delta.get('numa_hit', 0):>12,}  (alloc landed on target node)")
    print(f"  numa_miss       : {delta.get('numa_miss', 0):>12,}  (alloc fell back to other node)")
    print(f"  numa_local      : {delta.get('numa_local', 0):>12,}  (thread & page on same node)")
    print(f"  numa_other      : {delta.get('numa_other', 0):>12,}  (thread & page on different node)")
    print(f"  remote access % : {remote_pct:>11.2f}%")


# ---------------------------------------------------------------------------
# /proc/PID/numa_maps helpers
# ---------------------------------------------------------------------------

def read_numa_maps(pid: int) -> List[Dict]:
    """Parse /proc/PID/numa_maps into a list of segment dicts."""
    path = f"/proc/{pid}/numa_maps"
    segments = []
    try:
        with open(path) as f:
            for line in f:
                seg = parse_numa_maps_line(line.rstrip())
                if seg:
                    segments.append(seg)
    except OSError as e:
        print(f"[warn] cannot read {path}: {e}", file=sys.stderr)
    return segments


def parse_numa_maps_line(line: str) -> Optional[Dict]:
    """
    Example lines:
      7f1234000000 bind:0 anon=16384 dirty=16384 N0=16384
      7f2345000000 interleave:0-1 anon=16384 N0=8192 N1=8192
      7f3456000000 default file=/lib/x86_64-linux-gnu/libc.so anon=0
    """
    parts = line.split()
    if len(parts) < 2:
        return None

    addr = int(parts[0], 16)
    policy = parts[1]  # e.g. "bind:0", "interleave:0-3", "default"

    seg: Dict = {
        "addr": addr,
        "policy": policy,
        "anon_pages": 0,
        "file": None,
        "node_pages": {},  # {node_id: page_count}
        "total_pages": 0,
    }

    for token in parts[2:]:
        if token.startswith("file="):
            seg["file"] = token[5:]
        elif token.startswith("anon="):
            seg["anon_pages"] = int(token[5:])
        elif token.startswith("N") and "=" in token:
            node_str, count_str = token[1:].split("=", 1)
            try:
                seg["node_pages"][int(node_str)] = int(count_str)
            except ValueError:
                pass

    seg["total_pages"] = sum(seg["node_pages"].values())
    return seg


def segment_size_mb(seg: Dict, page_size: int = 4096) -> float:
    return seg["total_pages"] * page_size / (1024 * 1024)


def is_large_anon(seg: Dict, min_mb: float) -> bool:
    return seg["file"] is None and segment_size_mb(seg) >= min_mb


def aggregate_node_pages(segments: List[Dict]) -> Dict[int, int]:
    totals: Dict[int, int] = defaultdict(int)
    for seg in segments:
        for node, pages in seg["node_pages"].items():
            totals[node] += pages
    return dict(totals)


def print_memory_placement(segments: List[Dict],
                           bp_only: bool,
                           bp_min_mb: float,
                           page_size: int = 4096) -> None:
    if bp_only:
        segs = [s for s in segments if is_large_anon(s, bp_min_mb)]
        label = f"large anonymous segments (>= {bp_min_mb} MiB, likely buffer pool)"
    else:
        segs = [s for s in segments if s["total_pages"] > 0]
        label = "all segments with resident pages"

    print(f"\n=== /proc/PID/numa_maps — physical page placement ({label}) ===")

    total_pages = 0
    node_totals: Dict[int, int] = defaultdict(int)

    for seg in segs:
        size_mb = segment_size_mb(seg, page_size)
        node_str = "  ".join(
            f"N{n}={c}" for n, c in sorted(seg["node_pages"].items())
        )
        print(f"  0x{seg['addr']:016x}  policy={seg['policy']:<20s}  "
              f"size={size_mb:>8.1f} MiB  {node_str}")
        for n, c in seg["node_pages"].items():
            node_totals[n] += c
        total_pages += seg["total_pages"]

    if not segs:
        print("  (no matching segments found)")
        return

    print()
    grand_total = sum(node_totals.values())
    print("  --- Summary ---")
    for node in sorted(node_totals):
        pct = node_totals[node] / grand_total * 100 if grand_total > 0 else 0.0
        size_mb = node_totals[node] * page_size / (1024 * 1024)
        print(f"  Node {node}: {size_mb:>8.1f} MiB  ({pct:5.1f}%)")
    total_mb = grand_total * page_size / (1024 * 1024)
    print(f"  Total : {total_mb:>8.1f} MiB")

    # balance check
    if len(node_totals) >= 2:
        vals = list(node_totals.values())
        imbalance = (max(vals) - min(vals)) / max(vals) * 100
        print(f"  Imbalance: {imbalance:.1f}%  "
              f"({'OK' if imbalance < 10 else 'WARNING: uneven distribution'})")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def resolve_pid(args) -> int:
    if args.pid:
        return args.pid
    if args.pid_file:
        with open(args.pid_file) as f:
            return int(f.read().strip())
    raise ValueError("provide --pid or --pid-file")


def run_once(pid: int, bp_only: bool, bp_min_mb: float) -> None:
    segments = read_numa_maps(pid)
    print_memory_placement(segments, bp_only, bp_min_mb)

    vmstat = read_vmstat()
    print("\n=== /proc/vmstat NUMA counters (absolute) ===")
    for k in VMSTAT_KEYS:
        print(f"  {k:<20s}: {vmstat.get(k, 0):>12,}")


def plot_remote_ratio(timestamps: List[float], remote_ratios: List[float],
                      output_path: Optional[str]) -> None:
    """Plot remote access ratio over time. Falls back to ASCII if matplotlib unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(12, 5))
        ax.plot(timestamps, remote_ratios, color="tab:red", linewidth=1.5,
                marker="o", markersize=3, label="Remote access %")
        ax.axhline(y=sum(remote_ratios) / len(remote_ratios) if remote_ratios else 0,
                   color="gray", linestyle="--", linewidth=1, label="Average")
        ax.set_xlabel("Elapsed time (s)")
        ax.set_ylabel("Remote NUMA access (%)")
        ax.set_title("NUMA Remote Access Ratio Over Time")
        ax.set_ylim(0, max(max(remote_ratios) * 1.1, 5) if remote_ratios else 5)
        ax.legend()
        ax.grid(True, alpha=0.3)
        plt.tight_layout()

        plot_path = (output_path.rsplit(".", 1)[0] + "_plot.png"
                     if output_path else "numa_remote_ratio.png")
        plt.savefig(plot_path, dpi=150)
        plt.close()
        print(f"\n[plot] saved to {plot_path}")

    except ImportError:
        # ASCII fallback
        if not remote_ratios:
            return
        width = 60
        height = 15
        max_val = max(remote_ratios) if max(remote_ratios) > 0 else 1
        print("\n--- Remote NUMA Access % over time (ASCII) ---")
        for row in range(height, -1, -1):
            threshold = max_val * row / height
            line = f"{threshold:5.1f}% |"
            for v in remote_ratios:
                line += "#" if v >= threshold else " "
            print(line)
        print("       +" + "-" * len(remote_ratios))
        print(f"        0s{' ' * (len(remote_ratios) - 4)}{timestamps[-1]:.0f}s")
        avg = sum(remote_ratios) / len(remote_ratios)
        print(f"  avg={avg:.2f}%  max={max(remote_ratios):.2f}%  "
              f"min={min(remote_ratios):.2f}%")


def run_loop(pid: int, interval: float, bp_only: bool, bp_min_mb: float,
             duration: Optional[float] = None,
             output: Optional[str] = None) -> None:
    if duration:
        print(f"Monitoring PID {pid}, interval={interval}s, "
              f"duration={duration}s.\n")
    else:
        print(f"Monitoring PID {pid}, interval={interval}s. Ctrl-C to stop.\n")

    prev_vmstat = read_vmstat()
    prev_time = time.monotonic()
    start_time = prev_time

    # time series for plotting
    ts_elapsed: List[float] = []
    ts_remote_pct: List[float] = []

    # CSV writer setup
    csv_file = None
    writer = None
    if output:
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        csv_file = open(output, "w", newline="")
        writer = csv.writer(csv_file)
        writer.writerow(["timestamp", "elapsed_s", "numa_local", "numa_other",
                         "remote_pct", "numa_hit", "numa_miss"])

    try:
        while True:
            time.sleep(interval)
            now = time.monotonic()
            elapsed = now - prev_time
            elapsed_total = now - start_time

            if duration and elapsed_total >= duration:
                # do one final sample then break
                pass

            print("\n" + "=" * 70)
            print(f"  Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                  f"PID: {pid}  Elapsed: {elapsed_total:.0f}s")
            print("=" * 70)

            # memory placement
            segments = read_numa_maps(pid)
            print_memory_placement(segments, bp_only, bp_min_mb)

            # vmstat delta
            cur_vmstat = read_vmstat()
            delta = vmstat_delta(prev_vmstat, cur_vmstat)
            print_vmstat_report(delta, elapsed)

            total_access = (delta.get("numa_local", 0) +
                            delta.get("numa_other", 0))
            remote_pct = (delta["numa_other"] / total_access * 100
                          if total_access > 0 else 0.0)

            ts_elapsed.append(elapsed_total)
            ts_remote_pct.append(remote_pct)

            if writer:
                writer.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{elapsed_total:.1f}",
                    delta.get("numa_local", 0),
                    delta.get("numa_other", 0),
                    f"{remote_pct:.4f}",
                    delta.get("numa_hit", 0),
                    delta.get("numa_miss", 0),
                ])
                csv_file.flush()

            prev_vmstat = cur_vmstat
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
        plot_remote_ratio(ts_elapsed, ts_remote_pct, output)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor mysqld NUMA memory placement and access statistics.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pid", type=int, help="mysqld PID")
    group.add_argument("--pid-file", metavar="PATH",
                       help="path to mysql.pid file")

    parser.add_argument("--once", action="store_true",
                        help="print one snapshot and exit")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="sampling interval in seconds (default: 5)")
    parser.add_argument("--bp-only", action="store_true",
                        help="only show large anonymous segments (buffer pool)")
    parser.add_argument("--bp-min-mb", type=float, default=64.0,
                        help="minimum segment size in MiB to consider as "
                             "buffer pool (default: 64)")
    parser.add_argument("--duration", type=float, default=None,
                        help="stop after this many seconds and plot results")
    parser.add_argument("--output", metavar="PATH", default=None,
                        help="write per-sample CSV to this file; "
                             "plot image saved alongside it as <name>_plot.png")

    args = parser.parse_args()

    try:
        pid = resolve_pid(args)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    # verify process exists
    if not os.path.exists(f"/proc/{pid}"):
        print(f"error: PID {pid} not found in /proc", file=sys.stderr)
        sys.exit(1)

    if args.once:
        run_once(pid, args.bp_only, args.bp_min_mb)
    else:
        run_loop(pid, args.interval, args.bp_only, args.bp_min_mb,
                 duration=args.duration, output=args.output)


if __name__ == "__main__":
    main()
