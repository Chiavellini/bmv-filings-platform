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


def load_manifest(corpus_dir: Path) -> CorpusManifest:
    """Read the corpus manifest from ``<corpus_dir>/manifest.json``.

    Returns an empty manifest when the file does not exist yet (first run / fixtures).
    """
    path = manifest_path(Path(corpus_dir))
    if not path.exists():
        return CorpusManifest()
    raw = json.loads(path.read_text(encoding="utf-8"))
    fields = {f.name for f in dataclasses.fields(Document)}
    documents = []
    for d in raw.get("documents", []):
        doc = Document(**{k: v for k, v in d.items() if k in fields})
        # Legacy manifests (and batch-ingested docs) predate ``memberships``: synthesize a
        # single-company membership from the primary column so every read path is uniform.
        if not doc.memberships:
            doc.memberships = [{"company": doc.company, "industry": doc.industry}]
        documents.append(doc)
    return CorpusManifest(documents=documents)


def save_manifest(manifest: CorpusManifest, corpus_dir: Path) -> Path:
    """Atomically persist ``<corpus_dir>/manifest.json`` and return its path.

    Uploads mutate this file while the dashboard is live. Writing a sibling temporary file and
    replacing only after the JSON is complete prevents a process interruption from leaving a
    truncated manifest that makes the whole corpus unreadable.
    """
    corpus_dir = Path(corpus_dir)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_path(corpus_dir)
    documents = sorted((dataclasses.asdict(d) for d in manifest.documents), key=lambda d: d["doc_id"])
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
