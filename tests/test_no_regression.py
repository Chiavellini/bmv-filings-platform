"""No-regression gate for the extraction layer.

Runs the full deterministic cascade (the same scorer the scorecard uses) for every
ground-truth company and asserts that, versus a committed baseline, no company
loses correct extractions or gains accuracy FAILs. This is the guardrail that lets
the extraction layer be improved company-by-company without silently regressing a
company that already works.

Regenerate the baseline ONLY on an intended change, with:
    python3 -c "import json; from scripts.pipeline_scorecard import score_company; \
      from src.eval.compare_extractions import COMPANIES; \
      json.dump({c:{k:score_company(c)[k] for k in ('obs_total','obs_correct','obs_miss','obs_fail','obs_excluded')} \
      for c in COMPANIES}, open('tests/fixtures/scorecard_baseline.json','w'), indent=2, sort_keys=True)"
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pipeline_scorecard import score_company
from src.eval.compare_extractions import COMPANIES
from tests._corpus import requires_corpus

# Scoring reads every company's parsed filings from data/reports/, which is
# gitignored. Without the corpus this gate cannot say anything about regressions,
# so it skips rather than reporting 17 false failures.
pytestmark = requires_corpus

_BASELINE = json.loads(
    (Path(__file__).parent / "fixtures" / "scorecard_baseline.json").read_text()
)


@pytest.mark.parametrize("company", sorted(COMPANIES))
def test_no_regression(company):
    base = _BASELINE.get(company)
    if base is None:
        pytest.skip(f"{company} absent from baseline (no ground truth checked out)")
    cur = score_company(company)
    assert cur["obs_correct"] >= base["obs_correct"], (
        f"{company}: correct dropped {base['obs_correct']} → {cur['obs_correct']}"
    )
    assert cur["obs_fail"] <= base["obs_fail"], (
        f"{company}: FAILs rose {base['obs_fail']} → {cur['obs_fail']}"
    )
