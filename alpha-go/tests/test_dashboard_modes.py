"""The Explore page exposes only the two supported manual-test surfaces."""

import sqlite3

from app.streamlit_app import (
    _EXPLORE_MODES,
    _align_runtime_to_index,
    _compact_scope_names,
    _valid_multiselect_state,
)


def test_explore_modes_are_search_trends_ask():
    # Summary/Financials/News stay out of the mode list (they live inside the reader pane now);
    # Ask is the third mode (Phase C).
    assert _EXPLORE_MODES == ("Search", "Trends", "Ask")


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


# --- shareable links: ?q=&co=&dt=&ind=&doc= round-trip through session state ------------------

def test_state_from_params_restores_query_scope_and_document():
    from app.streamlit_app import _state_from_params

    state = _state_from_params({"q": "pricing pressure", "co": "walmex,bimbo",
                                "dt": "Quarterly release", "doc": "walmex/2026-1T"})
    assert state == {"query": "pricing pressure", "flt-companies": ["walmex", "bimbo"],
                     "flt-doctypes": ["Quarterly release"],
                     "_pending_reader_doc": "walmex/2026-1T"}
    assert _state_from_params({}) == {}
    assert _state_from_params({"co": ""}) == {}


def test_params_from_state_is_the_inverse_and_omits_empties():
    from app.streamlit_app import _params_from_state, _state_from_params

    params = _params_from_state(query="  ebitda ", companies=["ac"], doc_type_labels=[],
                                industries=["retail"], reader_doc="ac/2025-4T")
    assert params == {"q": "ebitda", "co": "ac", "ind": "retail", "doc": "ac/2025-4T"}
    assert _state_from_params(params) == {"query": "ebitda", "flt-companies": ["ac"],
                                          "flt-industries": ["retail"],
                                          "_pending_reader_doc": "ac/2025-4T"}
    assert _params_from_state(query="", companies=[], doc_type_labels=[], industries=[],
                              reader_doc=None) == {}
