"""The projection manifest must not encode the mount name it was built on.

``sync_shared_estate.py`` wrote ``markdown_path`` as an absolute path, so all
3,695 documents in the shipped projection began with the original computer's
mount point (e.g. ``/Volumes/<MountName>/...``). That manifest resolves only
on a computer whose mount happens to carry the same
name; anywhere else every document is missing. The manifest is stored relative to
the estate root and resolved against the *current* root at load time.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.corpus.manifest import (
    CorpusManifest,
    Document,
    load_manifest,
    manifest_path,
    save_manifest,
)


def _estate(tmp_path: Path, name: str) -> Path:
    """A mounted estate root; ``name`` stands in for the volume name."""
    root = tmp_path / name / "bmv-estate-v1"
    (root / "views" / "parsed" / "acme").mkdir(parents=True)
    (root / "projections" / "alpha-go").mkdir(parents=True)
    return root


def _document(markdown_path: str) -> Document:
    return Document(
        doc_id="acme/2024-1T",
        company="acme",
        period="2024-1T",
        doc_type="report",
        title="Acme 2024-1T",
        source_url=None,
        pdf_path=None,
        markdown_path=markdown_path,
        language="es",
    )


def test_save_stores_markdown_path_relative_to_the_estate_root(tmp_path: Path) -> None:
    root = _estate(tmp_path, "Estate")
    corpus = root / "projections" / "alpha-go"
    markdown = root / "views" / "parsed" / "acme" / "2024-1T.md"
    markdown.write_text("# report", encoding="utf-8")

    save_manifest(CorpusManifest(documents=[_document(str(markdown))]), corpus,
                  estate_root=root)

    raw = json.loads(manifest_path(corpus).read_text(encoding="utf-8"))
    stored = raw["documents"][0]["markdown_path"]
    assert not os.path.isabs(stored), f"manifest still hardcodes a mount: {stored}"
    assert stored == "views/parsed/acme/2024-1T.md"


def test_relative_manifest_resolves_under_a_differently_named_mount(
    tmp_path: Path,
) -> None:
    """The portability claim: same bundle, different volume name, still readable."""
    original = _estate(tmp_path, "Estate")
    corpus = original / "projections" / "alpha-go"
    markdown = original / "views" / "parsed" / "acme" / "2024-1T.md"
    markdown.write_text("# report", encoding="utf-8")
    save_manifest(CorpusManifest(documents=[_document(str(markdown))]), corpus,
                  estate_root=original)

    # The same bundle, mounted somewhere else entirely on another computer.
    relocated = _estate(tmp_path, "BMV-Archive-2")
    (relocated / "views" / "parsed" / "acme" / "2024-1T.md").write_text(
        "# report", encoding="utf-8"
    )
    relocated_corpus = relocated / "projections" / "alpha-go"
    manifest_path(relocated_corpus).write_text(
        manifest_path(corpus).read_text(encoding="utf-8"), encoding="utf-8"
    )

    loaded = load_manifest(relocated_corpus, estate_root=relocated)

    assert len(loaded.documents) == 1
    resolved = Path(loaded.documents[0].markdown_path)
    assert resolved.is_absolute() and resolved.is_file()
    assert relocated in resolved.parents


def test_legacy_absolute_manifest_rebinds_to_the_current_estate_root(
    tmp_path: Path,
) -> None:
    """The 3,695-document manifest already on the estate must keep working."""
    root = _estate(tmp_path, "Estate")
    corpus = root / "projections" / "alpha-go"
    markdown = root / "views" / "parsed" / "acme" / "2024-1T.md"
    markdown.write_text("# report", encoding="utf-8")
    # The same shape as the shipped bug: an absolute path from a mount that is not here.
    stale = "/mnt/legacy-mount/bmv-estate-v1/views/parsed/acme/2024-1T.md"
    manifest_path(corpus).write_text(
        json.dumps({"documents": [json.loads(json.dumps(_document(stale).__dict__))]}),
        encoding="utf-8",
    )

    loaded = load_manifest(corpus, estate_root=root)

    resolved = Path(loaded.documents[0].markdown_path)
    assert resolved.is_file(), f"legacy absolute path did not rebind: {resolved}"
    assert resolved == markdown.resolve()


def test_load_then_save_does_not_reintroduce_absolute_paths(tmp_path: Path) -> None:
    """An upload rewrites the manifest; that must not re-pin it to this mount."""
    root = _estate(tmp_path, "Estate")
    corpus = root / "projections" / "alpha-go"
    markdown = root / "views" / "parsed" / "acme" / "2024-1T.md"
    markdown.write_text("# report", encoding="utf-8")
    save_manifest(CorpusManifest(documents=[_document(str(markdown))]), corpus,
                  estate_root=root)

    loaded = load_manifest(corpus, estate_root=root)
    save_manifest(loaded, corpus, estate_root=root)

    raw = json.loads(manifest_path(corpus).read_text(encoding="utf-8"))
    assert not os.path.isabs(raw["documents"][0]["markdown_path"])


def test_paths_outside_the_estate_are_left_absolute(tmp_path: Path) -> None:
    """Local fixture corpora live outside any estate and must still round-trip."""
    root = _estate(tmp_path, "Estate")
    corpus = root / "projections" / "alpha-go"
    outside = tmp_path / "elsewhere" / "doc.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("# report", encoding="utf-8")

    save_manifest(CorpusManifest(documents=[_document(str(outside))]), corpus,
                  estate_root=root)
    loaded = load_manifest(corpus, estate_root=root)

    assert Path(loaded.documents[0].markdown_path) == outside.resolve()
