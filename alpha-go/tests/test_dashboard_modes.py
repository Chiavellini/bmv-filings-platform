"""The Explore page exposes only the two supported manual-test surfaces."""

import sqlite3

from app.streamlit_app import (
    _EXPLORE_MODES,
    _align_runtime_to_index,
    _compact_scope_names,
    _valid_multiselect_state,
)


def test_explore_modes_exclude_removed_panels():
    assert _EXPLORE_MODES == ("Search", "Trends")


def test_cascading_multiselect_discards_values_outside_upstream_scope():
    assert _valid_multiselect_state(["food", "retail"], ["retail", "telecom"]) == ["retail"]
    assert _valid_multiselect_state("retail", ["retail"]) == []


def test_scope_summary_stays_compact_for_many_selections():
    assert _compact_scope_names(["a", "b"], str) == "a, b"
    assert _compact_scope_names(["a", "b", "c", "d", "e"], str) == "a, b, c +2 more"


def test_news_is_not_a_standalone_navigation_surface():
    import inspect
    from app import streamlit_app

    assert "_page_news" not in inspect.getsource(streamlit_app.main)


def test_hashing_estate_index_selects_its_recorded_runtime(tmp_path):
    database = tmp_path / "alpha.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
    conn.executemany(
        "INSERT INTO meta(key,value) VALUES(?,?)",
        (("embedding_model", "hashing"), ("embedding_dim", "384")),
    )
    conn.commit()
    conn.close()
    config = {
        "index": {
            "embedding_backend": "auto",
            "strict_runtime": True,
            "embedding_model": "certified-multilingual-model",
        }
    }

    _align_runtime_to_index(config, database)

    assert config["index"]["embedding_backend"] == "hashing"
    assert config["index"]["strict_runtime"] is False
    assert config["index"]["hashing_dim"] == 384
    assert (
        config["index"]["embedding_model"]
        == "certified-multilingual-model"
    )
