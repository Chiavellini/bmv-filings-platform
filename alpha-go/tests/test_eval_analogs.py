"""Tests for the analog-PRECISION eval infrastructure (scripts/eval_analogs.py).

Scope is deliberately narrow: the candidate COLLECTOR's shape/provenance and the METRIC
skeleton's arithmetic. These do NOT assert any tuned analog behavior (that lives in
tests/test_analogs.py) — they only guard that the eval plumbing collects the right fields,
never leaks a self-judged relevance label, and computes precision / false-fire correctly once
a judge's labels exist.
"""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from src.corpus.ingest import ingest_source
from src.index.build import build_index
from src.index.embeddings import HashingEmbedder
from src.index.keyword_index import _analog_meta, _dictionary

ROOT = Path(__file__).resolve().parents[1]


def _load_eval_analogs():
    """Import scripts/eval_analogs.py (not a package) as a module."""
    spec = importlib.util.spec_from_file_location(
        "eval_analogs", ROOT / "scripts" / "eval_analogs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EA = _load_eval_analogs()


# --------------------------------------------------------------------------------------------
# Metric skeleton (pure) — arithmetic only, on synthetic judged records.
# --------------------------------------------------------------------------------------------
def test_precision_and_noise_counts_relevant_over_labeled():
    records = [
        {"analog_candidates": [
            {"relevant": True}, {"relevant": False}, {"relevant": True},
        ]},
        {"analog_candidates": [
            {"relevant": True}, {},  # the second is unlabeled → skipped, counted separately
        ]},
    ]
    m = EA.precision_and_noise(records)
    assert m["labeled_hits"] == 4
    assert m["relevant_hits"] == 3
    assert m["unlabeled_hits"] == 1
    assert m["precision"] == pytest.approx(0.75)
    assert m["noise_rate"] == pytest.approx(0.25)


def test_precision_at_k_caps_per_query():
    records = [
        {"analog_candidates": [
            {"relevant": True}, {"relevant": True}, {"relevant": False},
        ]},
    ]
    # k=2 keeps only the first two (both relevant) → precision 1.0
    assert EA.precision_and_noise(records, k=2)["precision"] == pytest.approx(1.0)
    # full → 2/3
    assert EA.precision_and_noise(records)["precision"] == pytest.approx(2 / 3)


def test_precision_none_when_nothing_labeled():
    m = EA.precision_and_noise([{"analog_candidates": [{}, {}]}])
    assert m["precision"] is None and m["noise_rate"] is None
    assert m["unlabeled_hits"] == 2


def test_polysemy_false_fire_rate():
    records = [
        # a trap_wrong that leaked a wrong-sense hit
        {"kind": "trap_wrong", "fired_concepts": ["pet_resin_plastic"],
         "analog_candidates": [{"wrong_sense": True}, {"wrong_sense": False}]},
        # a trap_wrong that fired nothing (clean)
        {"kind": "trap_wrong", "fired_concepts": [],
         "analog_candidates": []},
        # a trap_wrong judged but all correct-sense
        {"kind": "trap_wrong", "fired_concepts": ["x"],
         "analog_candidates": [{"wrong_sense": False}]},
        # non-trap rows are ignored
        {"kind": "trap_right", "fired_concepts": ["y"], "analog_candidates": []},
    ]
    ff = EA.polysemy_false_fire_rate(records)
    assert ff["trap_wrong_queries"] == 3
    assert ff["judged_queries"] == 2          # only the two with wrong_sense labels
    assert ff["judged_false_fire_queries"] == 1
    assert ff["judged_false_fire_rate"] == pytest.approx(0.5)
    assert ff["proxy_fired_any_concept"] == 2  # label-free: two trap_wrong fired a concept


def test_short_snippet_is_single_line_and_anchored():
    text = "Alpha beta gamma. " * 20 + "PARAXYLENE feedstock costs rose sharply."
    snip = EA._short_snippet(text, ["paraxylene"])
    assert "\n" not in snip
    assert "paraxylene" in snip.lower()
    assert len(snip) <= EA._SNIPPET_CHARS + 2  # + optional leading/trailing ellipsis


# --------------------------------------------------------------------------------------------
# Collector shape / provenance — runs the real collect() over a tiny purpose-built index.
# --------------------------------------------------------------------------------------------
@pytest.fixture
def analog_index(tmp_path):
    """A minimal index whose only analog-domain chunk is reachable ONLY via alias expansion.

    The chunk carries `paraxylene`/`monoethylene glycol` (aliases of the pet_resin_plastic
    concept) but NONE of the literal query words (pet/resin/plastic/packaging) — so a query of
    those literal words can reach it only through analog expansion, exercising the analog-only
    provenance path.
    """
    _dictionary.cache_clear()
    _analog_meta.cache_clear()
    corpus_dir = tmp_path / "corpus"
    dest = corpus_dir / "polymerco"
    dest.mkdir(parents=True)
    (dest / "polymerco_1Q24.md").write_text(
        "# PolymerCo — First Quarter 2024 Report\n\n"
        "Feedstock update: our paraxylene and monoethylene glycol input prices rose "
        "sharply during the quarter, pressuring beverage container margins.\n\n"
        "Unrelated note: headcount in the finance team was stable.\n",
        encoding="utf-8",
    )
    ingest_source(
        {"slug": "polymerco", "company": "polymerco", "doc_types": ["report"],
         "language": "en", "ir_website": {}},
        corpus_dir,
    )
    db = tmp_path / "index" / "alpha.db"
    config = {"index": {"embedding_backend": "hashing", "hashing_dim": 64,
                        "chunk": {"target_chars": 400, "overlap_chars": 50}}}
    build_index(corpus_dir, db, config, embedder=HashingEmbedder(dim=64))
    return db


def test_collect_shape_and_provenance(analog_index):
    query = {"query": "PET resin and plastic packaging",
             "kind": "trap_right", "concept": "pet_resin_plastic",
             "expect_company": "polymerco"}
    doc = EA.collect(config_path=ROOT / "configs" / "alpha_go.yaml",
                     db_path=analog_index, queries=[query])

    assert doc["n_queries"] == 1
    rec = doc["records"][0]
    # Top-level provenance fields the judge relies on.
    for key in ("query", "kind", "concept", "fired_concepts",
                "base_literal_miss", "n_analog_candidates_total",
                "analog_candidates", "fused_top_k"):
        assert key in rec, f"missing record key {key!r}"
    assert "pet_resin_plastic" in rec["fired_concepts"]

    # The paraxylene chunk is analog-only (no literal query term) → it must appear as a candidate,
    # carry its band + concept provenance, and NEVER carry a pre-filled relevance label.
    assert rec["analog_candidates"], "expected at least one analog-only candidate"
    for cand in rec["analog_candidates"]:
        assert cand["band"] in ("strong", "weak")
        assert "pet_resin_plastic" in cand["fired_concepts"]
        assert cand["company"] == "polymerco"
        for key in ("chunk_id", "doc", "period", "snippet"):
            assert key in cand
        assert "relevant" not in cand      # collector must NOT self-judge
        assert "wrong_sense" not in cand


def test_collect_omits_relevance_everywhere(analog_index):
    """No candidate anywhere carries a relevance verdict — that is the judge's job, later."""
    query = {"query": "returnable glass bottle recycling", "kind": "trap_right",
             "concept": "glass_metal_containers", "expect_company": "polymerco"}
    doc = EA.collect(config_path=ROOT / "configs" / "alpha_go.yaml",
                     db_path=analog_index, queries=[query])
    for rec in doc["records"]:
        for cand in rec["analog_candidates"]:
            assert "relevant" not in cand and "wrong_sense" not in cand
