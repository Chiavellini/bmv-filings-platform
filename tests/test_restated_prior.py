"""apply_restated_priors — restated-comparative cross-period post-pass."""

from src.extract.extract_metrics import MetricRow
from src.extract.tiered_extract import _shift_period_label, apply_restated_priors
from src.model.financial_model import METRICS


def _row(key, cur, prior=None, source="[regex_table] row"):
    return MetricRow(metric=key, label_es=key, current=cur, prior=prior,
                     var_pct=None, unit="currency", source_line=source)


def test_shift_period_label_formats():
    assert _shift_period_label("2Q21A") == "2Q22A"
    assert _shift_period_label("4Q99A") == "4Q00A"
    assert _shift_period_label("2021-2T") == "2022-2T"
    assert _shift_period_label("2Q21") == "2Q22"
    assert _shift_period_label("garbage") is None


def test_replaces_configured_cell_with_next_year_prior():
    data = {
        "2Q21A": {"revenue": _row("revenue", 83789.0)},
        "2Q22A": {"revenue": _row("revenue", 96434.0, prior=81654.0)},
    }
    cfg = {"restated_prior": {"revenue": ["2Q21A"]}}
    apply_restated_priors(data, METRICS, cfg)
    assert data["2Q21A"]["revenue"].current == 81654.0
    assert data["2Q21A"]["revenue"].source_line.startswith("[restated]")
    # the source year itself is untouched
    assert data["2Q22A"]["revenue"].current == 96434.0


def test_all_scope_and_missing_prior_kept():
    data = {
        "1Q20A": {"revenue_latam": _row("revenue_latam", 6931.0)},
        "1Q21A": {"revenue_latam": _row("revenue_latam", 7224.0, prior=6776.0)},
        # 1Q22 extraction carries no prior → 1Q21 keeps its as-reported value
        "1Q22A": {"revenue_latam": _row("revenue_latam", 7900.0, prior=None)},
    }
    cfg = {"restated_prior": {"revenue_latam": "all"}}
    apply_restated_priors(data, METRICS, cfg)
    assert data["1Q20A"]["revenue_latam"].current == 6776.0
    assert data["1Q21A"]["revenue_latam"].current == 7224.0


def test_calc_ratios_recomputed_for_touched_periods():
    data = {
        "2Q19A": {
            "revenue": _row("revenue", 72294.0),
            "ebitda": _row("ebitda", 7707.0),
            "ebitda_margin": MetricRow(
                metric="ebitda_margin", label_es="m", current=10.66, prior=None,
                var_pct=None, unit="pct", source_line="[calc] ebitda / revenue"),
        },
        "2Q20A": {"ebitda": _row("ebitda", 11375.0, prior=8805.0)},
    }
    cfg = {"restated_prior": {"ebitda": ["2Q19A"]}}
    apply_restated_priors(data, METRICS, cfg)
    assert data["2Q19A"]["ebitda"].current == 8805.0
    margin = data["2Q19A"]["ebitda_margin"]
    assert abs(margin.current - 8805.0 / 72294.0 * 100) < 0.1


def test_noop_without_config():
    data = {"2Q21A": {"revenue": _row("revenue", 83789.0)},
            "2Q22A": {"revenue": _row("revenue", 96434.0, prior=81654.0)}}
    apply_restated_priors(data, METRICS, {})
    assert data["2Q21A"]["revenue"].current == 83789.0
