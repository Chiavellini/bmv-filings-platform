"""Typed arbitrary-period observation/revision plumbing."""

from src.extract.extract_metrics import MetricRow
from src.extract.revisions import (
    ExtractionBatch,
    FactObservation,
    ObservationRole,
    PeriodKind,
    apply_observation_revisions,
    reduce_observations,
)
from src.extract.tiered_extract import PeriodSource, apply_restated_priors


def _row(metric, value, *, prior=None, source="[statement] official row"):
    return MetricRow(
        metric=metric,
        label_es=metric,
        current=value,
        prior=prior,
        var_pct=None,
        unit="currency",
        source_line=source,
    )


def _observation(metric, observed_period, value, report_period, **kwargs):
    return FactObservation(
        company="ACME",
        metric=metric,
        observed_period=observed_period,
        value=value,
        report_period=report_period,
        period_kind=PeriodKind.QUARTER,
        basis="ifrs",
        currency="MXN",
        unit="millions",
        source_tier="statement",
        trusted=True,
        **kwargs,
    )


def test_q4_issued_revision_can_replace_same_year_q3():
    q3 = _observation("revenue", "2024-3T", 200, "2024-3T",
                      role=ObservationRole.CURRENT, source_document_id="q3")
    q4_revision = _observation(
        "revenue", "2024-3T", 205, "2024-4T",
        role=ObservationRole.RESTATED, source_document_id="q4",
    )
    ebitda = _observation("ebitda", "2024-3T", 40, "2024-3T",
                          role=ObservationRole.CURRENT, source_document_id="q3")

    decisions = reduce_observations([q4_revision, ebitda, q3], mode="allow")
    selected = {decision.series_key.metric: decision for decision in decisions}

    assert selected["revenue"].selected.value == 205
    assert selected["revenue"].superseded == (q3,)
    assert selected["revenue"].conflicts == ()
    assert selected["ebitda"].selected.value == 40


def test_basis_dimensions_and_untrusted_conflicts_are_never_cross_merged():
    consolidated = _observation("revenue", "2024-3T", 200, "2024-3T",
                                role="current", source_document_id="q3")
    untrusted = FactObservation(
        company="ACME", metric="revenue", observed_period="2024-3T", value=205,
        report_period="2024-4T", period_kind="quarter", basis="ifrs",
        currency="MXN", unit="millions", source_tier="regex_table",
        trusted=False, role="restated", source_document_id="q4",
    )
    segment = _observation(
        "revenue", "2024-3T", 80, "2024-4T", role="restated",
        source_document_id="q4-segment", dimensions={"segment": "Mexico"},
    )

    decisions = reduce_observations([consolidated, untrusted, segment], mode="allow")
    consolidated_decision = next(
        decision for decision in decisions if not decision.series_key.dimensions
    )

    assert len(decisions) == 2
    assert consolidated_decision.selected.value == 200
    assert consolidated_decision.conflicts == (untrusted,)


def test_typed_current_revision_preserves_prior_for_explicit_restatement():
    from src.model.financial_model import METRICS

    definitions = [metric for metric in METRICS if metric.key == "revenue"]
    rows = {
        "2021-2T": {"revenue": _row("revenue", 200)},
        "2022-2T": {"revenue": _row("revenue", 300, prior=205)},
    }
    observation = FactObservation(
        metric="revenue", observed_period="2022-2T", value=310,
        report_period="2022-2T", role="restated", source_tier="statement",
    )
    cfg = {
        "company": {"currency": "MXN", "unit": "currency"},
        "restated_prior": {"revenue": ["2Q21A"]},
    }

    apply_observation_revisions(rows, [observation], definitions, cfg)
    assert rows["2022-2T"]["revenue"].prior == 205
    apply_restated_priors(rows, definitions, cfg)
    assert rows["2021-2T"]["revenue"].current == 205


def test_pipeline_applies_extractor_batch_and_exposes_revision_audit(tmp_path, monkeypatch):
    from src.extract import pipeline

    config = tmp_path / "acme.yaml"
    config.write_text(
        "company:\n  name: ACME\n  ticker: ACME\n  currency: MXN\n  unit: millions\n",
        encoding="utf-8",
    )
    q3_path = tmp_path / "2024-3T.md"
    q4_path = tmp_path / "2024-4T.md"
    q3_path.write_text("Q3", encoding="utf-8")
    q4_path.write_text("Q4", encoding="utf-8")
    docs = {
        "2024-3T": PeriodSource(period="2024-3T", source_path=q3_path),
        "2024-4T": PeriodSource(period="2024-4T", source_path=q4_path),
    }
    monkeypatch.setattr(pipeline, "_resolve_source", lambda *args, **kwargs: docs)

    def fake_extract(src, *args, **kwargs):
        if src.period == "2024-3T":
            return {"revenue": _row("revenue", 200)}
        revision = FactObservation(
            metric="revenue", observed_period="2024-3T", value=205,
            report_period="2024-4T", role="restated", source_tier="statement",
            source_document_id="ACME-2024-Q4", evidence="restated Q3 comparative",
        )
        return ExtractionBatch(
            rows={"revenue": _row("revenue", 260)}, observations=[revision],
        )

    monkeypatch.setattr(pipeline, "extract_metrics_tiered", fake_extract)
    df = pipeline.run(
        tmp_path, config=config, metrics=["revenue"], do_validate=False, verbose=False,
    )

    assert df.loc[df["period"] == "2024-3T", "revenue"].item() == 205
    applied = [event for event in df.attrs["revisions"] if event.get("applied")]
    assert applied[0]["selected_document_id"] == "ACME-2024-Q4"
    assert df.attrs["input_paths"] == (str(q3_path.absolute()), str(q4_path.absolute()))
    assert tuple(item["period"] for item in df.attrs["input_lineage"]) == (
        "2024-3T", "2024-4T",
    )


def test_pipeline_emits_and_applies_official_xbrl_q4_revision_without_global_policy(
    tmp_path, monkeypatch,
):
    """Production Tier 1, not a fake extractor, revises Q3 from Q4 contexts."""
    from src.extract import pipeline

    config = tmp_path / "acme.yaml"
    config.write_text(
        "company:\n  name: ACME\n  ticker: ACME\n"
        "  currency: MXN\n  unit: millions\n",
        encoding="utf-8",
    )

    def entry(value, start, end):
        return {
            "value": value,
            "period_start": start,
            "period_end": end,
            "instant": None,
            "unit": "ISO4217:MXN",
            "decimals": "-3",
            "dimensions": None,
        }

    docs = {
        "2024-3T": PeriodSource(
            period="2024-3T",
            period_end="2024-09-30",
            facts={"ifrs-full_Revenue": [
                entry(200_000_000, "2024-07-01", "2024-09-30"),
            ]},
            facts_document_id="q3-doc",
        ),
        "2024-4T": PeriodSource(
            period="2024-4T",
            period_end="2024-12-31",
            facts={"ifrs-full_Revenue": [
                entry(260_000_000, "2024-10-01", "2024-12-31"),
                entry(205_000_000, "2024-07-01", "2024-09-30"),
            ]},
            facts_document_id="q4-doc",
        ),
    }
    monkeypatch.setattr(pipeline, "_resolve_source", lambda *args, **kwargs: docs)

    df = pipeline.run(
        tmp_path,
        config=config,
        metrics=["revenue"],
        do_validate=False,
        verbose=False,
    )

    assert "latest_comparative" not in config.read_text(encoding="utf-8")
    assert df.loc[df["period"] == "2024-3T", "revenue"].item() == 205.0
    assert df.loc[df["period"] == "2024-4T", "revenue"].item() == 260.0
    event = next(item for item in df.attrs["revisions"] if item.get("applied"))
    assert event["observed_period"] == "2024-3T"
    assert event["selected_report_period"] == "2024-4T"
    assert event["selected_source_tier"] == "xbrl"
    assert event["selected_document_id"] == "q4-doc"


def test_company_extractor_can_return_batch_without_breaking_dict_api(monkeypatch):
    import sys
    import types

    from src.extract import tiered_extract
    from src.model.financial_model import METRICS

    observation = FactObservation(
        metric="revenue", observed_period="2024-3T", value=205,
        report_period="2024-4T", role="restated", source_tier="statement",
    )
    module = types.ModuleType("tests.fake_batch_extractor")
    module.extract = lambda text, defs: ExtractionBatch(
        rows={"revenue": _row("revenue", 260)}, observations=[observation],
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(
        tiered_extract._CUSTOM_EXTRACTORS,
        "batch_test", (module.__name__, "extract", False, False),
    )

    output = tiered_extract.extract_metrics_tiered(
        PeriodSource(period="2024-4T", text="Quarterly report"),
        [metric for metric in METRICS if metric.key == "revenue"],
        {"custom_extractor": "batch_test"},
        tiers={"search"},
    )

    assert output["revenue"].current == 260
    assert output.observations == [observation]


def test_pipeline_normalizes_live_eval_restatement_config(tmp_path, monkeypatch):
    from src.extract import pipeline

    config = tmp_path / "acme.yaml"
    config.write_text(
        "company:\n  name: ACME\n  currency: MXN\n  unit: millions\n"
        "restated_prior:\n  revenue: [2Q21A]\n",
        encoding="utf-8",
    )
    docs = {
        "2021-2T": PeriodSource(period="2021-2T"),
        "2022-2T": PeriodSource(period="2022-2T"),
    }
    monkeypatch.setattr(pipeline, "_resolve_source", lambda *args, **kwargs: docs)

    def fake_extract(src, *args, **kwargs):
        return ({"revenue": _row("revenue", 200)} if src.period == "2021-2T"
                else {"revenue": _row("revenue", 300, prior=205)})

    monkeypatch.setattr(pipeline, "extract_metrics_tiered", fake_extract)
    df = pipeline.run(
        tmp_path, config=config, metrics=["revenue"], do_validate=False, verbose=False,
    )
    assert df.loc[df["period"] == "2021-2T", "revenue"].item() == 205
    assert df.attrs["confidence"][("2021-2T", "revenue")]["source"].startswith("[restated]")


def test_directory_mode_attaches_sibling_facts_and_keeps_facts_only_period(tmp_path):
    import json
    from src.extract.pipeline import _from_directory

    (tmp_path / "2024-1T.md").write_text("Quarterly release", encoding="utf-8")
    first = {"Revenue": [{"value": 10}]}
    second = {"Revenue": [{"value": 20}]}
    (tmp_path / "ACME_2024-1T_facts.json").write_text(
        json.dumps({"facts": first}), encoding="utf-8",
    )
    (tmp_path / "ACME_2024-2T_facts.json").write_text(
        json.dumps({"facts": second}), encoding="utf-8",
    )

    docs = _from_directory(tmp_path)
    assert docs["2024-1T"].facts == first
    assert docs["2024-1T"].text == "Quarterly release"
    assert docs["2024-2T"].facts == second
    assert docs["2024-2T"].text == ""


def test_directory_sequence_forms_one_nonduplicating_period_union(tmp_path):
    import json
    from src.extract.pipeline import _resolve_source

    reports = tmp_path / "reports"
    parsed = tmp_path / "parsed"
    reports.mkdir()
    parsed.mkdir()
    facts = {"Revenue": [{"value": 10}]}
    (reports / "ACME_2024-1T_facts.json").write_text(
        json.dumps({"facts": facts}), encoding="utf-8",
    )
    (parsed / "ACME_2024-1T.md").write_text("Parsed derivative", encoding="utf-8")

    docs = _resolve_source(
        [reports, parsed], output_dir=tmp_path, period_filter=None,
        max_reports=10, cfg_path=None,
    )

    assert list(docs) == ["2024-1T"]
    assert docs["2024-1T"].text == "Parsed derivative"
    # Cross-directory pairing without catalog lineage is unsafe: the parsed
    # report may be an amendment while the facts belong to the old filing.
    assert docs["2024-1T"].facts is None


def test_controlled_nested_xbrl_facts_are_ingested_without_recursive_junk(tmp_path):
    import json
    from src.extract.pipeline import _from_directory

    company = tmp_path / "acme"
    xbrl = company / "xbrl"
    junk = company / "archive" / "nested"
    xbrl.mkdir(parents=True)
    junk.mkdir(parents=True)
    facts = {"Revenue": [{"value": 10}]}
    (xbrl / "ACME_2024-1T_facts.json").write_text(
        json.dumps({"facts": facts}), encoding="utf-8",
    )
    (junk / "ACME_2023-1T_facts.json").write_text(
        json.dumps({"facts": {"Revenue": [{"value": 1}]}}), encoding="utf-8",
    )

    docs = _from_directory(company)
    assert list(docs) == ["2024-1T"]
    assert docs["2024-1T"].facts == facts
    assert docs["2024-1T"].facts_path == xbrl / "ACME_2024-1T_facts.json"


def test_amended_text_does_not_receive_stale_facts_from_older_source(tmp_path, capsys):
    import json
    from src.extract import pipeline

    old = tmp_path / "old"
    amended = tmp_path / "amended"
    old.mkdir()
    amended.mkdir()
    (old / "ACME_2024-1T_facts.json").write_text(
        json.dumps({
            "facts": {
                "ifrs-full_Revenue": [{
                    "value": 200_000,
                    "period_start": "2024-01-01",
                    "period_end": "2024-03-31",
                    "unit": "ISO4217:MXN",
                    "decimals": "-3",
                    "dimensions": None,
                }],
            },
        }),
        encoding="utf-8",
    )
    (amended / "ACME_2024-1T.md").write_text(
        "AMENDED REVENUE 205", encoding="utf-8",
    )
    config = tmp_path / "acme.yaml"
    config.write_text(
        "company:\n  name: ACME\n  currency: MXN\n  unit: miles_mxn\n"
        "metric_overrides:\n  revenue:\n    patterns:\n"
        "      - regex: '^AMENDED REVENUE ([0-9.]+)'\n"
        "        multiplier: 1\n        source: table\n",
        encoding="utf-8",
    )

    df = pipeline.run(
        [old, amended], config=config, metrics=["revenue"],
        do_validate=False, verbose=False,
    )

    assert df.loc[df["period"] == "2024-1T", "revenue"].item() == 205
    assert "ignored lineage-incompatible facts" in capsys.readouterr().err
    assert df.attrs["input_paths"] == (
        str((amended / "ACME_2024-1T.md").absolute()),
    )
    assert df.attrs["input_lineage"][0]["facts_path"] is None


def test_individual_file_sequence_does_not_merge_old_facts_into_amended_text(tmp_path):
    import json
    from src.extract import pipeline

    old = tmp_path / "old"
    amended = tmp_path / "amended"
    old.mkdir()
    amended.mkdir()
    facts_path = old / "ACME_2024-1T_facts.json"
    facts_path.write_text(
        json.dumps({
            "facts": {
                "ifrs-full_Revenue": [{
                    "value": 200_000,
                    "period_start": "2024-01-01",
                    "period_end": "2024-03-31",
                    "unit": "ISO4217:MXN",
                    "decimals": "-3",
                    "dimensions": None,
                }],
            },
        }),
        encoding="utf-8",
    )
    amended_path = amended / "ACME_2024-1T.md"
    amended_path.write_text("AMENDED REVENUE 205", encoding="utf-8")
    config = tmp_path / "acme.yaml"
    config.write_text(
        "company:\n  name: ACME\n  currency: MXN\n  unit: miles_mxn\n"
        "metric_overrides:\n  revenue:\n    patterns:\n"
        "      - regex: '^AMENDED REVENUE ([0-9.]+)'\n"
        "        multiplier: 1\n        source: table\n",
        encoding="utf-8",
    )

    df = pipeline.run(
        [facts_path, amended_path],
        config=config,
        metrics=["revenue"],
        do_validate=False,
        verbose=False,
    )

    assert df.loc[0, "revenue"] == 205
    assert df.attrs["input_paths"] == (str(amended_path.absolute()),)


def test_mixed_sources_merge_complementary_artifacts_only_for_same_document():
    from pathlib import Path
    from src.extract.pipeline import _merge_period_sources

    facts = PeriodSource(
        period="2024-1T",
        facts={"Revenue": [{"value": 200}]},
        facts_path=Path("/estate/facts.json"),
        facts_document_id="same-doc",
    )
    text = PeriodSource(
        period="2024-1T",
        text="current text",
        source_path=Path("/estate/parsed/current.md"),
        source_document_id="same-doc",
    )
    merged = _merge_period_sources(facts, text)
    assert merged.text == "current text"
    assert merged.facts == facts.facts

    one_sided = PeriodSource(
        period="2024-1T",
        text="uncataloged amendment",
        source_path=Path("/estate/parsed/uncataloged.md"),
    )
    not_merged = _merge_period_sources(facts, one_sided)
    assert not_merged.text == "uncataloged amendment"
    assert not_merged.facts is None


def test_colocated_facts_with_one_sided_catalog_lineage_are_not_attached(tmp_path):
    import json
    import sqlite3
    from src.extract.pipeline import _from_directory

    estate = tmp_path / "estate"
    reports = estate / "views" / "reports" / "acme"
    reports.mkdir(parents=True)
    text_path = reports / "2024-1T.md"
    facts_path = reports / "ACME_2024-1T_facts.json"
    text_path.write_text("cataloged amendment", encoding="utf-8")
    facts_path.write_text(
        json.dumps({"facts": {"Revenue": [{"value": 200}]}}),
        encoding="utf-8",
    )

    connection = sqlite3.connect(estate / "catalog.db")
    connection.execute(
        "CREATE TABLE artifacts(artifact_id TEXT, document_id TEXT, path TEXT)"
    )
    connection.execute(
        "INSERT INTO artifacts VALUES('text-art', 'current-doc', ?)",
        ("views/reports/acme/2024-1T.md",),
    )
    connection.commit()
    connection.close()

    docs = _from_directory(reports)
    assert docs["2024-1T"].text == "cataloged amendment"
    assert docs["2024-1T"].facts is None
    assert docs["2024-1T"].facts_path is None


def test_cross_directory_facts_attach_when_catalog_proves_same_document(tmp_path):
    import json
    import sqlite3
    from src.extract.pipeline import _from_directories

    estate = tmp_path / "estate"
    reports = estate / "views" / "reports" / "acme"
    parsed = estate / "views" / "parsed" / "acme"
    reports.mkdir(parents=True)
    parsed.mkdir(parents=True)
    facts_path = reports / "ACME_2025-1T_facts.json"
    text_path = parsed / "2025-1T__aaaaaaaaaaaa__222222222222.md"
    facts = {"Revenue": [{"value": 10}]}
    facts_path.write_text(json.dumps({"facts": facts}), encoding="utf-8")
    text_path.write_text("current document", encoding="utf-8")

    conn = sqlite3.connect(estate / "catalog.db")
    conn.execute(
        "CREATE TABLE artifacts(artifact_id TEXT, document_id TEXT, path TEXT)"
    )
    conn.executemany(
        "INSERT INTO artifacts VALUES(?, 'current-doc', ?)",
        [
            ("facts-art", "views/reports/acme/ACME_2025-1T_facts.json"),
            ("text-art", "views/parsed/acme/2025-1T__aaaaaaaaaaaa__222222222222.md"),
        ],
    )
    conn.commit()
    conn.close()

    docs = _from_directories([reports, parsed])
    assert docs["2025-1T"].facts == facts
    assert docs["2025-1T"].source_document_id == "current-doc"
    assert docs["2025-1T"].source_artifact_id == "text-art"
    assert docs["2025-1T"].facts_document_id == "current-doc"
    assert docs["2025-1T"].facts_artifact_id == "facts-art"


def test_later_parsed_view_beats_lexicographically_earlier_reports_md(tmp_path):
    from src.extract.pipeline import _resolve_source

    reports = tmp_path / "estate" / "views" / "reports" / "acme"
    parsed = tmp_path / "estate" / "views" / "parsed" / "acme"
    reports.mkdir(parents=True)
    parsed.mkdir(parents=True)
    (reports / "2025-1T.md").write_text("legacy compatibility text", encoding="utf-8")
    (parsed / "2025-1T__bbbbbbbbbbbb__cccccccccccc.md").write_text(
        "current parsed derivative", encoding="utf-8",
    )

    docs = _resolve_source(
        [reports, parsed], output_dir=tmp_path, period_filter=None,
        max_reports=10, cfg_path=None,
    )

    assert list(docs) == ["2025-1T"]
    assert docs["2025-1T"].text == "current parsed derivative"


def test_multiple_uncatalogued_parsed_versions_fail_loudly(tmp_path):
    import pytest
    from src.extract.pipeline import AmbiguousPeriodSourceError, _from_directories

    parsed = tmp_path / "estate" / "views" / "parsed" / "acme"
    parsed.mkdir(parents=True)
    (parsed / "2025-1T__aaaaaaaaaaaa__111111111111.md").write_text("old", encoding="utf-8")
    (parsed / "2025-1T__bbbbbbbbbbbb__222222222222.md").write_text("new", encoding="utf-8")

    with pytest.raises(AmbiguousPeriodSourceError, match="current version cannot be proven"):
        _from_directories([parsed])


def test_parsed_view_uses_catalog_current_document_not_filename_order(tmp_path):
    import sqlite3
    from src.extract.pipeline import _from_directories

    estate = tmp_path / "estate"
    parsed = estate / "views" / "parsed" / "acme"
    parsed.mkdir(parents=True)
    old_name = "2025-1T__zzzzzzzzzzzz__111111111111.md"
    current_name = "2025-1T__aaaaaaaaaaaa__222222222222.md"
    (parsed / old_name).write_text("superseded document", encoding="utf-8")
    (parsed / current_name).write_text("current document", encoding="utf-8")

    conn = sqlite3.connect(estate / "catalog.db")
    conn.executescript(
        """
        CREATE TABLE documents(document_id TEXT PRIMARY KEY, period TEXT);
        CREATE TABLE artifacts(
            artifact_id TEXT PRIMARY KEY, document_id TEXT, role TEXT,
            format TEXT, path TEXT
        );
        CREATE TABLE document_derivations(
            output_artifact_id TEXT, created_at TEXT,
            processor_name TEXT, processor_version TEXT
        );
        CREATE TABLE source_records(
            source_key TEXT, source_record_id TEXT, current_document_id TEXT
        );
        CREATE TABLE source_record_versions(
            document_id TEXT, source_key TEXT, source_record_id TEXT
        );
        """
    )
    conn.executemany("INSERT INTO documents VALUES(?, '2025-1T')", [("old",), ("current",)])
    conn.executemany(
        "INSERT INTO artifacts VALUES(?, ?, 'parsed_text', 'md', ?)",
        [
            ("old-art", "old", f"views/parsed/acme/{old_name}"),
            ("cur-art", "current", f"views/parsed/acme/{current_name}"),
        ],
    )
    conn.executemany(
        "INSERT INTO document_derivations VALUES(?, ?, 'parser', '1')",
        [("old-art", "2026-01-01T00:00:00+00:00"),
         ("cur-art", "2026-02-01T00:00:00+00:00")],
    )
    conn.execute("INSERT INTO source_records VALUES('ir', 'q1', 'current')")
    conn.executemany(
        "INSERT INTO source_record_versions VALUES(?, 'ir', 'q1')",
        [("old",), ("current",)],
    )
    conn.commit()
    conn.close()

    docs = _from_directories([parsed])
    assert docs["2025-1T"].text == "current document"
