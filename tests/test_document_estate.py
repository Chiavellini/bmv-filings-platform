from pathlib import Path

from src.shared.document_estate import DocumentEstate, EstateDocument


def test_document_estate_catalogs_projects_artifacts_and_memberships(tmp_path):
    artifact = tmp_path / "2025-1T.pdf"
    artifact.write_bytes(b"%PDF shared fixture")
    with DocumentEstate(tmp_path / "estate.db") as estate:
        doc = EstateDocument("doc-1", "acme", "2025-1T", "quarterly_release", "Q1")
        estate.upsert_document(doc, [{"company": "acme", "industry": "food"}])
        estate.add_project_record("alpha-go", "acme/2025-1T", doc.document_id)
        first = estate.add_artifact(doc.document_id, artifact, project="alpha-go", role="original")
        second = estate.add_artifact(doc.document_id, artifact, project="alpha-go", role="original")
        estate.commit()

        assert first == second
        assert estate.stats() == {
            "documents": 1, "artifacts": 1, "companies": 1, "projects": 1,
            "bytes": artifact.stat().st_size, "duplicate_hashes": 0,
        }


def test_duplicate_hash_view_reports_zero_copy_candidates(tmp_path):
    one, two = tmp_path / "one.pdf", tmp_path / "two.pdf"
    one.write_bytes(b"same")
    two.write_bytes(b"same")
    with DocumentEstate(tmp_path / "estate.db") as estate:
        for doc_id, path, project in (("one", one, "root"), ("two", two, "soft")):
            estate.upsert_document(EstateDocument(
                doc_id, "acme", "2025-1T", "quarterly_release", doc_id,
            ))
            estate.add_artifact(doc_id, path, project=project, role="original")
        estate.commit()
        duplicate = estate.conn.execute("SELECT * FROM artifact_duplicates").fetchone()
        assert duplicate["copies"] == 2
        assert duplicate["logical_bytes"] == 8


def test_same_path_refresh_does_not_transfer_artifact_ownership(tmp_path):
    artifact = tmp_path / "shared.md"
    artifact.write_text("shared text", encoding="utf-8")
    with DocumentEstate(tmp_path / "estate.db") as estate:
        for document_id in ("root-doc", "alpha-doc"):
            estate.upsert_document(EstateDocument(
                document_id, "acme", "2026-1T", "quarterly_release", document_id,
            ))
        estate.add_artifact("root-doc", artifact, project="root", role="derived")
        estate.add_artifact("alpha-doc", artifact, project="alpha-go", role="search_text")
        artifact_id = estate.artifact_id(artifact)
        row = estate.conn.execute(
            "SELECT document_id,project,role FROM artifacts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        assert dict(row) == {
            "document_id": "root-doc", "project": "root", "role": "derived",
        }


def test_project_refresh_prunes_stale_rows_but_keeps_shared_documents(tmp_path):
    artifact = tmp_path / "shared.md"
    artifact.write_text("shared text", encoding="utf-8")
    with DocumentEstate(tmp_path / "estate.db") as estate:
        estate.upsert_document(EstateDocument(
            "root-doc", "acme", "2026-1T", "quarterly_release", "Shared",
        ))
        estate.add_project_record("root", "shared.md", "root-doc")
        estate.add_project_record("alpha-go", "estate/shared", "root-doc")
        estate.add_artifact("root-doc", artifact, project="root", role="derived")
        estate.reset_project("root")

        assert estate.conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE project='root'"
        ).fetchone()[0] == 0
        assert estate.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE document_id='root-doc'"
        ).fetchone()[0] == 1


def test_consolidation_is_hard_linked_and_reversible(tmp_path):
    from scripts.consolidate_document_estate import (
        apply, audit, rollback, verify_content_objects,
    )

    one, two = tmp_path / "one.pdf", tmp_path / "two.pdf"
    one.write_bytes(b"%PDF exact duplicate")
    two.write_bytes(b"%PDF exact duplicate")
    db = tmp_path / "estate.db"
    with DocumentEstate(db) as estate:
        for doc_id, path, project in (("one", one, "root"), ("two", two, "soft")):
            estate.upsert_document(EstateDocument(
                doc_id, "acme", "2026-1T", "quarterly_release", doc_id,
            ))
            estate.add_project_record(project, path.name, doc_id)
            estate.add_artifact(doc_id, path, project=project, role="original")
        estate.commit()

    before = audit(db)
    assert before["replacements"] == 1
    run_id, result = apply(db, tmp_path / "blobs", max_replacements=1)
    assert result["completed_actions"] == 1
    assert one.stat().st_ino == two.stat().st_ino
    assert one.read_bytes() == b"%PDF exact duplicate"
    assert verify_content_objects(db) == {
        "content_objects": 1, "verified_artifact_paths": 2, "errors": 0,
    }

    result = rollback(db, run_id)
    assert result == {"run_id": run_id, "restored": 1, "errors": 0}
    assert one.stat().st_ino != two.stat().st_ino
    assert two.read_bytes() == b"%PDF exact duplicate"


def test_segment_builder_prefers_shared_parsed_report_view(tmp_path, monkeypatch):
    import scripts.build_segments as build

    shared = tmp_path / "estate" / "views" / "reports"
    legacy = tmp_path / "legacy"
    (shared / "acme").mkdir(parents=True)
    (shared / "acme" / "2025-1T.md").write_text("report")
    monkeypatch.setattr(build, "SHARED_REPORTS_DIR", shared)
    monkeypatch.setattr(build, "REPORTS_DIR", legacy)

    assert build._report_cache("acme") == shared / "acme"
    assert build._report_cache("acme", force_download=True) == legacy / "acme"
    assert build._report_cache("unknown") == legacy / "unknown"


def test_earnings_walkforward_import_groups_artifacts_and_builds_search_text(tmp_path):
    import json

    from scripts.build_document_estate import import_earnings_walkforward

    walkforward = tmp_path / "walkforward"
    xbrl = walkforward / "xbrl"
    xbrl.mkdir(parents=True)
    (walkforward / "filings_meta.json").write_text(json.dumps([{
        "ticker": "AC",
        "slug": "ac",
        "filed_date": "22/07/2026 20:30",
        "zip_url": "https://example.test/ac.zip",
    }]), encoding="utf-8")
    (xbrl / "AC_2026-2T_facts.json").write_text('{"Revenue": 10}', encoding="utf-8")
    (xbrl / "AC_2026-2T_mdna.html").write_text(
        "<section><p>Ventas crecieron.</p></section>", encoding="utf-8"
    )

    database = tmp_path / "estate.db"
    derived = tmp_path / "derived"
    with DocumentEstate(database) as estate:
        assert import_earnings_walkforward(estate, walkforward, derived) == 1
        document = estate.conn.execute(
            "SELECT * FROM documents WHERE document_id='earnings:ac:2026-2T'"
        ).fetchone()
        artifacts = estate.conn.execute(
            "SELECT role,format FROM artifacts WHERE document_id=? ORDER BY role",
            (document["document_id"],),
        ).fetchall()

    assert document["company"] == "ac"
    assert document["doc_type"] == "regulatory_filing"
    assert document["published_at"] == "2026-07-22T20:30"
    assert [tuple(row) for row in artifacts] == [
        ("original", "html"),
        ("search_text", "md"),
        ("structured_facts", "json"),
    ]
    assert "Ventas crecieron." in (derived / "earnings" / "ac" / "2026-2T.md").read_text()


def test_soft_backfill_search_text_stays_attached_to_original_filing(tmp_path):
    import json

    from scripts.build_document_estate import import_soft_xbrl_backfill

    html = tmp_path / "reports" / "acme" / "xbrl" / "ACME_2021-2T_mdna.html"
    html.parent.mkdir(parents=True)
    html.write_text("<p>Pasajeros aumentaron.</p>", encoding="utf-8")
    report = tmp_path / "backfill.json"
    report.write_text(json.dumps({"successes": [{
        "slug": "acme",
        "filing": {"period": "2021-2T"},
        "artifacts": {"mdna": str(html)},
    }]}), encoding="utf-8")

    with DocumentEstate(tmp_path / "estate.db") as estate:
        estate.upsert_document(EstateDocument(
            "filing", "acme", "2021-2T", "regulatory_filing", "ACME filing",
        ))
        estate.add_project_record("soft", "acme/xbrl/original", "filing")
        estate.add_artifact("filing", html, project="soft", role="original")
        assert import_soft_xbrl_backfill(estate, report, tmp_path / "derived") == 1
        rows = estate.conn.execute(
            "SELECT document_id,project,role FROM artifacts ORDER BY project"
        ).fetchall()

    assert [tuple(row) for row in rows] == [
        ("filing", "soft", "original"),
        ("filing", "soft-xbrl-backfill", "search_text"),
    ]
