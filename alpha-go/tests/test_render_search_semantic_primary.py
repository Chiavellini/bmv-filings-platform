"""render_search surfaces the curated ANALOG engine (analogs.yaml), not generic embedding similarity.

Two guarantees:
  * A semantic-only hit (embedding neighbor, no literal or analog match) is NOT surfaced — the
    generic "similar company" noise (Bimbo for "convenience store") is gone.
  * A doc containing a curated analog ALIAS of the query (but not the literal phrase) surfaces in a
    clearly-labeled "Analogs" band with a "via <alias>" tag.
"""
from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

from src.search.filters import SearchFilters

from app.components.panels import render_search


class _FakeCol:
    """Two-pane column stand-in — usable as a context manager (`with left:`)."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeSt:
    def __init__(self):
        self.session_state: dict = {}
        self.captions: list = []
        self.warnings: list = []
        self.markdowns: list = []
        self.infos: list = []

    def caption(self, msg, *a, **k):
        self.captions.append(msg)

    def warning(self, msg, *a, **k):
        self.warnings.append(msg)

    def info(self, msg, *a, **k):
        self.infos.append(msg)

    def markdown(self, msg="", *a, **k):
        self.markdowns.append(msg)

    def divider(self, *a, **k):
        return None

    def toggle(self, *a, **k):
        return False

    def button(self, *a, **k):
        return False

    def columns(self, spec, *a, **k):
        n = len(spec) if hasattr(spec, "__len__") else int(spec)
        return [_FakeCol() for _ in range(n)]

    def expander(self, *a, **k):
        return nullcontext()


class _Store:
    """Minimal store: one document whose markdown text is supplied by the test."""

    def __init__(self, md_path: str, doc_id: str = "testco/2022-2T", company: str = "testco"):
        self._md, self._doc_id, self._company = md_path, doc_id, company

    def get_meta(self, key):
        return None

    def document_rows(self, where="", params=()):
        return [{"doc_id": self._doc_id, "company": self._company, "period": "2022-2T",
                 "doc_type": "release", "title": "t", "markdown_path": self._md}]

    def count(self, _table):
        return 1


class _Retriever:
    def __init__(self, store, hits):
        self.store = store
        self._hits = hits

    def search(self, *a, **k):
        return list(self._hits)


def test_semantic_only_hit_is_not_surfaced(tmp_path):
    # A doc with NEITHER the literal query phrase NOR any curated analog alias. The retriever
    # returns it as a semantic-only hit (embedding neighbor) — which must NOT surface anymore.
    doc = tmp_path / "testco_2022-2T.md"
    doc.write_text("Regional performance across the continent was strong this quarter.",
                   encoding="utf-8")
    store = _Store(str(doc))
    hit = SimpleNamespace(
        doc_id="testco/2022-2T", company="testco", period="2022-2T", doc_type="release",
        title="t", score=0.4, snippet=SimpleNamespace(text="Regional performance"),
        markdown_path=str(doc), char_start=0, char_end=20, chunk_id="c1", keyword_match=False)
    st = _FakeSt()

    render_search(st, _Retriever(store, [hit]), query="zzz nonexistent phrase",
                  filters=SearchFilters(), limit=20)

    # No literal mention and no analog alias → "No matches"; the embedding neighbor is NOT shown.
    assert any("No matches" in w for w in st.warnings), st.warnings
    assert not any(isinstance(m, str) and m.startswith("**Analogs**") for m in st.markdowns)


def test_analog_alias_surfaces_analogs_band(tmp_path):
    # The doc discusses OXXO (a curated analog of "convenience store" in analogs.yaml) but never
    # says the literal phrase → it must surface in the ANALOGS band, tagged "via OXXO".
    doc = tmp_path / "kof_2022-3T.md"
    doc.write_text("Our proximity format grew: OXXO added new stores across the region.",
                   encoding="utf-8")
    store = _Store(str(doc), doc_id="kof/2022-3T", company="kof")
    st = _FakeSt()

    render_search(st, _Retriever(store, []), query="convenience store",
                  filters=SearchFilters(), limit=20)

    assert st.warnings == []
    # A clearly-labeled Analogs band was rendered (curated analog engine, not embedding similarity).
    assert any(isinstance(m, str) and m.startswith("**Analogs**") for m in st.markdowns), st.markdowns
    # The reader auto-opened the analog doc and captioned it as an analog match via OXXO.
    assert st.session_state.get("reader_doc") == "kof/2022-3T"
    assert any("Analog match" in c and "OXXO" in c for c in st.captions), st.captions


def test_fx_keeps_spanish_metric_aliases_in_transparent_expansion_lane(tmp_path):
    doc = tmp_path / "banorte_2024-1T.md"
    doc.write_text(
        "El tipo de cambio se mantuvo estable. El efecto cambiario fue favorable.",
        encoding="utf-8",
    )
    store = _Store(str(doc), doc_id="banorte/2024-1T", company="banorte")
    st = _FakeSt()

    render_search(st, _Retriever(store, []), query="fx",
                  filters=SearchFilters(), limit=20)

    exact_headers = [m for m in st.markdowns
                     if isinstance(m, str) and m.startswith("**Mentions**")]
    expanded_headers = [m for m in st.markdowns
                        if isinstance(m, str) and m.startswith("**Related wording**")]
    assert exact_headers == []
    assert expanded_headers == ["**Related wording** (curated discovery) — 1 document(s)"]
    assert any(
        "not added to the Mentions count" in caption for caption in st.captions
    )
    assert not any(isinstance(m, str) and m.startswith("**Analogs**")
                   for m in st.markdowns)
    assert any("tipo de cambio" in c for c in st.captions)
