#!/usr/bin/env python3
"""Snapshot the official BMV issuer directory before adding issuers to the search corpus.

The current 50-company configuration is a curated production seed.  This command establishes a
larger, source-attributed registry without silently treating funds, debt vehicles, or duplicate
series as operating companies.  The registry is the review queue for the all-BMV acquisition
program and is refreshed independently of document downloads.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.sources.bmv_issuer import fetch_issuer_directory  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "catalog" / "bmv_issuer_universe.json")
    parser.add_argument("--include-debt", action="store_true",
                        help="also snapshot BMV's debt-route directory (kept separate by market)")
    args = parser.parse_args(argv)
    markets = ("CGEN_CAPIT", "CGEN_ELDEU") if args.include_debt else ("CGEN_CAPIT",)
    configured = yaml.safe_load((ROOT / "configs" / "bmv_corpus.yaml").read_text(encoding="utf-8")) or {}
    curated = {str(row["ticker"]).upper(): str(row["slug"]) for row in configured.get("companies", [])}
    rows = fetch_issuer_directory(markets=markets)
    payload = {
        "source": "BMV issuer directory",
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "markets": list(markets),
        "notes": (
            "This is a source snapshot and curation queue, not a claim that every record is an "
            "operating company. Add reviewed candidates to configs/bmv_corpus.yaml before download."
        ),
        "issuers": [
            {"ticker": row.ticker, "issuer_id": row.issuer_id, "legal_name": row.legal_name,
             "market": row.market, "sector": row.sector, "configured_slug": curated.get(row.ticker),
             "curation_status": "configured" if row.ticker in curated else "discovered"}
            for row in rows
        ],
    }
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Snapshot: issuers={len(rows)} configured={sum(row.ticker in curated for row in rows)} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
