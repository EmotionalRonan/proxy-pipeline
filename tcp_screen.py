#!/usr/bin/env python3
"""
TCP connectivity screening for proxy nodes — TEXT-BLOCK MODE.
Tests server:port TCP reachability with 4s timeout, 100 concurrent workers.
Outputs reachable_nodes.yaml preserving original text block formatting.

The input all_proxies.yaml contains text blocks. We extract server/port from each
block for TCP testing, then output reachable blocks as-is (original formatting).
"""
import socket
import time
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from pathlib import Path

# Add parent dir to path for proxy_text_blocks module
sys.path.insert(0, str(Path(__file__).parent))
from proxy_text_blocks import (
    extract_proxy_blocks_from_file,
    get_block_name, get_block_server, get_block_port,
    block_hash, blocks_to_proxies_yaml,
)

INPUT_YAML = Path(__file__).parent / "yaml_data" / "all_proxies.yaml"
OUTPUT_DIR = Path(__file__).parent / "screening_results"
OUTPUT_YAML = OUTPUT_DIR / "reachable_nodes.yaml"
REPORT_FILE = OUTPUT_DIR / "screening_report.txt"
SUMMARY_JSON = OUTPUT_DIR / "screening_summary.json"

MAX_WORKERS = 100
TIMEOUT = 4.0  # seconds


def test_tcp_block(block, index):
    """Test TCP connectivity from a text block. Returns (index, block, reachable, latency_ms, error)."""
    server = get_block_server(block)
    port = get_block_port(block)
    name = get_block_name(block) or "?"

    if not server or not port:
        return (index, block, name, False, 0.0, "missing server/port")

    try:
        start = time.monotonic()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        result = sock.connect_ex((str(server), int(port)))
        elapsed = (time.monotonic() - start) * 1000
        sock.close()
        if result == 0:
            return (index, block, name, True, round(elapsed, 1), None)
        else:
            return (index, block, name, False, round(elapsed, 1), f"errno={result}")
    except socket.gaierror:
        return (index, block, name, False, 0.0, "DNS failed")
    except Exception as e:
        return (index, block, name, False, 0.0, str(e)[:80])


def get_block_field(block, field):
    """Extract scalar field from text block using regex."""
    import re
    m = re.search(rf'^\s+{re.escape(field)}:\s*(.+)$', block, re.MULTILINE)
    return m.group(1).strip() if m else "?"


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] Loading proxy blocks from {INPUT_YAML}...")
    _, blocks = extract_proxy_blocks_from_file(str(INPUT_YAML))
    total = len(blocks)
    print(f"      Total blocks: {total}")

    print(f"[2/4] Testing TCP connectivity ({MAX_WORKERS} concurrent, {TIMEOUT}s timeout)...")

    results = []  # (index, block, name, reachable, latency_ms, error)
    done = 0
    last_print = 0
    start_time = time.monotonic()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(test_tcp_block, b, i): i for i, b in enumerate(blocks)}
        for future in as_completed(futures):
            results.append(future.result())
            done += 1
            if done - last_print >= 200:
                elapsed = time.monotonic() - start_time
                reachable = sum(1 for r in results if r[3])
                pct = done / total * 100
                rate = done / elapsed if elapsed > 0 else 0
                print(f"      Progress: {done}/{total} ({pct:.1f}%) | Reachable: {reachable} | {rate:.0f} nodes/s")
                last_print = done

    elapsed_total = time.monotonic() - start_time
    print(f"      Done in {elapsed_total:.1f}s")

    # Sort by reachable first, then by latency
    results.sort(key=lambda r: (not r[3], r[4]))

    reachable_results = [r for r in results if r[3]]
    unreachable_results = [r for r in results if not r[3]]

    print(f"\n[3/4] Results:")
    print(f"      Reachable:   {len(reachable_results)} / {total} ({len(reachable_results)/total*100:.1f}%)")
    print(f"      Unreachable: {len(unreachable_results)} / {total}")

    # Protocol breakdown
    type_reachable = Counter(get_block_field(r[1], 'type') for r in reachable_results)
    type_total = Counter(get_block_field(b, 'type') for b in blocks)
    type_unreachable = {t: type_total[t] - type_reachable.get(t, 0) for t in type_total}

    print("\n      Protocol breakdown:")
    for t in sorted(type_total.keys()):
        r = type_reachable.get(t, 0)
        ut = type_unreachable.get(t, 0)
        pct = r / type_total[t] * 100 if type_total[t] > 0 else 0
        print(f"        {t:12s}: {r:4d}/{type_total[t]:4d} reachable ({pct:5.1f}%)")

    # Latency distribution
    latencies = [r[4] for r in reachable_results if r[4] > 0]
    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p90 = latencies[int(len(latencies) * 0.9)]
        p99 = latencies[int(len(latencies) * 0.99)]
        print(f"\n      TCP Latency (ms): min={min(latencies):.0f} | p50={p50:.0f} | p90={p90:.0f} | p99={p99:.0f} | max={max(latencies):.0f}")

    # Sort reachable by latency ascending (fastest first)
    reachable_results.sort(key=lambda r: r[4])

    # Keep only the fastest 80% (by latency percentile)
    cutoff_idx = int(len(reachable_results) * 0.80)
    fast_results = reachable_results[:cutoff_idx]
    slow_results = reachable_results[cutoff_idx:]
    fast_blocks = [r[1] for r in fast_results]
    fast_latencies = [r[4] for r in fast_results]

    print(f"\n[4/4] Writing output files to {OUTPUT_DIR}/")
    print(f"      Top 80% fastest: {len(fast_blocks)} nodes (cutoff at {fast_latencies[-1]:.0f}ms)")
    print(f"      Dropped slowest 20%: {len(slow_results)} nodes (>{slow_results[0][4]:.0f}ms)")

    with open(OUTPUT_YAML, "w") as f:
        f.write(f"# TCP-Screened Reachable Proxy Nodes (fastest 80%)\n")
        f.write(f"# Total reachable: {len(reachable_results)} / {total}, kept fastest 80%: {len(fast_blocks)}\n")
        f.write(f"# Screened at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# TCP timeout: {TIMEOUT}s, Workers: {MAX_WORKERS}\n")
        f.write(f"# Latency range: {fast_latencies[0]:.0f}ms - {fast_latencies[-1]:.0f}ms\n\n")
        f.write(blocks_to_proxies_yaml("proxies:", fast_blocks))
    print(f"      -> {OUTPUT_YAML} ({OUTPUT_YAML.stat().st_size / 1024:.1f} KB)")

    # Write report
    with open(REPORT_FILE, "w") as f:
        f.write(f"TCP Connectivity Screening Report\n")
        f.write(f"{'=' * 60}\n")
        f.write(f"Screened at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total nodes tested: {total}\n")
        f.write(f"Reachable: {len(reachable_results)} ({len(reachable_results)/total*100:.1f}%)\n")
        f.write(f"Unreachable: {len(unreachable_results)} ({len(unreachable_results)/total*100:.1f}%)\n")
        f.write(f"Kept fastest 80%: {len(fast_blocks)} nodes (cutoff {fast_latencies[-1]:.0f}ms)\n")
        f.write(f"Timeout: {TIMEOUT}s | Workers: {MAX_WORKERS}\n")
        f.write(f"Duration: {elapsed_total:.1f}s\n\n")

        f.write("Protocol Breakdown:\n")
        f.write("-" * 40 + "\n")
        for t in sorted(type_total.keys()):
            r = type_reachable.get(t, 0)
            ut = type_unreachable.get(t, 0)
            pct = r / type_total[t] * 100 if type_total[t] > 0 else 0
            f.write(f"  {t:12s}: {r:4d}/{type_total[t]:4d} ({pct:5.1f}%) {ut} dead\n")

        if latencies:
            p50 = latencies[len(latencies) // 2]
            p90 = latencies[int(len(latencies) * 0.9)]
            p99 = latencies[int(len(latencies) * 0.99)]
            f.write(f"\nTCP Latency (ms): min={min(latencies):.0f} p50={p50:.0f} p90={p90:.0f} p99={p99:.0f} max={max(latencies):.0f}\n")

        f.write(f"\nTop 20 Fastest (by TCP latency):\n")
        f.write("-" * 40 + "\n")
        for i, (_, _, name, _, lat, _) in enumerate(reachable_results[:20]):
            server = get_block_server(reachable_results[i][1]) or "?"
            port = get_block_port(reachable_results[i][1]) or "?"
            ptype = get_block_field(reachable_results[i][1], 'type')
            f.write(f"  {i+1:3d}. [{ptype:8s}] {name:50s} {server}:{port}  ({lat:.0f}ms)\n")

        f.write(f"\nTop 30 Slowest (still reachable):\n")
        f.write("-" * 40 + "\n")
        slowest = sorted(reachable_results, key=lambda x: x[4], reverse=True)
        for i, (_, _, name, _, lat, _) in enumerate(slowest[:30]):
            server = get_block_server(slowest[i][1]) or "?"
            port = get_block_port(slowest[i][1]) or "?"
            ptype = get_block_field(slowest[i][1], 'type')
            f.write(f"  {i+1:3d}. [{ptype:8s}] {name:50s} {server}:{port}  ({lat:.0f}ms)\n")

        f.write(f"\nUnreachable Error Distribution:\n")
        f.write("-" * 40 + "\n")
        err_counts = Counter(r[5] for r in unreachable_results if r[5])
        for err, count in err_counts.most_common(20):
            f.write(f"  {count:5d}: {err}\n")

    print(f"      -> {REPORT_FILE}")

    # Write summary JSON
    summary = {
        "date": time.strftime('%Y-%m-%d %H:%M:%S'),
        "total": total,
        "reachable": len(reachable_results),
        "unreachable": len(unreachable_results),
        "fastest_80pct": len(fast_blocks),
        "cutoff_latency_ms": round(fast_latencies[-1], 1) if fast_latencies else None,
        "timeout_s": TIMEOUT,
        "duration_s": round(elapsed_total, 1),
        "protocol_breakdown": {t: {"total": type_total[t], "reachable": type_reachable.get(t, 0)}
                              for t in sorted(type_total.keys())},
        "latency_p50": round(latencies[len(latencies)//2], 1) if latencies else None,
        "latency_p90": round(latencies[int(len(latencies)*0.9)], 1) if latencies else None,
        "latency_p99": round(latencies[int(len(latencies)*0.99)], 1) if latencies else None,
        "latency_min": round(min(latencies), 1) if latencies else None,
        "latency_max": round(max(latencies), 1) if latencies else None,
    }
    with open(SUMMARY_JSON, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"      -> {SUMMARY_JSON}")

    print("\n=== TCP Screening Complete ===")
    return len(reachable_results)


if __name__ == "__main__":
    main()
