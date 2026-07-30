"""Ingest — fetch + parse IR sources into the corpus.

This wraps the VENDORED download + parse phases; it adds no new download/parse logic.
Reused entrypoints:
  - src.download.downloader:download_from_ir   (IR-page crawl)
  - src.parse.parse_pdf:parse_pdf              (PDF -> aligned markdown + DocMeta)
  - src.shared.report_index:index_report_files (group files by canonical period)
  - src.shared.report_index:infer_period_label (filename -> canonical period)

Mirrors the parent's proven orchestration (scripts/fetch_company_reports.py): download ->
canonicalize-by-period -> parse each primary PDF to a sibling .md.

Phase-1 simplification: ``index_report_files`` picks ONE primary file per period, so this
yields one Document per period. Multiple doc-types per period is a later enhancement.
See docs/ROADMAP.md Phase 1.
"""
from __future__ import annotations

import re
import sys
import hashlib
import shutil
from pathlib import Path

from src.corpus.doc_types import load_taxonomy
from src.corpus.manifest import CorpusManifest, Document, load_manifest, save_manifest
from src.shared.report_index import index_report_files, infer_period_label, period_sort_key
from src.shared.paths import PROJECT_ROOT

_DEFAULT_FILE_PATTERN = (
    r"(?:quarterly|trimestral|reporte|informe|results?|report|earnings?|10-[kqKQ]).*\.pdf"
)


def _infer_doc_type(stem: str, taxonomy) -> str:
    """Infer a canonical doc_type from the filename via the taxonomy's keyword rules."""
    return taxonomy.infer(stem)


def _has_indexable_files(directory: Path) -> bool:
    """True if the directory already holds report files that map to a canonical period."""
    return any(
        infer_period_label(p.stem) is not None
        for p in directory.glob("*")
        if p.suffix.lower() in {".pdf", ".md"}
    )


def _maybe_download(ir_website: dict, dest: Path, *, language: str | None = None) -> None:
    """Best-effort IR download into ``dest``; network failures are logged, not fatal.

    Tolerating failure keeps offline/fixture runs working: ingest proceeds with whatever
    files are already on disk. ``language`` (+ optional ``lang_include``/``lang_exclude`` regexes
    in ``ir_website``) drives the downloader's English-only link filter — a no-op for ``es``.
    """
    url = ir_website.get("url")
    if not url:
        return
    from src.download.downloader import download_from_ir  # lazy: avoids import cost offline

    kwargs: dict = {
        "file_pattern": ir_website.get("pdf_link_pattern") or _DEFAULT_FILE_PATTERN,
        "max_reports": ir_website.get("max_reports", 50),
        "delay_ms": ir_website.get("delay_ms", 500),
        "language": language,
    }
    if "use_playwright" in ir_website:
        kwargs["use_playwright"] = ir_website["use_playwright"]
    if "floor_year" in ir_website:
        kwargs["floor_year"] = ir_website["floor_year"]
    if ir_website.get("lang_include"):
        kwargs["lang_include"] = ir_website["lang_include"]
    if ir_website.get("lang_exclude"):
        kwargs["lang_exclude"] = ir_website["lang_exclude"]
    try:
        download_from_ir(url, dest, **kwargs)
    except Exception as exc:  # noqa: BLE001 — network/site failures must not abort ingest
        print(f"WARN download {url}: {type(exc).__name__}: {exc}", file=sys.stderr)


def _has_annual_files(directory: Path) -> bool:
    """True when the corpus dir already holds an annual (``YYYY-FY``) report."""
    return any(
        (infer_period_label(p.stem) or "").endswith("-FY")
        for p in directory.glob("*")
        if p.suffix.lower() in {".pdf", ".md"}
    )


def _copy_local_inputs(source_config: dict, dest: Path) -> None:
    """Materialize optional local report mirrors into the portable corpus directory."""
    def resolve(raw: str | None) -> Path | None:
        if not raw:
            return None
        p = Path(raw).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p

    floor_year = int(source_config.get("floor_year", 0) or 0)
    pdf_dir = resolve(source_config.get("local_pdf_dir"))
    if pdf_dir and pdf_dir.is_dir():
        # Mirrors normally contain the raw PDF and its already-parsed Markdown side by side.
        # Preserve both: ``index_report_files`` prefers Markdown for the same report, avoiding an
        # expensive and potentially version-skewed reparse during an offline corpus rebuild.
        for src in sorted(p for p in pdf_dir.iterdir() if p.suffix.lower() in {".pdf", ".md"}):
            if floor_year and not any(str(y) in src.stem for y in range(floor_year, 2100)):
                continue
            shutil.copy2(src, dest / src.name)

    mdna_dir = resolve(source_config.get("local_mdna_dir"))
    if mdna_dir and mdna_dir.is_dir():
        for src in sorted(mdna_dir.glob("*_mdna.html")):
            match = re.search(r"(\d{4}-(?:[1-4]T|FY))", src.name, re.IGNORECASE)
            if not match or (floor_year and int(match.group(1)[:4]) < floor_year):
                continue
            period = match.group(1).upper()
            # Retain the raw filing for the HTML reader as well as its text-derived index input.
            # The canonical name lets a manifest record resolve its original without relying on a
            # sibling project layout.
            raw_dest = dest / f"{period}.html"
            shutil.copy2(src, raw_dest)
            from bs4 import BeautifulSoup
            text = BeautifulSoup(src.read_text(encoding="utf-8", errors="replace"),
                                 "html.parser").get_text("\n", strip=True)
            if text.strip():
                (dest / f"{period}.md").write_text(text, encoding="utf-8")

    # Some issuers have a complete Markdown/PDF corpus except for periods preserved only as an
    # XBRL MD&A HTML filing.  Keep those originals without replacing the curated Markdown input.
    original_html_dir = resolve(source_config.get("local_original_html_dir"))
    if original_html_dir and original_html_dir.is_dir():
        for src in sorted(original_html_dir.glob("*_mdna.html")):
            match = re.search(r"(\d{4}-(?:[1-4]T|FY))", src.name, re.IGNORECASE)
            if not match or (floor_year and int(match.group(1)[:4]) < floor_year):
                continue
            shutil.copy2(src, dest / f"{match.group(1).upper()}.html")


def _group_pdf(group, markdown_path: Path) -> Path | None:
    """Best physical PDF for a grouped report, including non-sibling source filenames."""
    sibling = markdown_path.with_suffix(".pdf")
    if sibling.exists():
        return sibling
    # A common IR pattern names the Markdown canonically (``2024-1T.md``) while retaining the
    # vendor's original PDF filename (``1T24_results.pdf``).  Both are already period-grouped.
    return sorted(group.pdf_paths, key=lambda p: p.name.lower())[0] if group.pdf_paths else None


def _original_artifact(dest: Path, period: str, pdf_path: Path | None) -> tuple[Path | None, str | None]:
    """Return the portable raw source for a document, preferring the original PDF over HTML."""
    if pdf_path and pdf_path.exists():
        return pdf_path, "pdf"
    html_path = dest / f"{period}.html"
    if html_path.exists():
        return html_path, "html"
    return None, None


def _existing_path(raw: str | None, corpus_dir: Path) -> Path | None:
    """Resolve a legacy manifest path without assuming the process working directory."""
    if not raw:
        return None
    p = Path(raw)
    candidates = (p, PROJECT_ROOT / p, corpus_dir / p)
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _backfill_original_artifact(doc: Document, corpus_dir: Path) -> None:
    """Upgrade legacy records so every already-present source is explicitly reader-addressable."""
    existing = _existing_path(doc.original_path, corpus_dir)
    if existing:
        return
    pdf = _existing_path(doc.pdf_path, corpus_dir)
    if pdf:
        doc.original_path, doc.original_format = str(pdf), "pdf"
        return
    html = corpus_dir / doc.company / f"{doc.period}.html"
    if html.exists():
        doc.original_path, doc.original_format = str(html), "html"


def _maybe_download_annual(annual: dict, dest: Path, *, language: str | None = None) -> None:
    """Best-effort annual-report download (``doc_kind="annual"``) into ``dest``; failures logged.

    Mirrors :func:`_maybe_download` but points at the source's ``annual_reports`` section and uses
    the annual selectors, so integrated/annual/20-F PDFs (dropped by the quarterly pass) are kept
    and land as ``<YYYY>-FY`` periods alongside the quarterly reports.
    """
    url = annual.get("url")
    if not url:
        return
    from src.download.downloader import download_from_ir  # lazy: avoids import cost offline

    kwargs: dict = {
        "file_pattern": annual.get("pdf_link_pattern") or _DEFAULT_FILE_PATTERN,
        "max_reports": annual.get("max_reports", 25),
        "delay_ms": annual.get("delay_ms", 500),
        "language": language,
        "doc_kind": "annual",
    }
    if "use_playwright" in annual:
        kwargs["use_playwright"] = annual["use_playwright"]
    if "floor_year" in annual:
        kwargs["floor_year"] = annual["floor_year"]
    try:
        download_from_ir(url, dest, **kwargs)
    except Exception as exc:  # noqa: BLE001 — network/site failures must not abort ingest
        print(f"WARN annual download {url}: {type(exc).__name__}: {exc}", file=sys.stderr)


# Common Spanish function words — frequent in any Spanish prose, near-absent in English. Used
# (with accent density) as a dependency-free language check for the ``require_language`` gate.
_ES_STOPWORDS = frozenset(
    "de la el los las que con para por una uno del en se su al mas como pero esta este durante "
    "fue son ha han sus sobre entre cada nuestra nuestro trimestre resultados ventas".split()
)


def _looks_spanish(text: str, *, sample_chars: int = 6000) -> bool:
    """Heuristic, dependency-free Spanish detector (no langdetect).

    Spanish prose is dominated by function words (de/la/que/…) and carries accent/¿¡ marks;
    English reports about Mexican issuers only sprinkle accents in proper nouns. We flag Spanish
    when the stopword share is high or accent density is clearly non-incidental.
    """
    s = text[:sample_chars].lower()
    words = re.findall(r"[a-záéíóúñ]+", s)
    if len(words) < 30:
        return False
    stop_ratio = sum(1 for w in words if w in _ES_STOPWORDS) / len(words)
    accent_density = sum(s.count(c) for c in "áéíóúñ¿¡") / max(len(s), 1)
    return stop_ratio > 0.10 or accent_density > 0.006


def _markdown_for(period: str, selected: Path, dest: Path, *, force: bool) -> tuple[Path, Path | None, dict]:
    """Return (markdown_path, pdf_path, meta) for a period's primary file.

    Parses a PDF to a sibling ``<period>.md`` when needed; reads an existing ``.md`` directly.
    ``meta`` carries DocMeta-derived hints (scale, section names) when a PDF was parsed.
    """
    if selected.suffix.lower() == ".md":
        sibling_pdf = selected.with_suffix(".pdf")
        return selected, (sibling_pdf if sibling_pdf.exists() else None), {}

    from src.parse.parse_pdf import parse_pdf  # lazy import (pdfplumber)

    md_dest = dest / f"{period}.md"
    if md_dest.exists() and not force:
        return md_dest, selected, {}

    markdown, _blocks, doc = parse_pdf(selected, with_meta=True)
    md_dest.write_text(markdown, encoding="utf-8")
    meta = {"scale": doc.scale, "sections": [name for name, _ in doc.sections]}
    return md_dest, selected, meta


def ingest_source(
    source_config: dict,
    corpus_dir: Path,
    *,
    force_download: bool = False,
    taxonomy=None,
) -> CorpusManifest:
    """Download + parse one source into the corpus and return the updated manifest.

    Args:
        source_config: one ``sources[]`` entry from configs/alpha_go.yaml
            (slug, company, ir_website, doc_types, language).
        corpus_dir: corpus root (default data/corpus/). Files land in ``<corpus_dir>/<slug>/``.
        force_download: refetch + reparse even if local files already exist.
    """
    corpus_dir = Path(corpus_dir)
    slug = source_config["slug"]
    company = source_config.get("company", slug)
    language = source_config.get("language", "es")
    industry = source_config.get("industry")
    taxonomy = taxonomy or load_taxonomy()
    ir_website = source_config.get("ir_website") or {}
    annual_reports = source_config.get("annual_reports") or {}
    # When set (e.g. "en"), drop parsed docs that read as another language (safety net for
    # bilingual IR sites the link filter can't fully separate). Off unless opted in.
    require_language = source_config.get("require_language")

    dest = corpus_dir / slug
    dest.mkdir(parents=True, exist_ok=True)
    _copy_local_inputs(source_config, dest)

    # Reuse-if-exists: only hit the network when we have nothing on disk (or force). Annual reports
    # are gated separately so adding an `annual_reports` block to an already-ingested company fetches
    # them on the next run without forcing a full quarterly re-download.
    if force_download or not _has_indexable_files(dest):
        _maybe_download(ir_website, dest, language=language)
    if annual_reports and (force_download or not _has_annual_files(dest)):
        _maybe_download_annual(annual_reports, dest, language=language)

    # A local mirror can deliberately expose only its maintained parsed documents.  This avoids
    # silently reparsing a large, older PDF-only archive while retaining the raw PDFs for readers.
    files = list(dest.glob("*.md"))
    if not source_config.get("local_md_only"):
        files += list(dest.glob("*.pdf"))
    groups = index_report_files(files)

    manifest = load_manifest(corpus_dir)
    by_id = {d.doc_id: d for d in manifest.documents}

    for period in sorted(groups, key=period_sort_key):
        group = groups[period]
        selected = group.selected_path
        if selected is None:
            continue
        markdown_path, pdf_path, meta = _markdown_for(period, selected, dest, force=force_download)
        pdf_path = pdf_path or _group_pdf(group, markdown_path)
        original_path, original_format = _original_artifact(dest, period, pdf_path)
        if require_language == "en":
            try:
                if _looks_spanish(Path(markdown_path).read_text(encoding="utf-8")):
                    print(f"SKIP {slug}/{period}: reads as non-English (require_language=en)",
                          file=sys.stderr)
                    continue
            except OSError:
                pass
        doc_id = f"{slug}/{period}"
        by_id[doc_id] = Document(
            doc_id=doc_id,
            company=slug,
            period=period,
            doc_type=_infer_doc_type(selected.stem, taxonomy),
            title=f"{company} {period}",
            source_url=ir_website.get("url"),
            pdf_path=str(pdf_path) if pdf_path else None,
            markdown_path=str(markdown_path),
            language=language,
            industry=industry,
            extra=meta,
            source_path=str(selected.relative_to(corpus_dir)) if selected.is_relative_to(corpus_dir) else str(selected),
            source_format=selected.suffix.lower().lstrip("."),
            original_path=str(original_path) if original_path else None,
            original_format=original_format,
            content_sha256=hashlib.sha256(Path(markdown_path).read_bytes()).hexdigest(),
        )

    for doc in by_id.values():
        _backfill_original_artifact(doc, corpus_dir)
    manifest = CorpusManifest(documents=list(by_id.values()))
    save_manifest(manifest, corpus_dir)
    return manifest


def ingest_all(config: dict, corpus_dir: Path, *, force_download: bool = False) -> CorpusManifest:
    """Ingest every entry under ``config['sources']``; persist once at the end."""
    corpus_dir = Path(corpus_dir)
    taxonomy = load_taxonomy(config)
    manifest = load_manifest(corpus_dir)
    for source in config.get("sources", []):
        manifest = ingest_source(source, corpus_dir, force_download=force_download,
                                 taxonomy=taxonomy)
    return manifest
