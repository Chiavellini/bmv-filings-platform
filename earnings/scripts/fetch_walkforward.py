#!/usr/bin/env python3
"""fetch_walkforward.py — pristine 2026-2T XBRL for the walk-forward test.

Network: BMV only. Refetches the archive page into earnings/data/walkforward
(alpha-go's cache untouched), downloads each universe ticker's 2026-2T zip,
and extracts the facts json with alpha-go's extract_artifacts. Resumable.
"""
from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

WF_DIR = bs.EARNINGS_ROOT / "data" / "walkforward"
XBRL_DIR = WF_DIR / "xbrl"
PERIOD = "2026-2T"


def main() -> None:
    from src.download.bmv_xbrl import parse_archive_index, extract_artifacts
    import requests

    WF_DIR.mkdir(parents=True, exist_ok=True)
    XBRL_DIR.mkdir(parents=True, exist_ok=True)

    idx_path = WF_DIR / "archive_index_2026-07-27.html"
    if not idx_path.exists():
        resp = requests.get(
            "https://www.bmv.com.mx/es/emisoras/archivos-estadar-xbrl",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=120)
        resp.raise_for_status()
        idx_path.write_text(resp.text)
        print(f"archive index fetched: {len(resp.text)//1024} KB")
    filings = [f for f in parse_archive_index(idx_path.read_text())
               if f.kind == "quarterly" and f.period == PERIOD]

    cfg = bs.load_config()
    uni = bs.load_universe(cfg, "full")
    # archive ticker -> facts ticker naming (PE&OLES -> PEOLES)
    want = {}
    for slug, v in uni.items():
        want[v["ticker"].replace("PEOLES", "PE&OLES") if v["ticker"] == "PEOLES"
             else v["ticker"]] = (slug, v["ticker"])

    targets = [f for f in filings if f.ticker in want]
    print(f"{PERIOD} filings on page: {len(filings)}; in universe: {len(targets)}")

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    done = fetched = failed = 0
    meta_rows = []
    for f in sorted(targets, key=lambda x: x.ticker):
        slug, facts_ticker = want[f.ticker]
        facts_path = XBRL_DIR / f"{facts_ticker}_{PERIOD}_facts.json"
        meta_rows.append({"ticker": f.ticker, "slug": slug,
                          "filed_date": f.filed_date, "zip_url": f.zip_url})
        if facts_path.exists():
            done += 1
            continue
        try:
            time.sleep(0.5)
            r = session.get(f.zip_url, timeout=180)
            r.raise_for_status()
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            inner = [n for n in zf.namelist() if n.lower().endswith(".json")]
            instance_path = XBRL_DIR / f"{facts_ticker}_{PERIOD}.json"
            instance_path.write_bytes(zf.read(inner[0]))
            extract_artifacts(instance_path, XBRL_DIR)
            instance_path.unlink()          # keep only the small artifacts
            if not facts_path.exists():
                raise RuntimeError("extract_artifacts produced no facts json")
            fetched += 1
            print(f"  {f.ticker}: ok ({len(r.content)//1024} KB zip)")
        except Exception as e:
            failed += 1
            print(f"  {f.ticker}: FAILED ({e})")
    (WF_DIR / "filings_meta.json").write_text(json.dumps(meta_rows, indent=1))

    print(f"\nfacts present: {done + fetched}/{len(targets)} "
          f"(new {fetched}, failed {failed})")


if __name__ == "__main__":
    main()
