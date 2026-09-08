"""alpha-go dashboard — local AlphaSense.

Search the offline corpus with hybrid keyword + semantic retrieval, view the source document
with the match highlighted, and inspect trends and per-company financials. Zero-typing UX:
every filter and secondary action is a click (widgets are populated from the index via
``src.search.facets``); the one typed field is the search query itself, and even that can
start from a suggestion pill. The embedding model and the vector matrix are loaded once per
session (``st.cache_resource``) so search stays warm.

Run from the alpha-go/ root (use a venv that has streamlit installed):
    .venv/bin/python -m streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import os
import sqlite3

# Load the embedding model from the local HF cache only — no network at query time. Must be set
# before sentence-transformers/transformers import. Weights are already cached from build_index.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import sys
from pathlib import Path

# Make `import src.*` / `import app.*` resolve to alpha-go's vendored copy when launched via
# `streamlit run` (streamlit doesn't apply pyproject's pythonpath the way pytest does).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402
import streamlit as st  # noqa: E402

from app.components import ask as ask_panel  # noqa: E402
from app.components import panels  # noqa: E402
from app.components import upload as upload_panel  # noqa: E402
from src.corpus.doc_types import load_taxonomy  # noqa: E402
from src.index.store import IndexStore  # noqa: E402
from src.search.facets import facet_values, suggested_terms  # noqa: E402
from src.search.filters import SearchFilters  # noqa: E402
from src.search.retriever import HybridRetriever  # noqa: E402
from src.shared.paths import ESTATE_BRIDGE  # noqa: E402

_CONFIG_PATH = _ROOT / "configs" / "alpha_go.yaml"
_BMV_CATALOG_PATH = _ROOT / "configs" / "bmv_corpus.yaml"
_EXPLORE_MODES = ("Search", "Trends", "Ask")
_URL_SEEDED = "_url_seeded"
# Shareable-link contract: the address bar mirrors the search (q), the scope (co/dt/ind — the
# sidebar widgets' own values) and the open document (doc). Restored once per session.
_URL_KEYS = ("q", "co", "dt", "ind", "doc")


def _state_from_params(params) -> dict:
    """Session-state updates that restore a shareable link's search (pure; ``params`` is a mapping)."""
    def _get(k):
        v = params.get(k) if hasattr(params, "get") else None
        return v if isinstance(v, str) else (v[0] if isinstance(v, (list, tuple)) and v else None)

    def _split(v):
        return [x for x in (v or "").split(",") if x]

    out: dict = {}
    if (q := _get("q")):
        out["query"] = q
    if (co := _split(_get("co"))):
        out["flt-companies"] = co
    if (dt := _split(_get("dt"))):
        out["flt-doctypes"] = dt
    if (ind := _split(_get("ind"))):
        out["flt-industries"] = ind
    if (doc := _get("doc")):
        out["_pending_reader_doc"] = doc
    return out


def _params_from_state(*, query: str, companies: list, doc_type_labels: list,
                       industries: list, reader_doc: "str | None") -> dict:
    """Query-parameter payload for the current search state (pure inverse of the above)."""
    out: dict = {}
    if (query or "").strip():
        out["q"] = query.strip()
    if companies:
        out["co"] = ",".join(companies)
    if doc_type_labels:
        out["dt"] = ",".join(doc_type_labels)
    if industries:
        out["ind"] = ",".join(industries)
    if reader_doc:
        out["doc"] = reader_doc
    return out


def _seed_state_from_url() -> None:
    """Apply a shareable link once per session, before the widgets it targets are created."""
    if st.session_state.get(_URL_SEEDED):
        return
    st.session_state[_URL_SEEDED] = True
    try:
        updates = _state_from_params(st.query_params)
    except Exception:  # noqa: BLE001 — a malformed link must never break the page
        return
    for k, v in updates.items():
        st.session_state[k] = v


def _sync_url(desired: dict) -> None:
    """Mirror the current search into the address bar (no rerun) when it changed."""
    try:
        current = {k: st.query_params.get(k) for k in _URL_KEYS}
        current = {k: v for k, v in current.items() if v}
        if current != desired:
            st.query_params.from_dict(desired)
    except Exception:  # noqa: BLE001 — cosmetic; never fail the page over the URL
        return


def _valid_multiselect_state(value, options: list[str]) -> list[str]:
    """Keep a cascading multiselect's saved values inside its current option set.

    Streamlit persists widget state across reruns.  When an upstream industry selection narrows
    the company/document choices, stale downstream values must be removed before the widget is
    created or the UI can retain an impossible scope.
    """
    selected = value if isinstance(value, (list, tuple)) else []
    allowed = set(options)
    return [item for item in selected if item in allowed]


def _compact_scope_names(values: list[str], label, *, limit: int = 3) -> str:
    """Human-readable selection summary without turning the sidebar into a long tag list."""
    names = [label(value) for value in values]
    shown = names[:limit]
    suffix = f" +{len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(shown) + suffix


def _align_runtime_to_index(config: dict, db_path: Path) -> None:
    """Use the embedding runtime recorded by an already-built index.

    The checked-in config remains the certified semantic target. A portable
    estate can temporarily carry a deterministic hashing index; opening that
    exact vector space must not attempt to load a different model or depend on
    undocumented shell overrides.
    """
    if not db_path.is_file():
        return
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        metadata = dict(
            conn.execute(
                """SELECT key,value FROM meta
                   WHERE key IN ('embedding_model','embedding_dim')"""
            )
        )
        conn.close()
    except sqlite3.Error:
        return
    if metadata.get("embedding_model") == "hashing":
        index = config.setdefault("index", {})
        index.update(
            {
                "embedding_backend": "hashing",
                "strict_runtime": False,
                "hashing_dim": int(metadata.get("embedding_dim") or 256),
            }
        )


def _runtime_config() -> dict:
    """Load the certified config, with opt-in local dashboard overrides.

    These environment variables are deliberately runtime-only: they make it possible to open a
    validated fallback index for manual QA without changing the checked-in certified model/index
    settings.  Normal launches leave all three unset and use ``alpha_go.yaml`` verbatim.
    """
    config = yaml.safe_load(_CONFIG_PATH.read_text())
    if _BMV_CATALOG_PATH.exists():
        catalog = yaml.safe_load(_BMV_CATALOG_PATH.read_text(encoding="utf-8")) or {}
        config["company_catalog"] = catalog.get("companies", [])
        # The 50×50 coverage contract the company card measures each issuer against.
        config["coverage_contract"] = {
            k: catalog[k] for k in ("target_documents_per_company",
                                    "target_news_documents_per_company",
                                    "minimum_quarterlies", "minimum_annuals") if k in catalog}
    index = config.setdefault("index", {})
    if db_path := os.getenv("ALPHA_GO_INDEX_DB"):
        index["db_path"] = db_path
    elif ESTATE_BRIDGE.alpha_go_index_path.is_file():
        # A portable estate bundle may ship its certified index beside the
        # catalog. The bridge then becomes the only location users configure.
        index["db_path"] = str(ESTATE_BRIDGE.alpha_go_index_path)
    if backend := os.getenv("ALPHA_GO_EMBEDDING_BACKEND"):
        index["embedding_backend"] = backend
    else:
        selected_db = Path(index["db_path"])
        if not selected_db.is_absolute():
            selected_db = _ROOT / selected_db
        _align_runtime_to_index(config, selected_db)
    if os.getenv("ALPHA_GO_STRICT_RUNTIME") is not None:
        index["strict_runtime"] = os.getenv("ALPHA_GO_STRICT_RUNTIME", "").lower() in {
            "1", "true", "yes", "on"}
    return config


@st.cache_resource(show_spinner="Loading index + embedding model…")
def _resources():
    """Open the index and build the retriever once per session (model loads lazily, then warms)."""
    config = _runtime_config()
    db_path = Path(config["index"]["db_path"])
    if not db_path.is_absolute():
        db_path = _ROOT / db_path
    store = IndexStore(db_path)
    store.connect()
    retriever = HybridRetriever(store, config=config)
    # Resolve and validate the certified local model while the resource is being initialized;
    # this prevents a query-time fallback to a different embedding space.
    retriever.get_embedder()
    return store, retriever, config


def _index_revision(store: IndexStore) -> int:
    """Return SQLite's cross-process commit token for cache keying."""

    return int(store.connect().execute("PRAGMA data_version").fetchone()[0])


@st.cache_data(show_spinner=False)
def _facets(index_revision_token: int):
    """Clickable filter options straight from the index (companies, doc types, periods…)."""
    del index_revision_token  # used only as the Streamlit cache key
    store, _, _ = _resources()
    return facet_values(store)


@st.cache_data(show_spinner=False)
def _suggestions(index_revision_token: int):
    """Suggestion pills, filtered to concepts that actually occur in THIS corpus — the
    dictionary is shared with other projects and offers e.g. gym-membership metrics."""
    del index_revision_token  # used only as the Streamlit cache key
    store, _, _ = _resources()
    conn = store.connect()
    out = []
    for term in suggested_terms(max_n=40):
        row = conn.execute("SELECT count(*) AS n FROM chunks_fts WHERE chunks_fts MATCH ?",
                           [f'"{term}"']).fetchone()
        if row and row["n"] > 0:
            out.append(term)
        if len(out) >= 12:
            break
    return out


def _page_search() -> None:
    """Explore page: sidebar filters + the Search / Trends tabs over the offline corpus."""
    st.title("alpha-go")
    st.caption("Local AlphaSense — offline financial-document search & intelligence")

    try:
        store, retriever, config = _resources()
    except Exception as exc:  # noqa: BLE001 — surface a clear message instead of a stack trace
        st.error(f"Could not open the index ({type(exc).__name__}: {exc}). "
                 "Build it first: `python3 scripts/build_index.py`.")
        return

    n_docs = store.count("documents")
    n_chunks = store.count("chunks")
    index_revision = _index_revision(store)
    facets = _facets(index_revision)
    taxonomy = load_taxonomy(config)
    model_name = store.get_meta("embedding_model") or "unknown"
    company_names = {
        str(source.get("slug")): str(source.get("company") or source.get("slug"))
        for source in [*(config.get("sources") or []), *(config.get("company_catalog") or [])]
        if source.get("slug")
    }

    def company_label(value: str) -> str:
        return company_names.get(value, value.replace("_", " ").upper())

    def industry_label(value: str) -> str:
        return value.replace("_", " ").title()

    _seed_state_from_url()
    panels.apply_pending_state(st.session_state)   # scope chips / "try wording" from last run

    if model_name == "hashing":
        st.warning(
            "Lexical fallback is active. Exact search and vetted Spanish/English direct "
            "equivalences work; broad conceptual retrieval still needs the multilingual index."
        )

    with st.sidebar:
        st.header("Search scope")
        st.caption("Choose one or many at each level. Empty means all within the scope above.")

        # The three primary controls form a visible hierarchy.  Each is a searchable multiselect:
        # empty Industries means the whole corpus; empty Companies means every company in the
        # selected industries; empty Documents means every document in the resulting company set.
        industries = st.multiselect(
            "1 · Industries",
            facets.industries,
            key="flt-industries",
            format_func=industry_label,
            placeholder="All industries",
            help="Leave empty to include every industry.",
        )

        company_options = facets.companies_in(industries)
        st.session_state["flt-companies"] = _valid_multiselect_state(
            st.session_state.get("flt-companies"), company_options,
        )
        company_scope = st.multiselect(
            "2 · Companies",
            company_options,
            key="flt-companies",
            format_func=company_label,
            placeholder=("All companies in selected industries" if industries
                         else "All companies"),
            help="Select companies from one or several industries, or leave empty for all.",
        )

        # Types are a first-class corpus slice, not a hidden advanced filter.  Build their labels
        # from the configured taxonomy plus any legacy/raw types present in the live index.
        raw_by_label: dict[str, list[str]] = {}
        for raw in [*taxonomy.keys(), *facets.doc_types]:
            raw_by_label.setdefault(taxonomy.label_for(raw), []).append(raw)
        doc_labels = list(raw_by_label)
        st.session_state["flt-doctypes"] = _valid_multiselect_state(
            st.session_state.get("flt-doctypes"), doc_labels,
        )
        picked_doc_labels = st.multiselect(
            "3 · Document types",
            doc_labels,
            key="flt-doctypes",
            placeholder="All document types",
            help="News, quarterly releases, annual reports, press releases, and other corpus types.",
        )
        doc_type_keys = [k for label in picked_doc_labels for k in raw_by_label[label]]

        # Exact-document selection is an optional final narrowing step.  It respects all prior
        # scope choices, including document types, so its list stays compact on a broad corpus.
        document_choices = facets.documents_for(company_scope, industries, doc_types=doc_type_keys)
        documents_by_id = {doc.doc_id: doc for doc in document_choices}
        document_ids = list(documents_by_id)
        st.session_state["flt-documents"] = _valid_multiselect_state(
            st.session_state.get("flt-documents"), document_ids,
        )

        def document_label(doc_id: str) -> str:
            doc = documents_by_id[doc_id]
            parts = []
            if len(company_scope) != 1:
                parts.append(company_label(doc.company))
            if doc.period:
                parts.append(doc.period)
            if doc.doc_type:
                parts.append(taxonomy.label_for(doc.doc_type))
            parts.append(" ".join(doc.title.split()))
            return " · ".join(parts)

        document_scope = st.multiselect(
            "4 · Specific documents (optional)",
            document_ids,
            key="flt-documents",
            format_func=document_label,
            placeholder=("All documents for selected companies" if company_scope
                         else "All documents in current scope"),
            help="Leave empty to search every document in the selected type(s).",
        )

        with st.expander("More filters"):
            if document_scope:
                # Exact documents are the terminal scope. Ignoring period/type here prevents a
                # hidden contradictory filter from turning an explicit selection into zero hits.
                period_options: list[str] = []
                period_lo = period_hi = None
                st.caption("Exact documents selected; only the period filter is not needed.")
            else:
                period_scope = (company_scope or
                                (facets.companies_in(industries) if industries else []))
                period_options = facets.periods_for(period_scope)
                if len(period_options) >= 2:
                    prev = st.session_state.get("flt-period")
                    lo, hi = (prev if isinstance(prev, (list, tuple)) and len(prev) == 2
                              else (None, None))
                    st.session_state["flt-period"] = (
                        lo if lo in period_options else period_options[0],
                        hi if hi in period_options else period_options[-1],
                    )
                    period_lo, period_hi = st.select_slider(
                        "Period range", options=period_options, key="flt-period",
                    )
                else:
                    period_lo = period_hi = None

            default_limit = int((config.get("search") or {}).get("default_limit", 20))
            limit = st.slider("Max documents", 5, 50, default_limit, 5)

        # A full-range period selection means no period constraint.  Calculate the effective
        # document count with the same SearchFilters contract used by Search and Trends.
        full_range = bool(period_options) and (period_lo, period_hi) == (
            period_options[0], period_options[-1])
        filters = SearchFilters(
            doc_ids=document_scope,
            companies=company_scope,
            doc_types=doc_type_keys,
            period_from=None if full_range else period_lo,
            period_to=None if full_range else period_hi,
            industries=industries,
        )
        where, params = filters.to_sql() if not filters.is_empty() else ("", [])
        scoped_docs = len(store.document_rows(where, params))

        if document_scope:
            scope_name = f"{len(document_scope)} selected document(s)"
        elif company_scope:
            scope_name = _compact_scope_names(company_scope, company_label)
        elif industries:
            scope_name = _compact_scope_names(industries, industry_label)
        else:
            scope_name = "Whole corpus"
        st.info(f"**Searching:** {scope_name}\n\n{scoped_docs} document(s) in scope")

        st.divider()
        st.caption(f"Index: {n_docs} docs · {n_chunks} chunks")
        st.caption(f"Model: {model_name}")

    query = st.text_input("Search the corpus", key="query",
                          placeholder="e.g. raw material cost pressure on margins")
    st.caption(
        "Mentions are literal occurrences of the query plus vetted direct Spanish/English equivalents "
        "(case- and accent-insensitive). "
        "Related wording and semantic concepts are separate discovery lanes and never inflate "
        "the Mentions count."
    )
    st.caption(
        "Bilingual mode: Spanish and English exact search, transparent curated ES/EN related "
        "wording, and multilingual conceptual discovery when the certified model is available."
    )

    mode = st.radio("View", _EXPLORE_MODES,
                    horizontal=True, label_visibility="collapsed", key="explore-mode")
    if mode == "Search":
        panels.render_search(st, retriever, query=query, filters=filters, limit=limit,
                             search_cfg=config.get("search"), doc_type_label=taxonomy.label_for,
                             app_config=config)
    elif mode == "Ask":
        ask_panel.render_ask(st, retriever, store, query=query, filters=filters, config=config,
                             facets=facets, company_label=company_label,
                             doc_type_label=taxonomy.label_for)
    else:
        panels.render_trends(st, store, term=query, filters=filters,
                             suggestions=_suggestions(index_revision))

    _sync_url(_params_from_state(
        query=query, companies=list(company_scope), doc_type_labels=list(picked_doc_labels),
        industries=list(industries), reader_doc=st.session_state.get("reader_doc")))
    if (query or "").strip():
        st.caption("🔗 The address bar mirrors this search, scope and open document — copy it to share.")


def _page_upload() -> None:
    """Corpus page: add a document — with auto-detected type + company — to the live index."""
    try:
        store, retriever, config = _resources()
    except Exception as exc:  # noqa: BLE001 — surface a clear message instead of a stack trace
        st.error(f"Could not open the index ({type(exc).__name__}: {exc}). "
                 "Build it first: `python3 scripts/build_index.py`.")
        return
    upload_panel.render_upload(
        st,
        config,
        store,
        retriever,
        facets=_facets(_index_revision(store)),
        taxonomy=load_taxonomy(config),
    )


def main() -> None:
    st.set_page_config(page_title="alpha-go · local AlphaSense", layout="wide")
    nav = st.navigation({
        "Explore": [st.Page(_page_search, title="Search", icon="🔎", default=True)],
        "Corpus": [st.Page(_page_upload, title="Upload", icon="⬆️")],
    })
    nav.run()


if __name__ == "__main__":
    main()
