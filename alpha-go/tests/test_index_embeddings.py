"""Tests for the embedding backends (Phase 2)."""
from __future__ import annotations

import sys

import numpy as np

from src.index.embeddings import (
    HashingEmbedder,
    SentenceTransformerEmbedder,
    _validate_semantic_tokenizer,
    get_embedder,
)


def _force_st_absent(monkeypatch):
    """Make ``import sentence_transformers`` fail, regardless of whether it's installed."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)


def test_hashing_embedder_deterministic_and_normalized():
    e = HashingEmbedder(dim=64)
    a = e.encode(["Total revenues grew strongly"])[0]
    b = e.encode(["Total revenues grew strongly"])[0]
    assert a == b                                   # deterministic across calls (no salt)
    assert len(a) == 64 == e.dim
    assert abs(np.linalg.norm(a) - 1.0) < 1e-5      # L2-normalized


def test_hashing_embedder_distinguishes_text():
    e = HashingEmbedder(dim=128)
    v1, v2 = e.encode(["revenue and ebitda margins", "weather forecast for tuesday"])
    assert v1 != v2
    # Related text should be at least as similar to itself as to unrelated text.
    sim_self = float(np.dot(v1, v1))
    sim_other = float(np.dot(v1, v2))
    assert sim_self >= sim_other
    assert e.semantic_quality == "lexical"


def test_get_embedder_auto_falls_back_when_st_absent(monkeypatch):
    # Simulate sentence-transformers being unavailable so the test holds in any environment.
    _force_st_absent(monkeypatch)
    embedder, name = get_embedder({"index": {"embedding_backend": "auto"}})
    assert isinstance(embedder, HashingEmbedder)
    assert name == "hashing"


def test_get_embedder_hashing_explicit():
    embedder, name = get_embedder({"index": {"embedding_backend": "hashing", "hashing_dim": 32}})
    assert isinstance(embedder, HashingEmbedder) and embedder.dim == 32 and name == "hashing"


def test_get_embedder_unknown_backend_raises():
    import pytest
    with pytest.raises(ValueError):
        get_embedder({"index": {"embedding_backend": "nope"}})


def test_sentence_transformer_pool_is_reused_and_closed():
    class _FakeModel:
        def __init__(self):
            self.started = None
            self.stopped = None
            self.encode_kwargs = None

        def start_multi_process_pool(self, devices):
            self.started = devices
            return {"processes": [object(), object()], "input": None, "output": None}

        def stop_multi_process_pool(self, pool):
            self.stopped = pool

        def encode(self, texts, **kwargs):
            self.encode_kwargs = kwargs
            return np.ones((len(texts), 3), dtype="float32")

    embedder = SentenceTransformerEmbedder(batch_size=7)
    model = _FakeModel()
    embedder._model = model

    embedder.start_multi_process_pool(2)
    vectors = embedder.encode(["one", "two", "three", "four"])
    active_pool = embedder._pool
    embedder.stop_multi_process_pool()

    assert model.started == ["cpu", "cpu"]
    assert model.encode_kwargs["pool"] is active_pool
    assert model.encode_kwargs["batch_size"] == 7
    assert len(vectors) == 4
    assert model.stopped is active_pool
    assert embedder._pool is None


def test_semantic_tokenizer_health_gate_rejects_incomplete_snapshot():
    class _BrokenTokenizer:
        vocab_size = 5
        unk_token_id = 1

        def encode(self, _text, add_special_tokens=False):
            return [1]

    class _BrokenModel:
        tokenizer = _BrokenTokenizer()

    with pytest.raises(RuntimeError, match="Incomplete local model snapshot"):
        _validate_semantic_tokenizer(_BrokenModel(), "test/model")


def test_semantic_tokenizer_health_gate_accepts_real_bilingual_tokens():
    class _Tokenizer:
        vocab_size = 250_000
        unk_token_id = 1

        def encode(self, text, add_special_tokens=False):
            return [42] if text == "passengers" else [84]

    class _Model:
        tokenizer = _Tokenizer()

    _validate_semantic_tokenizer(_Model(), "test/model")


import pytest  # noqa: E402


@pytest.mark.model
def test_sentence_transformer_backend_real():
    """Opt-in: requires `pip install sentence-transformers`. Run with `pytest -m model`."""
    pytest.importorskip("sentence_transformers")
    e = SentenceTransformerEmbedder()
    # importorskip covers the package; the weights are a separate prerequisite. _ensure_model
    # loads with local_files_only=True, so an uncached model raises instead of downloading.
    try:
        e._ensure_model()
    except OSError as exc:
        pytest.skip(f"{e.model_name} weights not in local HF cache: {exc}")
    vecs = e.encode(["hello world"])
    assert len(vecs) == 1 and len(vecs[0]) == e.dim
    assert e.semantic_quality == "multilingual"
