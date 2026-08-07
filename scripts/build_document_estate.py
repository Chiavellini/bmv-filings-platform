#!/usr/bin/env python3
"""Catalog the monorepo's local corpora and build a zero-copy compatibility view."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.shared.document_estate import (  # noqa: E402
    DocumentEstate,
    EstateDocument,
    resolve_artifact_path,
)
from src.shared.paths import ESTATE_BRIDGE  # noqa: E402
from src.shared.report_index import infer_period_label  # noqa: E402

DEFAULT_DB = ESTATE_BRIDGE.catalog_path
DEFAULT_VIEW = ESTATE_BRIDGE.reports_view_dir
DEFAULT_EARNINGS = ROOT / "earnings" / "data" / "walkforward"
DEFAULT_DERIVED = ESTATE_BRIDGE.estate_root / "derived"
DEFAULT_SOFT_BACKFILL = (
    ESTATE_BRIDGE.estate_root / "expansion" / "soft_xbrl_backfill.json"
)
SUPPORTED = {".pdf", ".md", ".json", ".html", ".htm", ".gz"}


def _company_aliases() -> dict[str, str]:
    path = ROOT / "configs" / "company_aliases.yaml"
    config = {}
    if path.is_file():
        import yaml

        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        alias: canonical
        for canonical, aliases in (config.get("canonical") or {}).items()
        for alias in aliases or []
    }


COMPANY_ALIASES = _company_aliases()


def _expanded_memberships(
    memberships: list[dict] | None, fallback_company: str
) -> list[dict]:
    rows = list(memberships or [{"company": fallback_company, "industry": None}])
    seen = {row.get("company") for row in rows}
    for row in list(rows):
        canonical = COMPANY_ALIASES.get(row.get("company"))
        if canonical and canonical not in seen:
            rows.append({"company": canonical, "industry": row.get("industry")})
            seen.add(canonical)
    return rows


def _safe_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _doc_type(path: Path, period: str | None) -> str:
    signal = path.as_posix().casefold()
    if "news" in path.parts:
        return "news_article"
    if "xbrl" in path.parts or path.suffix.lower() in {".json", ".html", ".gz"}:
        return "regulatory_filing"
    if period and period.endswith("-FY"):
        return "annual_report"
    return "quarterly_release" if period else "unclassified"


def import_tree(estate: DocumentEstate, project: str, root: Path) -> int:
    if not root.exists():
        return 0
    estate.reset_project(project)
    count = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED):
        rel = path.relative_to(root)
        company = rel.parts[0] if len(rel.parts) > 1 else "unknown"
        period = infer_period_label(path.stem)
        document_id = f"legacy:{_safe_id(project, rel.as_posix())}"
        estate.upsert_document(EstateDocument(
            document_id=document_id, company=company, period=period,
            doc_type=_doc_type(path, period), title=path.stem,
            metadata={"legacy_project": project, "legacy_relative_path": rel.as_posix()},
        ), _expanded_memberships(None, company))
        estate.add_project_record(project, rel.as_posix(), document_id)
        estate.add_artifact(document_id, path, project=project,
                            role="original" if path.suffix.lower() == ".pdf" else "derived")
        count += 1
        if count % 500 == 0:
            estate.commit()
            print(f"{project}: catalogued {count} artifacts…", flush=True)
    estate.commit()
    return count


def _resolve_alpha(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else ROOT / "alpha-go" / path


def import_alpha_manifest(estate: DocumentEstate) -> int:
    estate.reset_project("alpha-go")
    manifest_path = ROOT / "alpha-go" / "data" / "corpus" / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = raw.get("documents", raw if isinstance(raw, list) else [])
    for row in rows:
        document_id = f"alpha-go:{row['doc_id']}"
        paths: list[tuple[Path, str]] = []
        seen = set()
        for field, role in (("original_path", "original"), ("pdf_path", "original"),
                            ("markdown_path", "search_text"), ("source_path", "source")):
            path = _resolve_alpha(row.get(field))
            if path and path.exists() and path.resolve() not in seen:
                seen.add(path.resolve())
                paths.append((path, role))

        # Shared-estate imports deliberately retain root/soft paths. Link Alpha's record to
        # the document that already owns that path instead of inventing a duplicate document.
        existing = None
        for path, _role in paths:
            existing = estate.artifact_document(path)
            if existing:
                break
        if existing:
            estate.add_project_record("alpha-go", row["doc_id"], existing)
            estate.add_memberships(
                existing,
                _expanded_memberships(row.get("memberships"), row["company"]),
                fallback_company=row["company"],
            )
            continue

        estate.upsert_document(EstateDocument(
            document_id=document_id, company=row["company"], period=row.get("period"),
            doc_type=row.get("doc_type") or "unclassified", title=row.get("title") or row["doc_id"],
            language=row.get("language"), source_url=row.get("source_url"), metadata=row.get("extra") or {},
        ), _expanded_memberships(row.get("memberships"), row["company"]))
        estate.add_project_record("alpha-go", row["doc_id"], document_id)
        for path, role in paths:
            estate.add_artifact(document_id, path, project="alpha-go", role=role)
    estate.commit()
    return len(rows)


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace a generated text artifact without mutating a possible hard-link in place."""
    encoded = text.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)


def import_earnings_walkforward(
    estate: DocumentEstate,
    walkforward: Path = DEFAULT_EARNINGS,
    derived_root: Path = DEFAULT_DERIVED,
) -> int:
    """Import the independently downloaded earnings walk-forward regulatory filings.

    Each filing owns its facts JSON and original MD&A HTML.  A compact, plain-text Markdown
    derivative is generated centrally so search consumers do not need to know the earnings
    subproject's internal layout.
    """
    estate.reset_project("earnings")
    metadata_path = walkforward / "filings_meta.json"
    xbrl_root = walkforward / "xbrl"
    if not metadata_path.is_file() or not xbrl_root.is_dir():
        return 0

    imported = 0
    for row in json.loads(metadata_path.read_text(encoding="utf-8")):
        ticker = str(row["ticker"])
        slug = str(row["slug"])
        period = str(row.get("period") or "2026-2T").upper()
        file_ticker = ticker.replace("&", "")
        facts = xbrl_root / f"{file_ticker}_{period}_facts.json"
        html = xbrl_root / f"{file_ticker}_{period}_mdna.html"
        if not facts.is_file() and not html.is_file():
            continue

        filed_at = None
        if row.get("filed_date"):
            try:
                filed_at = datetime.strptime(
                    row["filed_date"], "%d/%m/%Y %H:%M"
                ).isoformat(timespec="minutes")
            except ValueError:
                filed_at = row["filed_date"]
        document_id = f"earnings:{slug}:{period}"
        estate.upsert_document(EstateDocument(
            document_id=document_id,
            company=slug,
            period=period,
            doc_type="regulatory_filing",
            title=f"{ticker} official BMV filing {period}",
            language="es",
            source_url=row.get("zip_url"),
            published_at=filed_at,
            metadata={
                "legacy_project": "earnings",
                "ticker": ticker,
                "filing_kind": "quarterly",
            },
        ))
        estate.add_project_record("earnings", f"{slug}/{period}", document_id)
        if facts.is_file():
            estate.add_artifact(
                document_id, facts, project="earnings", role="structured_facts"
            )
        if html.is_file():
            estate.add_artifact(document_id, html, project="earnings", role="original")
            text = BeautifulSoup(
                html.read_text(encoding="utf-8", errors="replace"), "html.parser"
            ).get_text("\n", strip=True)
            if text:
                markdown = derived_root / "earnings" / slug / f"{period}.md"
                _atomic_write_text(
                    markdown,
                    f"# {ticker} official BMV filing — {period}\n\n"
                    f"Source: {row.get('zip_url') or 'BMV XBRL archive'}\n\n{text}\n",
                )
                estate.add_artifact(
                    document_id, markdown, project="earnings", role="search_text"
                )
        imported += 1
        if imported % 20 == 0:
            estate.commit()
            print(f"earnings: catalogued {imported} filings…", flush=True)
    estate.commit()
    return imported


def import_soft_xbrl_backfill(
    estate: DocumentEstate,
    report_path: Path = DEFAULT_SOFT_BACKFILL,
    derived_root: Path = DEFAULT_DERIVED,
) -> int:
    """Attach searchable text to the official XBRL filings acquired by the backfill."""
    estate.reset_project("soft-xbrl-backfill")
    if not report_path.is_file():
        return 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    imported = 0
    for row in payload.get("successes", []) or []:
        html_value = (row.get("artifacts") or {}).get("mdna")
        if not html_value:
            continue
        html = Path(html_value)
        if not html.is_file():
            continue
        owner = estate.artifact_document(html)
        if not owner:
            continue
        slug = row["slug"]
        period = row["filing"]["period"]
        text = BeautifulSoup(
            html.read_text(encoding="utf-8", errors="replace"), "html.parser"
        ).get_text("\n", strip=True)
        if not text:
            continue
        markdown = derived_root / "soft-xbrl-backfill" / slug / f"{period}.md"
        _atomic_write_text(markdown, text)
        estate.add_project_record(
            "soft-xbrl-backfill", f"{slug}/{period}", owner
        )
        estate.add_artifact(
            owner, markdown, project="soft-xbrl-backfill", role="search_text"
        )
        imported += 1
    estate.commit()
    return imported


def import_news(estate: DocumentEstate) -> int:
    estate.reset_project("alpha-go-news")
    db = ROOT / "alpha-go" / "data" / "news" / "catalog.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM articles").fetchall()
    for row in rows:
        memberships = [dict(r) for r in conn.execute(
            "SELECT company,industry FROM article_companies WHERE article_id=?", (row["article_id"],))]
        company = memberships[0]["company"] if memberships else "unknown"
        document_id = f"alpha-go:{row['article_id']}"
        estate.upsert_document(EstateDocument(
            document_id=document_id, company=company, period=row["published_at"][:10],
            doc_type="news_article", title=row["title"], language=row["language"],
            source_url=row["canonical_url"], published_at=row["published_at"],
            metadata={"publisher": row["publisher"], "provider": row["provider"],
                      "content_mode": row["content_mode"]},
        ), _expanded_memberships(memberships, company))
        estate.add_project_record("alpha-go-news", row["article_id"], document_id)
        path = _resolve_alpha(row["markdown_path"])
        if path and path.exists():
            estate.add_artifact(document_id, path, project="alpha-go-news", role="search_text")
    conn.close()
    estate.commit()
    return len(rows)


def _link(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() and destination.resolve() == source.resolve():
        return
    if destination.exists() or destination.is_symlink():
        return
    destination.symlink_to(source.resolve())


def build_view(estate: DocumentEstate, view: Path) -> int:
    """Mirror legacy report trees, then add Alpha-only quarterly/annual artifacts."""
    linked = 0
    for project, source_root in (("root", ROOT / "data" / "reports"),
                                 ("soft", ROOT / "soft" / "data" / "reports")):
        rows = estate.conn.execute("SELECT path FROM artifacts WHERE project=?", (project,)).fetchall()
        for row in rows:
            source = resolve_artifact_path(row["path"], catalog_dir=estate.path.parent)
            try:
                rel = source.relative_to(source_root)
            except ValueError:
                continue
            destination = view / rel
            if destination.exists() and destination.resolve() != source.resolve():
                destination = destination.with_name(f"{destination.stem}__{project}{destination.suffix}")
            before = destination.exists() or destination.is_symlink()
            _link(source, destination)
            linked += int(not before and destination.is_symlink())

    rows = estate.conn.execute("""
        SELECT d.document_id,d.company,d.period,d.doc_type,a.path,a.format
        FROM documents d JOIN artifacts a ON a.document_id=d.document_id
        WHERE a.project='alpha-go' AND d.doc_type IN ('quarterly_release','annual_report')
          AND d.period IS NOT NULL AND a.format IN ('pdf','md')
    """).fetchall()
    for row in rows:
        source = resolve_artifact_path(row["path"], catalog_dir=estate.path.parent)
        short = hashlib.sha256(row["document_id"].encode()).hexdigest()[:8]
        destination = view / row["company"] / f"{row['period']}__alpha_{short}{source.suffix.lower()}"
        before = destination.exists() or destination.is_symlink()
        _link(source, destination)
        linked += int(not before and destination.is_symlink())
    return linked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--view", type=Path, default=DEFAULT_VIEW)
    args = parser.parse_args(argv)
    with DocumentEstate(args.db) as estate:
        print(f"root artifacts={import_tree(estate, 'root', ROOT / 'data' / 'reports')}")
        print(f"soft artifacts={import_tree(estate, 'soft', ROOT / 'soft' / 'data' / 'reports')}")
        print(f"soft XBRL backfill search texts={import_soft_xbrl_backfill(estate)}")
        print(f"earnings filings={import_earnings_walkforward(estate)}")
        print(f"alpha manifest={import_alpha_manifest(estate)}")
        print(f"news articles={import_news(estate)}")
        print(f"new compatibility links={build_view(estate, args.view)}")
        print(json.dumps(estate.stats(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
