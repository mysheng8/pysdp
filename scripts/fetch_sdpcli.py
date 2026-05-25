"""fetch_sdpcli.py — Download and manage SDPCLI binary releases.

Usage:
  python scripts/fetch_sdpcli.py          # download if missing or outdated
  python scripts/fetch_sdpcli.py --force  # force re-download

Called automatically:
  - post-install hook (pip install pysdp)
  - server startup version check
"""
from __future__ import annotations

import io
import os
import platform
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# Default install location
_INSTALL_DIR = Path.home() / ".pysdp" / "sdpcli"
_VERSION_FILE = _INSTALL_DIR / "VERSION"


def _read_pyproject() -> dict:
    """Read [tool.pysdp] from pyproject.toml."""
    toml_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not toml_path.exists():
        for p in [Path.cwd() / "pyproject.toml", Path.cwd().parent / "pyproject.toml"]:
            if p.exists():
                toml_path = p
                break
    if sys.version_info >= (3, 11):
        import tomllib
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    else:
        try:
            import tomli
            with open(toml_path, "rb") as f:
                data = tomli.load(f)
        except ImportError:
            import re
            text = toml_path.read_text(encoding="utf-8")
            version_m = re.search(r'sdpcli_version\s*=\s*"([^"]+)"', text)
            url_m = re.search(r'sdpcli_release_url\s*=\s*"([^"]+)"', text)
            return {
                "sdpcli_version": version_m.group(1) if version_m else "0.1.0",
                "sdpcli_release_url": url_m.group(1) if url_m else "",
            }
    return data.get("tool", {}).get("pysdp", {})


def get_required_version() -> str:
    return _read_pyproject().get("sdpcli_version", "0.1.0")


def get_release_url(version: str) -> str:
    template = _read_pyproject().get(
        "sdpcli_release_url",
        "https://github.com/mysheng8/sdpcli-releases/releases/download/v{version}/SDPCLI-v{version}-win64.zip",
    )
    return template.format(version=version)


def get_local_version() -> str | None:
    if _VERSION_FILE.exists():
        return _VERSION_FILE.read_text().strip()
    return None


def get_sdpcli_path() -> Path | None:
    """Resolve SDPCLI executable path (priority order)."""
    env_path = os.environ.get("PYSDP_SDPCLI_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    installed = _INSTALL_DIR / "SDPCLI.exe"
    if installed.exists():
        return installed

    monorepo = Path(__file__).resolve().parent.parent.parent / "SDPCLI" / "bin" / "Debug" / "net472" / "SDPCLI.exe"
    if monorepo.exists():
        return monorepo

    return None


def download(version: str, force: bool = False) -> Path:
    """Download SDPCLI release zip and extract to install dir."""
    if not force and get_local_version() == version:
        print(f"SDPCLI v{version} already installed at {_INSTALL_DIR}")
        return _INSTALL_DIR

    url = get_release_url(version)
    print(f"Downloading SDPCLI v{version}...")
    print(f"  URL: {url}")

    try:
        req = Request(url, headers={"User-Agent": "pysdp-fetch/1.0"})
        with urlopen(req, timeout=120) as resp:
            data = resp.read()
    except URLError as e:
        print(f"  ERROR: Failed to download — {e}")
        print(f"  You can manually download from: {url}")
        print(f"  Extract to: {_INSTALL_DIR}")
        return _INSTALL_DIR

    print(f"  Downloaded {len(data) / 1024 / 1024:.1f} MB")

    if _INSTALL_DIR.exists():
        shutil.rmtree(_INSTALL_DIR)
    _INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(_INSTALL_DIR)

    _VERSION_FILE.write_text(version)
    print(f"  Installed to: {_INSTALL_DIR}")

    exe = _INSTALL_DIR / "SDPCLI.exe"
    if not exe.exists():
        subdirs = [d for d in _INSTALL_DIR.iterdir() if d.is_dir()]
        if subdirs:
            for item in subdirs[0].iterdir():
                shutil.move(str(item), str(_INSTALL_DIR / item.name))
            subdirs[0].rmdir()

    return _INSTALL_DIR


def check_sdpcli_version(auto_download: bool = True) -> bool:
    """Check if local SDPCLI version matches required. Auto-downloads if needed.
    Returns True if version is OK."""
    required = get_required_version()
    local = get_local_version()

    if local == required:
        return True

    if local:
        print(f"SDPCLI version mismatch: local={local}, required={required}")
    else:
        print(f"SDPCLI not found (required: v{required})")

    if auto_download:
        download(required)
        return True

    print(f"  Run: pysdp-fetch  (or: python scripts/fetch_sdpcli.py)")
    return False


def main():
    """CLI entry point."""
    force = "--force" in sys.argv
    version = get_required_version()
    download(version, force=force)


if __name__ == "__main__":
    main()
