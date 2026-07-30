#!/usr/bin/env python3
"""decide.py — the prospective HYBRID-1 instrument.

    EARNINGS_V3=1 python scripts/decide.py --period 2026-3T

One row per covered company: what each channel said, which tier fired, the
action (buy / short / neutral) and the size. Designed to be run each evening of
earnings season, between a company's release and the session that reacts to it.

Provenance (study.yaml `hybrid.adoption.provenance_requirement`, as corrected):
the analyst's call is a forecast and must already be hash-committed before the
release; the AI read and the system's score both derive from the published
report, so their deadline is the session they predict. This script refuses to
emit any row whose t0 session has already opened, and prints the sha256 to
commit — a row that first appears in git at or after its own t0 is excluded
from the arbiter.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import airead as ar
from earnlib import bootstrap as bs
from earnlib import hybrid as hy
from earnlib import study

import pandas as pd

sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
from run_refinements import add_margin_sue          # noqa: E402

LABELS = {"Positive": (1, 1.0), "Negative": (-1, 1.0), "Neutral": (0, 0.0),
          "N to P": (1, 0.5), "N to N": (-1, 0.5)}


def load_analyst_log(period: str) -> pd.DataFrame:
    """The desk's prospectively logged calls for this quarter.

    Reads data/analyst/log_<quarter>.csv — the file the Spanish protocol has
    the desk append to and commit before each release. Its git history, not its
    contents, is what makes a call admissible; this only parses it.
    """
    quarter = f"{period[5]}Q{period[2:4]}"          # 2026-3T -> 3Q26
    path = bs.EARNINGS_ROOT / "data" / "analyst" / f"log_{period[5]}T{period[2:4]}.csv"
    if not path.exists():
        path = bs.EARNINGS_ROOT / "data" / "analyst" / f"log_{quarter}.csv"
    if not path.exists():
        return pd.DataFrame(columns=["ticker", "direction", "strength",
                                     "conviccion", "razon_principal"])
    log = pd.read_csv(path)
    if log.empty:
        return pd.DataFrame(columns=["ticker", "direction", "strength",
                                     "conviccion", "razon_principal"])
    unknown = set(log["llamada"]) - set(LABELS)
    if unknown:
        raise SystemExit(f"unknown analyst labels in {path.name}: {unknown} "
                         f"(configs/analyst_labels.yaml is strict by design)")
    log["direction"] = log["llamada"].map(lambda v: LABELS[v][0])
    log["strength"] = log["llamada"].map(lambda v: LABELS[v][1])
    return log


def ai_reads_for(frame: pd.DataFrame, cfg: dict, allow_call: bool) -> pd.DataFrame:
    """Read each report with the frozen prompt; cached reads cost nothing."""
    rcfg = ar.load_reader_cfg()
    block = cfg["hybrid"]["reader"]
    kpi = pd.read_parquet(bs.OUTPUTS_DIR / "kpi_panel.parquet")
    avail = ar.availability(study.availability_map())
    arm = cfg["hybrid"]["contamination"].get("primary_arm", "B")

    dirs, convs, notes = [], [], []
    for _, r in frame.iterrows():
        text, source = ar.load_document(r["slug"], r["ticker"], r["period"])
        if not text:
            dirs.append(0); convs.append(None); notes.append("no document")
            continue
        history = ar.kpi_history(kpi, r["slug"], r["period"], avail)
        prompt = ar.build_prompt(rcfg, arm=arm, slug=r["slug"],
                                 ticker=r["ticker"], period=r["period"],
                                 sector=r.get("sector", ""), text=text,
                                 source=source, history=history)
        try:
            record = ar.call_reader(prompt, rcfg, model=block["model"],
                                    temperature=float(block["temperature"]),
                                    max_tokens=int(block["max_output_tokens"]),
                                    allow_call=allow_call)
        except LookupError:
            dirs.append(0); convs.append(None); notes.append("read not cached")
            continue
        except Exception as exc:                     # noqa: BLE001 - reported
            dirs.append(0); convs.append(None)
            notes.append(f"read failed: {type(exc).__name__}")
            continue
        fields, problems = ar.validate(record["response"], rcfg)
        dirs.append(fields["ai_dir"])
        convs.append(fields["conviction"])
        notes.append("; ".join(problems) if problems else "")
    out = frame.copy()
    out["ai_dir"] = dirs
    out["ai_conviction"] = convs
    out["ai_note"] = notes
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", required=True, help="e.g. 2026-3T")
    ap.add_argument("--asof", default=None,
                    help="ISO timestamp to evaluate the t0 guard against "
                         "(default: now). Use only to reproduce a past run.")
    ap.add_argument("--offline", action="store_true",
                    help="cache-only AI reads; rows without one abstain")
    ap.add_argument("--allow-past-t0", action="store_true",
                    help="emit rows whose session already opened — for "
                         "reproducing history ONLY; such rows are excluded "
                         "from the arbiter and are marked as such")
    args = ap.parse_args()

    assert bs.V3, "set EARNINGS_V3=1"
    cfg = bs.load_config()
    now = (pd.Timestamp(args.asof) if args.asof
           else pd.Timestamp.now(tz=cfg["event"]["timezone"]))
    if now.tzinfo is None:
        now = now.tz_localize(cfg["event"]["timezone"])

    panel = study.load_panel(cfg)
    events = study.assemble_events(cfg, panel)
    events = add_margin_sue(events, cfg)
    frame = events[events["period"] == args.period].copy()
    if frame.empty:
        raise SystemExit(f"no scored events for {args.period} — the reports "
                         f"may not be filed yet, or metrics need rebuilding")

    frame = study.apply_liquidity(
        frame, cfg["liquidity"]["primary_min_median_peso_volume"])

    # assemble_events already resolved t0 (the first session opening after the
    # report). The deadline is that session's OPEN, since the trade is entered
    # there — a decision made after it is not a prediction.
    open_h, open_m = (int(p) for p in cfg["event"]["session_open"].split(":"))
    frame["t0_session_open"] = [
        pd.Timestamp(t0).tz_localize(cfg["event"]["timezone"])
        + pd.Timedelta(hours=open_h, minutes=open_m)
        if pd.notna(t0) else pd.NaT
        for t0 in frame["t0"]
    ]

    stale = frame["t0_session_open"].notna() & (frame["t0_session_open"] <= now)
    if stale.any() and not args.allow_past_t0:
        names = ", ".join(sorted(frame.loc[stale, "ticker"].astype(str)))
        print(f"refusing {int(stale.sum())} row(s) whose session already "
              f"opened: {names}\n  (a decision committed at or after its own "
              f"t0 is excluded from the arbiter — rerun with --allow-past-t0 "
              f"only to reproduce history)", file=sys.stderr)
        frame = frame[~stale]
    frame["excluded_from_arbiter"] = stale.reindex(frame.index).fillna(False)
    if frame.empty:
        raise SystemExit("nothing left to decide")

    log = load_analyst_log(args.period)
    frame = frame.merge(log[["ticker", "direction", "strength"]],
                        on="ticker", how="left")
    frame["direction"] = frame["direction"].fillna(0).astype(int)
    frame["strength"] = frame["strength"].fillna(0.0)

    frame = ai_reads_for(frame, cfg, allow_call=not args.offline)
    decided = hy.apply_cascade(frame, cfg, ai_enabled=True)
    decided["size"] = float(cfg["hybrid"]["sizing"]["f_cap"])

    cols = ["ticker", "slug", "period", "event_dt", "t0_session_open",
            "action", "tier", "hybrid_dir", "size", "vetoed", "conflict",
            "direction", "strength", "s_ts", "margin_sue_z", "sigma_pre",
            "sigma_median_pit", "ai_dir", "ai_conviction", "ai_note",
            "excluded_from_arbiter"]
    table = decided[[c for c in cols if c in decided.columns]].sort_values(
        ["action", "ticker"])

    out_path = bs.OUTPUTS_DIR / f"decisions_{args.period}.csv"
    table.to_csv(out_path, index=False)
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()

    print(table.to_string(index=False))
    print(f"\nwrote {out_path}")
    print(f"sha256  {digest}")
    print("\nCommit this NOW, before the session it predicts opens:\n"
          f"  git add {out_path.relative_to(bs.REPO_ROOT)}\n"
          f"  git commit -m 'decisions {args.period} {digest[:12]}'")


if __name__ == "__main__":
    main()
