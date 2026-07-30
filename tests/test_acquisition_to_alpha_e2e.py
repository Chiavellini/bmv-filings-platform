"""Clean-fixture proof: acquisition -> derivative -> Alpha search projection."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

from src.acquisition.models import FetchedArtifact, SourceRecord
from src.acquisition.writer import EstateWriter
from src.consumers.alpha_go import AlphaGoProjectionConsumer
from src.consumers.derivatives import PdfMarkdownDerivativeConsumer
from src.consumers.outbox import OutboxDispatcher


ROOT = Path(__file__).resolve().parents[1]


def _source() -> SourceRecord:
    return SourceRecord(
        source_key="ir",
        source_record_id="acme-q1",
        issuer_slug="acme",
        document_type="quarterly_release",
        title="ACME Q1",
        period_year=2025,
        period_quarter=1,
        url="https://example.test/acme-q1.pdf",
        language="en",
    )


def _event(database: Path, event_type: str, document_id: str) -> dict:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT event_id,event_type,aggregate_id,payload_json
           FROM outbox WHERE event_type=? AND aggregate_id=?
           ORDER BY created_at DESC LIMIT 1""",
        (event_type, document_id),
    ).fetchone()
    conn.close()
    assert row is not None
    return {
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "aggregate_id": row["aggregate_id"],
        "payload": json.loads(row["payload_json"]),
    }


def _fts_hits(index: Path, term: str) -> int:
    conn = sqlite3.connect(index)
    count = conn.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
        (term,),
    ).fetchone()[0]
    conn.close()
    return count


def test_corrected_acquisition_replaces_old_alpha_search_text(tmp_path):
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    corpus = tmp_path / "alpha-corpus"
    index = tmp_path / "alpha-index.db"
    config = tmp_path / "hashing-runtime.yaml"
    config.write_text(
        "\n".join(
            [
                "index:",
                "  embedding_backend: hashing",
                "  strict_runtime: false",
                "  hashing_dim: 32",
                "  chunk:",
                "    target_chars: 200",
                "    overlap_chars: 20",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def parser(path: Path) -> str:
        raw = path.read_bytes()
        if b"corrected" in raw:
            return "# ACME\n\nPassengers increased sharply.\n"
        return "# ACME\n\nLegacy riders wording.\n"

    projection = AlphaGoProjectionConsumer(
        database,
        project_root=ROOT / "alpha-go",
        corpus=corpus,
        index_db=index,
        config=config,
        python_executable=sys.executable,
        timeout_seconds=120,
    )

    with EstateWriter(database, estate_root) as writer:
        first = writer.store_fetched(
            FetchedArtifact(
                _source(),
                b"%PDF-1.7 first",
                filename="release.pdf",
                media_type="application/pdf",
            ),
            memberships=(
                {
                    "company": "acme",
                    "industry": "transport",
                    "alpha_go": True,
                },
            ),
        )

    with PdfMarkdownDerivativeConsumer(
        database,
        estate_root,
        parser=parser,
        parser_name="fixture-parser",
        parser_version="1",
    ) as derivatives:
        with OutboxDispatcher(
            database, [derivatives, projection]
        ) as dispatcher:
            first_report = dispatcher.run(max_deliveries=10)
    assert (first_report.claimed, first_report.succeeded) == (2, 2)
    assert _fts_hits(index, "legacy") > 0
    assert _fts_hits(index, "passengers") == 0

    with EstateWriter(database, estate_root) as writer:
        corrected = writer.store_fetched(
            FetchedArtifact(
                _source(),
                b"%PDF-1.7 corrected",
                filename="release.pdf",
                media_type="application/pdf",
            ),
            memberships=(
                {
                    "company": "acme",
                    "industry": "transport",
                    "alpha_go": True,
                },
            ),
        )
    assert corrected.supersedes_document_id == first.document_id

    with PdfMarkdownDerivativeConsumer(
        database,
        estate_root,
        parser=parser,
        parser_name="fixture-parser",
        parser_version="1",
    ) as derivatives:
        with OutboxDispatcher(
            database, [derivatives, projection]
        ) as dispatcher:
            corrected_report = dispatcher.run(max_deliveries=10)
    assert (corrected_report.claimed, corrected_report.succeeded) == (2, 2)

    conn = sqlite3.connect(index)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    conn.close()
    assert _fts_hits(index, "legacy") == 0
    assert _fts_hits(index, "passengers") > 0

    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["documents"]) == 1
    assert (
        manifest["documents"][0]["extra"]["estate_document_id"]
        == corrected.document_id
    )


def test_same_hash_late_alpha_pin_reprojects_current_facets(tmp_path):
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    corpus = tmp_path / "alpha-corpus"
    index = tmp_path / "alpha-index.db"
    config = tmp_path / "hashing-runtime.yaml"
    config.write_text(
        "\n".join(
            [
                "index:",
                "  embedding_backend: hashing",
                "  strict_runtime: false",
                "  hashing_dim: 32",
                "  chunk:",
                "    target_chars: 200",
                "    overlap_chars: 20",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def parser(_path: Path) -> str:
        return "# ACME\n\nPassenger traffic increased.\n"

    with EstateWriter(database, estate_root) as writer:
        first = writer.store_fetched(
            FetchedArtifact(
                _source(),
                b"%PDF-1.7 stable",
                filename="release.pdf",
                media_type="application/pdf",
            ),
            memberships=(
                {
                    "company": "acme",
                    "industry": "transport",
                    "soft": True,
                },
            ),
        )

    projection = AlphaGoProjectionConsumer(
        database,
        project_root=ROOT / "alpha-go",
        corpus=corpus,
        index_db=index,
        config=config,
        python_executable=sys.executable,
        timeout_seconds=120,
    )
    with PdfMarkdownDerivativeConsumer(
        database,
        estate_root,
        parser=parser,
        parser_name="fixture-parser",
        parser_version="1",
    ) as derivatives:
        with OutboxDispatcher(
            database, [derivatives, projection]
        ) as dispatcher:
            initial = dispatcher.run(max_deliveries=10)
    assert (initial.succeeded, initial.skipped) == (1, 1)
    assert not index.exists()

    with EstateWriter(database, estate_root) as writer:
        adopted = writer.store_fetched(
            FetchedArtifact(
                _source(),
                b"%PDF-1.7 stable",
                filename="release.pdf",
                media_type="application/pdf",
            ),
            memberships=(
                {
                    "company": "acme",
                    "industry": "airports",
                    "alpha_go": True,
                    "soft": True,
                },
            ),
        )
    assert adopted.created is False
    assert adopted.document_id == first.document_id

    with PdfMarkdownDerivativeConsumer(
        database,
        estate_root,
        parser=parser,
        parser_name="fixture-parser",
        parser_version="1",
    ) as derivatives:
        with OutboxDispatcher(
            database, [derivatives, projection]
        ) as dispatcher:
            routed = dispatcher.run(max_deliveries=10)
    assert (routed.claimed, routed.succeeded) == (2, 2)
    assert _fts_hits(index, "passenger") > 0

    conn = sqlite3.connect(index)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert conn.execute(
        "SELECT company,industry FROM document_companies"
    ).fetchall() == [("acme", "airports")]
    conn.close()


def test_html_news_is_derived_and_searchable_without_pdf_parser(tmp_path):
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    corpus = tmp_path / "alpha-corpus"
    index = tmp_path / "alpha-index.db"
    config = tmp_path / "hashing-runtime.yaml"
    config.write_text(
        "\n".join(
            [
                "index:",
                "  embedding_backend: hashing",
                "  strict_runtime: false",
                "  hashing_dim: 32",
                "  chunk:",
                "    target_chars: 200",
                "    overlap_chars: 20",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    news_source = SourceRecord(
        source_key="news-feed",
        source_record_id="article-1",
        issuer_slug="acme",
        document_type="news_article",
        title="ACME announces airport investment",
        url="https://news.example.test/article-1",
        language="en",
    )
    with EstateWriter(database, estate_root) as writer:
        stored = writer.store_fetched(
            FetchedArtifact(
                news_source,
                (
                    b"<html><head><title>Airport investment</title></head>"
                    b"<body><script>ignore me</script>"
                    b"<p>ACME completed the Aeropuerto merger.</p></body></html>"
                ),
                filename="article-1.html",
                media_type="text/html",
            ),
            memberships=(
                {
                    "company": "acme",
                    "industry": "airports",
                    "alpha_go": True,
                },
            ),
        )
    projection = AlphaGoProjectionConsumer(
        database,
        project_root=ROOT / "alpha-go",
        corpus=corpus,
        index_db=index,
        config=config,
        python_executable=sys.executable,
        timeout_seconds=120,
    )
    with PdfMarkdownDerivativeConsumer(
        database,
        estate_root,
        parser=lambda _path: (_ for _ in ()).throw(
            AssertionError("HTML must not enter the PDF parser")
        ),
        parser_name="fixture-parser",
        parser_version="1",
    ) as derivatives:
        with OutboxDispatcher(
            database, [derivatives, projection]
        ) as dispatcher:
            report = dispatcher.run(max_deliveries=10)
    assert (report.claimed, report.succeeded) == (2, 2)
    assert _fts_hits(index, "aeropuerto") > 0
    assert _fts_hits(index, "merger") > 0

    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"][0]["doc_type"] == "news_article"
    assert (
        manifest["documents"][0]["extra"]["estate_document_id"]
        == stored.document_id
    )
    assert manifest["documents"][0]["original_format"] == "html"
