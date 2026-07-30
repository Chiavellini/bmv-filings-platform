"""Tests for the verification gate: suspect detection, classification, is_strong."""
from __future__ import annotations

import pandas as pd

from src.eval.verification_gate import score_metrics


def _df(rows, conf=None):
    df = pd.DataFrame(rows)
    df.attrs["confidence"] = conf or {}
    return df


def test_negative_on_positive_metric_is_suspect():
    df = _df([{"period": "2024-1T", "capex": 100.0},
             {"period": "2024-2T", "capex": -50.0}])
    scores, strong, wl = score_metrics(df, ["capex"], expectations={"capex": {"sparse": True}})
    s = scores[0]
    assert s.status == "SUSPECT"
    assert any(w["period"] == "2024-2T" and "negative" in w["reason"] for w in wl)
    assert strong is False


def test_sparse_low_coverage_is_not_weak():
    df = _df([{"period": f"2024-{q}T", "capex": 100.0 if q == 1 else None} for q in (1, 2, 3, 4)])
    scores, strong, wl = score_metrics(df, ["capex"], expectations={"capex": {"sparse": True}})
    # 1/4 coverage but sparse → STRONG (no suspects), no fill-worklist items.
    assert scores[0].status == "STRONG"
    assert strong is True
    assert wl == []


def test_window_excludes_pre_era_periods():
    rows = [{"period": "2020-4T", "sodimac_units": None},
            {"period": "2022-1T", "sodimac_units": 9.0},
            {"period": "2022-2T", "sodimac_units": 10.0}]
    scores, strong, _ = score_metrics(df := _df(rows), ["sodimac_units"],
                                      expectations={"sodimac_units": {"window": "2021-4T:"}})
    s = scores[0]
    # window starts 2021-4T → denominator is 2 (the 2022 periods), both covered.
    assert s.window == 2 and s.covered == 2 and s.status == "STRONG"
    assert strong is True


def test_verified_override_resolves_suspect():
    # A negative value that WOULD be suspect, but tagged [verified] → resolved.
    conf = {("2024-2T", "capex"): {"confidence": 1.0, "flagged": False,
                                   "source": "[verified] manual"}}
    df = _df([{"period": "2024-1T", "capex": 100.0},
             {"period": "2024-2T", "capex": 700.0}], conf=conf)
    scores, strong, wl = score_metrics(df, ["capex"], expectations={"capex": {"sparse": True}})
    assert scores[0].status == "STRONG" and wl == []


def test_magnitude_outlier_flagged():
    rows = [{"period": f"2023-{q}T", "total_units": v}
            for q, v in zip(range(1, 5), [800, 802, 805, 9999])]  # 9999 is an outlier
    rows += [{"period": f"2024-{q}T", "total_units": v}
             for q, v in zip(range(1, 5), [806, 808, 810, 812])]
    scores, strong, wl = score_metrics(_df(rows), ["total_units"])
    assert scores[0].status == "SUSPECT"
    assert any(w["period"] == "2023-4T" and "outlier" in w["reason"] for w in wl)


def test_sudden_low_break_is_suspect():
    # revenue in the hundreds for 10 periods, then single-digit → extraction fault
    vals = [800, 810, 805, 790, 820, 815, 800, 808, 812, 806, 4]
    rows, y, q = [], 2020, 1
    for v in vals:
        rows.append({"period": f"{y}-{q}T", "revenue": float(v)})
        q += 1
        if q > 4:
            y, q = y + 1, 1
    scores, strong, wl = score_metrics(_df(rows), ["revenue"])
    assert scores[0].status == "SUSPECT" and strong is False
    assert any(w["period"] == "2022-3T" and "outlier" in w["reason"] for w in wl)


def test_unresolved_pin_is_explained_suspect():
    # A would-be suspect whose verification was attempted and failed: no Suspect,
    # STRONG holds, worklist empty, but the unresolved count is reported.
    conf = {("2024-2T", "capex"): {"confidence": 0.8, "flagged": False,
                                   "source": "[table] Capex", "verify_status": "unresolved"}}
    df = _df([{"period": "2024-1T", "capex": 100.0},
             {"period": "2024-2T", "capex": -50.0}], conf=conf)
    scores, strong, wl = score_metrics(df, ["capex"], expectations={"capex": {"sparse": True}})
    assert scores[0].status == "STRONG"
    assert scores[0].unresolved == 1
    assert strong is True and wl == []


# ---------------------------------------------------------------------------
# Verified-override loader (pipeline._load_verified)
# ---------------------------------------------------------------------------
def test_load_verified_parses_values_and_blank(tmp_path, monkeypatch):
    import src.extract.pipeline as P
    from src.shared.paths import PROJECT_ROOT
    # write a temp verified file under a fake project root
    vdir = tmp_path / "data" / "verified"
    vdir.mkdir(parents=True)
    (vdir / "acme.csv").write_text(
        "period,key,value,note\n"
        "2024-1T,capex,1,815,quarterly\n"          # comma-thousands tolerated
        "2024-2T,ebt,1737,quarterly\n"
        "2016-1T,depreciation,BLANK,not disclosed\n"
        "2017-3T,ebitda,UNRESOLVED,table garbled in source PDF\n",
        encoding="utf-8")
    monkeypatch.setattr(P, "PROJECT_ROOT", tmp_path, raising=False)
    # _load_verified imports PROJECT_ROOT inside the function from src.shared.paths,
    # so patch there too.
    monkeypatch.setattr("src.shared.paths.PROJECT_ROOT", tmp_path, raising=False)
    out = P._load_verified(tmp_path / "configs" / "acme.yaml")
    assert out[("2024-2T", "ebt")][0] == 1737.0
    assert out[("2016-1T", "depreciation")][0] is None      # BLANK → blank sentinel
    assert out[("2017-3T", "ebitda")][0] is P.UNRESOLVED    # keep value, flag ships
