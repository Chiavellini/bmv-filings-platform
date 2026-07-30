"""Whole-book trust scorecard — the grade logic that turns validation sentinels + de-noised reconciler
findings + the N/A-AWARE core-matrix completeness into a single GREEN/AMBER/RED verdict per company.
Offline (no network, no rebuild)."""
from scripts.book_scorecard import _grade


def _st(status, sentinels=()):
    return {"status": status, "sentinels": list(sentinels)}


def test_green_when_core_complete_and_no_findings():
    assert _grade(_st("PASS"), [], core_complete=True) == "GREEN"


def test_green_when_status_says_incomplete_but_core_complete():
    # The whole fix: a company whose per-company status.json is INCOMPLETE (its stricter _REQUIRED gate)
    # but which is 100% in the N/A-aware core matrix is COMPLETE → GREEN, not AMBER.
    assert _grade(_st("INCOMPLETE"), [], core_complete=True) == "GREEN"


def test_amber_when_not_core_complete():
    # a real GAP remains in the core matrix → AMBER (regardless of the status.json string)
    assert _grade(_st("PASS"), [], core_complete=False) == "AMBER"


def test_amber_when_nonscale_finding():
    # a non-scale actionable finding (e.g. EV bridge) → review, not RED
    assert _grade(_st("PASS"), [{"Metric": "Enterprise value", "RootCause": ""}],
                  core_complete=True) == "AMBER"


def test_red_when_sentinel_present():
    # a present-but-wrong value (sentinel) → RED even if core-complete
    assert _grade(_st("BROKEN", ["P/BV 0.5x implausibly cheap"]), [], core_complete=True) == "RED"


def test_red_when_scale_finding():
    assert _grade(_st("PASS"), [{"Metric": "P/Sales", "RootCause": "shares/revenue scale"}],
                  core_complete=True) == "RED"


def test_missing_status_grades_on_matrix_and_findings():
    # No status.json → no sentinels to read; completeness + findings still decide. A core-complete row
    # with no finding is GREEN; one with a gap is AMBER.
    assert _grade(None, [], core_complete=True) == "GREEN"
    assert _grade(None, [], core_complete=False) == "AMBER"
