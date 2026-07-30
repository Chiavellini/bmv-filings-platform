#!/usr/bin/env python3
"""Fetch CNBV's latest bank-capital table (ICAP_BM) and cache it for the coverage engine.

Downloads the most recent ``ICAP_BM_<YYYYMM>.pdf`` from CNBV's Portafolio de Información, parses the
per-bank CCB / CCF (CET1) / ICAP ratios, and writes ``data/cnbv/bank_capital.json``. The per-company
build reads that cache offline (``src/download/cnbv.load_bank_capital``). Run periodically (CNBV
publishes monthly, with a lag) — e.g. from the daily refresh, which no-ops if already current.

    python3 scripts/fetch_cnbv_icap.py            # needs network — sandbox override in this env
    python3 scripts/fetch_cnbv_icap.py --show     # also print the mapped universe banks

Reports which universe banks resolved to a CNBV row.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.download import cnbv  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months-back", type=int, default=6, help="how far back to look for a report")
    ap.add_argument("--verify-ssl", action="store_true", help="verify TLS (off by default in this env)")
    ap.add_argument("--show", action="store_true", help="print each mapped universe bank's ratios")
    args = ap.parse_args()

    payload = cnbv.fetch_and_cache(verify_ssl=args.verify_ssl, months_back=args.months_back)
    if payload is None:
        sys.exit("could not fetch/parse any ICAP_BM report from CNBV (network? lag? URL change?)")

    banks = payload["banks"]
    print(f"[cnbv] period {payload['period']} · {len(banks)} banks parsed · {payload['source']}")
    print(f"[cnbv] wrote {cnbv.CACHE_FILE}")

    resolved = missing = 0
    for slug, name in sorted(cnbv.SLUG_TO_CNBV.items()):
        cap = banks.get(name)
        if cap:
            resolved += 1
            if args.show:
                print(f"  ✓ {slug:22s} → {name:18s} CET1 {cap['cet1']:.2f}%  ICAP {cap['icap']:.2f}%")
        else:
            missing += 1
            print(f"  ✗ {slug:22s} → {name!r} NOT FOUND in the CNBV table")
    print(f"[cnbv] {resolved}/{len(cnbv.SLUG_TO_CNBV)} universe banks resolved "
          f"({missing} unmapped — check the name against the printed table).")


if __name__ == "__main__":
    main()
