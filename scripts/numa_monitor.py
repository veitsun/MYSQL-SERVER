#!/usr/bin/env python3
"""Unified NUMA monitor — runtime access + allocation locality on one chart.

是一个统一的 NUMA 监控工具。它的核心作用是实时__和可视化 MySQL 服务器进程的 NUMA 内存访问局部性__——即判断 MySQL 在运行时，内存在本地 NUMA 节点和远端 NUMA 节点之间的分布情况。这对于优化数据库在 NUMA 架构服务器上的性能至关重要。


Three time-series collected simultaneously:

  LINE 1 (red)   — node numastat runtime remote %
      Source : /sys/devices/system/node/nodeX/numastat  other_node / (local_node + other_node)
      Meaning: fraction of actual memory accesses that crossed a NUMA node boundary.
               This is the hardware-level cross-NUMA access counter and is the most
               accurate measure of runtime access locality.

  LINE 2 (blue dashed) — AutoNUMA hint remote %
      Source : /proc/vmstat  numa_hint_faults / numa_hint_faults_local
      Meaning: AutoNUMA periodically unmaps pages; the next access causes a fault and
               the kernel records whether the page is local.  This is a *sampled* proxy
               (only pages that AutoNUMA chose to probe are counted).
               Requires kernel.numa_balancing = 1.

  LINE 3 (green dotted) — page allocation remote %
      Source : /proc/vmstat  numa_other / (numa_local + numa_other)
      Meaning: fraction of new page allocations that landed on a remote node.
               Active only during buffer pool init; trends to 0 once allocation is done.
               Useful to confirm that --innodb-numa-interleave-instance is working
               during startup.

Usage:
    python3 scripts/numa_monitor.py --pid <mysqld-pid> [options]
    python3 scripts/numa_monitor.py --pid-file <path/to/mysql.pid> [options]

    # one-shot snapshot (absolute counters)
    python3 scripts/numa_monitor.py --pid 12345 --once

    # run 60 s, 5 s interval, save CSV + auto-plot
    python3 scripts/numa_monitor.py --pid 12345 \\
        --interval 5 --duration 60 --output ./numa_profile/combined.csv
"""

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Source 1: /sys/devices/system/node/nodeX/numastat
# ---------------------------------------------------------------------------

_NODE_KEYS = ["numa_hit", "numa_miss", "local_node", "other_node"]


def read_node_numastats() -> Dict[int, Dict[str, int]]:
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
        path = os.path.join(base, entry, "numastat")
        stats: Dict[str, int] = {}
        try:
            with open(path) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and parts[0] in _NODE_KEYS:
                        stats[parts[0]] = int(parts[1])
        except OSError:
            continue
        result[node_id] = stats
    return result


def _node_delta(before: Dict[int, Dict[str, int]],
                after:  Dict[int, Dict[str, int]]) -> Dict[str, int]:
    totals: Dict[str, int] = defaultdict(int)
    for node in after:
        b = before.get(node, {})
        for k in _NODE_KEYS:
            totals[k] += after[node].get(k, 0) - b.get(k, 0)
    return dict(totals)


def _node_remote_pct(delta: Dict[str, int]) -> float:
    local = delta.get("local_node", 0)
    other = delta.get("other_node", 0)
    total = local + other
    return other / total * 100 if total > 0 else 0.0


# ---------------------------------------------------------------------------
# Source 2+3: /proc/vmstat
# ---------------------------------------------------------------------------

_VMSTAT_KEYS = [
    "numa_local", "numa_other",           # page allocation locality
    "numa_hint_faults", "numa_hint_faults_local",  # AutoNUMA hint
    "numa_pages_migrated",
]


def read_vmstat() -> Dict[str, int]:
    stats: Dict[str, int] = {}
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] in _VMSTAT_KEYS:
                    stats[parts[0]] = int(parts[1])
    except OSError as e:
        print(f"[warn] cannot read /proc/vmstat: {e}", file=sys.stderr)
    return stats


def _vmstat_delta(before: Dict[str, int],
                  after:  Dict[str, int]) -> Dict[str, int]:
    return {k: after.get(k, 0) - before.get(k, 0) for k in _VMSTAT_KEYS}


def _alloc_remote_pct(delta: Dict[str, int]) -> float:
    local = delta.get("numa_local", 0)
    other = delta.get("numa_other", 0)
    total = local + other
    return other / total * 100 if total > 0 else 0.0


def _hint_remote_pct(delta: Dict[str, int]) -> float:
    total = delta.get("numa_hint_faults", 0)
    loc   = delta.get("numa_hint_faults_local", 0)
    return (total - loc) / total * 100 if total > 0 else 0.0


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------

def print_snapshot(node_delta: Dict[str, int],
                   node_after:  Dict[int, Dict[str, int]],
                   vm_delta:    Dict[str, int],
                   elapsed:     float) -> None:
    # --- node numastat ---
    node_rp = _node_remote_pct(node_delta)
    print(f"\n=== node numastat runtime (delta over {elapsed:.1f}s) ===")
    print(f"  local_node  : {node_delta.get('local_node',  0):>14,}  (local-CPU → local-mem)")
    print(f"  other_node  : {node_delta.get('other_node',  0):>14,}  (remote-CPU → this-mem)  ← cross-NUMA")
    print(f"  runtime remote %: {node_rp:>9.2f}%")
    print("  Per-node breakdown:")
    for node in sorted(node_after):
        s = node_after[node]
        t = s.get("local_node", 0) + s.get("other_node", 0)
        rp = s.get("other_node", 0) / t * 100 if t > 0 else 0.0
        print(f"    Node {node}: local={s.get('local_node',0):>12,}  "
              f"other={s.get('other_node',0):>12,}  remote%={rp:5.1f}%")

    # --- AutoNUMA hint ---
    hint_rp = _hint_remote_pct(vm_delta)
    htotal  = vm_delta.get("numa_hint_faults", 0)
    hloc    = vm_delta.get("numa_hint_faults_local", 0)
    hmig    = vm_delta.get("numa_pages_migrated", 0)
    print(f"\n=== AutoNUMA hint faults (delta over {elapsed:.1f}s) ===")
    print(f"  hint_faults       : {htotal:>12,}  (pages sampled by AutoNUMA)")
    print(f"  hint_faults_local : {hloc:>12,}  (page already on local node)")
    print(f"  hint_faults_remote: {htotal - hloc:>12,}  (page on remote node)")
    print(f"  pages_migrated    : {hmig:>12,}  (moved closer to CPU)")
    print(f"  hint remote %     : {hint_rp:>9.2f}%")
    if htotal == 0:
        print("  [note] no hint faults — check kernel.numa_balancing=1")

    # --- page allocation ---
    alloc_rp = _alloc_remote_pct(vm_delta)
    print(f"\n=== Page allocation locality (delta over {elapsed:.1f}s) ===")
    print(f"  numa_local  : {vm_delta.get('numa_local',  0):>14,}  (new page allocated on local node)")
    print(f"  numa_other  : {vm_delta.get('numa_other',  0):>14,}  (new page allocated on remote node)")
    print(f"  alloc remote %  : {alloc_rp:>9.2f}%")
    if vm_delta.get("numa_local", 0) + vm_delta.get("numa_other", 0) == 0:
        print("  [note] 0 allocation events — buffer pool already fully allocated")


# ---------------------------------------------------------------------------
# Plot (matplotlib with ASCII fallback)
# ---------------------------------------------------------------------------

def _ascii_plot(timestamps: List[float], series: List[List[float]],
                labels: List[str]) -> None:
    if not timestamps:
        return
    width  = min(len(timestamps), 70)
    height = 16
    step   = max(1, len(timestamps) // width)
    ts_d   = timestamps[::step]
    all_vals = [v for s in series for v in s]
    max_val = max(all_vals) if all_vals else 1
    max_val = max_val if max_val > 0 else 1

    chars = ["#", "o", "+"]
    print("\n--- Remote % over time (ASCII) ---")
    for row in range(height, -1, -1):
        threshold = max_val * row / height
        line = f"{threshold:5.1f}% |"
        for i, ts in enumerate(ts_d):
            idx = i * step
            chosen = " "
            for si, s in enumerate(series):
                if idx < len(s) and s[idx] >= threshold:
                    chosen = chars[si % len(chars)]
                    break
            line += chosen
        print(line)
    print("       +" + "-" * len(ts_d))
    print(f"        0s{' ' * max(0, len(ts_d) - 4)}{ts_d[-1]:.0f}s")
    for si, (s, label) in enumerate(zip(series, labels)):
        if s:
            print(f"  {chars[si % len(chars)]} {label}: "
                  f"avg={sum(s)/len(s):.2f}%  max={max(s):.2f}%  min={min(s):.2f}%")


def plot_combined(timestamps:   List[float],
                  node_remote:  List[float],
                  hint_remote:  List[float],
                  alloc_remote: List[float],
                  output_path:  Optional[str]) -> None:
    # Determine split index: last sample where alloc_remote > 0 (buf pool init phase)
    split_idx = -1
    for i, v in enumerate(alloc_remote):
        if v > 0:
            split_idx = i

    # Green avg: mean of alloc_remote values that are > 0 (init phase only)
    green_vals = [v for v in alloc_remote if v > 0]
    green_avg = sum(green_vals) / len(green_vals) if green_vals else None

    # Blue post-init avg: mean of hint_remote after split_idx
    blue_post = hint_remote[split_idx + 1:] if split_idx >= 0 else hint_remote
    blue_post_avg = sum(blue_post) / len(blue_post) if blue_post else None

    # Blue overall avg
    blue_overall_avg = (sum(hint_remote) / len(hint_remote)
                        if hint_remote else None)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(14, 6))

        # Blue dashed: AutoNUMA hint remote %
        ax.plot(timestamps, hint_remote,
                color="tab:blue", linewidth=1.5, marker="s", markersize=3,
                linestyle="--",
                label="AutoNUMA hint remote %\n"
                      "(sampled proxy, requires numa_balancing=1)")

        # Green dotted: page allocation remote %
        ax.plot(timestamps, alloc_remote,
                color="tab:green", linewidth=1.5, marker="^", markersize=3,
                linestyle=":",
                label="page allocation remote %\n"
                      "(non-zero only during buf pool init)")

        # Three average horizontal lines
        if green_avg is not None:
            ax.axhline(y=green_avg, color="tab:green", linestyle="-",
                       linewidth=1.2,
                       label=f"alloc avg (init phase) = {green_avg:.1f}%")

        if blue_post_avg is not None and split_idx >= 0:
            # draw only over the post-init portion
            post_start = timestamps[split_idx + 1] if split_idx + 1 < len(timestamps) else timestamps[-1]
            ax.axhline(y=blue_post_avg, color="tab:blue", linestyle="-.",
                       linewidth=1.2, xmin=0, xmax=1,
                       label=f"hint avg (post-init) = {blue_post_avg:.1f}%")

        if blue_overall_avg is not None:
            ax.axhline(y=blue_overall_avg, color="navy", linestyle=":",
                       linewidth=1.2,
                       label=f"hint avg (overall) = {blue_overall_avg:.1f}%")

        ax.set_xlabel("Elapsed time (s)", fontsize=11)
        ax.set_ylabel("Remote NUMA access (%)", fontsize=11)
        ax.set_title("NUMA Remote Access Over Time\n"
                     "(blue=AutoNUMA hint proxy, green=page allocation)",
                     fontsize=12)

        all_vals = hint_remote + alloc_remote
        ymax = max(max(all_vals) * 1.15, 5) if all_vals else 5
        ax.set_ylim(0, ymax)
        ax.legend(fontsize=9, loc="upper right")
        ax.grid(True, alpha=0.3)
        plt.tight_layout()

        plot_path = (output_path.rsplit(".", 1)[0] + "_plot.png"
                     if output_path else "numa_combined_plot.png")
        plt.savefig(plot_path, dpi=150)
        plt.close()
        print(f"\n[plot] saved to {plot_path}")

    except ImportError:
        _ascii_plot(
            timestamps,
            [hint_remote, alloc_remote],
            ["AutoNUMA hint remote %",
             "page allocation remote %"],
        )


# ---------------------------------------------------------------------------
# Main loop
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
    vm_stats   = read_vmstat()
    print(f"=== One-shot NUMA snapshot (PID {pid}) ===\n")
    print("--- /sys node numastat (absolute) ---")
    for node in sorted(node_stats):
        s = node_stats[node]
        t = s.get("local_node", 0) + s.get("other_node", 0)
        rp = s.get("other_node", 0) / t * 100 if t > 0 else 0.0
        print(f"  Node {node}: local={s.get('local_node',0):>12,}  "
              f"other={s.get('other_node',0):>12,}  remote%={rp:5.1f}%")
    print("\n--- /proc/vmstat NUMA counters (absolute) ---")
    for k in _VMSTAT_KEYS:
        print(f"  {k:<35s}: {vm_stats.get(k, 0):>12,}")


def run_loop(pid:      int,
             interval: float,
             duration: Optional[float] = None,
             output:   Optional[str]   = None) -> None:
    stop_msg = f"duration={duration}s" if duration else "Ctrl-C to stop"
    print(f"Monitoring PID {pid}, interval={interval}s, {stop_msg}.")
    print("Collecting: node numastat (runtime) + AutoNUMA hint + alloc locality\n")

    prev_node = read_node_numastats()
    prev_vm   = read_vmstat()
    start     = time.monotonic()
    prev_time = start

    ts_elapsed:    List[float] = []
    ts_node_r:     List[float] = []
    ts_hint_r:     List[float] = []
    ts_alloc_r:    List[float] = []

    csv_file = None
    writer   = None
    if output:
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        csv_file = open(output, "w", newline="")
        writer   = csv.writer(csv_file)
        writer.writerow([
            "timestamp", "elapsed_s",
            "local_node", "other_node", "node_remote_pct",
            "hint_faults", "hint_local", "hint_remote_pct",
            "alloc_local", "alloc_other", "alloc_remote_pct",
            "pages_migrated",
        ])

    try:
        while True:
            time.sleep(interval)
            now          = time.monotonic()
            elapsed      = now - prev_time
            elapsed_total = now - start

            cur_node = read_node_numastats()
            cur_vm   = read_vmstat()

            nd = _node_delta(prev_node, cur_node)
            vd = _vmstat_delta(prev_vm, cur_vm)

            nr  = _node_remote_pct(nd)
            hr  = _hint_remote_pct(vd)
            ar  = _alloc_remote_pct(vd)

            print("\n" + "=" * 72)
            print(f"  Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                  f"PID: {pid}  Elapsed: {elapsed_total:.0f}s")
            print("=" * 72)

            print_snapshot(nd, cur_node, vd, elapsed)

            ts_elapsed.append(elapsed_total)
            ts_node_r.append(nr)
            ts_hint_r.append(hr)
            ts_alloc_r.append(ar)

            if writer:
                writer.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{elapsed_total:.1f}",
                    nd.get("local_node", 0),
                    nd.get("other_node", 0),
                    f"{nr:.4f}",
                    vd.get("numa_hint_faults", 0),
                    vd.get("numa_hint_faults_local", 0),
                    f"{hr:.4f}",
                    vd.get("numa_local", 0),
                    vd.get("numa_other", 0),
                    f"{ar:.4f}",
                    vd.get("numa_pages_migrated", 0),
                ])
                csv_file.flush()

            prev_node = cur_node
            prev_vm   = cur_vm
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
        plot_combined(ts_elapsed, ts_node_r, ts_hint_r, ts_alloc_r, output)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified NUMA monitor: runtime + hint + allocation remote % on one chart.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pid",      type=int,  help="mysqld PID")
    group.add_argument("--pid-file", metavar="PATH", help="path to mysql.pid file")

    parser.add_argument("--once",     action="store_true",
                        help="print one absolute snapshot and exit")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="sampling interval in seconds (default: 5)")
    parser.add_argument("--duration", type=float, default=None,
                        help="stop after this many seconds and plot results")
    parser.add_argument("--output",   metavar="PATH", default=None,
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
        run_loop(pid, args.interval, duration=args.duration, output=args.output)


if __name__ == "__main__":
    main()
