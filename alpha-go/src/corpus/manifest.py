"""Corpus manifest — the canonical record of every parsed document available to search.

A ``Document`` is one parsed report (markdown + metadata). The ``CorpusManifest`` is the
list of all documents under ``data/corpus/`` and is the hand-off contract from the Ingest
phase to the Index phase.

See docs/ROADMAP.md Phase 1.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Document:
    """One parsed report in the corpus."""

    doc_id: str                      # stable id, e.g. "walmex/2026-1T/release"
    company: str                     # slug, e.g. "walmex"
    period: str | None               # canonical period label, e.g. "2026-1T" (infer_period_label)
    doc_type: str                    # "release" | "report" | "presentation" | "transcript" | ...
    title: str
    source_url: str | None           # where it was downloaded from
    pdf_path: str | None             # local path to the raw PDF (if any)
    markdown_path: str               # local path to the parsed markdown
    language: str = "es"
    industry: str | None = None      # sector tag for facet scoping, e.g. "retail" | "food"
    # Every corpus this document belongs to, primary first — ``[{"company", "industry"}, ...]``.
    # ``company``/``industry`` above mirror ``memberships[0]`` for backward-compatible display;
    # a drop-in shared across companies (e.g. an in-house multi-company paper) has len > 1.
    memberships: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    # Portable source provenance.  Paths are stored relative to the project/corpus root when
    # possible; the original PDF/HTML path remains available for the reader when present.
    source_path: str | None = None
    source_format: str | None = None
    # Raw source artifact used by the in-app reader.  Kept separate from ``source_path`` because
    # the indexed document can be Markdown while its original is a sibling PDF or HTML filing.
    original_path: str | None = None
    original_format: str | None = None
    content_sha256: str | None = None

    def company_slugs(self) -> list[str]:
        """All corpus slugs this document belongs to (falls back to the primary ``company``)."""
        slugs = [m["company"] for m in self.memberships if m.get("company")]
        return slugs or [self.company]


@dataclass
class CorpusManifest:
    """All documents currently in the corpus."""

    documents: list[Document] = field(default_factory=list)

    def by_company(self, company: str) -> list[Document]:
        return [d for d in self.documents if company in d.company_slugs()]


def manifest_path(corpus_dir: Path) -> Path:
    """Location of the on-disk manifest JSON within a corpus directory."""
    return corpus_dir / "manifest.json"


# Every path a consumer opens. Stored relative to the estate root so the manifest
# travels with the bundle; resolved against the *current* root when read.
_PORTABLE_PATH_FIELDS = ("markdown_path", "pdf_path", "source_path", "original_path")


def _default_estate_root() -> Path | None:
    """The connected estate, when one is configured.

    Imported lazily: the manifest is also used for local fixture corpora that have
    no estate at all, and importing the bridge at module scope would make those
    fail to import rather than simply run without a root.
    """
    try:
        from src.shared.paths import ESTATE_BRIDGE

        return Path(ESTATE_BRIDGE.estate_root)
    except Exception:
        return None


def _exists(path: Path) -> bool:
    """``Path.exists()`` that answers False instead of raising.

    News documents carry their article URL in the provenance fields. Joining one
    onto a root yields a path component far longer than the filesystem allows, so
    the probe raises ENAMETOOLONG rather than returning False; anything that
    cannot be stat'd simply is not a file here.
    """
    try:
        return path.exists()
    except (OSError, ValueError):
        return False


def _rebind_absolute(path: Path, estate_root: Path) -> Path | None:
    """Re-root an absolute path written on another computer onto this estate.

    The shipped projection stored paths rooted at the original computer's mount
    point (e.g. ``/Volumes/<MountName>/bmv-estate-v1/views/...``).
    Mounted under any other name, that prefix is meaningless, but the tail below
    the estate root is unchanged — so the longest tail that exists here is the
    same file. Returns ``None`` when nothing matches, leaving the caller's value
    untouched rather than inventing a path.
    """
    parts = path.parts
    for index in range(1, len(parts)):
        candidate = estate_root.joinpath(*parts[index:])
        if _exists(candidate):
            return candidate.resolve()
    return None


def resolve_corpus_path(
    raw: str | None,
    *,
    estate_root: Path | None,
    corpus_dir: Path | None = None,
) -> str | None:
    """Turn a stored manifest path into a path that opens on this computer.

    Unresolvable values are returned unchanged so fixtures and genuinely missing
    documents still surface as themselves instead of a fabricated location.
    """
    if not raw:
        return raw
    path = Path(raw)
    if not path.is_absolute():
        for base in (estate_root, corpus_dir):
            if base is not None and _exists(Path(base) / path):
                return str((Path(base) / path).resolve())
        return raw
    if _exists(path):
        return str(path.resolve())
    if estate_root is not None:
        rebound = _rebind_absolute(path, Path(estate_root))
        if rebound is not None:
            return str(rebound)
    return raw


def _to_portable(raw: str | None, estate_root: Path | None) -> str | None:
    """Store paths inside the estate relative to its root; leave the rest alone."""
    if not raw or estate_root is None:
        return raw
    path = Path(raw)
    if not path.is_absolute():
        return raw
    try:
        return path.resolve().relative_to(Path(estate_root).resolve()).as_posix()
    except (ValueError, OSError):
        return raw


def load_manifest(
    corpus_dir: Path, *, estate_root: Path | None = None
) -> CorpusManifest:
    """Read the corpus manifest from ``<corpus_dir>/manifest.json``.

    Returns an empty manifest when the file does not exist yet (first run / fixtures).
    Stored paths are resolved against ``estate_root`` (the connected estate by
    default) so a bundle mounted under any name yields openable paths.
    """
    corpus_dir = Path(corpus_dir)
    path = manifest_path(corpus_dir)
    if not path.exists():
        return CorpusManifest()
    if estate_root is None:
        estate_root = _default_estate_root()
    raw = json.loads(path.read_text(encoding="utf-8"))
    fields = {f.name for f in dataclasses.fields(Document)}
    documents = []
    for d in raw.get("documents", []):
        doc = Document(**{k: v for k, v in d.items() if k in fields})
        # Legacy manifests (and batch-ingested docs) predate ``memberships``: synthesize a
        # single-company membership from the primary column so every read path is uniform.
        if not doc.memberships:
            doc.memberships = [{"company": doc.company, "industry": doc.industry}]
        for name in _PORTABLE_PATH_FIELDS:
            setattr(
                doc,
                name,
                resolve_corpus_path(
                    getattr(doc, name),
                    estate_root=estate_root,
                    corpus_dir=corpus_dir,
                ),
            )
        documents.append(doc)
    return CorpusManifest(documents=documents)


def save_manifest(
    manifest: CorpusManifest, corpus_dir: Path, *, estate_root: Path | None = None
) -> Path:
    """Atomically persist ``<corpus_dir>/manifest.json`` and return its path.

    Uploads mutate this file while the dashboard is live. Writing a sibling temporary file and
    replacing only after the JSON is complete prevents a process interruption from leaving a
    truncated manifest that makes the whole corpus unreadable.

    Paths inside the estate are written relative to its root, so rewriting the
    manifest on one computer cannot pin the corpus to that computer's mount.
    """
    corpus_dir = Path(corpus_dir)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    if estate_root is None:
        estate_root = _default_estate_root()
    path = manifest_path(corpus_dir)
    documents = sorted((dataclasses.asdict(d) for d in manifest.documents), key=lambda d: d["doc_id"])
    for document in documents:
        for name in _PORTABLE_PATH_FIELDS:
            if name in document:
                document[name] = _to_portable(document[name], estate_root)
    payload = {"documents": documents}
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=".manifest-", suffix=".json", dir=corpus_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def manifest_checksum(manifest: CorpusManifest) -> str:
    """Stable checksum used to prove which manifest produced an index."""
    payload = json.dumps(
        sorted((dataclasses.asdict(d) for d in manifest.documents), key=lambda d: d["doc_id"]),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
