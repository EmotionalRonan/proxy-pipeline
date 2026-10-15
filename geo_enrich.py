#!/usr/bin/env python3
"""
Geo-enrich proxy nodes: resolve server IPs/domains to countries,
rename nodes as "🇨🇳 CN #0001", "🇺🇸 US #0002", etc. grouped by country.

TEXT-BLOCK MODE: Each proxy node is kept as an opaque text block.
Only the "name" line is modified; everything else preserved EXACTLY as-is.

Uses ip-api.com batch API (free tier: 45 req/min, 100 IPs/batch).
Results cached to geo_cache.json to avoid re-querying known IPs.
"""
import json
import socket
import time
import sys
import re
import os
import urllib.request
import urllib.error
import ssl
from pathlib import Path
from collections import defaultdict

# Add parent dir to path for proxy_text_blocks module
sys.path.insert(0, str(Path(__file__).parent))
from proxy_text_blocks import (
    extract_proxy_blocks_from_file,
    get_block_name, get_block_server, get_block_port,
    block_hash, set_block_name, blocks_to_proxies_yaml,
    split_proxy_blocks,
)

# Disable proxy — geo API needs direct connection
proxy_handler = urllib.request.ProxyHandler({})
opener = urllib.request.build_opener(proxy_handler)
urllib.request.install_opener(opener)

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

# ===== CONFIG =====
INPUT_YAML = Path(__file__).parent / "yaml_data" / "all_proxies.yaml"
OUTPUT_YAML = Path(__file__).parent / "yaml_data" / "all_proxies_geo.yaml"
CACHE_FILE = Path(__file__).parent / "geo_cache.json"

BATCH_SIZE = 100
REQUEST_DELAY = 1.5  # seconds between batches (rate limit: 45/min)
DNS_TIMEOUT = 3


def country_to_flag(code):
    """Convert 2-letter country code to flag emoji."""
    if not code or len(code) != 2:
        return "🏳️"
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)


def resolve_domain(domain, timeout=DNS_TIMEOUT):
    """Resolve domain to IP. Returns IP string or None."""
    try:
        addrs = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
        for addr in addrs:
            ip = addr[4][0]
            if ip:
                return ip
    except Exception:
        pass
    return None


def is_ip(s):
    """Check if string is an IPv4 address."""
    return bool(re.match(r'^\d+\.\d+\.\d+\.\d+$', s))


def query_ip_api(ips):
    """
    Batch query ip-api.com for country info.
    Returns dict of {ip: {"country": "...", "countryCode": "..."}}
    """
    url = "http://ip-api.com/batch"
    data = json.dumps([{"query": ip, "fields": "status,country,countryCode"} for ip in ips]).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    })

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                results = json.loads(resp.read().decode())
                out = {}
                for ip, r in zip(ips, results):
                    if r.get("status") == "success":
                        out[ip] = {
                            "country": r.get("country", "Unknown"),
                            "countryCode": r.get("countryCode", "??"),
                        }
                    else:
                        out[ip] = {"country": "Unknown", "countryCode": "??"}
                return out
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                wait = (attempt + 1) * 5
                print(f"      Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            print(f"    API HTTP error: {e}")
            return {}
        except Exception as e:
            if attempt < max_retries:
                print(f"      Connection error, retrying ({attempt+1}/{max_retries})...")
                time.sleep(3)
                continue
            print(f"    API error: {e}")
            return {}
    return {}


def load_cache():
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def main():
    print("=" * 60)
    print("  GEO ENRICHMENT (Text-Block Mode) — Resolve server countries")
    print("=" * 60)

    # Load proxy text blocks
    print(f"\n[1/5] Loading proxy blocks from {INPUT_YAML}...")
    header, blocks = extract_proxy_blocks_from_file(str(INPUT_YAML))
    print(f"      {len(blocks)} node blocks loaded")

    # Collect unique servers from text blocks
    print(f"\n[2/5] Collecting unique servers...")
    server_set = set()
    for block in blocks:
        s = get_block_server(block)
        if s:
            server_set.add(s)

    print(f"      {len(server_set)} unique servers")

    # Separate IPs and domains
    ips_to_query = set()
    domains_to_resolve = set()
    for s in server_set:
        if is_ip(s):
            ips_to_query.add(s)
        else:
            domains_to_resolve.add(s)

    print(f"      IPs: {len(ips_to_query)}, Domains: {len(domains_to_resolve)}")

    # Load cache
    cache = load_cache()
    print(f"      Cached entries: {len(cache)}")

    # Resolve domains to IPs
    print(f"\n[3/5] Resolving {len(domains_to_resolve)} domains...")
    resolved = 0
    for i, domain in enumerate(sorted(domains_to_resolve)):
        ip = resolve_domain(domain)
        if ip:
            ips_to_query.add(ip)
            resolved += 1
            if domain not in cache:
                cache[domain] = {"ip": ip, "country": None, "countryCode": None}
        if (i + 1) % 50 == 0:
            print(f"      Resolved {i+1}/{len(domains_to_resolve)}...")
    print(f"      Resolved: {resolved}/{len(domains_to_resolve)}")
    print(f"      Total IPs to query: {len(ips_to_query)}")

    # Filter out already-cached IPs
    uncached_ips = []
    for ip in ips_to_query:
        if ip in cache and cache[ip].get("countryCode"):
            continue
        uncached_ips.append(ip)

    print(f"      Already cached: {len(ips_to_query) - len(uncached_ips)}")
    print(f"      Need to query: {len(uncached_ips)}")

    # Batch query ip-api.com
    print(f"\n[4/5] Querying ip-api.com (batch size={BATCH_SIZE})...")
    new_results = {}

    if uncached_ips:
        for i in range(0, len(uncached_ips), BATCH_SIZE):
            batch = uncached_ips[i:i + BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            total_batches = (len(uncached_ips) + BATCH_SIZE - 1) // BATCH_SIZE
            print(f"      Batch {batch_num}/{total_batches}: {len(batch)} IPs...", end=" ", flush=True)

            results = query_ip_api(batch)
            new_results.update(results)
            print(f"got {len(results)} results")

            for ip, info in results.items():
                cache[ip] = info

            if i + BATCH_SIZE < len(uncached_ips):
                time.sleep(REQUEST_DELAY)

        save_cache(cache)
        print(f"      Newly queried: {len(new_results)}")
    else:
        print("      All IPs already cached — no API calls needed")

    # Build server -> country mapping
    print(f"\n[5/5] Renaming nodes with country flags (text blocks)...")
    server_country = {}

    for ip in ips_to_query:
        if ip in cache and cache[ip].get("countryCode"):
            server_country[ip] = cache[ip]

    for domain in domains_to_resolve:
        if domain in cache:
            ip = cache[domain].get("ip")
            if ip and ip in cache and cache[ip].get("countryCode"):
                server_country[domain] = cache[ip]
            elif ip:
                server_country[domain] = {"country": "Unknown", "countryCode": "??"}
        else:
            server_country[domain] = {"country": "Unknown", "countryCode": "??"}

    # Group blocks by country
    country_blocks = defaultdict(list)
    unknown_blocks = []

    for block in blocks:
        server = get_block_server(block) or ""
        cc = server_country.get(server, {}).get("countryCode", "??")
        if cc and cc != "??":
            country_blocks[cc].append(block)
        else:
            unknown_blocks.append(block)

    # Rename: replace name line in each block, keep everything else EXACT
    renamed_blocks = []
    stats = {}

    for cc in sorted(country_blocks.keys()):
        blks = country_blocks[cc]
        flag = country_to_flag(cc)
        country_name = cc  # fallback
        for s in server_set:
            if server_country.get(s, {}).get("countryCode") == cc:
                country_name = server_country[s].get("country", cc)
                break

        stats[cc] = {"count": len(blks), "country": country_name}

        for idx, block in enumerate(blks, 1):
            new_name = f"{flag} {cc} #{idx:04d}"
            renamed_blocks.append(set_block_name(block, new_name))

    # Unknown nodes
    for idx, block in enumerate(unknown_blocks, 1):
        new_name = f"🏳️ ?? #{idx:04d}"
        renamed_blocks.append(set_block_name(block, new_name))

    stats["??"] = {"count": len(unknown_blocks), "country": "Unknown"}

    # Write output — text blocks as-is, only name changed!
    with open(OUTPUT_YAML, "w", encoding="utf-8") as f:
        f.write(f"# Geo-Enriched Proxy Nodes\n")
        f.write(f"# Processed: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Total nodes: {len(renamed_blocks)}\n")
        f.write(f"# Countries: {len(country_blocks)}\n\n")
        f.write(blocks_to_proxies_yaml("proxies:", renamed_blocks))

    # Print stats
    print(f"\n{'='*60}")
    print(f"  GEO ENRICHMENT COMPLETE")
    print(f"{'='*60}")
    print(f"  Total nodes: {len(renamed_blocks)}")
    print(f"  Countries: {len(country_blocks)}")
    print(f"\n  Country distribution:")
    for cc in sorted(stats.keys(), key=lambda x: -stats[x]["count"]):
        s = stats[cc]
        flag = country_to_flag(cc) if cc != "??" else "🏳️"
        print(f"    {flag} {s['country']:30s} ({cc}): {s['count']:5d} nodes")

    print(f"\n  Output: {OUTPUT_YAML}")

    # Also overwrite all_proxies.yaml with geo-enriched version
    with open(INPUT_YAML, "w", encoding="utf-8") as f:
        f.write(f"# Geo-Enriched Proxy Nodes\n")
        f.write(f"# Processed: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Total nodes: {len(renamed_blocks)}\n")
        f.write(f"# Countries: {len(country_blocks)}\n\n")
        f.write(blocks_to_proxies_yaml("proxies:", renamed_blocks))
    print(f"  Also updated: {INPUT_YAML}")


if __name__ == "__main__":
    main()
