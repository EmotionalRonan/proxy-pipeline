#!/usr/bin/env python3
"""
Master orchestration script for daily proxy node pipeline.

Runs all steps sequentially:
  1. Download YAML files from source sites + deduplicate
  2. Geo-enrich (resolve countries, rename nodes)
  3. TCP connectivity screening
  4. Mihomo HTTP delay testing
  5. Sync verified nodes to Obsidian + git push + dufs upload
  6. Cleanup temp/cache files

Usage:
  python3 run_daily_pipeline.py [--skip-download] [--skip-geo] [--skip-tcp] [--skip-mihomo] [--skip-sync] [--skip-cleanup]

All intermediate output goes to outputs/ subdirectories.
Final deliverable: outputs/mihomo_results_v2/mihomo_verified_nodes.yaml
                   → synced to ~/Project/ObProject/obs/proyl/verified_proxies.yaml + git push
"""

import os
import sys
import time
import json
import subprocess
import datetime
from pathlib import Path

# ===== CONFIG =====
SCRIPT_DIR = Path(__file__).parent
PYTHON = sys.executable  # 自动使用运行本脚本的 python 解释器（CI / 本机通用）

# Step scripts (all in same directory)
STEP_SCRIPTS = {
    "download": SCRIPT_DIR / "download_and_extract.py",
    "geo":      SCRIPT_DIR / "geo_enrich.py",
    "tcp":      SCRIPT_DIR / "tcp_screen.py",
    "mihomo":   SCRIPT_DIR / "mihomo_test_v2.py",
    "sync":     SCRIPT_DIR / "sync_to_obsidian.py",
}

# Key output files to check
OUTPUT_FILES = {
    "all_proxies":       SCRIPT_DIR / "yaml_data" / "all_proxies.yaml",
    "all_proxies_geo":   SCRIPT_DIR / "yaml_data" / "all_proxies_geo.yaml",
    "reachable":         SCRIPT_DIR / "screening_results" / "reachable_nodes.yaml",
    "screening_report":  SCRIPT_DIR / "screening_results" / "screening_report.txt",
    "screening_summary": SCRIPT_DIR / "screening_results" / "screening_summary.json",
    "mihomo_verified":   SCRIPT_DIR / "mihomo_results_v2" / "mihomo_verified_nodes.yaml",
    "mihomo_report":     SCRIPT_DIR / "mihomo_results_v2" / "mihomo_test_report.txt",
    "mihomo_json":       SCRIPT_DIR / "mihomo_results_v2" / "mihomo_results.json",
}


def run_step(name, script, timeout=None):
    """Run a Python script and return (success, stdout_lines, elapsed_sec)."""
    print(f"\n{'='*65}")
    print(f"  STEP: {name}")
    print(f"  Script: {script}")
    print(f"{'='*65}")

    t0 = time.monotonic()
    env = os.environ.copy()
    env["HTTP_PROXY"] = ""
    env["HTTPS_PROXY"] = ""
    env["http_proxy"] = ""
    env["https_proxy"] = ""

    try:
        result = subprocess.run(
            [PYTHON, str(script)],
            cwd=str(SCRIPT_DIR),
            capture_output=True, text=True,
            timeout=timeout or 600,  # 10 min default
            env=env,
        )
        elapsed = time.monotonic() - t0
        stdout = result.stdout
        stderr = result.stderr

        if result.returncode == 0:
            print(f"  ✅ {name} completed in {elapsed:.1f}s")
            return True, stdout.split('\n'), elapsed
        elif result.returncode == 2:
            # Exit code 2 = mihomo binary not found (skip gracefully)
            print(f"  ⚠️  {name} skipped (exit code 2) in {elapsed:.1f}s")
            return False, stdout.split('\n'), elapsed
        else:
            print(f"  ❌ {name} FAILED (exit {result.returncode}) in {elapsed:.1f}s")
            print(f"  STDOUT (last 30 lines):")
            for line in stdout.split('\n')[-30:]:
                print(f"    {line}")
            if stderr.strip():
                print(f"  STDERR (last 20 lines):")
                for line in stderr.split('\n')[-20:]:
                    print(f"    {line}")
            return False, stdout.split('\n'), elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        print(f"  ❌ {name} TIMEOUT after {elapsed:.1f}s")
        return False, [], elapsed
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"  ❌ {name} ERROR: {e}")
        return False, [], elapsed


def extract_summary():
    """Extract key numbers from output files for the final report."""
    summary = {
        "date": datetime.date.today().isoformat(),
        "steps": {},
    }

    # Screening summary
    ss = OUTPUT_FILES["screening_summary"]
    if ss.exists():
        try:
            with open(ss) as f:
                data = json.load(f)
            summary["tcp"] = {
                "total": data.get("total", 0),
                "reachable": data.get("reachable", 0),
                "unreachable": data.get("unreachable", 0),
                "duration_s": data.get("duration_s", 0),
            }
            summary["protocols"] = data.get("protocol_breakdown", {})
        except Exception:
            pass

    # Mihomo results
    mj = OUTPUT_FILES["mihomo_json"]
    if mj.exists():
        try:
            with open(mj) as f:
                data = json.load(f)
            summary["mihomo"] = {
                "sample_size": data.get("sample_size", 0),
                "alive": data.get("alive", 0),
                "dead_actual": data.get("dead_actual", 0),
                "weighted_estimate": data.get("weighted_estimate", 0),
                "total_population": data.get("total_population", 0),
                "top_fastest": data.get("top_fastest", [])[:10],
                "per_type_estimates": data.get("per_type_estimates", {}),
            }
        except Exception:
            pass

    # Country distribution from geo YAML header
    geo_yaml = OUTPUT_FILES["all_proxies_geo"]
    if geo_yaml.exists():
        try:
            with open(geo_yaml) as f:
                header_lines = []
                for line in f:
                    if line.startswith("# "):
                        header_lines.append(line[2:].strip())
                    else:
                        break
            for line in header_lines:
                if "Total nodes:" in line:
                    try:
                        summary["geo_total"] = int(line.split(":")[-1].strip())
                    except:
                        pass
        except Exception:
            pass

    # Verified node count
    vy = OUTPUT_FILES["mihomo_verified"]
    if vy.exists():
        try:
            count = 0
            with open(vy) as f:
                for line in f:
                    if line.strip().startswith("name:") and "  - " not in line:
                        count += 1
            if count == 0:
                import yaml
                with open(vy) as f:
                    data = yaml.safe_load(f)
                    if data and "proxies" in data:
                        count = len(data["proxies"])
            summary["verified_count"] = count
        except Exception:
            pass

    return summary


def print_final_report(summary, timings):
    """Print a concise final report."""
    print(f"\n{'='*65}")
    print(f"  PIPELINE COMPLETE — {summary['date']}")
    print(f"{'='*65}")

    total_time = sum(timings.values())
    print(f"  Total time: {total_time:.0f}s")
    for step, t in timings.items():
        print(f"    {step:12s}: {t:5.0f}s")

    # TCP stats
    tcp = summary.get("tcp", {})
    if tcp:
        print(f"\n  📡 TCP Screening:")
        print(f"    Tested: {tcp['total']} → Reachable: {tcp['reachable']} ({tcp['reachable']/tcp['total']*100:.1f}%)")

    # Protocol breakdown
    protos = summary.get("protocols", {})
    if protos:
        print(f"    By protocol:")
        for ptype in sorted(protos.keys()):
            pd = protos[ptype]
            pct = pd['reachable'] / pd['total'] * 100 if pd['total'] > 0 else 0
            print(f"      {ptype:12s}: {pd['reachable']:4d}/{pd['total']:4d} ({pct:5.1f}%)")

    # Mihomo stats
    mihomo = summary.get("mihomo", {})
    if mihomo:
        print(f"\n  🧪 Mihomo HTTP Test:")
        print(f"    Sampled: {mihomo['sample_size']} → Alive: {mihomo['alive']}")
        print(f"    Weighted estimate: ~{mihomo['weighted_estimate']} / {mihomo['total_population']}")
        print(f"    Verified YAML: {summary.get('verified_count', '?')} nodes")

        # Per-type estimates
        pte = mihomo.get("per_type_estimates", {})
        if pte:
            print(f"    Per-type estimates:")
            for ptype in sorted(pte.keys()):
                td = pte[ptype]
                print(f"      {ptype:12s}: ~{td['estimated']:5d}/{td['total']:5d}")

        # Top fastest
        top = mihomo.get("top_fastest", [])[:5]
        if top:
            print(f"\n  ⚡ Top 5 fastest:")
            for i, n in enumerate(top):
                print(f"    {i+1}. [{n.get('type','?'):8s}] {n['delay_ms']:5.0f}ms  {n.get('name','?')[:50]}")

    # Output files
    print(f"\n  📁 Output files:")
    for name, path in OUTPUT_FILES.items():
        if path.exists():
            size_kb = path.stat().st_size / 1024
            print(f"    {'✅' if size_kb > 0 else '⚠️ '} {path.name:30s} ({size_kb:.1f} KB)")

    # Git sync status
    target = Path.home() / "Project/ObProject/obs/proyl/verified_proxies.yaml"
    if target.exists():
        print(f"\n  🔄 Git sync: verified_proxies.yaml synced to Obsidian repo")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Daily proxy node pipeline")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-geo", action="store_true")
    parser.add_argument("--skip-tcp", action="store_true")
    parser.add_argument("--skip-mihomo", action="store_true")
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--skip-cleanup", action="store_true")
    args = parser.parse_args()

    # Ensure output dirs exist
    for d in [
        SCRIPT_DIR / "yaml_data" / "raw",
        SCRIPT_DIR / "screening_results",
        SCRIPT_DIR / "mihomo_results_v2" / "mihomo_runtime",
    ]:
        d.mkdir(parents=True, exist_ok=True)

    timings = {}
    overall_start = time.monotonic()

    # Step 1: Download + Deduplicate
    if not args.skip_download:
        ok, _, elapsed = run_step("Download + Dedup", STEP_SCRIPTS["download"], timeout=300)
        timings["download"] = elapsed
        if not ok:
            print("\n❌ Download step failed. Aborting pipeline.")
            sys.exit(1)
    else:
        print("\n⏭️  Skipping download step")

    # Step 2: Geo-enrich
    if not args.skip_geo:
        ok, _, elapsed = run_step("Geo-Enrich", STEP_SCRIPTS["geo"], timeout=300)
        timings["geo"] = elapsed
        if not ok:
            print("\n❌ Geo-enrich step failed. Aborting pipeline.")
            sys.exit(1)
    else:
        print("\n⏭️  Skipping geo-enrich step")

    # Step 3: TCP screening
    if not args.skip_tcp:
        ok, _, elapsed = run_step("TCP Screening", STEP_SCRIPTS["tcp"], timeout=300)
        timings["tcp"] = elapsed
        if not ok:
            print("\n❌ TCP screening failed. Aborting pipeline.")
            sys.exit(1)
    else:
        print("\n⏭️  Skipping TCP screening step")

    # Step 4: Mihomo HTTP test
    if not args.skip_mihomo:
        ok, _, elapsed = run_step("Mihomo HTTP Test", STEP_SCRIPTS["mihomo"], timeout=600)
        timings["mihomo"] = elapsed
        # Don't abort on mihomo failure — sync step can still run with existing data
    else:
        print("\n⏭️  Skipping mihomo test step")

    # Step 5: Sync to Obsidian + git push
    if not args.skip_sync:
        ok, _, elapsed = run_step("Sync to Obsidian", STEP_SCRIPTS["sync"], timeout=120)
        timings["sync"] = elapsed
    else:
        print("\n⏭️  Skipping sync step")

    # Extract summary BEFORE cleanup (JSON/report files must still exist)
    summary = extract_summary()

    # Step 6: Cleanup temp/cache files
    if not args.skip_cleanup:
        t0 = time.monotonic()
        print(f"\n{'='*65}")
        print(f"  STEP: Cleanup Temp Files")
        print(f"{'='*65}")

        cleanup_paths = [
            SCRIPT_DIR / "yaml_data" / "raw",
            SCRIPT_DIR / "yaml_data" / "per_source",
            SCRIPT_DIR / "yaml_data" / "download_log.txt",
            SCRIPT_DIR / "__pycache__",
            SCRIPT_DIR / "mihomo_results_v2" / "mihomo_runtime",
            SCRIPT_DIR / "mihomo_results_v2" / "mihomo_results.json",
            SCRIPT_DIR / "mihomo_results_v2" / "mihomo_test_report.txt",
            SCRIPT_DIR / "screening_results" / "screening_summary.json",
            SCRIPT_DIR / "screening_results" / "screening_report.txt",
        ]
        import shutil
        for p in cleanup_paths:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                print(f"  🗑  removed dir: {p.name}/")
            elif p.exists():
                p.unlink()
                print(f"  🗑  removed file: {p.name}")

        # Also remove per-source YAML files (clashnode_cc.yaml etc) in yaml_data/
        yaml_dir = SCRIPT_DIR / "yaml_data"
        for f in yaml_dir.glob("*.yaml"):
            name = f.name
            # Keep only all_proxies.yaml and all_proxies_geo.yaml
            if name not in ("all_proxies.yaml", "all_proxies_geo.yaml"):
                f.unlink()
                print(f"  🗑  removed file: {name}")

        elapsed = time.monotonic() - t0
        timings["cleanup"] = elapsed
        print(f"  ✅ Cleanup completed in {elapsed:.1f}s")
    else:
        print("\n⏭️  Skipping cleanup step")

    # Print final report (summary already extracted before cleanup)
    print_final_report(summary, timings)

    total_elapsed = time.monotonic() - overall_start
    print(f"\n  🏁 Pipeline finished in {total_elapsed:.0f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
