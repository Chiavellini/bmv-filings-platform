"""Legacy standalone News page renderer.

News is now selected from the main Search page's Document types control, alongside filings.
This module is retained only for backward-compatible imports; it is not exposed in navigation.
"""
from __future__ import annotations

from app.components import panels
from src.search.facets import facet_values
from src.search.filters import SearchFilters


def _valid(value, options: list[str]) -> list[str]:
    selected = value if isinstance(value, (list, tuple)) else []
    allowed = set(options)
    return [item for item in selected if item in allowed]


def render_news(st, *, store, retriever, company_names: dict[str, str]) -> None:
    st.title("News")
    st.caption("External company news — a separate, rights-aware corpus. Filing results never mix in.")
    n_docs, n_chunks = store.count("documents"), store.count("chunks")
    facets = facet_values(store)

    def company_label(value: str) -> str:
        return company_names.get(value, value.replace("_", " ").upper())

    def industry_label(value: str) -> str:
        return value.replace("_", " ").title()

    with st.sidebar:
        st.header("News scope")
        st.caption("Empty means all within the scope above. News articles can belong to several companies.")
        industries = st.multiselect("1 · Industries", facets.industries, key="news-industries",
                                    format_func=industry_label, placeholder="All industries")
        company_options = facets.companies_in(industries)
        st.session_state["news-companies"] = _valid(
            st.session_state.get("news-companies"), company_options,
        )
        companies = st.multiselect("2 · Companies", company_options, key="news-companies",
                                   format_func=company_label, placeholder="All companies")
        docs = facets.documents_for(companies, industries)
        doc_ids = [d.doc_id for d in docs]
        st.session_state["news-documents"] = _valid(
            st.session_state.get("news-documents"), doc_ids,
        )

        by_id = {d.doc_id: d for d in docs}
        def document_label(doc_id: str) -> str:
            doc = by_id[doc_id]
            return " · ".join(part for part in (
                company_label(doc.company), doc.period, doc.title,
            ) if part)
        documents = st.multiselect("3 · Articles", doc_ids, key="news-documents",
                                   format_func=document_label, placeholder="All articles")
        filters = SearchFilters(doc_ids=documents, companies=companies, industries=industries)
        where, params = filters.to_sql() if not filters.is_empty() else ("", [])
        scoped = len(store.document_rows(where, params))
        st.info(f"**Searching news:** {scoped} article(s) in scope")
        st.divider()
        st.caption(f"News index: {n_docs} articles · {n_chunks} chunks")

    query = st.text_input("Search news", key="news-query",
                          placeholder="e.g. foreign exchange exposure")
    st.caption("Exact mentions stay literal. Curated ES/EN related wording and multilingual concepts are separate lanes.")
    if not n_docs:
        st.info(
            "No news has been synced yet. Run `scripts/sync_news_gdelt.py --apply` for the default "
            "metadata/link-only discovery pilot, or configure an approved RSS/licensed provider."
        )
        return
    mode = st.radio("News view", ("Search", "Trends"), horizontal=True,
                    label_visibility="collapsed", key="news-mode")
    if mode == "Search":
        panels.render_search(st, retriever, query=query, filters=filters, limit=20,
                             search_cfg={"keyword_weight": 0.3, "semantic_weight": 0.7})
    else:
        panels.render_trends(st, store, term=query, filters=filters, suggestions=[])
