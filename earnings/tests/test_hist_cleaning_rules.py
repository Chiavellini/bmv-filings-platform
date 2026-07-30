"""Issue C — pre-committed mechanical cleaning rules for metrics_hist
(audit_v2.hist_cleaning in study.yaml). Fixtures replicate every known-bad
exemplar plus the known-legit stress case that must survive."""
import numpy as np
import pandas as pd
import pytest


def _frame(rows):
    return pd.DataFrame(rows, columns=["slug", "period", "metric", "current"])


def _clean(df):
    from earnlib.bootstrap import load_config
    from earnlib.quality import clean_metrics_hist

    return clean_metrics_hist(df, load_config()["audit_v2"]["hist_cleaning"])


def test_r1_margin_as_level_gruma():
    quarters = [f"20{y:02d}-{q}T" for y in range(15, 20) for q in range(1, 5)]
    rows = [("gruma", p, "revenue", 20000.0 + 100 * i)
            for i, p in enumerate(quarters)]
    # margins-as-levels contamination with 6 real levels interleaved (enough
    # clean obs that the short-series rule R4 does not also fire)
    ebitda = [("gruma", p, "ebitda",
               3000.0 + 50 * i if i % 3 == 0 else 15.9)
              for i, p in enumerate(quarters)]
    clean, log = _clean(_frame(rows + ebitda))
    kept = clean[(clean.metric == "ebitda")]["current"]
    assert (log[log.metric == "ebitda"]["current"] < 100).all()
    assert (kept >= 100).all()                           # every 15.9 dropped
    assert set(log[log.metric == "ebitda"]["rule"]) == {"r1_margin_level"}


def test_r1b_eps_as_net_income_kimber():
    quarters = [f"2018-{q}T" for q in range(1, 5)] + [f"2019-{q}T" for q in range(1, 5)]
    ni = [0.38, 1095.0, 0.40, 1200.0, 1150.0, 1080.0, 1220.0, 1175.0]
    rows = ([("kimber", p, "revenue", 11000.0) for p in quarters] +
            [("kimber", p, "net_income", v) for p, v in zip(quarters, ni)])
    clean, log = _clean(_frame(rows))
    dropped = log[log.metric == "net_income"]["current"].tolist()
    assert sorted(dropped) == [0.38, 0.40]
    assert (log[log.metric == "net_income"]["rule"] == "r1b_eps_as_net_income").all()


def test_r2_identity_drops_corrupt_member_only():
    rows = []
    for i, q in enumerate(["2014-1T", "2014-2T", "2014-3T", "2014-4T", "2015-1T"]):
        rows.append(("ac", q, "revenue", 15000.0 + 100 * i))
        rows.append(("ac", q, "operating_income", 2500.0 + 50 * i))
    rows = [r if not (r[1] == "2014-4T" and r[2] == "operating_income")
            else ("ac", "2014-4T", "operating_income", 20693.0) for r in rows]
    rows = [r if not (r[1] == "2014-4T" and r[2] == "revenue")
            else ("ac", "2014-4T", "revenue", 10222.0) for r in rows]
    clean, log = _clean(_frame(rows))
    assert 20693.0 in log["current"].values               # oi dropped
    assert 10222.0 in clean["current"].values             # revenue kept


def test_r3_jump_unit_slip():
    vals = [374.0, 338.0, 321.0, 255.0, 201786.0, 228.0, 473.0]
    rows = [("gruma", f"200{4 + i // 4}-{i % 4 + 1}T", "net_income", v)
            for i, v in enumerate(vals)]
    clean, log = _clean(_frame(rows))
    assert 201786.0 in log["current"].values
    assert (log["rule"] == "r3_jump").any()


def test_r0_exact_zero_dropped():
    vals = [0.0, 505.0, 520.0, 540.0, 490.0, 515.0, 530.0]
    rows = [("chedraui", f"201{8 + q // 4}-{q % 4 + 1}T", "net_income", v)
            for q, v in enumerate(vals)]
    clean, log = _clean(_frame(rows))
    assert 0.0 in log["current"].values
    assert (log[log.current == 0.0]["rule"] == "r0_exact_zero").all()
    assert len(clean) == 6


def test_legit_covid_quarter_survives():
    """sports_world 2020-2T: collapsed but real. Must not be dropped."""
    quarters = ["2019-3T", "2019-4T", "2020-1T", "2020-2T", "2020-3T", "2020-4T"]
    rows = []
    for q in quarters:
        crisis = q == "2020-2T"
        rows += [("sports_world", q, "revenue", 87.711 if crisis else 460.0),
                 ("sports_world", q, "net_income", -306.142 if crisis else 25.0),
                 ("sports_world", q, "ebitda", -106.115 if crisis else 80.0)]
    clean, log = _clean(_frame(rows))
    crisis_rows = clean[clean.period == "2020-2T"]
    assert len(crisis_rows) == 3, f"COVID quarter wrongly dropped: {log}"
