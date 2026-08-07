"""Build Alpha Go index fixtures whose completeness can be varied per test."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import struct

SEMANTIC_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def build_alpha_index(
    path: Path,
    *,
    documents: int = 3,
    chunks_per_document: int = 2,
    dim: int = 384,
    model: str | None = SEMANTIC_MODEL,
    embeddings: bool = True,
    fts: bool = True,
    embedding_dim_meta: int | None = -1,
) -> Path:
    """Write an index with the production schema.

    ``embedding_dim_meta`` defaults to the sentinel ``-1`` meaning "record ``dim``";
    pass ``None`` to omit the key entirely, as the shipped 10-document index does.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY, company TEXT NOT NULL, period TEXT,
            doc_type TEXT, title TEXT, markdown_path TEXT, content_sha256 TEXT
        );
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY,
            doc_id TEXT NOT NULL REFERENCES documents(doc_id),
            ordinal INTEGER NOT NULL, text TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, text);
        CREATE TABLE embeddings (
            chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id),
            dim INTEGER NOT NULL, vector BLOB NOT NULL
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    vector = struct.pack(f"{dim}f", *([0.1] * dim))
    for d in range(documents):
        doc_id = f"acme/2024-{d}T"
        connection.execute(
            "INSERT INTO documents (doc_id, company, period, doc_type, title,"
            " markdown_path, content_sha256) VALUES (?,?,?,?,?,?,?)",
            (doc_id, "acme", f"2024-{d}T", "report", "t", f"views/parsed/{d}.md", "s"),
        )
        for c in range(chunks_per_document):
            chunk_id = f"{doc_id}#{c}"
            connection.execute(
                "INSERT INTO chunks (chunk_id, doc_id, ordinal, text) VALUES (?,?,?,?)",
                (chunk_id, doc_id, c, f"chunk {c} text"),
            )
            if fts:
                connection.execute(
                    "INSERT INTO chunks_fts (chunk_id, text) VALUES (?,?)",
                    (chunk_id, f"chunk {c} text"),
                )
            if embeddings:
                connection.execute(
                    "INSERT INTO embeddings (chunk_id, dim, vector) VALUES (?,?,?)",
                    (chunk_id, dim, vector),
                )
    if model is not None:
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('embedding_model', ?)", (model,)
        )
    recorded_dim = dim if embedding_dim_meta == -1 else embedding_dim_meta
    if recorded_dim is not None:
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('embedding_dim', ?)",
            (str(recorded_dim),),
        )
    connection.commit()
    connection.close()
    return path
