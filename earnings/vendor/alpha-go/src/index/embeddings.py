"""Embedders — local sentence embeddings for the semantic index, with offline fallback.

Two interchangeable backends (duck-typed: ``.encode(list[str]) -> list[list[float]]`` and
``.dim``):

- ``SentenceTransformerEmbedder`` — true semantics via sentence-transformers (lazy import).
  The documented default; used when the library is installed.
- ``HashingEmbedder`` — a deterministic bag-of-words feature-hashing embedder (pure numpy,
  offline, no model download). The fallback so the whole hybrid pipeline runs + tests today.

``get_embedder(config)`` selects per ``index.embedding_backend`` (auto | hashing |
sentence-transformers); ``auto`` prefers sentence-transformers and falls back to hashing with
a logged warning. Returns ``(embedder, name)`` where ``name`` is recorded in the index meta.
"""
from __future__ import annotations

import re
import sys

import numpy as np

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_TOKEN = re.compile(r"\w+", re.UNICODE)


class HashingEmbedder:
    """Deterministic, dependency-free embedder: hashed bag-of-words, L2-normalized.

    Not a substitute for real sentence embeddings (it captures lexical overlap, not deep
    semantics), but it is fully offline and deterministic — ideal as a fallback and for tests.
    """

    def __init__(self, dim: int = 256):
        self._dim = int(dim)
        self.semantic_quality = "lexical"

    @property
    def dim(self) -> int:
        return self._dim

    def _vector(self, text: str) -> np.ndarray:
        vec = np.zeros(self._dim, dtype="float32")
        for tok in _TOKEN.findall(text.lower()):
            # Stable hash (Python's hash() is salted per-process) -> deterministic bucket+sign.
            h = 2166136261
            for ch in tok:
                h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
            vec[h % self._dim] += 1.0 if (h >> 31) & 1 == 0 else -1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        return vec

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t).tolist() for t in texts]


class SentenceTransformerEmbedder:
    """sentence-transformers backend (lazy-loaded). Real semantics when installed."""

    def __init__(self, model_name: str = DEFAULT_MODEL, *, batch_size: int = 64):
        self.model_name = model_name
        self.batch_size = max(1, int(batch_size))
        self._model = None
        self.semantic_quality = "multilingual"

    def _ensure_model(self) -> None:
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # lazy / optional dep

            # Search and upload are certified offline paths. Never let a new/unknown document
            # trigger a network lookup or silently change the model revision.
            self._model = SentenceTransformer(self.model_name, local_files_only=True)

    @property
    def dim(self) -> int:
        self._ensure_model()
        # sentence-transformers 5.x renamed this accessor. Prefer the current API while keeping
        # compatibility with the older pinned runtime used by existing offline installations.
        getter = getattr(self._model, "get_embedding_dimension", None)
        if getter is None:
            getter = self._model.get_sentence_embedding_dimension
        return int(getter())

    def encode(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        arr = self._model.encode(texts, batch_size=self.batch_size,
                                 normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, row)) for row in arr]


# Backwards-compatible alias for the original stub name.
Embedder = SentenceTransformerEmbedder


def get_embedder(config: dict) -> tuple[object, str]:
    """Select an embedder from ``config['index']``. Returns ``(embedder, name)``.

    ``embedding_backend``: "auto" (default) | "hashing" | "sentence-transformers".
    """
    index_cfg = (config or {}).get("index", {})
    backend = index_cfg.get("embedding_backend", "auto")
    model_name = index_cfg.get("embedding_model", DEFAULT_MODEL)

    def _hashing() -> tuple[object, str]:
        return HashingEmbedder(dim=index_cfg.get("hashing_dim", 256)), "hashing"

    if backend == "hashing":
        return _hashing()
    if backend in ("auto", "sentence-transformers"):
        try:
            import sentence_transformers  # noqa: F401 — availability probe
            return SentenceTransformerEmbedder(
                model_name, batch_size=index_cfg.get("embedding_inner_batch_size", 64)
            ), model_name
        except Exception as exc:  # noqa: BLE001
            if backend == "sentence-transformers":
                raise
            if index_cfg.get("strict_runtime", False):
                raise RuntimeError(
                    "Certified runtime requires sentence-transformers and the configured local "
                    f"model ({model_name}); install the pinned model before building or running."
                ) from exc
            print(
                f"WARN embeddings: sentence-transformers unavailable ({type(exc).__name__}); "
                f"falling back to deterministic HashingEmbedder.",
                file=sys.stderr,
            )
            return _hashing()
    raise ValueError(f"unknown embedding_backend: {backend!r}")
