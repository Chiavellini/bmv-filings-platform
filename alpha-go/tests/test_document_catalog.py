from __future__ import annotations

from pathlib import Path

import pytest

from src.catalog.store import CatalogDocument, CatalogStore
from src.corpus.manifest import CorpusManifest, Document
from src.sources.base import SourceRecord


def _manifest_doc() -> Document:
    return Document(
        doc_id="acme/2025-1T", company="acme", period="2025-1T",
        doc_type="quarterly_release", title="Acme 2025-1T",
        source_url="https://bmv.example/acme-2025-1t.zip", pdf_path=None,
        markdown_path="data/corpus/acme/2025-1T.md", language="es", industry="food",
        memberships=[{"company": "acme", "industry": "food"}],
        extra={"source": "BMV XBRL", "source_record_id": "ACME:quarterly:2025-1T",
               "ticker": "ACME", "filed_date": "15/04/2025"},
        original_path="data/corpus/acme/2025-1T.html", original_format="html",
        content_sha256="abc123",
    )


def test_catalog_reconciles_manifest_and_index_status(tmp_path: Path):
    doc = _manifest_doc()
    with CatalogStore(tmp_path / "documents.db") as catalog:
        assert catalog.reconcile_manifest(
            CorpusManifest([doc]), indexed_doc_ids={doc.doc_id}
        ) == 1
        saved = catalog.get(doc.doc_id)
        assert saved is not None
        assert saved.status == "indexed"
        assert saved.source == "BMV XBRL"
        assert saved.source_record_id == "ACME:quarterly:2025-1T"
        assert saved.fiscal_year == 2025 and saved.fiscal_quarter == 1
        assert saved.mime_type == "text/html"
        assert saved.memberships == ({"company": "acme", "industry": "food"},)
        assert catalog.stats().documents == 1


def test_manifest_migration_does_not_treat_shared_landing_url_as_record_id(tmp_path: Path):
    first = _manifest_doc()
    first.extra = {}
    second = Document(**{**first.__dict__, "doc_id": "acme/2025-2T", "period": "2025-2T"})
    with CatalogStore(tmp_path / "documents.db") as catalog:
        assert catalog.reconcile_manifest(CorpusManifest([first, second])) == 2
        assert catalog.get(first.doc_id).source_record_id == first.doc_id
        assert catalog.get(second.doc_id).source_record_id == second.doc_id


def test_manifest_only_reconcile_does_not_demote_indexed_record(tmp_path: Path):
    doc = _manifest_doc()
    with CatalogStore(tmp_path / "documents.db") as catalog:
        catalog.reconcile_manifest(CorpusManifest([doc]), indexed_doc_ids={doc.doc_id})
        catalog.reconcile_manifest(CorpusManifest([doc]))
        assert catalog.get(doc.doc_id).status == "indexed"


def test_catalog_upsert_preserves_discovery_timestamp_and_replaces_memberships(tmp_path: Path):
    base = CatalogDocument(
        doc_id="acme/annual/2024/x", company="acme", doc_type="annual_report",
        title="Acme annual", source="issuer_ir", source_record_id="annual-2024",
        canonical_url="https://example.test/annual.pdf", status="discovered",
        memberships=({"company": "acme", "industry": "food"},),
    )
    with CatalogStore(tmp_path / "documents.db") as catalog:
        catalog.upsert_many([base])
        first_discovered = catalog._conn.execute(
            "SELECT discovered_at FROM documents WHERE doc_id=?", (base.doc_id,)
        ).fetchone()[0]
        catalog.upsert_many([CatalogDocument(
            **{**base.__dict__, "status": "downloaded",
               "memberships": ({"company": "globex", "industry": "retail"},)}
        )])
        assert catalog.get(base.doc_id).status == "downloaded"
        memberships = catalog.get(base.doc_id).memberships
        assert memberships == ({"company": "globex", "industry": "retail"},)
        assert catalog._conn.execute(
            "SELECT discovered_at FROM documents WHERE doc_id=?", (base.doc_id,)
        ).fetchone()[0] == first_discovered


def test_rejected_catalog_record_requires_reason():
    with pytest.raises(ValueError, match="rejection_reason"):
        CatalogDocument(
            doc_id="x", company="acme", doc_type="other", title="Bad",
            source="ir", source_record_id="bad", status="rejected",
        )


def test_catalog_accepts_discovery_before_file_download(tmp_path: Path):
    source = SourceRecord(
        source="BMV XBRL", source_record_id="AC:annual:2024-FY", company="ac",
        ticker="AC", doc_type="annual_report", title="AC 2024-FY",
        canonical_url="https://bmv.example/ac.zip", filed_at="01/04/2025",
        period="2024-FY", language="es", mime_type="application/zip",
        document_family_id="ac:annual_report:2024-FY", metadata={"industry": "beverage"},
    )
    record = CatalogDocument.from_source_record(source)
    with CatalogStore(tmp_path / "documents.db") as catalog:
        catalog.upsert_many([record])
        saved = catalog.get(source.proposed_doc_id)
        assert saved.status == "discovered"
        assert saved.original_path is None
        assert saved.memberships[0]["industry"] == "beverage"
