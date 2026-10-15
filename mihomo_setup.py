#!/usr/bin/env python3
"""
Mihomo binary download and setup manager.

Cross-platform: auto-detects OS + CPU arch and downloads the matching
mihomo release (macOS arm64/amd64 or Linux amd64/arm64). Works both on the
local Mac and inside GitHub Actions (ubuntu-latest = linux/amd64).
"""

import os
import sys
import gzip
import stat
import platform
import urllib.request
import hashlib
from pathlib import Path

MIHOMO_VERSION = "v1.19.29"
MIHOMO_DIR = Path.home() / ".mihomo"
MIHOMO_BIN = MIHOMO_DIR / "mihomo"
MIHOMO_VERSION_FILE = MIHOMO_DIR / ".version"

GITHUB_BASE = "https://github.com/MetaCubeX/mihomo/releases/download"


def detect_arch():
    """Return (os_tag, arch_tag) for the current platform's mihomo release."""
    machine = platform.machine().lower()
    if sys.platform.startswith("darwin"):
        os_tag = "darwin"
        arch_tag = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    elif sys.platform.startswith("linux"):
        os_tag = "linux"
        arch_tag = "amd64" if machine in ("x86_64", "amd64") else "arm64"
    else:
        os_tag = "linux"
        arch_tag = "amd64"
    return os_tag, arch_tag


def get_download_url(version, arch=None):
    """Build the mihomo download URL for the current platform (or a given arch)."""
    os_tag, arch_tag = detect_arch()
    if arch is not None:
        arch_tag = arch
    filename = f"mihomo-{os_tag}-{arch_tag}-{version}.gz"
    return f"{GITHUB_BASE}/{version}/{filename}"


def download_file(url, dest, timeout=120):
    """Download a file with progress reporting."""
    print(f"   Downloading: {url}")
    try:
        # Disable proxy for GitHub downloads
        proxy_handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(proxy_handler)
        urllib.request.install_opener(opener)

        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; mihomo-setup)",
            "Accept": "application/octet-stream",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = resp.headers.get("Content-Length")
            if total:
                total = int(total)

            data = b""
            chunk_size = 1024 * 64
            downloaded = 0
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                data += chunk
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(f"\r   Progress: {downloaded}/{total} ({pct:.0f}%)", end="")
            print()

        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"\n   Download failed: {e}")
        return False


def ensure_mihomo():
    """
    Ensure mihomo binary exists at MIHOMO_BIN.
    If not present or version mismatch, download it.
    Returns (binary_path, version) or (None, None) on failure.
    """
    # Check if current binary is already good
    if MIHOMO_BIN.exists() and MIHOMO_VERSION_FILE.exists():
        try:
            with open(MIHOMO_VERSION_FILE) as f:
                installed_version = f.read().strip()
            if installed_version == MIHOMO_VERSION:
                print(f"   Mihomo {installed_version} already installed at {MIHOMO_BIN}")
                return (str(MIHOMO_BIN), installed_version)
        except Exception:
            pass

    # Need to download
    os_tag, arch_tag = detect_arch()
    print(f"[Mihomo Setup] Installing mihomo {MIHOMO_VERSION} for {os_tag}/{arch_tag}...")
    MIHOMO_DIR.mkdir(parents=True, exist_ok=True)

    url = get_download_url(MIHOMO_VERSION)
    gz_path = MIHOMO_DIR / "mihomo.gz"

    # Download
    if not download_file(url, gz_path):
        # Try compatible variant
        print("   Trying compatible variant...")
        url2 = f"{GITHUB_BASE}/{MIHOMO_VERSION}/mihomo-{os_tag}-{arch_tag}-compatible-{MIHOMO_VERSION}.gz"
        if not download_file(url2, gz_path):
            print("   ERROR: Could not download mihomo binary")
            return (None, None)

    # Decompress
    print(f"   Decompressing...")
    try:
        with gzip.open(gz_path, "rb") as f_in:
            with open(MIHOMO_BIN, "wb") as f_out:
                f_out.write(f_in.read())
    except Exception as e:
        print(f"   Decompress failed: {e}")
        return (None, None)

    # Make executable
    os.chmod(MIHOMO_BIN, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

    # Verify binary works
    try:
        import subprocess
        result = subprocess.run(
            [str(MIHOMO_BIN), "-v"],
            capture_output=True, text=True, timeout=5
        )
        version_output = result.stdout.strip() or result.stderr.strip()
        print(f"   Version check: {version_output[:80]}")
    except Exception as e:
        print(f"   Warning: version check failed: {e}")

    # Clean up gz file
    gz_path.unlink(missing_ok=True)

    # Write version file
    with open(MIHOMO_VERSION_FILE, "w") as f:
        f.write(MIHOMO_VERSION)

    print(f"   Mihomo {MIHOMO_VERSION} installed to {MIHOMO_BIN}")
    return (str(MIHOMO_BIN), MIHOMO_VERSION)


def get_mihomo_path():
    """Get the mihomo binary path, ensuring it exists."""
    path, version = ensure_mihomo()
    return path


if __name__ == "__main__":
    path, version = ensure_mihomo()
    if path:
        print(f"\n✅ Mihomo ready: {path}")
        print(f"   Version: {version}")
    else:
        print("\n❌ Failed to setup mihomo")
        sys.exit(1)
