"""Phase-0 smoke tests — prove the scaffold is wired and self-contained.

These assert the *foundation*, not behavior: vendored infra resolves to alpha-go's own
copy, paths re-root inside alpha-go, the new feature packages import, and the documented
contracts (dataclasses, schema, stub entrypoints) exist. Feature tests arrive per-phase.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

ALPHA_GO_ROOT = Path(__file__).resolve().parents[1]


def test_paths_reroot_into_alpha_go():
    from src.shared.paths import CONFIGS_DIR, PROJECT_ROOT
    assert PROJECT_ROOT == ALPHA_GO_ROOT
    assert CONFIGS_DIR == ALPHA_GO_ROOT / "configs"
    assert CONFIGS_DIR.is_dir()


@pytest.mark.parametrize(
    "mod",
    [
        # vendored core
        "src.download.downloader",
        "src.parse.parse_pdf",
        "src.extract.tiered_extract",
        "src.extract.semantic_search",
        "src.model.financial_model",
        "src.shared.report_index",
        "src.shared.validator",
        # new layers
        "src.corpus.ingest",
        "src.corpus.manifest",
        "src.index.store",
        "src.index.chunker",
        "src.index.keyword_index",
        "src.index.embeddings",
        "src.index.build",
        "src.search.retriever",
        "src.search.fusion",
        "src.search.snippets",
        "src.search.filters",
        "src.qa.rag",
        "src.qa.prompts",
        "app.components.panels",
    ],
)
def test_module_imports(mod):
    assert importlib.import_module(mod) is not None


def test_self_contained_no_parent_imports():
    """No vendored/new module may import from a top-level package other than `src`/`app`.

    Guards the self-containment invariant: alpha-go must not reach back into the parent.
    (Heuristic source scan over the new layers + a couple of vendored anchors.)
    """
    suspects = ["import pdfs", "from pdfs", "Desktop/pdfs/src"]
    for path in (ALPHA_GO_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for s in suspects:
            assert s not in text, f"{path} references the parent repo ({s!r})"


def test_index_schema_present():
    from src.index.store import SCHEMA_SQL, SCHEMA_VERSION
    assert SCHEMA_VERSION >= 1
    for table in ("documents", "chunks", "chunks_fts", "embeddings", "meta"):
        assert table in SCHEMA_SQL


def test_filters_dataclass_contract():
    from src.search.filters import SearchFilters
    assert SearchFilters().is_empty()
    assert not SearchFilters(companies=["walmex"]).is_empty()


def test_qa_answer_short_circuits_without_hits():
    """Phase-5 ``answer`` is implemented; with zero hits it returns the NOT_FOUND sentinel."""
    from src.qa.prompts import NOT_FOUND
    from src.qa.rag import answer
    assert answer("q", []).text == NOT_FOUND
