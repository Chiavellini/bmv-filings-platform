"""Financials panel helper — per-company metric table via the vendored extraction cascade.

Kept in its own module (imported lazily by ``panels.render_financials``) because it pulls in the
heavy vendored extraction stack. Works from the parsed markdown already on disk
(``data/corpus/<slug>/<period>.md``); XBRL facts / PDF tables are optional and simply skipped
when absent, so the cascade degrades gracefully.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from src.shared.paths import PROJECT_ROOT


def _periods_for(slug: str) -> list[str]:
    """Period labels available for a company (newest first), from its corpus markdown."""
    corpus = PROJECT_ROOT / "data" / "corpus" / slug
    return sorted((p.stem for p in corpus.glob("*.md")), reverse=True)


def _extract(slug: str, period: str) -> list[dict]:
    """Run the cascade for one (company, period) and return display rows.

    Cached by the caller. Raises on missing config/markdown so the panel can show the error.
    """
    from src.extract.tiered_extract import (
        PeriodSource,
        extract_metrics_tiered,
        period_end_from_label,
    )
    from src.model.financial_model import METRICS, apply_config

    cfg_path = PROJECT_ROOT / "configs" / f"{slug}.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    metric_defs = apply_config(METRICS, cfg)

    md_path = PROJECT_ROOT / "data" / "corpus" / slug / f"{period}.md"
    text = md_path.read_text(encoding="utf-8")

    src = PeriodSource(period=period, text=text, period_end=period_end_from_label(period))
    extracted = extract_metrics_tiered(src, metric_defs, cfg)

    rows: list[dict] = []
    for key, row in extracted.items():
        current = getattr(row, "current", None)
        if current is None:
            continue
        rows.append({
            "metric": key,
            "label": getattr(row, "label_es", "") or key,
            "value": current,
            "prior": getattr(row, "prior", None),
            "unit": getattr(row, "unit", ""),
            "source": getattr(row, "source_line", ""),
        })
    rows.sort(key=lambda r: r["metric"])
    return rows


def accuracy_for(slug: str) -> "dict | None":
    """Per-metric extraction accuracy vs ground truth via ``compare_extractions``, or ``None``.

    Reuses the vendored ``src.eval.compare_extractions.run_comparison`` — the parent's accuracy
    harness. alpha-go does NOT vendor the parent's ground-truth CSVs, so this stays DORMANT until a
    ``data/ground_truth/<slug>-actual.csv`` is dropped in; then it lights up the panel with a live
    pass-rate. When present, the comparison runs over the same ``data/corpus/<slug>`` markdown the
    panel serves. Returns ``{pass_rate, n, by_metric: {key: PASS|FAIL|MISS}}`` (latest period per
    metric) or ``None`` when there is no ground truth / the harness can't score this corpus.
    """
    gt = PROJECT_ROOT / "data" / "ground_truth" / f"{slug}-actual.csv"
    corpus_dir = PROJECT_ROOT / "data" / "corpus" / slug
    if not gt.exists() or not corpus_dir.is_dir():
        return None
    try:
        from src.eval.compare_extractions import COMPANIES, run_comparison
    except Exception:  # noqa: BLE001
        return None
    comp = COMPANIES.get(slug)
    if comp is None:
        return None
    # COMPANIES defaults are parent-repo paths; point the run at THIS project's corpus + GT, then
    # restore so the shared registry isn't mutated for other callers.
    saved = {k: comp.get(k) for k in ("actual_file", "source_dir")}
    comp["actual_file"], comp["source_dir"] = str(gt), str(corpus_dir)
    try:
        results = run_comparison(slug)
    except Exception:  # noqa: BLE001 — period-format / harness mismatch → dormant, never crash
        return None
    finally:
        comp.update(saved)

    latest: dict = {}
    for r in results:
        if r["status"] == "EXCLUDED":
            continue
        cur = latest.get(r["key"])
        if cur is None or r["period"] > cur["period"]:
            latest[r["key"]] = r
    scored = [r for r in latest.values() if r["status"] in ("PASS", "FAIL", "MISS")]
    if not scored:
        return None
    passed = sum(1 for r in scored if r["status"] == "PASS")
    return {"pass_rate": passed / len(scored), "n": len(scored),
            "by_metric": {k: v["status"] for k, v in latest.items()}}


def render_financials_table(st, config, slug: str) -> None:
    """Render the metric table for ``slug``; a period selector defaults to the latest period.

    When a ground-truth CSV is wired for ``slug`` (see :func:`accuracy_for`), each row carries a
    PASS/FAIL/MISS confidence indicator and the header shows the overall pass-rate; otherwise the
    panel is honest that no accuracy is available.
    """
    periods = _periods_for(slug)
    if not periods:
        st.caption(f"No parsed reports for {slug}.")
        return
    period = st.selectbox("Period", periods, index=0)

    cached = st.cache_data(_extract, show_spinner="Extracting metrics…")
    try:
        rows = cached(slug, period)
    except Exception as exc:  # noqa: BLE001 — never crash the dashboard on an extraction error
        st.error(f"Could not extract metrics for {slug} {period} "
                 f"({type(exc).__name__}: {exc}).")
        return

    if not rows:
        st.warning(f"No metrics extracted for {slug} {period} from the prose. "
                   "(XBRL facts / PDF tables are not wired into the corpus yet.)")
        return

    acc = st.cache_data(accuracy_for, show_spinner="Scoring vs ground truth…")(slug)
    if acc is not None:
        st.caption(f"{len(rows)} metric(s) · {slug} {period} · **extraction accuracy vs ground "
                   f"truth: {acc['pass_rate']:.0%}** ({acc['n']} metrics scored, latest period)")
        by_metric = acc["by_metric"]
        for r in rows:
            r["✓"] = by_metric.get(r["metric"], "—")     # PASS / FAIL / MISS confidence indicator
    else:
        st.caption(f"{len(rows)} metric(s) · {slug} {period} · accuracy not available "
                   f"(drop data/ground_truth/{slug}-actual.csv to score vs ground truth)")
    st.dataframe(rows, use_container_width=True, hide_index=True)
