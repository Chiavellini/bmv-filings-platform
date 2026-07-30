"""Upload — add a single user-supplied document to a company's corpus + live index.

Ties the existing pieces together for one uploaded file: parse to markdown (reusing the vendored
``parse_pdf`` for PDFs and ``edgar.html_to_text`` for HTML; pass-through for md/txt), write it into
``data/corpus/<slug>/``, upsert the ``Document`` into ``manifest.json``, and incrementally index it
(:func:`src.index.build.add_document_to_index`) so it is searchable immediately — no full rebuild.

Supports adding to an existing company or creating a new one on the fly (the index/manifest are the
source of truth for search facets, so a new company needs no config ``sources[]`` entry).
"""
from __future__ import annotations

import re
import hashlib
import tempfile
from collections.abc import Iterable
from pathlib import Path

from src.corpus.manifest import CorpusManifest, Document, load_manifest, save_manifest
from src.index.build import add_document_to_index
from src.shared.paths import DATA_DIR
from src.shared.report_index import infer_period_label

SUPPORTED_EXTS = {".pdf", ".html", ".htm", ".md", ".txt"}


def slugify(name: str) -> str:
    """Filesystem/id-safe slug: lowercase, non-alphanumerics → single underscore."""
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return s or "doc"


def suggest_company(filename: str, companies: Iterable[str]) -> str | None:
    """Best-guess company slug for an uploaded file by matching known company slugs against the
    slugified filename.

    Returns the known company whose slug appears in the filename (as an underscore-delimited token,
    else as a plain substring), preferring the **longest** match so a short slug like ``ac`` never
    shadows a longer ``ac_bebidas``. ``None`` when nothing matches — the caller then treats it as a
    new company.
    """
    stem = slugify(Path(filename).stem)
    tokens = set(stem.split("_"))
    best: str | None = None
    for company in companies:
        cslug = slugify(company)
        if not cslug:
            continue
        # Whole-token match is strongest; fall back to substring (handles run-together names).
        if cslug in tokens or cslug in stem:
            if best is None or len(cslug) > len(slugify(best)):
                best = company
    return best


def clean_label(label: str) -> str:
    """Filesystem/id-safe label that PRESERVES period forms (hyphens + case, e.g. ``2025-2T``).

    Unlike :func:`slugify`, keeps ``-`` and letter case so a quarter label like ``2025-2T`` stays
    intact for :func:`infer_period_label`; only spaces and unsafe characters collapse to ``-``.
    """
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (label or "").strip()).strip("-_.")
    return s or "doc"


def _collision_safe_label(
    requested: str, *, slug: str, filename: str, ext: str,
    manifest: CorpusManifest, dest: Path,
) -> tuple[str, bool]:
    """Protect an existing filing when an upload reuses its period as the label."""
    by_id = {d.doc_id: d for d in manifest.documents}

    def occupied(candidate: str) -> bool:
        existing = by_id.get(f"{slug}/{candidate}")
        same_upload = bool(
            existing
            and (existing.extra or {}).get("uploaded")
            and (existing.extra or {}).get("original_filename") == filename
        )
        if same_upload:
            return False
        return bool(existing or (dest / f"{candidate}.md").exists()
                    or (dest / f"{candidate}{ext}").exists())

    if not occupied(requested):
        return requested, False
    file_label = clean_label(Path(filename).stem)
    base = requested if file_label.casefold() in requested.casefold() else f"{requested}-{file_label}"
    candidate = base
    n = 2
    while occupied(candidate):
        candidate = f"{base}-{n}"
        n += 1
    return candidate, True


def _to_markdown(ext: str, data: bytes, dest: Path, label: str) -> tuple[str, Path]:
    """Extract searchable text and preserve the uploaded source artifact on disk."""
    if ext == ".pdf":
        from src.parse.parse_pdf import parse_pdf  # lazy (pdfplumber)

        pdf_path = dest / f"{label}.pdf"
        pdf_path.write_bytes(data)
        markdown, _blocks, _doc = parse_pdf(pdf_path, with_meta=True)
        return markdown, pdf_path
    if ext in (".html", ".htm"):
        from src.download.edgar import html_to_text  # lazy (BeautifulSoup)

        html_path = dest / f"{label}{ext}"
        html_path.write_bytes(data)
        return html_to_text(data.decode("utf-8", errors="replace")), html_path
    # Markdown/text are their own original artifact. TXT remains a .txt sibling while the parsed
    # search representation is written to .md below; a Markdown upload naturally shares the path.
    original_path = dest / f"{label}{ext}"
    original_path.write_bytes(data)
    return data.decode("utf-8", errors="replace"), original_path


def extract_uploaded_text(filename: str, data: bytes) -> str:
    """Extract text for pre-ingest classification without mutating the corpus."""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"Unsupported file type '{ext}'. Supported: {sorted(SUPPORTED_EXTS)}")
    with tempfile.TemporaryDirectory(prefix="alpha-go-classify-") as tmp:
        markdown, _original = _to_markdown(ext, data, Path(tmp), "preview")
    return markdown


def _normalize_targets(
    targets: "list[dict] | None", slug: str, company: str, industry: str | None,
) -> list[dict]:
    """Resolve the corpus target set to ``[{"slug","company","industry"}, ...]`` (primary first).

    Accepts either the multi-corpus ``targets`` list or the single-company ``slug``/``company``/
    ``industry`` triple (back-compat). Slugs are normalized and de-duplicated preserving order,
    so a drop-in shared across corpora yields one membership per distinct company.
    """
    raw = list(targets) if targets else [{"slug": slug, "company": company, "industry": industry}]
    out: list[dict] = []
    seen: set = set()
    for t in raw:
        tslug = slugify(t.get("slug") or t.get("company") or "")
        if not tslug or tslug in seen:
            continue
        seen.add(tslug)
        out.append({"slug": tslug, "company": (t.get("company") or tslug),
                    "industry": t.get("industry")})
    if not out:
        raise ValueError("At least one target company is required.")
    return out


def add_uploaded_document(
    store,
    config: dict,
    *,
    slug: str = "",
    company: str = "",
    industry: str | None = None,
    targets: "list[dict] | None" = None,
    doc_type_key: str,
    title: str,
    label: str,
    filename: str,
    data: bytes,
    embedder,
    corpus_dir: Path | None = None,
    storage_dir: Path | None = None,
    estate_bridge=None,
    language: str = "en",
) -> dict:
    """Parse, store to corpus, upsert manifest, and incrementally index one uploaded document.

    The document can belong to **one or more** corpora: pass ``targets`` as a list of
    ``{"slug","company","industry"}`` (primary first) for a multi-company drop-in, or the
    single ``slug``/``company``/``industry`` triple for the common one-company case. The file is
    parsed once and stored under the **primary** company's directory (``doc_id = "<primary>/
    <label>"``); every target company is recorded as a membership so the doc is searchable in
    each of their corpora. Re-uploading the same (primary, label) cleanly replaces the prior
    version.
    """
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"Unsupported file type '{ext}'. Supported: {sorted(SUPPORTED_EXTS)}")

    resolved = _normalize_targets(targets, slug, company, industry)
    primary = resolved[0]
    slug, company, industry = primary["slug"], primary["company"], primary["industry"]
    requested_label = clean_label(label) if label else (
        infer_period_label(Path(filename).stem) or clean_label(Path(filename).stem))
    corpus_dir = Path(corpus_dir) if corpus_dir else DATA_DIR / "corpus"
    storage_dir = Path(storage_dir) if storage_dir else corpus_dir
    dest = storage_dir / slug
    dest.mkdir(parents=True, exist_ok=True)
    previous_manifest = load_manifest(corpus_dir)
    label, renamed_for_collision = _collision_safe_label(
        requested_label, slug=slug, filename=filename, ext=ext,
        manifest=previous_manifest, dest=dest,
    )

    md_path = dest / f"{label}.md"
    original_candidate = dest / f"{label}{ext}"
    touched_paths = {md_path, original_candidate}
    previous_files = {
        path: path.read_bytes() if path.exists() else None for path in touched_paths
    }
    manifest_written = False
    try:
        markdown, original_path = _to_markdown(ext, data, dest, label)
        if not markdown or not markdown.strip():
            raise ValueError("Could not extract any text from the uploaded file.")
        md_path.write_text(markdown, encoding="utf-8")

        period = (infer_period_label(requested_label) or infer_period_label(label)
                  or infer_period_label(Path(filename).stem))
        doc_id = f"{slug}/{label}"
        memberships = [{"company": t["slug"], "industry": t["industry"]} for t in resolved]
        doc = Document(
            doc_id=doc_id,
            company=slug,
            period=period,
            doc_type=doc_type_key,
            title=title.strip() or f"{company} · {label}",
            source_url=None,
            pdf_path=str(original_path) if ext == ".pdf" else None,
            markdown_path=str(md_path),
            language=language,
            industry=industry,
            memberships=memberships,
            extra={"uploaded": True, "original_filename": filename},
            source_path=str(original_path),
            source_format=ext.lstrip("."),
            original_path=str(original_path),
            original_format=ext.lstrip("."),
            content_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        )

        # Persist the manifest before indexing; both writes are individually atomic. If indexing
        # fails, the exception path restores the prior manifest and every replaced source file.
        by_id = {d.doc_id: d for d in previous_manifest.documents}
        by_id[doc_id] = doc
        save_manifest(CorpusManifest(documents=list(by_id.values())), corpus_dir)
        manifest_written = True

        stats = add_document_to_index(store, doc, markdown, config, embedder=embedder)
    except Exception:
        if manifest_written:
            save_manifest(previous_manifest, corpus_dir)
        for path, old_bytes in previous_files.items():
            if old_bytes is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                path.write_bytes(old_bytes)
        raise

    estate_registered = False
    estate_document_id = None
    estate_error = None
    if estate_bridge is not None:
        try:
            estate_document_id = estate_bridge.register_alpha_upload(
                alpha_doc_id=doc.doc_id,
                company=slug,
                period=period,
                doc_type=doc_type_key,
                title=doc.title,
                language=language,
                memberships=memberships,
                markdown_path=md_path,
                original_path=original_path,
                original_filename=filename,
            )
            estate_registered = True
        except Exception as exc:  # local search remains valid; caller must surface this
            estate_error = f"{type(exc).__name__}: {exc}"

    stats.update(company=slug, companies=[t["slug"] for t in resolved],
                 period=period, doc_type=doc_type_key, markdown_path=str(md_path),
                 label=label, requested_label=requested_label,
                 renamed_for_collision=renamed_for_collision,
                 estate_registered=estate_registered,
                 estate_document_id=estate_document_id,
                 estate_error=estate_error)
    return stats
