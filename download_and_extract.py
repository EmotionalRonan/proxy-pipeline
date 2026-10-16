#!/usr/bin/env python3
"""
Download all YAML files and extract proxies sections.

V2: Discovers YAML links by scraping each source website's latest article page,
instead of guessing date-based URL patterns.
"""
import subprocess
import os
import re
import sys
import json
import yaml
from datetime import date, timedelta

# ── No-proxy curl helper ────────────────────────────────────────────
_NO_PROXY_ENV = {
    **os.environ,
    "HTTP_PROXY": "", "HTTPS_PROXY": "",
    "http_proxy": "", "https_proxy": "",
}

def _fetch(url):
    """GET a URL, return (body, final_url) or (None, None) on failure."""
    try:
        r = subprocess.run(
            ["curl", "-sL", "--max-time", "15", "-w", "\n__FINAL__%{url_effective}", url],
            capture_output=True, text=True, timeout=20,
            env=_NO_PROXY_ENV,
        )
        out = r.stdout
        # Split off final URL
        if "__FINAL__" in out:
            body, final = out.rsplit("__FINAL__", 1)
            return body.strip(), final.strip()
        return out.strip(), url
    except Exception:
        return None, None


def _download(url, dest):
    """Download URL to dest file, return True on success."""
    try:
        r = subprocess.run(
            ["curl", "-sL", "--max-time", "30", "-o", dest, url],
            capture_output=True, text=True, timeout=35,
            env=_NO_PROXY_ENV,
        )
        return r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 10
    except Exception:
        return False


# ── Link discovery strategies ──
#
# Two strategies:
#   A) "article":  scrape latest article page → extract .yaml links (most reliable)
#   B) "date_cdn": generate URLs from date patterns → HEAD-verify (fallback for
#      sites where article content is stale but CDN files are current)
#
# For article-type sources, we fetch the category/freenode page to find the
# latest post (not the homepage, which mixes non-free-node content).

ARTICLE_SOURCES = [
    # ── clashnode.cc: /free-node/YYYY-M-D-*.htm ──
    {
        "name": "clashnode.cc",
        "list_url": "https://clashnode.cc/",
        "article_re": r'href="(/free-node/(\d{4}-\d{1,2}-\d{1,2})-[^"]+\.htm)"',
        "sort_key": "date_str",
        "base_url": "https://clashnode.cc",
    },
    # ── nodeclash.com: same structure ──
    {
        "name": "nodeclash.com",
        "list_url": "https://nodeclash.com/",
        "article_re": r'href="(/free-node/(\d{4}-\d{1,2}-\d{1,2})-[^"]+\.htm)"',
        "sort_key": "date_str",
        "base_url": "https://nodeclash.com",
    },
    # ── freeclashnode.com: same structure ──
    {
        "name": "freeclashnode.com",
        "list_url": "https://freeclashnode.com/",
        "article_re": r'href="(/free-node/(\d{4}-\d{1,2}-\d{1,2})-[^"]+\.htm)"',
        "sort_key": "date_str",
        "base_url": "https://freeclashnode.com",
    },
    # ── freev2raynode.com: /free-node-subscription/YYYY-M-D-*.htm ──
    {
        "name": "freev2raynode.com",
        "list_url": "https://freev2raynode.com/",
        "article_re": r'href="(/free-node-subscription/(\d{4}-\d{1,2}-\d{1,2})-[^"]+\.htm)"',
        "sort_key": "date_str",
        "base_url": "https://freev2raynode.com",
    },
    # ── clashnodev2ray.github.io: /YYYY/MM/DD/free-ssr-node/ ──
    {
        "name": "clashnodev2ray.github.io",
        "list_url": "https://clashnodev2ray.github.io/",
        "article_re": r'href="(/(\d{4})/(\d{2})/(\d{2})/free-ssr-node/)"',
        "sort_key": "date_ymd",
        "base_url": "https://clashnodev2ray.github.io",
    },
    # ── wanzhuanmi.com: /freenode/YYYY-MM-DD/ ──
    {
        "name": "wanzhuanmi.com",
        "list_url": "https://wanzhuanmi.com/",
        "article_re": r'href="(https://wanzhuanmi\.com/freenode/(\d{4}-\d{2}-\d{2})/)"',
        "sort_key": "date_str",
    },
    # ── stairnode.com: /daily/YYYY-MM-DD/ ──
    {
        "name": "stairnode.com",
        "list_url": "https://www.stairnode.com/",
        "article_re": r'href="(https://www\.stairnode\.com/daily/(\d{4}-\d{2}-\d{2})/)"',
        "sort_key": "date_str",
    },
    # ── mibei77.com: WordPress, numeric post IDs ──
    {
        "name": "mibei77.com",
        "list_url": "https://www.mibei77.com/",
        "article_re": r'href="(https://www\.mibei77\.com/(\d+)\.html)"',
        "sort_key": "numeric",
    },
]

# Date-CDN sources: sites where articles reference old files, but CDN has
# date-patterned files that are current. We generate candidate URLs and
# HEAD-verify the newest available.
DATE_CDN_SOURCES = [
    {
        "name": "oneclash.cc",
        "urls": lambda d: [
            f"https://oss.oneclash.cc/{d.year}/{d.month:02d}/{d.strftime('%Y%m%d')}.yaml",
            f"https://oss.oneclash.cc/{d.year}/{d.month:02d}/m{d.strftime('%Y%m%d')}.yaml",
        ],
    },
    {
        "name": "v2rayshare.net",
        "urls": lambda d: [
            f"https://static.v2rayshare.net/{d.year}/{d.month:02d}/{d.strftime('%Y%m%d')}.yaml",
            f"https://static.v2rayshare.net/{d.year}/{d.month:02d}/m{d.strftime('%Y%m%d')}.yaml",
        ],
    },
]


def _head_check(url):
    """Quick HEAD request, returns HTTP status code or 0 on error."""
    try:
        r = subprocess.run(
            ["curl", "-sI", "--max-time", "8", "-o", "/dev/null", "-w", "%{http_code}", url],
            capture_output=True, text=True, timeout=12,
            env=_NO_PROXY_ENV,
        )
        code = r.stdout.strip()
        return int(code) if code.isdigit() else 0
    except Exception:
        return 0


def discover_yaml_links():
    """
    Discover YAML links from all sources using two strategies:
    - Article scraping: fetch latest article page → extract .yaml links
    - Date CDN: generate date-pattern URLs → HEAD verify newest
    Returns {source_name: [yaml_urls]}.
    """
    result = {}
    yaml_re = re.compile(r'https?://[^\s"\'<>]+\.yaml[^\s"\'<>]*')
    today = date.today()

    # ── Strategy A: Article scraping ──
    for rule in ARTICLE_SOURCES:
        name = rule["name"]
        print(f"  [{name}] Fetching article list...")
        html, _ = _fetch(rule["list_url"])
        if not html:
            print(f"    FAILED: page not reachable")
            continue

        matches = re.findall(rule["article_re"], html)
        if not matches:
            print(f"    WARNING: no article links found")
            continue

        sk = rule["sort_key"]
        if sk == "numeric":
            best = max(matches, key=lambda m: int(m[1]))
            article_url = best[0]
        elif sk == "date_str":
            best = max(matches, key=lambda m: m[1])
            article_url = best[0]
            if not article_url.startswith("http"):
                article_url = rule["base_url"] + article_url
        elif sk == "date_ymd":
            best = max(matches, key=lambda m: (m[1], m[2], m[3]))
            article_url = best[0]
            if not article_url.startswith("http"):
                article_url = rule["base_url"] + article_url
        else:
            article_url = matches[0][0]

        print(f"    Latest article: {article_url}")

        article_html, _ = _fetch(article_url)
        if not article_html:
            print(f"    FAILED: article page not reachable")
            continue

        yaml_urls = yaml_re.findall(article_html)
        if not yaml_urls:
            print(f"    WARNING: no .yaml links in article")
            continue

        result[name] = sorted(set(yaml_urls))
        print(f"    Found {len(result[name])} YAML link(s)")

    # ── Strategy B: Date CDN (HEAD-verify newest) ──
    MAX_BACKTRACK = 7
    for rule in DATE_CDN_SOURCES:
        name = rule["name"]
        print(f"  [{name}] Probing CDN (date backtrack up to {MAX_BACKTRACK}d)...")
        found = False
        for offset in range(MAX_BACKTRACK + 1):
            d = today - timedelta(days=offset)
            urls = rule["urls"](d)
            if _head_check(urls[0]) == 200:
                tag = f" (-{offset}d)" if offset > 0 else ""
                print(f"    Best date: {d.isoformat()}{tag}")
                verified = [u for u in urls if _head_check(u) == 200]
                if verified:
                    result[name] = verified
                    print(f"    Found {len(verified)} YAML link(s)")
                    found = True
                    break
        if not found:
            print(f"    WARNING: no valid date in {MAX_BACKTRACK}d range")

    return result


# ── Proxy processing ────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "yaml_data")
DOWNLOAD_DIR = os.path.join(OUTPUT_DIR, "raw")

log_lines = []


def log(msg):
    print(msg)
    log_lines.append(msg)


def proxy_key(proxy):
    """Generate a unique key for dedup."""
    return f"{proxy.get('name','')}|{proxy.get('server','')}|{proxy.get('port','')}|{proxy.get('type','')}"


def is_valid_proxy(proxy):
    if not isinstance(proxy, dict):
        return False
    return bool(proxy.get("name")) and bool(proxy.get("server")) and proxy.get("port") is not None and bool(proxy.get("type"))


def download_and_extract(yaml_links):
    """Download YAML files from discovered links and extract proxies."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    all_proxies = []
    seen_keys = set()
    total_links = 0
    success_downloads = 0
    fail_downloads = 0

    for site, links in yaml_links.items():
        log(f"\n{'=' * 60}")
        log(f"Processing: {site} ({len(links)} links)")
        log(f"{'=' * 60}")

        for i, url in enumerate(links):
            total_links += 1
            safe_name = f"{site.replace('.', '_')}_{i}"
            output_file = os.path.join(DOWNLOAD_DIR, f"{safe_name}.yaml")

            log(f"  [{i}] {url}")

            if not _download(url, output_file):
                log(f"    FAILED: download error")
                fail_downloads += 1
                continue

            file_size = os.path.getsize(output_file)
            log(f"    Downloaded: {file_size} bytes")

            try:
                with open(output_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                data = yaml.safe_load(content)
            except Exception as e:
                log(f"    YAML parse error: {e}")
                continue

            if not data or not isinstance(data, dict):
                log(f"    No valid YAML data")
                continue

            proxies = data.get("proxies", [])
            if not proxies or not isinstance(proxies, list):
                log(f"    No 'proxies:' list found")
                continue

            proxy_count = 0
            for p in proxies:
                if is_valid_proxy(p):
                    key = proxy_key(p)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_proxies.append((site, p))
                        proxy_count += 1

            log(f"    {len(proxies)} raw → {proxy_count} unique added")
            success_downloads += 1

    return all_proxies, total_links, success_downloads, fail_downloads


def write_output(all_proxies, yaml_links):
    """Write consolidated and per-source YAML files in text-block-compatible format.
    
    Output format uses '  - name:' (2-space indent) so downstream text-block
    parsers (geo_enrich, tcp_screen, mihomo_test) can correctly parse it.
    'name' is always the first field to ensure text-block parsers detect it.
    """
    total_proxies = len(all_proxies)

    def _format_proxy(proxy, source):
        """Format a single proxy dict as a text-block-compatible YAML string."""
        proxy["_source"] = source
        # Reorder dict: name first, then everything else
        if "name" in proxy:
            reordered = {"name": proxy["name"]}
            for k, v in proxy.items():
                if k != "name":
                    reordered[k] = v
            proxy = reordered
        # Use yaml.dump but add 2-space indent to make it text-block compatible
        raw = yaml.dump([proxy], allow_unicode=True, default_flow_style=False, sort_keys=False)
        # yaml.dump outputs "- name: ..." at indent 0; we need "  - name: ..." at indent 2
        lines = raw.split('\n')
        indented_lines = []
        for line in lines:
            if line.startswith('- '):
                indented_lines.append('  ' + line)
            elif line.strip():
                indented_lines.append('  ' + line)
            else:
                indented_lines.append(line)
        return '\n'.join(indented_lines)

    # Consolidated
    consolidated_file = os.path.join(OUTPUT_DIR, "all_proxies.yaml")
    with open(consolidated_file, 'w', encoding='utf-8') as f:
        f.write(f"# Consolidated proxies from {len(yaml_links)} sites\n")
        f.write(f"# Total unique proxies: {total_proxies}\n")
        f.write(f"# Date: {date.today().isoformat()}\n")
        f.write(f"#\n")
        for site in yaml_links:
            count = sum(1 for s, _ in all_proxies if s == site)
            f.write(f"#   {site}: {count} proxies\n")
        f.write(f"\nproxies:\n")
        for source, proxy in all_proxies:
            f.write(_format_proxy(proxy, source))

    # Per-source
    for site in yaml_links:
        site_proxies = [(s, p) for s, p in all_proxies if s == site]
        if not site_proxies:
            continue
        site_file = os.path.join(OUTPUT_DIR, f"{site.replace('.', '_')}.yaml")
        with open(site_file, 'w', encoding='utf-8') as f:
            f.write(f"# Source: {site}\n")
            f.write(f"# Proxies: {len(site_proxies)}\n")
            f.write(f"# Date: {date.today().isoformat()}\n\nproxies:\n")
            for source, p in site_proxies:
                f.write(_format_proxy(p, source))

    return consolidated_file


def main():
    print("=" * 60)
    print("Step 1: Discovering YAML links from source websites...")
    print("=" * 60)

    yaml_links = discover_yaml_links()

    if not yaml_links:
        print("\nFATAL: No YAML links discovered from any source!")
        sys.exit(1)

    print(f"\nDiscovered YAML links from {len(yaml_links)} sources:")
    for site, links in yaml_links.items():
        print(f"  {site}: {len(links)} links")

    print(f"\n{'=' * 60}")
    print("Step 2: Downloading YAML files and extracting proxies...")
    print("=" * 60)

    all_proxies, total_links, success, fail = download_and_extract(yaml_links)

    print(f"\n{'=' * 60}")
    print("Step 3: Writing output files...")
    print("=" * 60)

    consolidated = write_output(all_proxies, yaml_links)

    # Summary
    log(f"\n{'=' * 60}")
    log("SUMMARY")
    log(f"{'=' * 60}")
    log(f"Sources discovered: {len(yaml_links)}")
    log(f"Total YAML links: {total_links}")
    log(f"Successful: {success}")
    log(f"Failed: {fail}")
    log(f"Unique proxies: {len(all_proxies)}")

    # Save log
    log_file = os.path.join(OUTPUT_DIR, "download_log.txt")
    with open(log_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(log_lines))

    # Print final summary for pipeline integration
    print(f"\nDOWNLOAD_SUMMARY: sources={len(yaml_links)} links={total_links} success={success} fail={fail} proxies={len(all_proxies)}")

    return consolidated, len(all_proxies)


if __name__ == "__main__":
    main()
