from pathlib import Path

from scripts.audit_dashboard_search import audit, percentile


def test_percentile_uses_nearest_rank():
    assert percentile([], 0.95) == 0.0
    assert percentile([1, 2, 3, 4], 0.50) == 2
    assert percentile([1, 2, 3, 4], 0.95) == 4


def test_audit_passes_fixture_index_and_reports_company_metrics(built_index):
    store, _ = built_index
    report = audit(Path(store.db_path), queries=("revenue", "ebitda", "fx"))

    assert report["status"] == "pass"
    assert report["documents"] == 2
    assert report["companies"] == 1
    company = report["company_reports"][0]
    assert company["company"] == "acme"
    assert company["documents"] == 2
    assert company["words"] > 0
    assert company["queries"]["revenue"]["exact_mentions"] == 0
    assert company["queries"]["revenue"]["related_occurrences"] > 0
    assert company["queries"]["revenue"]["trend_exact_mentions"] == 0
    assert not report["failures"]


def test_audit_company_scope_does_not_report_unselected_memberships(built_index):
    store, _ = built_index
    report = audit(Path(store.db_path), queries=("revenue",), companies={"nobody"})
    assert report["companies"] == 0
    assert report["company_reports"] == []


def test_substring_only_fragment_is_not_a_mention(built_index):
    store, _ = built_index
    # "marg" is only a fragment of "margin" and should not be reported as a mention.
    report = audit(Path(store.db_path), queries=("marg",))

    assert report["status"] == "pass"
    assert not report["failures"]
    assert not any(w["kind"] == "fts_substring_ranking_gap" for w in report["warnings"])
