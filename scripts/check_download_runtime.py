#!/usr/bin/env python3
"""check_download_runtime.py — are the optional download runtimes actually present?

`playwright` and `curl_cffi` are optional extras in `pyproject.toml` but
mandatory for specific issuers: eight declare ``use_playwright`` (their IR pages
render links via JavaScript) and others declare ``impersonate`` (their hosts
filter on TLS fingerprint). When a runtime is missing the downloader does not
fail — it records a diagnostic and returns whatever static links it found. For
Grupo Herdez that meant scraping governance PDFs instead of quarterly reports,
and nothing in the pipeline said so.

`requirements/acquisition-worker.txt` pins both, but a plain `pip install .`
installs neither, so a clean clone or a USB handoff silently ships a degraded
downloader. This turns that into an exit code.

Usage:
    python3 scripts/check_download_runtime.py          # exit 1 if a needed runtime is missing
    python3 scripts/check_download_runtime.py --quiet  # summary lines only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.acquisition.registry import load_issuer_registry  # noqa: E402

INSTALL_HINTS = {
    "playwright": (
        "pip install 'playwright==1.60.0' && playwright install chromium"
    ),
    "curl_cffi": "pip install 'curl-cffi==0.15.0'",
}


def playwright_ready() -> tuple[bool, str]:
    """Both the package *and* a browser binary must be present."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "package not installed"
    try:
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
    except Exception as exc:  # noqa: BLE001 — any launch failure is a failure
        return False, f"{type(exc).__name__}: {exc}"
    if not path or not Path(path).exists():
        return False, "chromium binary missing (run: playwright install chromium)"
    return True, "ok"


def curl_cffi_ready() -> tuple[bool, str]:
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return False, "package not installed"
    return True, "ok"


def dependants() -> dict[str, list[str]]:
    """Which enabled issuer sources need which runtime."""
    registry = load_issuer_registry()
    needs: dict[str, list[str]] = {"playwright": [], "curl_cffi": []}
    for issuer, source in registry.enabled_sources(kind="investor_relations"):
        if source.use_playwright:
            needs["playwright"].append(issuer.slug)
        if source.impersonate:
            needs["curl_cffi"].append(issuer.slug)
    return {runtime: sorted(slugs) for runtime, slugs in needs.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quiet", action="store_true", help="summary lines only")
    args = ap.parse_args(argv)

    checks = {"playwright": playwright_ready(), "curl_cffi": curl_cffi_ready()}
    needed = dependants()

    failures: list[str] = []
    for runtime, (ok, detail) in checks.items():
        slugs = needed.get(runtime, [])
        status = "ok  " if ok else "FAIL"
        print(f"{status} {runtime:<12} {detail:<45} {len(slugs)} issuer(s) depend on it")
        if not args.quiet and slugs:
            print(f"       {', '.join(slugs)}")
        # A missing runtime only matters if something actually needs it.
        if not ok and slugs:
            failures.append(runtime)

    if failures:
        print("\nThese issuers will silently produce degraded results:", file=sys.stderr)
        for runtime in failures:
            print(
                f"  {runtime}: {', '.join(needed[runtime])}\n"
                f"    fix: {INSTALL_HINTS[runtime]}",
                file=sys.stderr,
            )
        return 1
    print("\nAll runtimes required by enabled sources are present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
