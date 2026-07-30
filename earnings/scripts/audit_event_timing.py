#!/usr/bin/env python3
"""audit_event_timing.py — W2: is the strategy trading the RIGHT day?

Cross-validates the backtest's reaction-day assignment (t0 from
``info_dt = min(XBRL filed_dt, press_dt)``) against the human-curated report
calendar (outputs/report_calendar.parquet: date + Cierre/Apertura for
3Q24-2Q26). The calendar is treated as ground truth for WHEN the report
actually hit the market.

READ-ONLY: joins timing fields only. It never touches returns, prices or
event_windows, so holdout quarters inside the calendar window (2025-4T) leak
nothing — stated in TIMING_AUDIT.md.

Outputs: outputs/results*/timing_audit.csv + outputs/TIMING_AUDIT.md
"""
from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib.calendar import CDMX, SESSION_OPEN, TradingCalendar

import pandas as pd


def model_bucket(al, filed_local) -> str:
    """pre_open / in_session / after_close from an Alignment + local time."""
    if al.filing_day is not None and filed_local.time() < SESSION_OPEN:
        return "pre_open"
    if al.filed_in_session:
        return "in_session"
    return "after_close"


def main() -> None:
    cal_df = pd.read_parquet(bs.OUTPUTS_DIR / "report_calendar.parquet")
    events = pd.read_parquet(bs.art_path("events_all"))
    index_df = pd.read_parquet(bs.OUTPUTS_DIR / "index_mxx.parquet")
    dates = sorted(pd.to_datetime(index_df["date"]).dt.date.unique())
    cal = TradingCalendar(list(dates))

    cfg = bs.load_config()
    max_lag = cfg["event"]["max_filing_lag_days"]
    events = events.copy()
    qe = {"1": "-03-31", "2": "-06-30", "3": "-09-30", "4": "-12-31"}
    period_end = pd.to_datetime(
        events["period"].str[:4] + events["period"].str[5].map(qe))
    lag_days = (events["info_dt"].dt.tz_localize(None) - period_end).dt.days
    # study eligibility mirrors study.assemble_events/dev_sample: universe +
    # facts + prices + not stale (the >60d filing-lag exclusion)
    events["study_eligible"] = (
        events["in_universe"] & events["has_facts"] & events["has_prices"]
        & (lag_days <= max_lag))

    ev = events[events["info_dt"].notna()][
        ["ticker", "slug", "period", "filed_dt", "press_dt", "info_dt",
         "date_source", "in_universe", "study_eligible"]]
    j = cal_df[cal_df["slug"].notna()].merge(
        ev, on=["slug", "period"], how="inner", suffixes=("_cal", ""))

    rows = []
    for _, r in j.iterrows():
        info_dt = r["info_dt"].to_pydatetime()
        al_model = cal.align(info_dt)
        if al_model is None:
            continue
        info_local = info_dt.astimezone(CDMX)
        mbucket = model_bucket(al_model, info_local)

        # Excel-implied information timestamp: before open / after close
        excel_dt = datetime.combine(
            r["cal_date"], time(6, 0) if r["session"] == "pre_open" else time(16, 0),
            tzinfo=CDMX)
        al_excel = cal.align(excel_dt)
        if al_excel is None:
            continue
        i_model = cal.index_of(al_model.t0)
        i_excel = cal.index_of(al_excel.t0)
        lag = (i_model - i_excel) if (i_model is not None and i_excel is not None) else None
        rows.append({
            "ticker": r["ticker_cal"], "slug": r["slug"], "period": r["period"],
            "session_excel": r["session"], "cal_date": r["cal_date"],
            "info_dt": info_dt.isoformat(), "date_source": r["date_source"],
            "model_bucket": mbucket,
            "model_t0": al_model.t0.isoformat(), "excel_t0": al_excel.t0.isoformat(),
            "t0_match": al_model.t0 == al_excel.t0, "t0_lag_days": lag,
            "date_match": info_local.date() == r["cal_date"],
            "xbrl_lag": info_local.date() > r["cal_date"],
            "model_early": info_local.date() < r["cal_date"],
            "year_corrected": bool(r["year_corrected"]),
            "in_universe": bool(r["in_universe"]),
            "study_eligible": bool(r["study_eligible"]),
        })
    df = pd.DataFrame(rows)
    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(bs.RESULTS_DIR / "timing_audit.csv", index=False)

    # ---- report ----
    def pct(x):
        return f"{100 * x:.1f}%"

    lines = [
        "# W2 timing audit — model t0 vs human report calendar",
        "",
        f"_Generated {datetime.now().date().isoformat()} from "
        f"`report_calendar.parquet` (3Q24-2Q26) x `{bs.art_path('events_all').name}`._",
        "",
        "**Holdout hygiene**: this audit joins TIMING fields only (no returns,",
        "no prices, no event_windows). Holdout quarters inside the calendar",
        "window (2025-4T) contribute timing rows but zero return information.",
        "",
        f"- calendar rows with slug: {int(cal_df['slug'].notna().sum())}",
        f"- matched to an events_all row with info_dt: {len(df)}",
        f"- **t0 match rate (all matched): {pct(df['t0_match'].mean())}** "
        f"({int(df['t0_match'].sum())}/{len(df)})",
        f"- events trading the WRONG day: {int((~df['t0_match']).sum())} "
        f"({pct((~df['t0_match']).mean())})",
        "",
        "The number that matters for the backtest is the STUDY-ELIGIBLE subset",
        "(universe + facts + prices + the <=60d staleness gate, which already",
        "drops the extreme XBRL-lag offenders):",
        "",
        f"- study-eligible matched events: "
        f"{int(df['study_eligible'].sum())}",
        f"- **t0 match rate (study-eligible): "
        f"{pct(df.loc[df['study_eligible'], 't0_match'].mean())}** "
        f"({int(df.loc[df['study_eligible'], 't0_match'].sum())}"
        f"/{int(df['study_eligible'].sum())})",
        f"- study-eligible wrong-day events: "
        f"{int((~df.loc[df['study_eligible'], 't0_match']).sum())} "
        f"({pct((~df.loc[df['study_eligible'], 't0_match']).mean())})",
        "",
        "## Where the wrong days come from",
        "",
        "### model bucket x excel session (counts, t0 mismatches in parens)",
        "",
    ]
    conf = df.groupby(["model_bucket", "session_excel"]).agg(
        n=("t0_match", "size"), wrong=("t0_match", lambda s: int((~s).sum())))
    lines.append("| model \\ excel | " + " | ".join(
        sorted(df["session_excel"].unique())) + " |")
    lines.append("|---|" + "---|" * df["session_excel"].nunique())
    for mb in ["pre_open", "in_session", "after_close"]:
        if mb not in conf.index.get_level_values(0):
            continue
        cells = []
        for se in sorted(df["session_excel"].unique()):
            try:
                c = conf.loc[(mb, se)]
                cells.append(f"{int(c['n'])} ({int(c['wrong'])})")
            except KeyError:
                cells.append("0")
        lines.append(f"| {mb} | " + " | ".join(cells) + " |")

    lines += ["", "### t0 lag distribution (model minus excel, trading days)", ""]
    lag_counts = df["t0_lag_days"].value_counts().sort_index()
    lines.append("| lag | n |")
    lines.append("|---|---|")
    for lag_v, n in lag_counts.items():
        lines.append(f"| {int(lag_v):+d} | {int(n)} |")

    lines += ["", "### accuracy by date_source", ""]
    by_src = df.groupby("date_source").agg(
        n=("t0_match", "size"), match=("t0_match", "mean"),
        xbrl_lag=("xbrl_lag", "mean"))
    lines.append("| date_source | n | t0 match | info date after calendar date |")
    lines.append("|---|---|---|---|")
    for src, r in by_src.iterrows():
        lines.append(f"| {src} | {int(r['n'])} | {pct(r['match'])} | {pct(r['xbrl_lag'])} |")

    lines += ["", "### date-level agreement", "",
              f"- info_dt date == calendar date: {pct(df['date_match'].mean())}",
              f"- info_dt date AFTER calendar date (XBRL/press lags true release): "
              f"{pct(df['xbrl_lag'].mean())}",
              f"- info_dt date BEFORE calendar date: {pct(df['model_early'].mean())}",
              ""]

    worst = (df[~df["t0_match"]].groupby("slug")["period"]
             .count().sort_values(ascending=False))
    lines += ["### worst offenders (wrong-day events per company)", ""]
    lines.append("| slug | wrong days |")
    lines.append("|---|---|")
    for slug, n in worst.head(15).items():
        lines.append(f"| {slug} | {int(n)} |")

    # the specific mechanism: same-date in-session filings that the calendar
    # says were after the close (t0 should be next day -> matches), versus
    # in-session filings on a LATER date (model trades late)
    ins = df[df["model_bucket"] == "in_session"]
    lines += ["", "### in-session filings decomposition", "",
              f"- in-session model events in coverage: {len(ins)}",
              f"- of those, same-date as calendar: {int(ins['date_match'].sum())} "
              f"(t0 match {pct(ins.loc[ins['date_match'], 't0_match'].mean()) if ins['date_match'].any() else 'n/a'})",
              f"- on a later date (XBRL lag): {int(ins['xbrl_lag'].sum())} "
              f"(t0 match {pct(ins.loc[ins['xbrl_lag'], 't0_match'].mean()) if ins['xbrl_lag'].any() else 'n/a'})",
              ""]

    (bs.OUTPUTS_DIR / "TIMING_AUDIT.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
