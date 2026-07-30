"""IndexStore — the single SQLite database backing the search index.

Schema (see docs/architecture/system_design.md for the authoritative ER description):

    documents(doc_id PK, company, period, doc_type, title, source_url,
              pdf_path, markdown_path, language)
    chunks(chunk_id PK, doc_id FK, ordinal, text, char_start, char_end, heading)
    chunks_fts            -- standard FTS5 table over chunk text (BM25 keyword search)
    embeddings(chunk_id PK FK, dim, vector BLOB)   -- float32 vectors for cosine search
    meta(key PK, value)   -- schema version, embedding model name, build timestamp

Keyword search needs no extra dependency (stdlib sqlite3 ships FTS5 + bm25()). Semantic
search stores vectors here and ranks them in-process (numpy) — adequate for a local corpus.

``chunks_fts`` is a standard (self-contained) FTS5 table rather than an external-content
one: the index is rebuilt wholesale, so the simpler form is more robust and the modest extra
text storage is irrelevant at local-corpus scale.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

# Authoritative DDL — kept here so docs and code can't drift.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    company       TEXT NOT NULL,
    period        TEXT,
    doc_type      TEXT,
    title         TEXT,
    source_url    TEXT,
    pdf_path      TEXT,
    markdown_path TEXT,
    language      TEXT,
    industry      TEXT,
    source_path   TEXT,
    source_format TEXT,
    content_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   TEXT PRIMARY KEY,
    doc_id     TEXT NOT NULL REFERENCES documents(doc_id),
    ordinal    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end   INTEGER NOT NULL,
    heading    TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    text
);
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id),
    dim      INTEGER NOT NULL,
    vector   BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS document_companies (
    doc_id   TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    company  TEXT NOT NULL,
    industry TEXT,
    PRIMARY KEY (doc_id, company)
);
CREATE INDEX IF NOT EXISTS idx_docco_company ON document_companies(company);
"""

# Tables the index must contain (used by migration checks / tests).
TABLES = ("documents", "chunks", "chunks_fts", "embeddings", "meta", "document_companies")


class IndexStore:
    """Thin wrapper over the SQLite index database."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    # -- connection / schema ------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Open (and memoize) the DB connection, creating the file/parent if needed."""
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: Streamlit reruns each session on a fresh thread while
            # @st.cache_resource shares one store — safe here because access is read-only at
            # query time and writes happen in single-threaded build scripts.
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
        return self._conn

    def migrate(self) -> None:
        """Apply SCHEMA_SQL and record SCHEMA_VERSION in meta (idempotent)."""
        conn = self.connect()
        conn.executescript(SCHEMA_SQL)
        self._ensure_column("documents", "industry", "TEXT")
        self._ensure_column("documents", "source_path", "TEXT")
        self._ensure_column("documents", "source_format", "TEXT")
        self._ensure_column("documents", "content_sha256", "TEXT")
        # Backfill the many-to-many membership table from legacy single-company rows. Safe to
        # re-run: the (doc_id, company) PK makes existing rows (incl. multi-company ones) win.
        conn.execute(
            "INSERT OR IGNORE INTO document_companies (doc_id, company, industry) "
            "SELECT doc_id, company, industry FROM documents"
        )
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        conn.commit()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        """Add ``column`` to ``table`` if missing (idempotent ALTER for pre-existing DBs).

        ``CREATE TABLE IF NOT EXISTS`` never adds columns to an already-created table, so a DB
        built before a new column needs this to gain it without a full rebuild/re-embed.
        """
        conn = self.connect()
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- writes -------------------------------------------------------------

    def clear(self) -> None:
        """Remove all rows (for a clean wholesale rebuild). Keeps the schema."""
        conn = self.connect()
        for table in ("embeddings", "chunks_fts", "chunks", "document_companies", "documents"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()

    def upsert_document(self, doc) -> None:
        """Insert/replace a documents row from a corpus ``Document``."""
        self.connect().execute(
            """INSERT OR REPLACE INTO documents
               (doc_id, company, period, doc_type, title, source_url, pdf_path,
                markdown_path, language, industry, source_path, source_format,
                content_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (doc.doc_id, doc.company, doc.period, doc.doc_type, doc.title,
             doc.source_url, doc.pdf_path, doc.markdown_path, doc.language,
             getattr(doc, "industry", None), getattr(doc, "source_path", None),
             getattr(doc, "source_format", None), getattr(doc, "content_sha256", None)),
        )

    def set_document_companies(self, doc_id: str, memberships: "list[dict]") -> None:
        """Replace a document's corpus memberships (the many-to-many ``document_companies`` rows).

        ``memberships`` is a list of ``{"company": slug, "industry": tag_or_None}`` — one per
        corpus the document belongs to (primary first, by convention). Delete-then-insert so a
        re-index cleanly reflects the current set; the ``(doc_id, company)`` PK dedupes repeats.
        """
        conn = self.connect()
        conn.execute("DELETE FROM document_companies WHERE doc_id=?", (doc_id,))
        seen: set = set()
        for m in memberships or []:
            company = m.get("company")
            if not company or company in seen:
                continue
            seen.add(company)
            conn.execute(
                "INSERT OR REPLACE INTO document_companies (doc_id, company, industry) "
                "VALUES (?, ?, ?)",
                (doc_id, company, m.get("industry")),
            )

    def document_companies(self, doc_id: str) -> list:
        """The corpus memberships of one document as ``[{"company", "industry"}, ...]``."""
        rows = self.connect().execute(
            "SELECT company, industry FROM document_companies WHERE doc_id=? ORDER BY company",
            (doc_id,),
        ).fetchall()
        return [{"company": r["company"], "industry": r["industry"]} for r in rows]

    def insert_chunk(self, chunk) -> None:
        """Insert a chunks row from an index ``Chunk``."""
        self.connect().execute(
            """INSERT OR REPLACE INTO chunks
               (chunk_id, doc_id, ordinal, text, char_start, char_end, heading)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chunk.chunk_id, chunk.doc_id, chunk.ordinal, chunk.text,
             chunk.char_start, chunk.char_end, chunk.heading),
        )

    def upsert_embedding(self, chunk_id: str, dim: int, blob: bytes) -> None:
        self.connect().execute(
            "INSERT OR REPLACE INTO embeddings (chunk_id, dim, vector) VALUES (?, ?, ?)",
            (chunk_id, dim, blob),
        )

    def set_meta(self, key: str, value: str) -> None:
        self.connect().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value))
        )

    def commit(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    # -- reads --------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.connect().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def document_rows(self, where: str = "", params: "list | tuple" = ()) -> list:
        """Every ``documents`` row (optionally scoped by a ``SearchFilters.to_sql`` clause).

        The universe for the corpus-wide literal mention scan: the linear occurrence view scans
        each of these documents' full markdown with a substring matcher, so it stays Ctrl+F-exact
        even where the FTS tokenizer would miss a match (e.g. "returnable" inside "returnables").
        ``where`` references ``documents`` columns (``company``, ``period``, …) so a scoped search
        only materializes its subset.
        """
        sql = (
            "SELECT doc_id, company, period, doc_type, title, source_url, markdown_path, "
            "       source_path, source_format, content_sha256, "
            "       (SELECT GROUP_CONCAT(dc.company) FROM document_companies dc "
            "        WHERE dc.doc_id = documents.doc_id) AS companies "
            "FROM documents"
        )
        if where:
            sql += f" WHERE {where}"
        return self.connect().execute(sql, list(params)).fetchall()

    def count(self, table: str) -> int:
        if table not in TABLES:
            raise ValueError(f"unknown table: {table}")
        return self.connect().execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
