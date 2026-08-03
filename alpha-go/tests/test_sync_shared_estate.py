from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from src.corpus.manifest import CorpusManifest, Document, load_manifest, save_manifest
from src.index.build import add_document_to_index
from src.index.embeddings import HashingEmbedder
from src.index.store import IndexStore


SCRIPT = Path(__file__).parents[1] / "scripts" / "sync_shared_estate.py"
SPEC = importlib.util.spec_from_file_location("alpha_go_sync_shared_estate", SCRIPT)
assert SPEC and SPEC.loader
sync_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync_module
SPEC.loader.exec_module(sync_module)


def _write_config(path: Path, *, dimension: int = 8) -> None:
    path.write_text(
        "\n".join(
            [
                "index:",
                "  embedding_backend: hashing",
                f"  hashing_dim: {dimension}",
                "  strict_runtime: false",
                "  chunk:",
                "    target_chars: 80",
                "    overlap_chars: 10",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _create_estate(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            company TEXT NOT NULL,
            period TEXT,
            doc_type TEXT NOT NULL,
            title TEXT NOT NULL,
            language TEXT,
            source_url TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            project TEXT NOT NULL,
            role TEXT NOT NULL,
            format TEXT NOT NULL,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL
        );
        CREATE TABLE memberships (
            document_id TEXT NOT NULL,
            company TEXT NOT NULL,
            industry TEXT,
            PRIMARY KEY(document_id, company)
        );
        CREATE TABLE source_record_versions (
            document_id TEXT PRIMARY KEY,
            document_family_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            supersedes_document_id TEXT
        );
        """
    )
    return conn


def _add_version(
    conn: sqlite3.Connection,
    directory: Path,
    *,
    document_id: str,
    family: str,
    version: int,
    text: str,
    supersedes: str | None = None,
) -> Path:
    markdown = directory / f"{document_id}.md"
    markdown.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(markdown.read_bytes()).hexdigest()
    conn.execute(
        """INSERT INTO documents(
               document_id,company,period,doc_type,title,language,source_url
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            document_id,
            "gap",
            "2025-1T",
            "quarterly_release",
            f"GAP 2025-1T v{version}",
            "es",
            f"https://example.test/{document_id}",
        ),
    )
    conn.execute(
        """INSERT INTO artifacts(
               artifact_id,document_id,project,role,format,path,sha256
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            f"artifact-{document_id}",
            document_id,
            "derivatives",
            "search_text",
            "md",
            str(markdown),
            digest,
        ),
    )
    conn.execute(
        "INSERT INTO memberships(document_id,company,industry) VALUES(?,?,?)",
        (document_id, "gap", "airports"),
    )
    conn.execute(
        """INSERT INTO source_record_versions(
               document_id,document_family_id,version,supersedes_document_id
           ) VALUES(?,?,?,?)""",
        (document_id, family, version, supersedes),
    )
    conn.commit()
    return markdown


def _create_index(path: Path, *, dimension: int = 8) -> None:
    store = IndexStore(path)
    store.connect()
    store.migrate()
    store.set_meta("embedding_model", "hashing")
    store.set_meta("embedding_dim", str(dimension))
    store.commit()
    store.close()


@pytest.fixture
def projection(tmp_path):
    estate_path = tmp_path / "estate.db"
    estate = _create_estate(estate_path)
    family = "gap:quarterly_release:2025-1T:es:release"
    _add_version(
        estate,
        tmp_path,
        document_id="estate-v1",
        family=family,
        version=1,
        text="legacyfreight declined during the quarter",
    )
    corpus = tmp_path / "corpus"
    index = tmp_path / "alpha.db"
    config = tmp_path / "runtime.yaml"
    _create_index(index)
    _write_config(config)
    yield {
        "estate_conn": estate,
        "estate": estate_path,
        "family": family,
        "corpus": corpus,
        "index": index,
        "config": config,
        "tmp": tmp_path,
    }
    estate.close()


def _sync(projection, *document_ids: str, config: Path | None = None):
    return sync_module.sync_shared_estate(
        estate=projection["estate"],
        corpus=projection["corpus"],
        db=projection["index"],
        config=config or projection["config"],
        apply=True,
        document_ids=document_ids,
    )


def _logical_index(path: Path) -> tuple[str, ...]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return tuple(conn.iterdump())
    finally:
        conn.close()


def test_same_estate_event_twice_is_unchanged(projection):
    first = _sync(projection, "estate-v1")
    manifest_path = projection["corpus"] / "manifest.json"
    before_manifest = manifest_path.read_bytes()
    before_index = _logical_index(projection["index"])

    second = _sync(projection, "estate-v1")

    assert first.changed == 1
    assert first.indexed == 1
    assert second.changed == 0
    assert second.unchanged == 1
    assert second.indexed == 0
    assert not second.manifest_changed
    assert manifest_path.read_bytes() == before_manifest
    assert _logical_index(projection["index"]) == before_index


def test_broad_reconcile_removes_shared_row_when_search_artifact_is_missing(
    projection,
):
    _sync(projection, "estate-v1")
    (projection["tmp"] / "estate-v1.md").unlink()

    result = _sync(projection)

    assert result.eligible == 0
    assert result.removed == 1
    assert load_manifest(projection["corpus"]).documents == []
    connection = sqlite3.connect(projection["index"])
    assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    connection.close()


def test_read_only_projection_audit_counts_index_drift(projection):
    _sync(projection, "estate-v1")
    healthy = sync_module.audit_shared_estate_projection(
        estate=projection["estate"],
        corpus=projection["corpus"],
        db=projection["index"],
    )
    assert healthy.healthy
    assert healthy.eligible_families == 1
    assert healthy.indexed_eligible_documents == 1

    connection = sqlite3.connect(projection["index"])
    connection.execute("DELETE FROM embeddings")
    connection.execute("DELETE FROM chunks_fts")
    connection.execute("DELETE FROM chunks")
    connection.execute("DELETE FROM document_companies")
    connection.execute("DELETE FROM documents")
    connection.commit()
    connection.close()

    drift = sync_module.audit_shared_estate_projection(
        estate=projection["estate"],
        corpus=projection["corpus"],
        db=projection["index"],
    )
    assert not drift.healthy
    assert drift.missing_from_index == 1
    assert drift.samples["missing_from_index"]


def test_search_rows_require_verified_markdown_not_raw_artifact(tmp_path):
    estate_path = tmp_path / "estate.db"
    connection = _create_estate(estate_path)
    raw = tmp_path / "filing.json"
    raw.write_text('{"facts": []}', encoding="utf-8")
    connection.execute(
        """INSERT INTO documents(
               document_id,company,period,doc_type,title,language,source_url
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            "raw-xbrl",
            "gap",
            "2025-1T",
            "regulatory_filing",
            "Raw XBRL",
            "es",
            "https://example.test/xbrl",
        ),
    )
    connection.execute(
        """INSERT INTO artifacts(
               artifact_id,document_id,project,role,format,path,sha256
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            "artifact-raw",
            "raw-xbrl",
            "acquisition",
            "raw_xbrl",
            "json",
            str(raw),
            hashlib.sha256(raw.read_bytes()).hexdigest(),
        ),
    )
    connection.execute(
        "INSERT INTO memberships(document_id,company,industry) VALUES(?,?,?)",
        ("raw-xbrl", "gap", "airports"),
    )
    connection.commit()
    assert sync_module._rows(estate_path) == []

    markdown = tmp_path / "filing.md"
    markdown.write_text("searchable filing", encoding="utf-8")
    connection.execute(
        """INSERT INTO artifacts(
               artifact_id,document_id,project,role,format,path,sha256
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            "artifact-md",
            "raw-xbrl",
            "root",
            "search_text",
            "md",
            str(markdown),
            "0" * 64,
        ),
    )
    connection.commit()
    assert sync_module._rows(estate_path) == []

    connection.execute(
        "UPDATE artifacts SET sha256=? WHERE artifact_id='artifact-md'",
        (hashlib.sha256(markdown.read_bytes()).hexdigest(),),
    )
    connection.commit()
    connection.close()
    assert [row.document_id for row in sync_module._rows(estate_path)] == [
        "raw-xbrl"
    ]


def test_explicit_missing_document_is_not_a_successful_noop(
    projection, capsys
):
    with pytest.raises(sync_module.ProjectionResolutionError):
        _sync(projection, "missing-document")

    exit_code = sync_module.main(
        [
            "--estate",
            str(projection["estate"]),
            "--corpus",
            str(projection["corpus"]),
            "--db",
            str(projection["index"]),
            "--config",
            str(projection["config"]),
            "--apply",
            "--json",
            "--document-id",
            "missing-document",
        ]
    )
    response = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert response["status"] == "unresolved"


def test_concurrent_narrow_projections_retain_both_manifest_entries(
    projection,
):
    _add_version(
        projection["estate_conn"],
        projection["tmp"],
        document_id="estate-other",
        family="gap:news:other",
        version=1,
        text="secondfamily searchable text",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda document_id: _sync(projection, document_id),
                ("estate-v1", "estate-other"),
            )
        )

    assert all(result.eligible == 1 for result in results)
    assert len(load_manifest(projection["corpus"]).documents) == 2
    connection = sqlite3.connect(projection["index"])
    assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    connection.close()


def test_corrected_version_replaces_document_chunks_fts_and_embeddings(projection):
    _sync(projection, "estate-v1")
    first_document = load_manifest(projection["corpus"]).documents[0]
    _add_version(
        projection["estate_conn"],
        projection["tmp"],
        document_id="estate-v2",
        family=projection["family"],
        version=2,
        supersedes="estate-v1",
        text="correctedpassengers increased during the quarter",
    )

    corrected = _sync(projection, "estate-v2")
    manifest = load_manifest(projection["corpus"])

    assert corrected.changed == 1
    assert corrected.indexed == 1
    assert len(manifest.documents) == 1
    current = manifest.documents[0]
    assert current.doc_id == first_document.doc_id
    assert current.extra["estate_document_id"] == "estate-v2"
    assert current.extra["estate_document_version"] == 2

    conn = sqlite3.connect(projection["index"])
    conn.row_factory = sqlite3.Row
    try:
        documents = conn.execute(
            "SELECT doc_id,content_sha256 FROM documents"
        ).fetchall()
        assert [row["doc_id"] for row in documents] == [current.doc_id]
        assert documents[0]["content_sha256"] == current.content_sha256
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'correctedpassengers'"
        ).fetchone()[0] > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'legacyfreight'"
        ).fetchone()[0] == 0
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == chunk_count
        assert {
            row[0]
            for row in conn.execute("SELECT DISTINCT dim FROM embeddings")
        } == {8}
    finally:
        conn.close()

    repeated = _sync(projection, "estate-v2")
    assert repeated.changed == 0
    assert repeated.indexed == 0


def test_family_projection_removes_legacy_hash_derived_search_row(projection):
    markdown = projection["tmp"] / "estate-v1.md"
    digest = hashlib.sha256(markdown.read_bytes()).hexdigest()
    legacy = Document(
        doc_id=f"estate/root/{digest[:20]}",
        company="gap",
        period="2025-1T",
        doc_type="quarterly_release",
        title="Legacy projection",
        source_url=None,
        pdf_path=None,
        markdown_path=str(markdown),
        language="es",
        industry="airports",
        memberships=[{"company": "gap", "industry": "airports"}],
        extra={
            "shared_estate": True,
            "estate_document_id": "estate-v1",
            "origin_project": "root",
        },
        source_path=str(markdown),
        source_format="markdown",
        content_sha256=digest,
    )
    save_manifest(CorpusManifest([legacy]), projection["corpus"])
    store = IndexStore(projection["index"])
    store.connect()
    config = {
        "index": {
            "embedding_backend": "hashing",
            "hashing_dim": 8,
            "chunk": {"target_chars": 80, "overlap_chars": 10},
        }
    }
    add_document_to_index(
        store,
        legacy,
        markdown.read_text(encoding="utf-8"),
        config,
        embedder=HashingEmbedder(8),
    )
    store.close()

    result = _sync(projection, "estate-v1")
    current = load_manifest(projection["corpus"]).documents[0]

    assert result.indexed == 1
    assert current.doc_id != legacy.doc_id
    conn = sqlite3.connect(projection["index"])
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM documents WHERE doc_id=?", (legacy.doc_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id=?", (legacy.doc_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT COUNT(*) FROM embeddings e
               JOIN chunks c USING(chunk_id) WHERE c.doc_id=?""",
            (legacy.doc_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM documents WHERE doc_id=?", (current.doc_id,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("mismatch", ["model", "dimension"])
def test_embedding_runtime_mismatch_refuses_without_mutation(projection, mismatch):
    _sync(projection, "estate-v1")
    _add_version(
        projection["estate_conn"],
        projection["tmp"],
        document_id="estate-v2",
        family=projection["family"],
        version=2,
        supersedes="estate-v1",
        text="correctedpassengers increased during the quarter",
    )
    mismatch_config = projection["tmp"] / "mismatch.yaml"
    _write_config(mismatch_config, dimension=16 if mismatch == "dimension" else 8)
    if mismatch == "model":
        store = IndexStore(projection["index"])
        store.connect()
        store.set_meta("embedding_model", "some-other-model")
        store.commit()
        store.close()

    manifest_path = projection["corpus"] / "manifest.json"
    before_manifest = manifest_path.read_bytes()
    before_index = _logical_index(projection["index"])

    with pytest.raises(RuntimeError, match=f"embedding {mismatch} mismatch"):
        _sync(projection, "estate-v2", config=mismatch_config)

    assert manifest_path.read_bytes() == before_manifest
    assert _logical_index(projection["index"]) == before_index
    assert load_manifest(projection["corpus"]).documents[0].extra[
        "estate_document_id"
    ] == "estate-v1"


def test_cli_accepts_repeatable_document_ids_and_explicit_config(tmp_path):
    config = tmp_path / "runtime.yaml"
    args = sync_module._parser().parse_args(
        [
            "--config",
            str(config),
            "--document-id",
            "doc-one",
            "--document-id",
            "doc-two",
        ]
    )
    assert args.config == config
    assert args.document_id == ["doc-one", "doc-two"]


def test_broad_rows_collapse_parser_copies_and_prefer_alpha_original(tmp_path):
    estate_path = tmp_path / "estate.db"
    conn = _create_estate(estate_path)
    root_markdown = tmp_path / "root.md"
    alpha_markdown = tmp_path / "alpha.md"
    root_markdown.write_text("passengers increased", encoding="utf-8")
    alpha_markdown.write_text("pasajeros aumentaron", encoding="utf-8")
    for document_id, project, markdown in (
        ("legacy-root", "root", root_markdown),
        ("alpha-go:gap/2025-1T", "alpha-go", alpha_markdown),
    ):
        conn.execute(
            """INSERT INTO documents(
                   document_id,company,period,doc_type,title,language,source_url
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                document_id,
                "gap",
                "2025-1T",
                "quarterly_release",
                "GAP 2025-1T",
                "es",
                "https://example.test/gap",
            ),
        )
        conn.execute(
            """INSERT INTO artifacts(
                   artifact_id,document_id,project,role,format,path,sha256
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                f"artifact-{document_id}",
                document_id,
                project,
                "search_text",
                "md",
                str(markdown),
                hashlib.sha256(markdown.read_bytes()).hexdigest(),
            ),
        )
        conn.execute(
            "INSERT INTO memberships(document_id,company,industry) VALUES(?,?,?)",
            (document_id, "gap", "airports"),
        )
    conn.commit()
    conn.close()

    rows = sync_module._rows(estate_path)

    assert len(rows) == 1
    assert rows[0].document_id == "alpha-go:gap/2025-1T"
    assert sync_module._stable_search_doc_id(
        rows[0].document_family_id, rows[0].document_id
    ) == "gap/2025-1T"


def test_derived_html_is_retained_as_original_source(tmp_path):
    estate_path = tmp_path / "estate.db"
    conn = _create_estate(estate_path)
    markdown = _add_version(
        conn,
        tmp_path,
        document_id="estate-v1",
        family="gap:regulatory:2025-1T",
        version=1,
        text="regulatory filing text",
    )
    html = tmp_path / "filing.html"
    html.write_text("<html><body>Original filing</body></html>", encoding="utf-8")
    conn.execute(
        """INSERT INTO artifacts(
               artifact_id,document_id,project,role,format,path,sha256
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            "artifact-html",
            "estate-v1",
            "soft",
            "derived",
            "html",
            str(html),
            hashlib.sha256(html.read_bytes()).hexdigest(),
        ),
    )
    conn.commit()
    conn.close()

    original, source_format = sync_module._original_artifact(
        estate_path, "estate-v1"
    )

    assert markdown.is_file()
    assert original == html
    assert source_format == "html"


def test_news_projection_preserves_publisher_link_as_source(tmp_path):
    estate_path = tmp_path / "estate.db"
    conn = _create_estate(estate_path)
    markdown = tmp_path / "article.md"
    markdown.write_text("# News\n\nPassenger traffic increased.", encoding="utf-8")
    digest = hashlib.sha256(markdown.read_bytes()).hexdigest()
    conn.execute(
        """INSERT INTO documents(
               document_id,company,period,doc_type,title,language,source_url
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            "alpha-go:news/article-1",
            "gap",
            "2025-07-01",
            "news_article",
            "GAP traffic",
            "en",
            "https://publisher.test/gap-traffic",
        ),
    )
    conn.execute(
        """INSERT INTO artifacts(
               artifact_id,document_id,project,role,format,path,sha256
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            "artifact-news",
            "alpha-go:news/article-1",
            "alpha-go-news",
            "search_text",
            "md",
            str(markdown),
            digest,
        ),
    )
    conn.execute(
        "INSERT INTO memberships(document_id,company,industry) VALUES(?,?,?)",
        ("alpha-go:news/article-1", "gap", "airports"),
    )
    conn.commit()
    conn.close()

    row = sync_module._rows(estate_path)[0]
    document = sync_module._document_from_row(estate_path, row)

    assert document.doc_id == "news/article-1"
    assert document.source_path == "https://publisher.test/gap-traffic"
    assert document.source_format == "news"
