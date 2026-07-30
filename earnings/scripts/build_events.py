#!/usr/bin/env python3
"""build_events.py — quarterly filing events from the cached BMV archive page.

Offline: parses alpha-go/data/bmv/archive_index.html (snapshot 2026-07-21) with
alpha-go's parse_archive_index, normalizes the "dd/mm/YYYY HH:MM" timestamps to
tz-aware CDMX datetimes, dedupes refilings to the earliest timestamp, and joins
ticker -> slug (scanned from soft facts filenames) -> Yahoo symbol (study.yaml).

Outputs: outputs/events.parquet, outputs/universe.csv
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib.calendar import CDMX

import pandas as pd
from src.download.bmv_xbrl import parse_archive_index

# facts-filename ticker -> BMV archive ticker (soft strips the ampersand)
TICKER_ALIASES = {"PEOLES": "PE&OLES"}
ARCHIVE_TO_FACTS = {v: k for k, v in TICKER_ALIASES.items()}

# earnings-release title matching for eventos relevantes (press_dt recovery)
_EARNINGS_RE = re.compile(r"resultado|reporte\s+financiero|informaci[oó]n\s+financiera",
                          re.IGNORECASE)
_Q_CUES = [
    ("4", re.compile(r"\b4T\b|4T\s*\d{2}|4to\.?\s*trimestre|cuarto\s+trimestre|"
                     r"cuatro\s+trimestre|a[ñn]o\s+completo|full\s+year|fourth\s+quarter",
                     re.IGNORECASE)),
    ("3", re.compile(r"\b3T\b|3T\s*\d{2}|3er\.?\s*trimestre|tercer\s+trimestre|"
                     r"third\s+quarter", re.IGNORECASE)),
    ("2", re.compile(r"\b2T\b|2T\s*\d{2}|2do\.?\s*trimestre|segundo\s+trimestre|"
                     r"second\s+quarter", re.IGNORECASE)),
    ("1", re.compile(r"\b1T\b|1T\s*\d{2}|1er\.?\s*trimestre|primer\s+trimestre|"
                     r"first\s+quarter", re.IGNORECASE)),
]
# publication-month sanity windows per fiscal quarter: (months, year_offset)
_Q_WINDOWS = {"1": ({4, 5}, 0), "2": ({7, 8}, 0), "3": ({10, 11}, 0),
              "4": ({1, 2, 3}, 1)}


def ticker_slug_map() -> dict[str, str]:
    """TICKER -> slug from soft facts filenames (<TICKER>_<period>_facts.json)."""
    out: dict[str, str] = {}
    for facts in bs.facts_root_glob():
        slug = facts.parents[1].name
        ticker = facts.name.split("_", 1)[0]
        prev = out.get(ticker)
        if prev is not None and prev != slug:
            print(f"WARN ticker {ticker} maps to both {prev} and {slug}; keeping {prev}")
            continue
        out[ticker] = slug
    for facts_t, arch_t in TICKER_ALIASES.items():
        if facts_t in out:
            out[arch_t] = out[facts_t]
    return out


def press_release_dates() -> dict[tuple[str, str], datetime]:
    """(slug, period) -> earliest matching earnings press-release timestamp,
    from the alpha-go document catalog (eventos relevantes, ISO minutes)."""
    import sqlite3

    db = bs.ALPHA_GO_ROOT / "data" / "catalog" / "documents.db"
    if not db.exists():
        print("WARN documents.db missing — no press-release recovery")
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select company, title, published_at from documents "
        "where doc_type='relevant_event' and published_at is not null").fetchall()
    con.close()

    out: dict[tuple[str, str], datetime] = {}
    for slug, title, pub in rows:
        if not title or not _EARNINGS_RE.search(title):
            continue
        try:
            dt = datetime.fromisoformat(pub).replace(tzinfo=CDMX)
        except ValueError:
            continue
        for q, cue in _Q_CUES:
            if not cue.search(title):
                continue
            months, yoff = _Q_WINDOWS[q]
            if dt.month not in months:
                break                      # cue matched but timing implausible
            period = f"{dt.year - yoff}-{q}T"
            key = (slug, period)
            if key not in out or dt < out[key]:
                out[key] = dt
            break
    return out


def main() -> None:
    cfg = bs.load_config()
    universe = bs.load_universe(cfg, "full")
    html = (bs.ALPHA_GO_ROOT / "data" / "bmv" / "archive_index.html").read_text()
    filings = [f for f in parse_archive_index(html) if f.kind == "quarterly"]

    t2s = ticker_slug_map()
    slug2sym = {slug: v["symbol"] for slug, v in universe.items()}
    slug2ticker = {slug: v["ticker"] for slug, v in universe.items()}

    bad_dates = 0
    rows = []
    for f in filings:
        try:
            filed = datetime.strptime(f.filed_date, "%d/%m/%Y %H:%M").replace(tzinfo=CDMX)
        except ValueError:
            bad_dates += 1
            continue
        slug = t2s.get(f.ticker)
        symbol = slug2sym.get(slug) if slug else None
        rows.append({
            "ticker": f.ticker,
            "slug": slug,
            "symbol": symbol,
            "period": f.period,
            "filed_dt": filed,
            "zip_url": f.zip_url,
        })

    df = pd.DataFrame(rows).sort_values(["ticker", "period", "filed_dt"])
    dup_mask = df.duplicated(["ticker", "period"], keep=False)
    df["is_refiled"] = dup_mask
    # information first hit the market at the EARLIEST filing per (ticker, period)
    df = df.drop_duplicates(["ticker", "period"], keep="first").reset_index(drop=True)

    df["has_facts"] = [
        bs.facts_exists(str(s), ARCHIVE_TO_FACTS.get(t, t), p) if s else False
        for s, t, p in zip(df["slug"], df["ticker"], df["period"])
    ]
    df["has_prices"] = [
        (bs.SNAPSHOT_DIR / f"yf_{str(sym).replace('.MX', '')}_MX.json").exists() if sym else False
        for sym in df["symbol"]
    ]
    df["in_universe"] = df["slug"].isin(universe.keys())

    # info_dt: when the information actually reached the market — the earlier
    # of the XBRL filing and a matching earnings press release (eventos
    # relevantes). This is what recovers February Q4 announcements whose XBRL
    # only arrives with the April audited annual.
    press = press_release_dates()
    qe = {"1": "-03-31", "2": "-06-30", "3": "-09-30", "4": "-12-31"}
    press_col, info_col, src_col = [], [], []
    bad_press = 0
    for _, r in df.iterrows():
        pdt = press.get((r["slug"], r["period"])) if r["slug"] else None
        filed = r["filed_dt"]
        pend = None
        m = re.match(r"^(20\d{2})-([1-4])T$", r["period"])
        if m:
            pend = datetime.fromisoformat(m.group(1) + qe[m.group(2)]).replace(tzinfo=CDMX)
        ok = (
            pdt is not None and pend is not None
            and pend < pdt <= pend + timedelta(days=120)
        )
        if pdt is not None and not ok:
            bad_press += 1
            pdt = None
        press_col.append(pdt)
        if pdt is not None and pdt < filed:
            info_col.append(pdt)
            src_col.append("press_release")
        else:
            info_col.append(filed)
            src_col.append("xbrl")
    df["press_dt"] = press_col
    df["info_dt"] = info_col
    df["date_source"] = src_col

    # ---- audit_v3 timing correction (pre-committed in study.yaml) ---------- #
    # W2 found two wrong-day mechanisms (all measured on timing fields only):
    #   1. press-release recovery false-matches pre-announcement notices when
    #      the XBRL is timely (WALMEX -5d every quarter, CEMEX -4..-7d);
    #   2. the human calendar knows the true release when XBRL lags it.
    # Rule: (a) out of calendar coverage, keep a press-based date ONLY when the
    # XBRL is stale (>60d after period end — the Q4-recovery regime the
    # mechanism was built for); (b) inside coverage, info_dt := min(filed_dt,
    # calendar-implied timestamp) — the press channel is dropped there.
    av3 = cfg.get("audit_v3") or {}
    df["timing_overridden"] = False
    if bs.V3 and av3.get("timing_correction"):
        tc = av3["timing_rule"]
        stale_days = int(tc["press_requires_xbrl_stale_days"])
        pend_by_period = {
            p: datetime.fromisoformat(p[:4] + qe[p[5]]).replace(tzinfo=CDMX)
            for p in df["period"].unique() if re.match(r"^(20\d{2})-([1-4])T$", p)
        }
        # (a) revert non-stale press recoveries to the XBRL timestamp
        reverted = 0
        for i, r in df.iterrows():
            if r["date_source"] != "press_release":
                continue
            pend = pend_by_period.get(r["period"])
            if pend is not None and (r["filed_dt"] - pend).days <= stale_days:
                df.at[i, "info_dt"] = r["filed_dt"]
                df.at[i, "date_source"] = "xbrl"
                df.at[i, "timing_overridden"] = True
                reverted += 1
        # (b) calendar override inside coverage
        cal = pd.read_parquet(bs.OUTPUTS_DIR / "report_calendar.parquet")
        cal = cal[cal["slug"].notna()]
        cal_key = {(r["slug"], r["period"]): (r["cal_date"], r["session"])
                   for _, r in cal.iterrows()}
        from datetime import time as _time

        overridden = 0
        for i, r in df.iterrows():
            hit = cal_key.get((r["slug"], r["period"])) if r["slug"] else None
            if hit is None:
                continue
            cal_date, session = hit
            excel_dt = datetime.combine(
                cal_date, _time(6, 0) if session == "pre_open" else _time(16, 0),
                tzinfo=CDMX)
            info = min(r["filed_dt"], excel_dt)
            if info != r["info_dt"] or r["date_source"] == "press_release":
                df.at[i, "timing_overridden"] = True
                overridden += 1
            df.at[i, "info_dt"] = info
            df.at[i, "date_source"] = (
                "report_calendar" if excel_dt < r["filed_dt"] else "xbrl")
        print(f"timing correction: {reverted} non-stale press recoveries reverted "
              f"to xbrl; {overridden} rows calendar-corrected")

    bs.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    df.attrs["repo_head"] = bs.repo_head()
    df.to_parquet(bs.art_path("events"), index=False)

    # universe.csv — survivorship disclosure: every configured slug + exclusion info
    uni_rows = []
    for slug, v in universe.items():
        sub = df[df["slug"] == slug]
        uni_rows.append({
            "slug": slug, "ticker": slug2ticker[slug], "symbol": slug2sym[slug],
            "n_filings": len(sub),
            "n_with_facts": int(sub["has_facts"].sum()),
            "first_period": sub["period"].min() if len(sub) else None,
            "last_period": sub["period"].max() if len(sub) else None,
        })
    pd.DataFrame(uni_rows).to_csv(bs.art_path("universe", ".csv"), index=False)

    # ---- verify block ----
    uni = df[df["in_universe"]]
    print(f"quarterly filings parsed : {len(rows)} (unparseable dates: {bad_dates})")
    print(f"unique (ticker, period)  : {len(df)}  refiled pairs flagged: {int(df['is_refiled'].sum())}")
    print(f"tickers total            : {df['ticker'].nunique()}")
    print(f"universe events          : {len(uni)} across {uni['ticker'].nunique()} tickers")
    print(f"universe w/ facts+prices : {int((uni['has_facts'] & uni['has_prices']).sum())}")
    for tk, per in [("WALMEX", "2021-2T"), ("WALMEX", "2026-1T"), ("AC", "2026-1T")]:
        row = df[(df["ticker"] == tk) & (df["period"] == per)]
        stamp = row.iloc[0]["filed_dt"] if len(row) else "MISSING"
        print(f"  spot-check {tk} {per}: {stamp}")

    rec = df[df["date_source"] == "press_release"]
    rec_q4 = rec[rec["period"].str.endswith("4T")]
    print(f"press-release recovered dates : {len(rec)} (Q4: {len(rec_q4)}) "
          f"across {rec['slug'].nunique()} slugs; implausible matches dropped: {bad_press}")
    for sl, per in [("alsea", "2024-4T"), ("amx", "2024-4T")]:
        row = df[(df["slug"] == sl) & (df["period"] == per)]
        if len(row):
            print(f"  spot-check {sl} {per}: press={row.iloc[0]['press_dt']} "
                  f"xbrl={row.iloc[0]['filed_dt']} src={row.iloc[0]['date_source']}")
    viol = int((rec["press_dt"] >= rec["filed_dt"]).sum())
    assert viol == 0, f"{viol} recovered rows with press_dt >= filed_dt"
    assert bad_dates == 0, "unparseable filed_date strings found"


if __name__ == "__main__":
    main()
