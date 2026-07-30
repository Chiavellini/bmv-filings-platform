from scripts.reembed_index import reembed
from src.index.embeddings import HashingEmbedder
from src.index.store import IndexStore


class _TestSemanticEmbedder(HashingEmbedder):
    """Fast test double with a different dimension and genuine-semantic capability marker."""

    def __init__(self):
        super().__init__(dim=32)
        self.semantic_quality = "multilingual"
        self.model_name = "test/document-agnostic-semantic"


class _PooledTestSemanticEmbedder(_TestSemanticEmbedder):
    def __init__(self):
        super().__init__()
        self.started = 0
        self.stopped = 0

    def start_multi_process_pool(self, workers, worker_threads=None):
        self.started = workers
        self.worker_threads = worker_threads

    def stop_multi_process_pool(self):
        self.stopped += 1


def test_reembed_is_copy_on_write_and_replaces_every_vector(built_index, tmp_path):
    source, _ = built_index
    source_path = source.db_path
    destination = tmp_path / "semantic.db"
    original_dim = source.get_meta("embedding_dim")

    stats = reembed(
        source_path, destination,
        {"index": {"embedding_batch_size": 2}},
        embedder=_TestSemanticEmbedder(),
    )

    migrated = IndexStore(destination)
    migrated.connect()
    assert stats["embedded"] == migrated.count("chunks")
    assert migrated.count("embeddings") == migrated.count("chunks")
    assert migrated.get_meta("embedding_model") == "test/document-agnostic-semantic"
    assert migrated.get_meta("embedding_dim") == "32"
    assert source.get_meta("embedding_dim") == original_dim


def test_reembed_uses_and_closes_configured_migration_workers(built_index, tmp_path):
    source, _ = built_index
    embedder = _PooledTestSemanticEmbedder()

    reembed(
        source.db_path,
        tmp_path / "semantic.db",
        {
            "index": {
                "embedding_batch_size": 2,
                "embedding_migration_workers": 3,
            }
        },
        embedder=embedder,
    )

    assert embedder.started == 3
    assert embedder.worker_threads == 1
    assert embedder.stopped == 1


def test_reembed_resume_reuses_completed_vectors(built_index, tmp_path):
    source, _ = built_index
    destination = tmp_path / "semantic.db"
    building = destination.with_suffix(".db.building")
    # A first complete run proves the copy; moving it back to the protected work name simulates
    # an interrupted migration whose vectors should be recognized rather than recomputed.
    reembed(
        source.db_path, destination, {"index": {"embedding_batch_size": 2}},
        embedder=_TestSemanticEmbedder(),
    )
    destination.replace(building)
    stats = reembed(
        source.db_path, destination, {"index": {"embedding_batch_size": 2}},
        embedder=_TestSemanticEmbedder(), resume=True,
    )
    assert stats["embedded"] == stats["chunks"]


def test_reembed_refuses_to_promote_a_checkpoint_after_source_changes(
    built_index, tmp_path,
):
    import pytest

    from src.corpus.manifest import Document
    from src.index.build import add_document_to_index

    source, _ = built_index
    destination = tmp_path / "semantic.db"
    building = destination.with_suffix(".db.building")
    reembed(
        source.db_path, destination, {"index": {"embedding_batch_size": 2}},
        embedder=_TestSemanticEmbedder(),
    )
    destination.replace(building)

    doc = Document(
        doc_id="unknown/new-upload",
        company="unknown",
        period="2026-1T",
        doc_type="internal",
        title="New upload",
        source_url=None,
        pdf_path=None,
        markdown_path=str(tmp_path / "new-upload.md"),
        content_sha256="new-content",
    )
    add_document_to_index(
        source, doc, "A newly uploaded document about foreign exchange.",
        {"index": {}}, embedder=_TestSemanticEmbedder(),
    )

    with pytest.raises(RuntimeError, match="Source index changed"):
        reembed(
            source.db_path, destination, {"index": {"embedding_batch_size": 2}},
            embedder=_TestSemanticEmbedder(), resume=True,
        )
