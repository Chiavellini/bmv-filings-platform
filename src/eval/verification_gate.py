"""
verification_gate.py — post-extraction strength scoring + suspect detection.

After extraction we want one deterministic answer: *is this sheet strong enough,
and if not, exactly which cells must a human/subagent verify?* This module scores
each metric (coverage, confidence, suspect cells), classifies it, and emits a
prioritized worklist plus an ``is_strong`` verdict. The bar (user-chosen) is
"no unexplained suspects": every SUSPECT cell resolved AND every disclosed metric
at its coverage ceiling. Disclosure-bound metrics (N/D, pre-era, ungrabbable) are
declared EXPECTED_EMPTY via per-company ``metric_expectations`` and never chased.

Confidence comes from ``df.attrs['confidence']`` (populated by pipeline.run); a
``[verified]`` override is treated as resolved (never suspect), and an
``unresolved`` verify_status (verification attempted, source unreadable) is an
*explained* suspect — counted, red-flag comment ships, but STRONG isn't blocked.

Cell-level sign/range/magnitude detection lives in ``series_checks`` (shared
with the pipeline so the workbook marking and this gate always agree).
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from src.eval.series_checks import compute_cell_suspects, in_window as _in_window

_LOW_CONF = 0.5         # below this (or flagged) a present value is suspect
_WEAK_COVERAGE = 0.5    # disclosed metric below this fraction of its window = WEAK


@dataclass
class Suspect:
    key: str
    period: str
    value: float | None
    reason: str


@dataclass
class MetricScore:
    key: str
    status: str                 # STRONG | WEAK | SUSPECT | EMPTY | EXPECTED_EMPTY
    covered: int
    window: int                 # disclosable periods (denominator)
    mean_conf: float | None
    suspects: list[Suspect] = field(default_factory=list)
    note: str = ""
    unresolved: int = 0         # verification attempted, source unreadable — shipped flagged
    llm_agree: str = ""         # advisory cross-check tally "agreed/checked" ("" = not run)


def score_metrics(df, keys, *, expectations: dict | None = None) -> tuple[list[MetricScore], bool, list[dict]]:
    """Score each metric, return (scores, is_strong, worklist).

    ``expectations`` (optional, per-company): ``{key: {sign, window, range,
    expected_empty}}``. ``df`` must carry ``df.attrs['confidence']``.
    """
    expectations = expectations or {}
    conf = getattr(df, "attrs", {}).get("confidence", {}) or {}
    periods = [str(p) for p in df["period"]] if "period" in df.columns else []

    # quick lookup: (period, key) -> value
    val_at: dict[tuple[str, str], float] = {}
    for _, row in df.iterrows():
        p = str(row["period"])
        for k in keys:
            if k in df.columns:
                v = row[k]
                if v is not None and not (isinstance(v, float) and math.isnan(v)):
                    val_at[(p, k)] = float(v)

    # Sign / range / magnitude suspicion, shared with the pipeline (which
    # attaches the same result as df.attrs['suspects'] for the workbook).
    cell_susp = compute_cell_suspects(df, keys, expectations, conf=conf)

    scores: list[MetricScore] = []
    worklist: list[dict] = []
    for k in keys:
        exp = expectations.get(k, {})
        window_spec = exp.get("window")
        win_periods = [p for p in periods if _in_window(p, window_spec)]
        window = len(win_periods) or len(periods)
        present = [(p, val_at[(p, k)]) for p in win_periods if (p, k) in val_at]
        covered = len(present)

        confs = [conf.get((p, k), {}).get("confidence", 0.4) for p, _ in present]
        mean_conf = statistics.mean(confs) if confs else None

        # ── suspect detection ──
        suspects: list[Suspect] = []
        unresolved = 0
        for p, v in present:
            info = conf.get((p, k), {})
            src = info.get("source", "") or ""
            if "[verified]" in src:
                continue  # manually verified → resolved
            if info.get("verify_status") == "unresolved":
                # verification was attempted and could not resolve this cell —
                # an *explained* suspect: ships with its red-flag comment,
                # counted here, but doesn't block STRONG or re-enter the worklist.
                unresolved += 1
                continue
            reason = cell_susp.get((p, k))
            if reason and "magnitude" not in reason:
                # sign / range violations outrank validator flags
                suspects.append(Suspect(k, p, v, reason))
            elif info.get("flagged"):
                suspects.append(Suspect(k, p, v, "failed a cross-check"))
            elif info.get("confidence", 1.0) < _LOW_CONF:
                suspects.append(Suspect(k, p, v, f"low confidence {info.get('confidence', 0):.2f}"))
            elif reason:
                suspects.append(Suspect(k, p, v, reason))

        # ── classification ──
        # `sparse`: disclosed irregularly (e.g. quarterly capex only some quarters)
        # — low coverage is NOT a weakness, only suspects matter.
        sparse = bool(exp.get("sparse"))
        if exp.get("expected_empty"):
            status = "EXPECTED_EMPTY"
        elif suspects:
            status = "SUSPECT"
        elif covered == 0:
            status = "EXPECTED_EMPTY" if (window_spec or sparse) else "EMPTY"
        elif sparse:
            status = "STRONG"
        elif covered < _WEAK_COVERAGE * window:
            status = "WEAK"
        else:
            status = "STRONG"

        # ── advisory LLM cross-check tally (informational only — the status
        # classification above is already final and never reads this) ──
        agreed = checked = 0
        for p, _ in present:
            cc = conf.get((p, k), {}).get("crosscheck")
            if isinstance(cc, dict) and cc.get("agree") is not None:
                checked += 1
                if cc["agree"]:
                    agreed += 1
        llm_agree = f"{agreed}/{checked}" if checked else ""

        scores.append(MetricScore(k, status, covered, window, mean_conf, suspects,
                                  note=window_spec or ("sparse" if sparse else ""),
                                  unresolved=unresolved, llm_agree=llm_agree))

        # ── worklist: suspects first, then missing cells of WEAK/EMPTY disclosed ──
        for s in suspects:
            worklist.append({"key": k, "period": s.period, "value": s.value,
                             "reason": s.reason, "priority": 1})
        if status in ("WEAK", "EMPTY"):
            for p in win_periods:
                if (p, k) not in val_at:
                    worklist.append({"key": k, "period": p, "value": None,
                                     "reason": f"{status.lower()} coverage — value missing",
                                     "priority": 2})

    disclosed = [s for s in scores if s.status not in ("EXPECTED_EMPTY",)]
    is_strong = (not any(s.status == "SUSPECT" for s in disclosed)
                 and not any(s.status in ("WEAK", "EMPTY") for s in disclosed))
    worklist.sort(key=lambda w: (w["priority"], w["key"], w["period"]))
    return scores, is_strong, worklist


def scorecard_markdown(scores: list[MetricScore], is_strong: bool) -> str:
    order = {"SUSPECT": 0, "WEAK": 1, "EMPTY": 2, "STRONG": 3, "EXPECTED_EMPTY": 4}
    with_llm = any(s.llm_agree for s in scores)
    if with_llm:
        rows = ["| Metric | Status | Coverage | Mean conf | Suspect cells | Unresolved | LLM agree |",
                "|---|---|---|---|---|---|---|"]
    else:
        rows = ["| Metric | Status | Coverage | Mean conf | Suspect cells | Unresolved |",
                "|---|---|---|---|---|---|"]
    for s in sorted(scores, key=lambda x: (order.get(x.status, 9), x.key)):
        mc = f"{s.mean_conf:.2f}" if s.mean_conf is not None else "—"
        row = (f"| {s.key} | {s.status} | {s.covered}/{s.window} | {mc} | "
               f"{len(s.suspects)} | {s.unresolved or ''} |")
        if with_llm:
            row += f" {s.llm_agree or '—'} |"
        rows.append(row)
    verdict = "✅ STRONG — no unexplained suspects" if is_strong else "❌ NEEDS VERIFICATION"
    n_susp = sum(len(s.suspects) for s in scores)
    n_unres = sum(s.unresolved for s in scores)
    unres_note = (f"; {n_unres} unresolved cell(s) ship with a red-flag comment"
                  if n_unres else "")
    return (f"**Gate verdict: {verdict}**  ({n_susp} suspect cell(s){unres_note})\n\n"
            + "\n".join(rows))
