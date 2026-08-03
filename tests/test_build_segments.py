"""
test_build_segments.py — the single-markdown CLI (scripts/build_segments.py).

Covers the pure pieces that don't need network or a real PDF parse: markdown
parsing, the metrics-list → outline compiler, slug/dirname derivation, IR-option
extraction from a tuned config, and the download canonicalizer.
"""

from __future__ import annotations

import importlib
import hashlib
import json
import sys

import pandas as pd
import pytest

cli = importlib.import_module("scripts.build_segments")


# A tiny resolver stand-in: only "ebitda" and "revenue" resolve (exact), like the
# real MetricResolver would for unambiguous canonical keys.
class _FakeResolved:
    def __init__(self, key, conf):
        self.keys = [key]
        self.confidence = conf


class _FakeResolver:
    def resolve(self, label):
        norm = label.strip().lower()
        if norm == "ebitda":
            return _FakeResolved("ebitda", 1.0)
        if norm == "revenue":
            return _FakeResolved("revenue", 1.0)
        if norm == "total income":      # ambiguous: low-confidence fuzzy guess
            return _FakeResolved("rev_total_other", 0.70)
        return None


_VALID = {"revenue", "ebitda", "gross_profit", "net_income", "rev_total_other"}


def test_parse_input_extracts_name_link_and_metrics():
    md = (
        "# Walmex\n"
        "IR: https://www.walmex.mx/en/financial-information/quarterly.html\n\n"
        "## Metrics\n"
        "- revenue\n"
        "* gross profit\n"
        "1. operating income\n"
    )
    name, url, metrics = cli.parse_input(md)
    assert name == "Walmex"
    assert url == "https://www.walmex.mx/en/financial-information/quarterly.html"
    assert metrics == ["revenue", "gross profit", "operating income"]


def test_parse_input_ignores_url_inside_a_list_item():
    md = "# Co\n\n## Metrics\n- https://example.com/not-a-metric\n- revenue\n"
    name, url, metrics = cli.parse_input(md)
    assert url == "https://example.com/not-a-metric"   # first URL is the IR link
    assert metrics == ["revenue"]                       # the URL item is not a metric


@pytest.mark.parametrize("md,missing", [
    ("IR: https://x.com\n- revenue\n", "company name"),
    ("# Co\n- revenue\n", "IR link"),
    ("# Co\nIR: https://x.com\n", "metrics"),
])
def test_parse_input_requires_each_field(md, missing):
    with pytest.raises(cli.InputError) as exc:
        cli.parse_input(md)
    assert missing in str(exc.value)


def test_slugify_and_dirname():
    assert cli.slugify("Grupo Bimbo") == "grupo_bimbo"
    assert cli.slugify("La Comer, S.A.B.") == "la_comer_s_a_b"
    assert cli.dirname_for("Grupo Bimbo") == "Grupo_Bimbo"


def test_publish_latest_workbook_keeps_exactly_the_newest_model(tmp_path):
    source_dir = tmp_path / "company" / "excel"
    source_dir.mkdir(parents=True)
    source = source_dir / "Acme.xlsx"
    source.write_bytes(b"new workbook")

    latest = tmp_path / "outputs" / "latest"
    archive = tmp_path / "outputs" / "archive" / "deliverables"
    latest.mkdir(parents=True)
    (latest / "OldCo.xlsx").write_bytes(b"old workbook")
    (latest / "README.txt").write_text("stale", encoding="utf-8")
    (latest / "stale").mkdir()
    (latest / "stale" / "model.xlsx").write_bytes(b"stale workbook")

    published = cli.publish_latest_workbook(source, latest, archive)

    assert published == latest / "Acme.xlsx"
    assert published.read_bytes() == b"new workbook"
    assert list(latest.iterdir()) == [published]
    assert source.read_bytes() == b"new workbook"
    archived = list(archive.iterdir())
    assert len(archived) == 1
    assert (archived[0] / "OldCo.xlsx").read_bytes() == b"old workbook"
    assert (archived[0] / "README.txt").read_text(encoding="utf-8") == "stale"
    assert (archived[0] / "stale" / "model.xlsx").read_bytes() == b"stale workbook"


def test_publish_latest_workbook_preserves_previous_on_copy_failure(tmp_path, monkeypatch):
    source = tmp_path / "Acme.xlsx"
    source.write_bytes(b"new workbook")
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    previous = latest / "Previous.xlsx"
    previous.write_bytes(b"previous workbook")
    archive = tmp_path / "outputs" / "archive" / "deliverables"

    def fail_copy(*_args, **_kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(cli.shutil, "copy2", fail_copy)
    with pytest.raises(OSError, match="copy failed"):
        cli.publish_latest_workbook(source, latest, archive)

    assert list(latest.iterdir()) == [previous]
    assert previous.read_bytes() == b"previous workbook"


def test_publish_latest_workbook_rolls_back_when_archive_move_fails(tmp_path, monkeypatch):
    source = tmp_path / "Acme.xlsx"
    source.write_bytes(b"new workbook")
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    previous = latest / "Previous.xlsx"
    previous.write_bytes(b"previous workbook")
    archive = tmp_path / "outputs" / "archive" / "deliverables"

    original_replace = cli.Path.replace

    def fail_archive(self, target):
        target = cli.Path(target)
        if archive in target.parents:
            raise OSError("archive unavailable")
        return original_replace(self, target)

    monkeypatch.setattr(cli.Path, "replace", fail_archive)
    with pytest.raises(OSError, match="archive unavailable"):
        cli.publish_latest_workbook(source, latest, archive)

    assert list(latest.iterdir()) == [previous]
    assert previous.read_bytes() == b"previous workbook"


def test_repeated_same_company_builds_archive_every_prior_version(tmp_path, monkeypatch):
    latest = tmp_path / "outputs" / "latest"
    archive = tmp_path / "outputs" / "archive" / "deliverables"
    source = tmp_path / "Acme.xlsx"
    source.write_bytes(b"v1")
    cli.publish_latest_workbook(source, latest, archive)
    monkeypatch.setattr(
        cli,
        "_next_archive_target",
        lambda archive_dir, _previous: archive_dir / f"fixed_{len(list(archive_dir.iterdir()))}",
    )
    source.write_bytes(b"v2")
    cli.publish_latest_workbook(source, latest, archive)
    source.write_bytes(b"v3")
    cli.publish_latest_workbook(source, latest, archive)

    assert (latest / "Acme.xlsx").read_bytes() == b"v3"
    assert sorted((p / "Acme.xlsx").read_bytes() for p in archive.iterdir()) == [b"v1", b"v2"]


def _receipt(path, workbook_bytes, *, build_id="build-1"):
    path.write_text(
        json.dumps({
            "version": 3,
            "build_id": build_id,
            "workbook": {"sha256": hashlib.sha256(workbook_bytes).hexdigest()},
            "published": True,
        }),
        encoding="utf-8",
    )


def test_publication_binds_latest_workbook_to_atomic_receipt(tmp_path):
    source = tmp_path / "Acme.xlsx"
    source.write_bytes(b"new workbook")
    prepared = tmp_path / "prepared.json"
    _receipt(prepared, source.read_bytes())
    latest = tmp_path / "outputs" / "latest"
    archive = tmp_path / "outputs" / "archive" / "deliverables"
    current_receipt = tmp_path / "outputs" / "latest_manifest.json"

    cli.publish_latest_workbook(
        source,
        latest,
        archive,
        receipt_source=prepared,
        receipt_path=current_receipt,
    )

    assert (latest / "Acme.xlsx").read_bytes() == b"new workbook"
    assert json.loads(current_receipt.read_text())["build_id"] == "build-1"


def test_publication_uses_one_immutable_receipt_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "Acme.xlsx"
    source.write_bytes(b"new workbook")
    prepared = tmp_path / "prepared.json"
    _receipt(prepared, source.read_bytes(), build_id="validated")
    latest = tmp_path / "outputs" / "latest"
    archive = tmp_path / "outputs" / "archive" / "deliverables"
    current_receipt = tmp_path / "outputs" / "latest_manifest.json"
    original_copy = cli.shutil.copy2

    def mutate_receipt_after_validation(source_path, target_path):
        result = original_copy(source_path, target_path)
        _receipt(prepared, source.read_bytes(), build_id="mutated")
        return result

    monkeypatch.setattr(cli.shutil, "copy2", mutate_receipt_after_validation)
    cli.publish_latest_workbook(
        source,
        latest,
        archive,
        receipt_source=prepared,
        receipt_path=current_receipt,
    )

    assert json.loads(current_receipt.read_text())["build_id"] == "validated"


def test_receipt_failure_rolls_back_workbook_and_receipt(tmp_path, monkeypatch):
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "Previous.xlsx").write_bytes(b"previous workbook")
    archive = tmp_path / "outputs" / "archive" / "deliverables"
    archive.mkdir(parents=True)
    current_receipt = tmp_path / "outputs" / "latest_manifest.json"
    _receipt(current_receipt, b"previous workbook", build_id="old")
    source = tmp_path / "Acme.xlsx"
    source.write_bytes(b"new workbook")
    prepared = tmp_path / "prepared.json"
    _receipt(prepared, source.read_bytes(), build_id="new")
    original_write = cli._atomic_write_bytes
    prepared_bytes = prepared.read_bytes()

    def fail_current_receipt(target_path, data):
        if cli.Path(target_path) == current_receipt and data == prepared_bytes:
            raise OSError("receipt unavailable")
        return original_write(target_path, data)

    monkeypatch.setattr(cli, "_atomic_write_bytes", fail_current_receipt)
    with pytest.raises(OSError, match="receipt unavailable"):
        cli.publish_latest_workbook(
            source,
            latest,
            archive,
            receipt_source=prepared,
            receipt_path=current_receipt,
        )

    assert list(latest.iterdir()) == [latest / "Previous.xlsx"]
    assert (latest / "Previous.xlsx").read_bytes() == b"previous workbook"
    assert json.loads(current_receipt.read_text())["build_id"] == "old"


def test_source_change_during_staging_cannot_break_receipt_binding(tmp_path, monkeypatch):
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    previous = latest / "Previous.xlsx"
    previous.write_bytes(b"previous workbook")
    archive = tmp_path / "outputs" / "archive" / "deliverables"
    current_receipt = tmp_path / "outputs" / "latest_manifest.json"
    _receipt(current_receipt, previous.read_bytes(), build_id="old")

    source = tmp_path / "Acme.xlsx"
    source.write_bytes(b"receipt-bound workbook")
    prepared = tmp_path / "prepared.json"
    _receipt(prepared, source.read_bytes(), build_id="new")

    def copy_changed_source(_source_path, target_path):
        cli.Path(target_path).write_bytes(b"concurrent replacement")

    monkeypatch.setattr(cli.shutil, "copy2", copy_changed_source)
    with pytest.raises(ValueError, match="staged workbook changed"):
        cli.publish_latest_workbook(
            source,
            latest,
            archive,
            receipt_source=prepared,
            receipt_path=current_receipt,
        )

    assert list(latest.iterdir()) == [previous]
    assert previous.read_bytes() == b"previous workbook"
    assert json.loads(current_receipt.read_text())["build_id"] == "old"


def test_recovery_completes_crashed_swap_from_pending_receipt(tmp_path):
    parent = tmp_path / "outputs"
    latest = parent / "latest"
    latest.mkdir(parents=True)
    (latest / "New.xlsx").write_bytes(b"new workbook")
    backup = parent / ".latest-previous-crash"
    backup.mkdir()
    (backup / "Old.xlsx").write_bytes(b"old workbook")
    archive = parent / "archive" / "deliverables"
    archive.mkdir(parents=True)
    current_receipt = parent / "latest_manifest.json"
    _receipt(current_receipt, b"old workbook", build_id="old")
    pending = parent / ".latest-pending-receipt.json"
    _receipt(pending, b"new workbook", build_id="new")

    cli._recover_interrupted_publication(
        latest,
        archive,
        receipt_path=current_receipt,
        pending_receipt=pending,
    )

    assert (latest / "New.xlsx").read_bytes() == b"new workbook"
    assert json.loads(current_receipt.read_text())["build_id"] == "new"
    assert not pending.exists()
    assert any((path / "Old.xlsx").exists() for path in archive.iterdir())


def test_publication_lock_rejects_concurrent_writer(tmp_path):
    latest = tmp_path / "outputs" / "latest"
    with cli._publication_lock(latest):
        with pytest.raises(RuntimeError, match="another publication"):
            with cli._publication_lock(latest):
                pass


def test_cli_returns_distinct_status_for_review_candidate(tmp_path, monkeypatch):
    input_path = tmp_path / "company.md"
    input_path.write_text("placeholder", encoding="utf-8")
    candidate = cli.BuildResult(
        candidate_path=tmp_path / "candidate.xlsx",
        latest_path=None,
        manifest_path=tmp_path / "manifest.json",
        publish_blockers=("not strong",),
    )
    monkeypatch.setattr(cli, "run", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(sys, "argv", ["build_segments.py", str(input_path)])
    assert cli.main() == 3


def test_mid_build_markdown_change_is_candidate_only_and_keeps_latest(tmp_path, monkeypatch):
    from src.acquisition import service as acquisition_service
    from src.eval import validation_report
    from src.extract import pipeline

    original = (
        "# Acme\n"
        "IR: https://example.com/investors\n"
        "## Revenue\n"
        "- Net Sales {revenue}\n"
    )
    md_path = tmp_path / "acme.md"
    md_path.write_text(original, encoding="utf-8")
    original_sha = hashlib.sha256(md_path.read_bytes()).hexdigest()
    analyst_path = tmp_path / "analyst.csv"
    analyst_path.write_text(
        "section,label,key\nRevenue,Net Sales,revenue\n",
        encoding="utf-8",
    )

    outputs = tmp_path / "outputs"
    latest = outputs / "latest"
    latest.mkdir(parents=True)
    previous = latest / "Previous.xlsx"
    previous.write_bytes(b"previous analyst handoff")
    reports = tmp_path / "reports" / "acme"
    reports.mkdir(parents=True)
    (reports / "2025-1T.md").write_text("parsed report", encoding="utf-8")

    monkeypatch.setattr(cli, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(cli, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(cli, "CONFIGS_DIR", tmp_path / "configs")
    monkeypatch.setattr(cli, "_report_sources", lambda *_args, **_kwargs: (reports,))
    monkeypatch.setattr(cli, "_available_periods", lambda _dirs: ["2025-1T"])

    fresh = acquisition_service.QuarterlySyncFreshness(
        issuer_slug="acme",
        fresh=True,
        reason="fresh",
        max_age_seconds=86400,
        run_id="run-1",
        completed_at="2026-08-01T00:00:00+00:00",
        age_seconds=1,
        input_document_ids=("doc-1",),
        input_artifact_ids=("artifact-1",),
        catalog_document_ids=("doc-1",),
        run_document_ids=("doc-1",),
        input_watermark="watermark-1",
    )
    monkeypatch.setattr(
        acquisition_service,
        "check_quarterly_sync_freshness",
        lambda *_args, **_kwargs: fresh,
    )
    monkeypatch.setattr(
        acquisition_service,
        "check_quarterly_publication_freshness",
        lambda *_args, **_kwargs: fresh,
    )

    def mutate_input_during_extraction(*_args, output_csv=None, **_kwargs):
        md_path.write_text(original + "\n<!-- edited during build -->\n", encoding="utf-8")
        df = pd.DataFrame([{"period": "2025-1T", "revenue": 100.0}])
        df.attrs["confidence"] = {
            ("2025-1T", "revenue"): {
                "confidence": 1.0,
                "source": "[verified] test",
            }
        }
        df.attrs["input_paths"] = (str(reports / "2025-1T.md"),)
        df.attrs["input_lineage"] = ({"path": str(reports / "2025-1T.md")},)
        return df

    monkeypatch.setattr(pipeline, "run", mutate_input_during_extraction)

    def write_report(_df, _keys, *, out_path, **_kwargs):
        cli.Path(out_path).write_text("# validation\n", encoding="utf-8")

    monkeypatch.setattr(validation_report, "write_validation_report", write_report)

    def must_not_publish(*_args, **_kwargs):
        raise AssertionError("changed Markdown must not reach publication")

    monkeypatch.setattr(cli, "publish_latest_workbook", must_not_publish)

    result = cli.run(md_path, analyst_metrics=analyst_path)

    assert not result.published
    assert "onboarding Markdown changed during the build" in result.publish_blockers
    assert result.candidate_path.is_file()
    assert list(latest.iterdir()) == [previous]
    assert previous.read_bytes() == b"previous analyst handoff"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["input"]["sha256"] == original_sha
    assert manifest["workbook"]["sha256"] == hashlib.sha256(
        result.candidate_path.read_bytes()
    ).hexdigest()


def test_canonicalize_renames_to_period_pdfs(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "Walmex_1Q26_Release.pdf").write_bytes(b"%PDF-1.4\n")
    (staging / "Walmex_4Q25_Earnings_Release.pdf").write_bytes(b"%PDF-1.4\n")
    out = tmp_path / "downloads"

    saved = cli._canonicalize(list(staging.glob("*.pdf")), out)

    assert sorted(saved) == ["2025-4T", "2026-1T"]
    assert (out / "2026-1T.pdf").exists()
    assert (out / "2025-4T.pdf").exists()


def test_ir_options_reads_tuned_config(tmp_path):
    cfg = tmp_path / "co.yaml"
    cfg.write_text(
        "company:\n  ticker: CO\n"
        "ir_website:\n"
        "  url: https://tuned.example.com/ir\n"
        "  pdf_link_pattern: 'report.*\\.pdf'\n"
        "  use_playwright: true\n"
        "  delay_ms: 250\n",
        encoding="utf-8",
    )
    opts = cli._ir_options(cfg)
    assert opts["url"] == "https://tuned.example.com/ir"
    assert opts["file_pattern"] == "report.*\\.pdf"
    assert opts["use_playwright"] is True
    assert opts["delay_ms"] == 250
    assert cli._company_ticker(cfg) == "CO"


def test_ir_options_reads_impersonate():
    import tempfile
    import pathlib
    with tempfile.TemporaryDirectory() as d:
        cfg = pathlib.Path(d) / "co.yaml"
        cfg.write_text("ir_website:\n  url: https://x\n  impersonate: chrome\n", encoding="utf-8")
        assert cli._ir_options(cfg)["impersonate"] == "chrome"


def test_ir_options_empty_without_config():
    assert cli._ir_options(None) == {}


# ── Outline compiler ─────────────────────────────────────────────────────────

def test_derived_token_normalization():
    assert cli._derived_token("YoY") == "YoY"
    assert cli._derived_token("YoY %") == "YoY"
    assert cli._derived_token("bps Δ") == "bps change"
    assert cli._derived_token("% of Total Units") == "As % of Total"
    assert cli._derived_token("As % of Consolidated") == "As % of"
    assert cli._derived_token("2-year comp") == "2-year comp"
    assert cli._derived_token("EBITDA Margin") == "EBITDA Margin"   # passes through
    # Not a derived row — keeps its own identity as a data label:
    assert cli._derived_token("SSS YoY") is None
    assert cli._derived_token("Total Income") is None


def test_compile_outline_sections_pins_and_autoresolve():
    md = (
        "# Co\nIR: https://x\n\n"
        "## Total Income\n"
        "- Total Income {revenue}\n"     # pinned
        "- YoY\n"                          # derived
        "## Profitability\n"
        "- EBITDA\n"                       # auto-resolves (exact)
        "- Margin\n"                       # derived
        "- Total Income\n"                 # ambiguous → unmapped (blank)
    )
    outline, sections, mapping, report = cli.compile_outline(md, _FakeResolver(), _VALID)

    assert sections == ["Total Income", "Profitability"]
    # Pinned + section-qualified so the heading isn't swallowed as a data row:
    assert mapping["Total Income/Total Income"] == "revenue"
    assert mapping["Profitability/EBITDA"] == "ebitda"
    # The ambiguous "Total Income" under Profitability stays unmapped (no wrong key):
    assert "Profitability/Total Income" not in mapping
    kinds = {(label, kind) for label, kind, _ in report}
    assert ("YoY", "derived") in kinds
    assert ("EBITDA", "auto") in kinds
    assert ("Total Income", "unmapped") in kinds


def test_compile_outline_bad_pin_is_blank_not_wrong():
    md = "# Co\nIR: https://x\n\n## S\n- Mystery Metric {no_such_key}\n"
    _o, _s, mapping, report = cli.compile_outline(md, _FakeResolver(), _VALID)
    assert mapping == {}                                  # nothing mapped to a bad key
    assert ("Mystery Metric", "bad-pin") in {(l, k) for l, k, _ in report}


def test_explicit_pin_overrides_derived_label_heuristic_end_to_end():
    from src.excel.segments_sheet import parse_outline

    md = (
        "# Co\nIR: https://x\n\n"
        "## Profitability\n"
        "- Gross Profit {gross_profit}\n"
        "- Margin {gross_margin}\n"
    )
    outline, sections, mapping, report = cli.compile_outline(
        md, _FakeResolver(), _VALID | {"gross_profit", "gross_margin"},
    )
    rows = parse_outline(outline, sections, mapping)

    assert ("Margin", "pinned", "gross_margin") in report
    margin = next(row for row in rows if row.label == "Margin")
    assert (margin.kind, margin.key, margin.derived) == ("data", "gross_margin", None)
    assert cli.metric_contract_issues(report) == []


@pytest.mark.parametrize(
    ("analyst_label", "derived_kind"),
    [
        ("YoY %", "yoy"),
        ("bps Δ", "bps_change"),
        ("% of Total Units", "pct_total"),
        ("2 year comp", "two_year"),
    ],
)
def test_derived_alias_keeps_exact_analyst_display_label(analyst_label, derived_kind):
    from src.excel.segments_sheet import build_outline_workbook, parse_outline

    md = (
        "# Co\nIR: https://x\n\n"
        "## Revenue\n"
        "- Net Sales {revenue}\n"
        f"- {analyst_label}\n"
    )
    outline, sections, mapping, _report = cli.compile_outline(
        md, _FakeResolver(), _VALID,
    )
    rows = parse_outline(outline, sections, mapping)
    derived = next(row for row in rows if row.kind == "derived")
    assert (derived.label, derived.derived) == (analyst_label, derived_kind)

    df = pd.DataFrame([
        {"period": f"{year}-{quarter}T", "revenue": 100 + quarter}
        for year in (2024, 2025)
        for quarter in (1, 2, 3, 4)
    ])
    ws = build_outline_workbook("CO: Co", rows, df, preserve_requested_rows=True).active
    rendered = [ws.cell(row, 2).value for row in range(6, ws.max_row + 1)]
    assert analyst_label in rendered


def test_duplicate_labels_keep_distinct_positional_keys_in_workbook():
    from src.excel.segments_sheet import build_outline_workbook, parse_outline

    md = (
        "# Co\nIR: https://x\n\n"
        "## Operations\n"
        "- Stores {stores}\n"
        "- Stores {new_stores}\n"
    )
    outline, sections, mapping, report = cli.compile_outline(
        md, _FakeResolver(), _VALID | {"stores", "new_stores"},
    )
    rows = parse_outline(outline, sections, mapping)
    stores = [row for row in rows if row.label == "Stores"]
    assert [row.key for row in stores] == ["stores", "new_stores"]
    assert cli.requested_key_sequence(report) == (None, "stores", "new_stores")

    df = pd.DataFrame([{"period": "2025-1T", "stores": 10, "new_stores": 2}])
    ws = build_outline_workbook("CO: Co", rows, df).active
    store_rows = [row for row in range(6, ws.max_row + 1) if ws.cell(row, 2).value == "Stores"]
    assert [ws.cell(row, 3).value for row in store_rows] == [10, 2]


def test_repeated_section_and_label_keep_distinct_positional_keys():
    from src.excel.segments_sheet import parse_outline

    md = (
        "# Co\nIR: https://x\n\n"
        "## Operations\n"
        "- Stores {stores}\n"
        "## Operations\n"
        "- Stores {new_stores}\n"
    )
    outline, sections, mapping, _report = cli.compile_outline(
        md, _FakeResolver(), _VALID | {"stores", "new_stores"},
    )
    rows = parse_outline(outline, sections, mapping)

    assert [row.key for row in rows if row.label == "Stores"] == [
        "stores",
        "new_stores",
    ]


def test_strict_metric_contract_requires_explicit_pins_only_for_data_rows():
    report = [
        ("Revenue", "section", ""),
        ("Net Sales", "pinned", "revenue"),
        ("YoY", "derived", "YoY"),
        ("EBITDA", "auto", "ebitda (1.00)"),
        ("Mystery", "unmapped", None),
        ("Bad", "bad-pin", "not_a_key"),
    ]

    strict = cli.metric_contract_issues(report)
    assert len(strict) == 3
    assert any("auto-mapped" in issue for issue in strict)
    assert any("no pinned metric key" in issue for issue in strict)
    assert any("unknown metric key" in issue for issue in strict)

    exploratory = cli.metric_contract_issues(report, require_pins=False)
    assert len(exploratory) == 2
    assert not any("auto-mapped" in issue for issue in exploratory)


def test_analyst_metric_metadata_supports_relative_xlsx_and_worksheet():
    md = (
        "# Co\n"
        "IR: https://x\n"
        "Analyst-Metrics: ../requests/Metrics.xlsx#Requested Metrics\n"
        "Analyst-Company: CO TICKER\n"
        "## Revenue\n- Net Sales {revenue}\n"
    )

    assert cli._analyst_sheet_metadata(md) == (
        "../requests/Metrics.xlsx",
        "CO TICKER",
        "Requested Metrics",
    )


def test_requested_contract_sequences_preserve_row_roles_and_keys():
    report = [
        ("Revenue", "section", ""),
        ("Net Sales", "pinned", "revenue"),
        ("YoY", "derived", "YoY"),
    ]
    assert cli.requested_label_sequence(report) == ("Revenue", "Net Sales", "YoY")
    assert cli.requested_key_sequence(report) == (None, "revenue", None)
    assert cli.requested_kind_sequence(report) == ("section", "row", "row")


def test_outline_workbook_fills_data_and_makes_formulas():
    # End-to-end of the pure path: compiled outline + a fake df → workbook.
    from src.excel.segments_sheet import parse_outline, build_outline_workbook

    md = ("# Co\nIR: https://x\n\n## Profitability\n"
          "- EBITDA {ebitda}\n- YoY\n- Net Income {net_income}\n")
    outline, sections, mapping, _r = cli.compile_outline(md, _FakeResolver(), _VALID)
    rows = parse_outline(outline, sections, mapping)

    df = pd.DataFrame([
        {"period": f"{y}-{q}T", "ebitda": 100 + q, "net_income": 50 + q}
        for y in (2024, 2025) for q in (1, 2, 3, 4)
    ])
    wb = build_outline_workbook("CO: Co", rows, df, units="MXN mn")
    ws = wb["Segments"]
    flat = [c.value for row in ws.iter_rows() for c in row if c.value is not None]
    # A YoY formula exists, and an extracted hard input made it into a cell.
    assert any(isinstance(v, str) and v.startswith("=IFERROR(") for v in flat)
    assert any(v in (101, 102, 103, 104, 105) for v in flat)
