"""Ask threads — SQLite persistence for conversations (create, turns, history, delete)."""
from __future__ import annotations

from src.state.threads import SCHEMA_VERSION, ThreadStore, default_threads_path


def test_thread_lifecycle_round_trips(tmp_path):
    store = ThreadStore(tmp_path / "state" / "threads.db")
    t = store.create_thread("  How has   tone changed? ", scope={"companies": ["walmex"]})
    assert t.title == "How has tone changed?" and t.turns == 0
    assert store.list_threads()[0].thread_id == t.thread_id

    a = store.add_turn(t.thread_id, question="q1", answer_text="a1 [1]", mode="llm",
                       plan={"sub_queries": ["x"]}, citations=[{"marker": 1, "doc_id": "d"}])
    b = store.add_turn(t.thread_id, question="q2", answer_text=None, mode="evidence")
    assert (a.ordinal, b.ordinal) == (1, 2)
    turns = store.turns(t.thread_id)
    assert [x.question for x in turns] == ["q1", "q2"]
    assert turns[0].plan == {"sub_queries": ["x"]} and turns[0].citations[0]["doc_id"] == "d"
    assert turns[1].answer_text is None and turns[1].mode == "evidence"
    assert store.history(t.thread_id) == [("q1", "a1 [1]")]         # unanswered turns excluded
    assert store.get_thread(t.thread_id).turns == 2

    store.rename_thread(t.thread_id, "Renamed")
    assert store.get_thread(t.thread_id).title == "Renamed"
    store.delete_thread(t.thread_id)
    assert store.list_threads() == [] and store.turns(t.thread_id) == []
    assert store.connect().execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] \
        == str(SCHEMA_VERSION)


def test_reopening_the_database_keeps_threads(tmp_path):
    path = tmp_path / "threads.db"
    ThreadStore(path).create_thread("persist me", thread_id="abc")
    again = ThreadStore(path)
    assert [t.title for t in again.list_threads()] == ["persist me"]
    assert again.get_thread("abc").thread_id == "abc" and again.get_thread("nope") is None


def test_default_threads_path_is_outside_the_index_and_a_db_file():
    p = default_threads_path()
    assert p.suffix == ".db" and "index" not in p.parts
