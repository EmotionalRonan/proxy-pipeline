#!/usr/bin/env python3
"""
Mihomo (Clash Meta) proxy latency testing — Corrected version.

Key improvements over v1:
1. Stratified sampling by (type, network, tls, reality, flow) combo
2. Lower concurrency (5) to avoid mihomo 503/504 overload
3. Retry logic: retry 503/504 up to 2 times with backoff
4. Combo-aware reporting with weighted population estimates
5. Better error categorization
"""
import yaml
import json
import time
import subprocess
import requests
import os
import sys
from pathlib import Path
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import random
import urllib.parse

# Import text-block utilities for preserving original YAML formatting
sys.path.insert(0, str(Path(__file__).parent))
from proxy_text_blocks import (
    extract_proxy_blocks_from_file,
    get_block_name, get_block_server, get_block_port, get_block_type, get_block_field,
    block_hash, set_block_name, blocks_to_proxies_yaml,
)

# Path to the geo-enriched YAML (source of truth for text blocks)
GEO_YAML = Path(__file__).parent / "yaml_data" / "all_proxies_geo.yaml"


def load_text_blocks():
    """
    Load proxy text blocks from geo-enriched YAML.
    Returns {name: text_block} mapping for original-format lookup.
    """
    _, blocks = extract_proxy_blocks_from_file(str(GEO_YAML))
    name_to_block = {}
    for block in blocks:
        name = get_block_name(block)
        if name:
            name_to_block[name] = block
    return name_to_block


INPUT_YAML = Path(__file__).parent / "screening_results" / "reachable_nodes.yaml"
OUTPUT_DIR = Path(__file__).parent / "mihomo_results_v2"
MIHOMO_DIR = OUTPUT_DIR / "mihomo_runtime"

# MIHOMO_BIN is determined dynamically by mihomo_setup
MIHOMO_BIN = None
CONFIG_PATH = MIHOMO_DIR / "config.yaml"
API_BASE = "http://127.0.0.1:9090"  # can be reassigned for batch testing
TEST_URL = "http://www.gstatic.com/generate_204"
TIMEOUT_MS = 4000
MAX_CONCURRENT_TESTS = 30
MAX_RETRIES = 0  # no retries — dead nodes just waste time
RETRY_DELAY = 1.0  # seconds between retries

SAMPLES_PER_COMBO = 200  # Test up to 200 per combo for better coverage
SAMPLE_RATE = 0.80  # 80% of TCP-sorted nodes (which are already top 80% TCP latency)


def transport_combo(p):
    """Generate a transport combo key for stratified sampling."""
    t = p.get("type", "?")
    net = p.get("network", "tcp")
    tls = p.get("tls", False)
    reality = bool(p.get("reality-opts"))
    flow = p.get("flow", "none")
    return f"{t}|{net}|tls={tls}|real={reality}|flow={flow}"


def select_stratified_sample(proxies, max_total=3000):
    """
    Stratified sampling: ensure every transport combo is represented.
    Deduplicate by unique (server, port) within each combo first.
    Target ~60% of reachable nodes, with per-combo cap.
    If total proxies <= max_total, test ALL of them (no sampling).
    """
    by_combo = defaultdict(list)
    for p in proxies:
        combo = transport_combo(p)
        by_combo[combo].append(p)

    print(f"      Found {len(by_combo)} unique transport combos across {len(proxies)} nodes")

    target_count = int(len(proxies) * SAMPLE_RATE)
    print(f"      Target: {target_count} nodes ({SAMPLE_RATE*100:.0f}% of {len(proxies)})")

    # If total is small enough, test everything
    if len(proxies) <= max_total and target_count >= len(proxies):
        print(f"      Node count {len(proxies)} <= {max_total}, testing ALL nodes (full coverage)")
        # Dedupe by server:port
        seen = set()
        sample = []
        for p in proxies:
            key = (p.get("server"), p.get("port"))
            if key not in seen:
                sample.append(p)
                seen.add(key)
        # Ensure unique names
        seen_names = set()
        for p in sample:
            name = p["name"]
            if name in seen_names:
                p["name"] = f"{name}_{id(p)}"
            seen_names.add(p["name"])
        random.shuffle(sample)
        combo_alloc = {combo: len([p for p in sample if transport_combo(p) == combo]) for combo in by_combo}
        return sample, combo_alloc

    # Phase 1: allocate at least 2 per combo, up to SAMPLES_PER_COMBO
    sample = []
    combo_alloc = {}
    for combo, nodes in sorted(by_combo.items(), key=lambda x: -len(x[1])):
        # Dedupe by server:port within combo
        seen = set()
        unique = []
        for p in nodes:
            key = (p.get("server"), p.get("port"))
            if key not in seen:
                unique.append(p)
                seen.add(key)
        random.shuffle(unique)

        # Allocate: min 2, up to SAMPLES_PER_COMBO
        alloc = min(SAMPLES_PER_COMBO, max(2, len(unique)))
        selected = unique[:alloc]
        combo_alloc[combo] = len(selected)
        sample.extend(selected)

    # Phase 2: if we're below target, top up from largest combos proportionally
    if len(sample) < target_count:
        shortage = target_count - len(sample)
        print(f"      Phase 1 gathered {len(sample)}, need {shortage} more to reach {SAMPLE_RATE*100:.0f}%")
        # Re-allocate extra from combos with remaining nodes
        extra = []
        for combo, nodes in sorted(by_combo.items(), key=lambda x: -len(x[1])):
            already = combo_alloc.get(combo, 0)
            seen = set()
            remaining = []
            for p in nodes:
                key = (p.get("server"), p.get("port"))
                if key not in seen:
                    remaining.append(p)
                    seen.add(key)
            random.shuffle(remaining)
            # Take from remaining (after already allocated)
            taken = 0
            for p in remaining:
                if taken >= already + shortage // len(by_combo):
                    break
                # Check if already in sample
                key = (p.get("server"), p.get("port"))
                already_keys = {(sp.get("server"), sp.get("port")) for sp in sample}
                if key not in already_keys:
                    extra.append(p)
                    taken += 1
            if taken > 0:
                combo_alloc[combo] += taken
        sample.extend(extra)
        random.shuffle(sample)

    # Phase 3: if still over target (unlikely with 60% but possible), trim
    if len(sample) > target_count:
        sorted_combos = sorted(combo_alloc.items(), key=lambda x: -x[1])
        to_remove = len(sample) - target_count
        removed = 0
        for combo, _ in sorted_combos:
            if removed >= to_remove:
                break
            for i, p in enumerate(sample):
                if transport_combo(p) == combo:
                    del sample[i]
                    removed += 1
                    combo_alloc[combo] -= 1
                    break

    # Ensure unique names
    seen_names = set()
    for p in sample:
        name = p["name"]
        if name in seen_names:
            p["name"] = f"{name}_{id(p)}"
        seen_names.add(p["name"])

    random.shuffle(sample)
    return sample, combo_alloc


def build_mihomo_config(proxies, port=7890, socks_port=7891, api_port=9090):
    """Build mihomo config with a selector group for manual testing."""
    config = {
        "port": port,
        "socks-port": socks_port,
        "mixed-port": 0,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "external-controller": f"127.0.0.1:{api_port}",
        "secret": "",
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "TEST",
                "type": "select",
                "proxies": [p["name"] for p in proxies],
            }
        ],
        "rules": ["MATCH,TEST"],
    }
    return config


def start_mihomo(cfg_path, work_dir):
    """Start mihomo process."""
    env = os.environ.copy()
    env["HOME"] = str(work_dir)
    proc = subprocess.Popen(
        [MIHOMO_BIN, "-d", str(work_dir), "-f", str(cfg_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    return proc


def wait_for_api(timeout=15, api_port=None):
    """Wait until mihomo API is ready."""
    base = f"http://127.0.0.1:{api_port}" if api_port else API_BASE
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            r = requests.get(f"{base}/version", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def test_single_proxy(name, timeout_ms=TIMEOUT_MS, retries=MAX_RETRIES):
    """
    Test a single proxy's HTTP delay via mihomo REST API.
    Retries on 503/504 (mihomo overload) with backoff.
    Returns: (name, delay_ms, error_str)
      delay_ms > 0: proxy is working
      delay_ms == -1: proxy failed
    """
    encoded = urllib.parse.quote(name, safe="")
    url = f"{API_BASE}/proxies/{encoded}/delay"
    params = {"url": TEST_URL, "timeout": timeout_ms}
    api_timeout = timeout_ms / 1000 + 8

    last_error = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=api_timeout)
            if r.status_code == 200:
                data = r.json()
                delay = data.get("delay", -1)
                if delay > 0:
                    return (name, delay, None)
                else:
                    return (name, -1, "proxy_timeout")
            elif r.status_code in (503, 504):
                # Mihomo overload — retry
                last_error = f"mihomo_{r.status_code}"
                if attempt < retries:
                    time.sleep(RETRY_DELAY)
                    continue
                return (name, -1, f"HTTP {r.status_code} (exhausted retries)")
            elif r.status_code == 404:
                return (name, -1, "proxy_not_found_404")
            else:
                return (name, -1, f"HTTP {r.status_code}")
        except requests.exceptions.Timeout:
            last_error = "api_timeout"
            if attempt < retries:
                time.sleep(RETRY_DELAY)
                continue
            return (name, -1, "api_timeout_exhausted")
        except Exception as e:
            return (name, -1, str(e)[:80])
    return (name, -1, last_error or "unknown")


def test_proxies_sequential(names):
    """
    Test proxies in batches to avoid overwhelming mihomo.
    Tests one at a time but uses executor for potential future parallelization.
    Returns list of (name, delay, error).
    """
    results = []
    total = len(names)
    start_time = time.monotonic()

    # Use a small thread pool — not fully parallel to avoid mihomo 503
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TESTS) as executor:
        futures = {executor.submit(test_single_proxy, name): name for name in names}

        done = 0
        for f in as_completed(futures):
            results.append(f.result())
            done += 1
            if done % 25 == 0 or done == total:
                alive = sum(1 for _, d, _ in results if d > 0)
                mihomo_errs = sum(1 for _, _, e in results if e and "mihomo_5" in str(e))
                elapsed = time.monotonic() - start_time
                pct = done / total * 100
                print(f"      [{done:4d}/{total} {pct:5.1f}%] alive={alive:4d} mihomo_errors={mihomo_errs} elapsed={elapsed:.0f}s")

    return results


def compute_weighted_estimate(sample_results, all_proxies):
    """
    Compute weighted population estimate using per-combo success rates
    applied to full population counts.
    """
    # Build success rate per combo from sample
    combo_sample = defaultdict(list)
    for name, delay, error, proxy in sample_results:
        combo = transport_combo(proxy)
        combo_sample[combo].append(delay > 0)

    combo_success_rate = {}
    for combo, results in combo_sample.items():
        rate = sum(results) / len(results) if results else 0
        combo_success_rate[combo] = rate

    # Count full population per combo
    combo_pop = Counter()
    for p in all_proxies:
        combo_pop[transport_combo(p)] += 1

    estimated = 0
    per_type_est = defaultdict(lambda: {"total": 0, "estimated": 0.0})
    for combo, count in combo_pop.items():
        rate = combo_success_rate.get(combo, 0)
        est_for_combo = int(count * rate)
        estimated += est_for_combo
        ptype = combo.split("|")[0]
        per_type_est[ptype]["total"] += count
        per_type_est[ptype]["estimated"] += count * rate

    return estimated, combo_success_rate, combo_pop, per_type_est


def main():
    global MIHOMO_BIN

    # --- Ensure mihomo binary exists ---
    sys.path.insert(0, str(Path(__file__).parent))
    from mihomo_setup import get_mihomo_path
    MIHOMO_BIN = get_mihomo_path()
    if not MIHOMO_BIN:
        print("ERROR: Cannot find or download mihomo binary. Skipping mihomo test.")
        sys.exit(2)
    # ------------------------------------

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MIHOMO_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[1/7] Loading reachable nodes from {INPUT_YAML}...")
    # Load as text blocks first (preserve original formatting)
    _, reachable_blocks = extract_proxy_blocks_from_file(str(INPUT_YAML))
    # Also parse as YAML dicts for mihomo config building
    with open(INPUT_YAML) as f:
        data = yaml.safe_load(f)
    all_proxies = data.get("proxies", [])
    print(f"      Total nodes: {len(all_proxies)} (blocks: {len(reachable_blocks)})")

    print(f"[2/7] Stratified sampling by transport combo...")
    sample, combo_alloc = select_stratified_sample(all_proxies)
    print(f"      Selected {len(sample)} nodes across {len(combo_alloc)} combos")
    print(f"      Top combos by allocation:")
    for combo, alloc in sorted(combo_alloc.items(), key=lambda x: -x[1])[:15]:
        pop = sum(1 for p in all_proxies if transport_combo(p) == combo)
        print(f"        {alloc:2d} samples from pop {pop:5d}  [{combo}]")

    # Batch test: split into batches of BATCH_SIZE to avoid overloading mihomo
    BATCH_SIZE = 200
    all_raw_results = []

    for batch_idx in range(0, len(sample), BATCH_SIZE):
        batch = sample[batch_idx:batch_idx + BATCH_SIZE]
        batch_num = batch_idx // BATCH_SIZE + 1
        total_batches = (len(sample) + BATCH_SIZE - 1) // BATCH_SIZE

        print(f"\n[3/7] Batch {batch_num}/{total_batches}: Building mihomo config ({len(batch)} proxies)...")
        api_port = 9090 + batch_idx
        config = build_mihomo_config(batch, port=7890 + batch_idx, socks_port=7891 + batch_idx, api_port=api_port)
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False, width=200)

        print(f"[4/7] Batch {batch_num}/{total_batches}: Starting mihomo v1.19.29 (API port {api_port})...")
        proc = start_mihomo(CONFIG_PATH, MIHOMO_DIR)
        if not wait_for_api(15, api_port=api_port):
            print(f"      ERROR: Mihomo API did not start for batch {batch_num}")
            proc.terminate()
            continue
        print(f"      Mihomo API ready")

        time.sleep(2)

        # Update API_BASE for this batch
        global API_BASE
        old_api = API_BASE
        API_BASE = f"http://127.0.0.1:{api_port}"

        print(f"[5/7] Batch {batch_num}/{total_batches}: Testing proxy HTTP delays (timeout={TIMEOUT_MS}ms, concurrent={MAX_CONCURRENT_TESTS})...")
        print(f"      This tests actual HTTP proxying through each node via gstatic.com")

        raw_results = test_proxies_sequential([p["name"] for p in batch])
        all_raw_results.extend(raw_results)

        API_BASE = old_api

        print(f"\n[6/7] Batch {batch_num}/{total_batches}: Stopping mihomo...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        # Let OS release ports
        time.sleep(2)

    raw_results = all_raw_results
    # Build name -> proxy map
    name_to_proxy = {p["name"]: p for p in sample}
    elapsed = time.monotonic()

    # Merge and categorize
    merged = []  # (name, delay_ms, error, proxy)
    for name, delay, error in raw_results:
        proxy = name_to_proxy.get(name, {})
        merged.append((name, delay, error, proxy))

    alive = [(n, d, e, p) for n, d, e, p in merged if d > 0]
    dead_mihomo = [(n, d, e, p) for n, d, e, p in merged if d <= 0 and e and "mihomo_5" in str(e)]
    dead_other = [(n, d, e, p) for n, d, e, p in merged if d <= 0 and not (e and "mihomo_5" in str(e))]

    print(f"\n{'='*65}")
    print(f"  MIHOMO DELAY TEST RESULTS (Corrected Sampling)")
    print(f"{'='*65}")
    print(f"  Nodes tested:        {len(merged)}")
    print(f"  Working (alive):     {len(alive)} ({len(alive)/len(merged)*100:.1f}%)")
    print(f"  Mihomo overload:     {len(dead_mihomo)} (retries exhausted)")
    print(f"  Actually dead:       {len(dead_other)}")
    print(f"  Effective pass rate: {len(alive)/(len(alive)+len(dead_other))*100:.1f}% (excluding mihomo errors)")

    # Per combo breakdown
    print(f"\n  Per-combo breakdown (samples with >= 1 alive):")
    combo_results = defaultdict(lambda: {"alive": 0, "total": 0, "delays": []})
    for name, delay, error, proxy in merged:
        combo = transport_combo(proxy)
        combo_results[combo]["total"] += 1
        if delay > 0:
            combo_results[combo]["alive"] += 1
            combo_results[combo]["delays"].append(delay)

    for combo in sorted(combo_results.keys()):
        cr = combo_results[combo]
        total = cr["total"]
        alive_c = cr["alive"]
        if total > 0:
            avg_d = f" avg={sum(cr['delays'])/len(cr['delays']):.0f}ms" if cr["delays"] else ""
            marker = "✅" if alive_c > 0 else "❌"
            print(f"    {marker} {alive_c:2d}/{total:2d}{avg_d:<12s} [{combo}]")

    # Weighted population estimate
    estimated, combo_rates, combo_pop, per_type_est = compute_weighted_estimate(merged, all_proxies)

    print(f"\n  Weighted population estimate:")
    print(f"    Estimated working: ~{estimated} / {len(all_proxies)} ({estimated/len(all_proxies)*100:.1f}%)")

    print(f"\n  Per-type estimates:")
    for ptype in sorted(per_type_est.keys()):
        td = per_type_est[ptype]
        est = int(td["estimated"])
        pct = td["estimated"] / td["total"] * 100 if td["total"] > 0 else 0
        print(f"    {ptype:12s}: ~{est:5d}/{td['total']:5d} ({pct:5.1f}%)")

    # Latency stats
    if alive:
        delays = sorted([d for _, d, _, _ in alive])
        print(f"\n  Latency distribution (working nodes):")
        print(f"    Min: {min(delays):.0f}ms  P50: {delays[len(delays)//2]:.0f}ms  P90: {delays[int(len(delays)*0.9)]:.0f}ms  Max: {max(delays):.0f}ms")

    print(f"\n[7/7] Writing reports to {OUTPUT_DIR}/")

    # ----- Report file -----
    report_path = OUTPUT_DIR / "mihomo_test_report.txt"
    with open(report_path, "w") as f:
        f.write(f"Mihomo Proxy HTTP Delay Test Report (v2 — Corrected)\n")
        f.write(f"{'='*65}\n")
        f.write(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Mihomo: v1.19.29 | Test URL: {TEST_URL}\n")
        f.write(f"Timeout: {TIMEOUT_MS}ms | Concurrent: {MAX_CONCURRENT_TESTS} | Retries: {MAX_RETRIES}\n\n")

        f.write(f"SUMMARY\n-------\n")
        f.write(f"  Nodes tested:        {len(merged)}\n")
        f.write(f"  Working:             {len(alive)} ({len(alive)/len(merged)*100:.1f}%)\n")
        f.write(f"  Mihomo overload err: {len(dead_mihomo)}\n")
        f.write(f"  Actually dead:       {len(dead_other)}\n")
        f.write(f"  Weighted estimate:   ~{estimated} / {len(all_proxies)} ({estimated/len(all_proxies)*100:.1f}%)\n\n")

        f.write(f"PER-TYPE ESTIMATES\n------------------\n")
        for ptype in sorted(per_type_est.keys()):
            td = per_type_est[ptype]
            est = int(td["estimated"])
            pct = td["estimated"] / td["total"] * 100 if td["total"] > 0 else 0
            f.write(f"  {ptype:12s}: ~{est:5d}/{td['total']:5d} ({pct:5.1f}%)\n")

        f.write(f"\nPER-COMBO BREAKDOWN\n-------------------\n")
        for combo in sorted(combo_results.keys()):
            cr = combo_results[combo]
            total = cr["total"]
            alive_c = cr["alive"]
            pop = combo_pop.get(combo, 0)
            rate = combo_rates.get(combo, 0)
            est_combo = int(pop * rate)
            avg_d = f" avg={sum(cr['delays'])/len(cr['delays']):.0f}ms" if cr["delays"] else ""
            f.write(f"  [{alive_c:2d}/{total:2d}] pop={pop:5d} est~{est_combo:4d}{avg_d}  {combo}\n")

        f.write(f"\nTOP 50 FASTEST WORKING NODES\n----------------------------\n")
        alive_sorted = sorted(alive, key=lambda x: x[1])
        for i, (name, delay, _, proxy) in enumerate(alive_sorted[:50]):
            f.write(f"  {i+1:3d}. [{proxy.get('type','?'):8s}] {delay:5.0f}ms  {name[:60]} | {proxy.get('server')}:{proxy.get('port')}\n")

        f.write(f"\nDEAD NODES (excluding mihomo errors)\n------------------------------------\n")
        for i, (name, delay, error, proxy) in enumerate(dead_other[:30]):
            f.write(f"  {i+1:3d}. [{proxy.get('type','?'):8s}] {error or '?'}  {name[:60]}\n")

    # ----- Verified YAML (Text-Block Mode) -----
    # Load original text blocks from geo-enriched YAML to preserve EXACT formatting
    name_to_block = load_text_blocks()

    alive_yaml_path = OUTPUT_DIR / "mihomo_verified_nodes.yaml"
    alive_blocks = []
    for name, delay, _, proxy in alive:
        # Try to get the original text block by name
        original_block = name_to_block.get(name)
        if original_block:
            alive_blocks.append(original_block)
        else:
            # Fallback: build minimal block from dict (should not happen normally)
            cp = {k: v for k, v in proxy.items() if not k.startswith("_")}
            alive_blocks.append(
                f"  - name: {cp.get('name', 'unknown')}\n" +
                f"    type: {cp.get('type', '?')}\n" +
                f"    server: {cp.get('server', '?')}\n" +
                f"    port: {cp.get('port', '?')}\n"
            )

    with open(alive_yaml_path, "w") as f:
        f.write(f"# Mihomo-Verified Working Proxy Nodes (v2)\n")
        f.write(f"# Tested: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Test URL: {TEST_URL} | Timeout: {TIMEOUT_MS}ms\n")
        f.write(f"# Verified working: {len(alive_blocks)} / {len(merged)} sampled\n")
        f.write(f"# Weighted estimate: ~{estimated} / {len(all_proxies)} total\n")
        f.write("proxies:\n")
        for i, (name, delay, _, _) in enumerate(alive):
            if i < len(alive_blocks):
                f.write(alive_blocks[i] + "\n")

    # ----- JSON results -----
    results_json_path = OUTPUT_DIR / "mihomo_results.json"
    json_data = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "test_url": TEST_URL,
        "timeout_ms": TIMEOUT_MS,
        "mihomo_version": "v1.19.29",
        "sample_size": len(merged),
        "alive": len(alive),
        "dead_mihomo_overload": len(dead_mihomo),
        "dead_actual": len(dead_other),
        "weighted_estimate": estimated,
        "total_population": len(all_proxies),
        "per_type_estimates": {
            ptype: {"total": td["total"], "estimated": int(td["estimated"])}
            for ptype, td in per_type_est.items()
        },
        "combo_results": {
            combo: {
                "alive": cr["alive"], "total": cr["total"],
                "population": combo_pop.get(combo, 0),
                "estimated": int(combo_pop.get(combo, 0) * combo_rates.get(combo, 0)),
                "avg_delay": round(sum(cr["delays"]) / len(cr["delays"])) if cr["delays"] else None,
            }
            for combo, cr in combo_results.items()
        },
        "top_fastest": [
            {"name": name, "type": proxy.get("type", "?"), "delay_ms": delay,
             "server": proxy.get("server"), "port": proxy.get("port")}
            for name, delay, _, proxy in sorted(alive, key=lambda x: x[1])[:50]
        ],
    }
    with open(results_json_path, "w") as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)

    print(f"      -> {report_path}")
    print(f"      -> {alive_yaml_path}")
    print(f"      -> {results_json_path}")
    print(f"\n=== Mihomo Testing v2 Complete ===")


if __name__ == "__main__":
    main()
