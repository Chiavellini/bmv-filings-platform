"""Durable, separate acquisition ledger for the news corpus."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.news.models import NewsArticle, NewsMembership


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class NewsCatalogStats:
    articles: int
    memberships: int
    by_mode: dict[str, int]


class NewsCatalog:
    """Deduplicated news ledger, intentionally independent from filing catalog/index files."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.migrate()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS articles (
                article_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                canonical_url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                publisher TEXT NOT NULL,
                published_at TEXT NOT NULL,
                language TEXT,
                author TEXT,
                summary TEXT NOT NULL DEFAULT '',
                content_mode TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                markdown_path TEXT,
                indexed INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(provider, provider_id),
                UNIQUE(content_sha256, publisher, published_at)
            );
            CREATE TABLE IF NOT EXISTS article_companies (
                article_id TEXT NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
                company TEXT NOT NULL,
                industry TEXT,
                matched_alias TEXT NOT NULL,
                alias_kind TEXT NOT NULL,
                PRIMARY KEY(article_id, company)
            );
            CREATE INDEX IF NOT EXISTS idx_news_published ON articles(published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_news_company ON article_companies(company);
        """)
        self.conn.commit()

    def upsert(self, article: NewsArticle, memberships: Iterable[NewsMembership], *,
               markdown_path: str | None = None, indexed: bool = False) -> str:
        """Upsert one article and memberships; URL/content duplicates resolve to the first record."""
        existing = self.conn.execute(
            "SELECT article_id FROM articles WHERE canonical_url=? OR "
            "(content_sha256=? AND publisher=? AND published_at=?)",
            (article.canonical_url, article.content_sha256, article.publisher, article.published_at),
        ).fetchone()
        article_id = existing["article_id"] if existing else article.article_id
        now = _now()
        self.conn.execute("""
            INSERT INTO articles(article_id, provider, provider_id, canonical_url, title, publisher,
                published_at, language, author, summary, content_mode, content_sha256, markdown_path,
                indexed, metadata_json, discovered_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(article_id) DO UPDATE SET
                title=excluded.title, publisher=excluded.publisher, published_at=excluded.published_at,
                language=excluded.language, author=excluded.author, summary=excluded.summary,
                content_mode=excluded.content_mode, content_sha256=excluded.content_sha256,
                markdown_path=COALESCE(excluded.markdown_path, articles.markdown_path),
                indexed=MAX(articles.indexed, excluded.indexed), metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
        """, (article_id, article.provider, article.provider_id, article.canonical_url, article.title,
              article.publisher, article.published_at, article.language, article.author, article.summary,
              article.content_mode, article.content_sha256, markdown_path, int(indexed),
              json.dumps(article.metadata, ensure_ascii=False, sort_keys=True), now, now))
        self.conn.execute("DELETE FROM article_companies WHERE article_id=?", (article_id,))
        self.conn.executemany("""
            INSERT INTO article_companies(article_id, company, industry, matched_alias, alias_kind)
            VALUES(?,?,?,?,?)
        """, [(article_id, m.company, m.industry, m.matched_alias, m.alias_kind)
               for m in memberships])
        self.conn.commit()
        return article_id

    def stats(self) -> NewsCatalogStats:
        articles = self.conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"]
        memberships = self.conn.execute("SELECT count(*) AS n FROM article_companies").fetchone()["n"]
        modes = {r["content_mode"]: r["n"] for r in self.conn.execute(
            "SELECT content_mode, count(*) AS n FROM articles GROUP BY content_mode"
        ).fetchall()}
        return NewsCatalogStats(articles, memberships, modes)
