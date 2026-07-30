"""Materialize verified news records into a standalone corpus and search index."""
from __future__ import annotations

from pathlib import Path

from src.corpus.manifest import Document
from src.index.build import add_document_to_index
from src.news.aliases import CompanyAliasResolver
from src.news.catalog import NewsCatalog
from src.news.models import NewsArticle, NewsMembership


def ingest_article(article: NewsArticle, *, resolver: CompanyAliasResolver, catalog: NewsCatalog,
                   corpus_dir: str | Path, store=None, config: dict | None = None,
                   embedder=None) -> dict:
    """Verify company mentions, persist entitlement-safe text, and optionally index immediately.

    Articles with no verified company alias are recorded nowhere in a company corpus.  This is the
    critical guard against attaching general market coverage to an issuer because it merely looks
    semantically similar.
    """
    matches = resolver.resolve(article.content_text)
    # A metadata/link provider may have verified the phrase in full publisher text while only
    # returning a headline and canonical link.  Accept that evidence only if it names one of
    # our controlled aliases exactly (never an opaque provider-supplied company identifier).
    provider_aliases = article.metadata.get("provider_matched_aliases", [])
    matches.extend(resolver.resolve_verified_aliases(provider_aliases))
    deduped = {match.company: match for match in matches}
    matches = list(deduped.values())
    memberships = [NewsMembership(m.company, m.industry, m.alias, m.kind) for m in matches]
    if not memberships:
        return {"article_id": article.article_id, "indexed": False, "memberships": 0,
                "reason": "no verified company alias"}

    primary = memberships[0]
    corpus_dir = Path(corpus_dir)
    dest = corpus_dir / primary.company
    dest.mkdir(parents=True, exist_ok=True)
    markdown_path = dest / f"{article.article_id.rsplit('/', 1)[-1]}.md"
    markdown_path.write_text(article.content_text, encoding="utf-8")
    catalog_id = catalog.upsert(article, memberships, markdown_path=str(markdown_path),
                                indexed=store is not None and embedder is not None)
    doc = Document(
        doc_id=catalog_id, company=primary.company, period=article.published_at[:10],
        doc_type="news_article", title=article.title, source_url=article.canonical_url,
        pdf_path=None, markdown_path=str(markdown_path), language=article.language,
        industry=primary.industry,
        memberships=[{"company": m.company, "industry": m.industry} for m in memberships],
        extra={"news": True, "provider": article.provider, "provider_id": article.provider_id,
               "publisher": article.publisher, "published_at": article.published_at,
               "content_mode": article.content_mode, "author": article.author,
               "matched_aliases": [m.matched_alias for m in memberships]},
        source_path=article.canonical_url, source_format="news",
        content_sha256=article.content_sha256,
    )
    chunks = 0
    if store is not None and embedder is not None:
        chunks = add_document_to_index(store, doc, article.content_text, config or {}, embedder=embedder)["chunks"]
    return {"article_id": catalog_id, "indexed": bool(chunks), "memberships": len(memberships),
            "chunks": chunks, "markdown_path": str(markdown_path)}
