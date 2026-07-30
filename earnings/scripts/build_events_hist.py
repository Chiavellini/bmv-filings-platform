#!/usr/bin/env python3
"""build_events_hist.py — pre-XBRL-era event dates from report front pages.

For every historical report markdown (root data/reports/<slug>/*.md, period
< 2021-2T), extract the publication date printed in the document head
("Mexico City, April 22nd, 2014" / "Ciudad de México, a 22 de julio de 2014").
Day-level only: info_dt = date @ 23:59 CDMX encodes the pre-committed
after-close assumption (t0 = next trading day). No extractable date -> no event
(counted). Writes outputs/events_hist.parquet and the merged
outputs/events_all.parquet (archive events + historical rows).
"""
from __future__ import annotations

import re
import sys
import unicodedata
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib.calendar import CDMX

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_metrics_hist import PDF_COMPANIES, SLUG_MAP, XBRL_START, period_of, _QEND

HEAD_CHARS = 3000

_MONTHS_EN = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
_MONTHS_ES = {m: i + 1 for i, m in enumerate(
    ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
     "agosto", "septiembre", "octubre", "noviembre", "diciembre"])}

_EN_RE = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+(\d{1,2})\s*(?:st|nd|rd|th)?\s*,?\s+(20\d\d)",
    re.IGNORECASE)
_ES_RE = re.compile(
    r"(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|octubre|noviembre|diciembre)\s+(?:de[l]?\s+)?(20\d\d)",
    re.IGNORECASE)


def _deaccent(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def header_date(text: str):
    head = _deaccent(text[:HEAD_CHARS].replace("\n", " "))
    cands = []
    for m in _EN_RE.finditer(head):
        cands.append((m.start(), int(m.group(3)), _MONTHS_EN[m.group(1).lower()],
                      int(m.group(2))))
    for m in _ES_RE.finditer(head):
        cands.append((m.start(), int(m.group(3)), _MONTHS_ES[m.group(2).lower()],
                      int(m.group(1))))
    for _, y, mo, d in sorted(cands):
        try:
            return datetime(y, mo, d)
        except ValueError:
            continue
    return None


def main() -> None:
    cfg = bs.load_config()
    uni = bs.load_universe(cfg, "full")
    tickers = {s: v["ticker"] for s, v in uni.items()}
    symbols = {s: v["symbol"] for s, v in uni.items()}

    rows, misses = [], {}
    for root_slug in PDF_COMPANIES:
        rep_dir = bs.LEGACY_REPORTS_DIR / root_slug
        if not rep_dir.exists():
            continue
        slug = SLUG_MAP.get(root_slug, root_slug)
        seen = set()
        for md in sorted(rep_dir.glob("*.md")):
            period = period_of(md.stem)
            if period is None or period >= XBRL_START or period in seen:
                continue
            seen.add(period)
            pend = datetime.fromisoformat(f"{period[:4]}-{_QEND[period[5]]}")
            dt = header_date(md.read_text(errors="ignore"))
            ok = dt is not None and pend < dt <= pend + pd.Timedelta(days=90)
            if not ok:
                misses[slug] = misses.get(slug, 0) + 1
                continue
            rows.append({
                "ticker": tickers.get(slug), "slug": slug,
                "symbol": symbols.get(slug), "period": period,
                "filed_dt": None, "zip_url": None, "is_refiled": False,
                "has_facts": True,          # metrics come from metrics_hist
                "has_prices": True,         # gated again at measure time
                "in_universe": True,
                "press_dt": None,
                "info_dt": datetime.combine(dt.date(), time(23, 59), tzinfo=CDMX),
                "date_source": "pdf_header",
            })

    hist = pd.DataFrame(rows)
    hist.to_parquet(bs.art_path("events_hist"), index=False)

    events = pd.read_parquet(bs.art_path("events"))
    existing = set(zip(events["slug"], events["period"]))
    add = hist[~hist.apply(lambda r: (r["slug"], r["period"]) in existing, axis=1)]
    merged = pd.concat([events, add], ignore_index=True)
    merged.to_parquet(bs.art_path("events_all"), index=False)

    # ---- verify block ----
    print(f"historical events dated : {len(hist)} across {hist['slug'].nunique()} slugs")
    print(f"date-extraction misses  : {misses}")
    print(f"merged events_all       : {len(merged)} (archive {len(events)} + new {len(add)})")
    per = hist.groupby("slug")["period"].agg(["min", "max", "count"])
    print(per.to_string())
    for sl, p, want in [("walmex", "2014-1T", "2014-04-22"),
                        ("kof", "2016-2T", "2016-07-27")]:
        row = hist[(hist["slug"] == sl) & (hist["period"] == p)]
        got = str(row.iloc[0]["info_dt"])[:10] if len(row) else "MISSING"
        flag = "OK" if got == want else f"MISMATCH (want {want})"
        print(f"  spot-check {sl} {p}: {got} {flag}")


if __name__ == "__main__":
    main()
