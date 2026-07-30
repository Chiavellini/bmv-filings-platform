"""Tests for reciprocal rank fusion (Phase 3)."""
from __future__ import annotations

from src.search.fusion import reciprocal_rank_fusion


def test_rrf_ranks_consensus_first():
    # 'b' appears high in both lists; it should win over single-list leaders.
    kw = ["a", "b", "c"]
    sem = ["b", "d", "a"]
    fused = reciprocal_rank_fusion([kw, sem])
    ids = [cid for cid, _ in fused]
    assert ids[0] == "b"
    assert set(ids) == {"a", "b", "c", "d"}


def test_rrf_weights_bias_toward_a_list():
    kw = ["x"]          # only in keyword list
    sem = ["y"]         # only in semantic list
    kw_heavy = reciprocal_rank_fusion([kw, sem], weights=[0.9, 0.1])
    assert kw_heavy[0][0] == "x"
    sem_heavy = reciprocal_rank_fusion([kw, sem], weights=[0.1, 0.9])
    assert sem_heavy[0][0] == "y"


def test_rrf_is_deterministic_on_ties():
    # Two ids with identical contributions must order by chunk_id ascending.
    out = reciprocal_rank_fusion([["m", "n"], ["n", "m"]])
    assert [cid for cid, _ in out] == ["m", "n"]


def test_rrf_empty_lists():
    assert reciprocal_rank_fusion([[], []]) == []


def test_rrf_validates_weight_length():
    import pytest
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([["a"], ["b"]], weights=[1.0])
