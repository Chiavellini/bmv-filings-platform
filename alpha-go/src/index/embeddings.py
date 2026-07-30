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


def _semantic_cpu_worker(
    target_device,
    model,
    input_queue,
    output_queue,
    threads: int,
) -> None:
    """Sentence-transformers worker with an explicit per-process CPU budget."""
    import torch

    torch.set_num_threads(max(1, int(threads)))
    model.__class__._multi_process_worker(
        target_device, model, input_queue, output_queue
    )


def _validate_semantic_tokenizer(model, model_name: str) -> None:
    """Reject incomplete local snapshots that silently tokenize everything as unknown."""
    tokenizer = getattr(model, "tokenizer", None)
    vocab_size = int(getattr(tokenizer, "vocab_size", 0) or 0)
    if tokenizer is None or vocab_size < 1_000:
        raise RuntimeError(
            f"Incomplete local model snapshot for {model_name}: tokenizer vocabulary "
            f"has {vocab_size} entries. Download the model tokenizer assets before indexing."
        )
    unknown = getattr(tokenizer, "unk_token_id", None)
    if unknown is None:
        return
    probe_ids = [
        token_id
        for phrase in ("passengers", "pasajeros")
        for token_id in tokenizer.encode(phrase, add_special_tokens=False)
    ]
    if not probe_ids or all(token_id == unknown for token_id in probe_ids):
        raise RuntimeError(
            f"Incomplete local model snapshot for {model_name}: English/Spanish "
            "health probes tokenize only as unknown tokens."
        )


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

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        batch_size: int = 64,
        model_path: str | None = None,
    ):
        self.model_name = model_name
        self.model_path = model_path
        self.batch_size = max(1, int(batch_size))
        self._model = None
        self._pool = None
        self.semantic_quality = "multilingual"

    def _ensure_model(self) -> None:
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # lazy / optional dep

            # Search and upload are certified offline paths. Never let a new/unknown document
            # trigger a network lookup or silently change the model revision.
            self._model = SentenceTransformer(
                self.model_path or self.model_name,
                local_files_only=True,
            )
            _validate_semantic_tokenizer(self._model, self.model_name)

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
        kwargs = {}
        if self._pool is not None:
            kwargs["pool"] = self._pool
            kwargs["chunk_size"] = max(1, len(texts) // (len(self._pool["processes"]) * 2))
        arr = self._model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            **kwargs,
        )
        return [list(map(float, row)) for row in arr]

    def start_multi_process_pool(
        self, workers: int, worker_threads: int | None = None
    ) -> None:
        """Keep CPU embedding workers alive across resumable migration batches."""
        self._ensure_model()
        if self._pool is None and int(workers) > 1:
            if worker_threads is None:
                self._pool = self._model.start_multi_process_pool(
                    ["cpu"] * int(workers)
                )
            else:
                import torch.multiprocessing as mp

                self._model.to("cpu")
                self._model.share_memory()
                ctx = mp.get_context("spawn")
                input_queue = ctx.Queue()
                output_queue = ctx.Queue()
                processes = []
                for _ in range(int(workers)):
                    process = ctx.Process(
                        target=_semantic_cpu_worker,
                        args=(
                            "cpu",
                            self._model,
                            input_queue,
                            output_queue,
                            int(worker_threads),
                        ),
                        daemon=True,
                    )
                    process.start()
                    processes.append(process)
                self._pool = {
                    "input": input_queue,
                    "output": output_queue,
                    "processes": processes,
                }

    def stop_multi_process_pool(self) -> None:
        if self._pool is not None:
            self._model.stop_multi_process_pool(self._pool)
            self._pool = None


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
            model_path = index_cfg.get("embedding_model_path")
            if not model_path:
                try:
                    from src.shared.paths import ESTATE_BRIDGE

                    portable_model = (
                        ESTATE_BRIDGE.alpha_go_embedding_model_path
                    )
                    if portable_model.is_dir():
                        model_path = str(portable_model)
                except (ImportError, AttributeError):
                    pass
            return SentenceTransformerEmbedder(
                model_name,
                batch_size=index_cfg.get("embedding_inner_batch_size", 64),
                model_path=model_path,
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
