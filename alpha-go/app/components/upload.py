"""Upload panel — add a document to a company's corpus from the dashboard.

Thin view over :func:`src.corpus.upload.add_uploaded_document`: drop a file and the panel
auto-detects its **document class** (via the canonical keyword taxonomy) and the **company** it
belongs to (by matching the filename against known companies), pre-fills both — editable — fields,
and on confirm parses, indexes incrementally, and makes it searchable immediately (no full rebuild).
"""
from __future__ import annotations

from pathlib import Path

_NEW_COMPANY = "＋ New company…"
_NEW_INDUSTRY = "＋ New industry…"


def _prettify(stem: str) -> str:
    """Filename stem → a human display name (`bimbo_annual_2024` → `Bimbo Annual 2024`)."""
    return " ".join(stem.replace("_", " ").replace("-", " ").split()).title()


def render_upload(st, config, store, retriever, *, facets, taxonomy) -> None:
    # Success banner survives the post-upload rerun (which refreshes the sidebar facets).
    msg = st.session_state.pop("up-last-msg", None)
    if msg:
        st.success(msg)

    st.header("Upload documents")
    st.caption(
        "Add a PDF, HTML, Markdown, or text file. Alpha Go reads the document first, suggests "
        "every materially covered company, and lets you confirm the metadata before indexing."
    )

    from src.corpus.classification import classify_document
    from src.corpus.upload import add_uploaded_document, extract_uploaded_text, slugify
    from src.shared.paths import ESTATE_BRIDGE

    configured = {
        str(source.get("slug")): {
            "slug": str(source.get("slug")),
            "company": str(source.get("company") or source.get("slug")),
        }
        for source in [*(config.get("sources") or []), *(config.get("company_catalog") or [])]
        if source.get("slug")
    }
    known_companies = [
        configured.get(slug, {"slug": slug, "company": slug})
        for slug in facets.companies
    ]

    # --- File first: a dropped file drives the auto-detection below. ---
    uploaded = st.file_uploader("Document", type=["pdf", "md", "txt", "html", "htm"],
                                key="up-file")

    # On a *fresh* file (name changed since last render), seed the companies + doc-type widgets
    # from the filename. Writing session_state BEFORE the widgets instantiate lets detection win
    # over a stale prior selection; the user can still override afterward (choice then persists).
    if uploaded is not None and st.session_state.get("up-last-file") != uploaded.name:
        st.session_state["up-last-file"] = uploaded.name
        stem = Path(uploaded.name).stem
        try:
            with st.spinner("Reading and classifying document content…"):
                preview_text = extract_uploaded_text(uploaded.name, uploaded.getvalue())
                profile = classify_document(
                    uploaded.name, preview_text,
                    companies=known_companies, taxonomy=taxonomy,
                )
            st.session_state["up-profile"] = profile.to_dict()
        except Exception as exc:  # noqa: BLE001 — classification degrades to editable defaults
            profile = None
            st.session_state["up-profile"] = {
                "_error": f"{type(exc).__name__}: {exc}",
            }
        detected_companies = (
            [signal.value for signal in profile.companies if signal.value]
            if profile else []
        )
        detected_company = detected_companies[0] if detected_companies else None
        detected_key = profile.doc_type.value if profile else taxonomy.infer(stem)
        st.session_state["up-companies"] = detected_companies
        st.session_state["up-doctype"] = taxonomy.label_for(detected_key)
        st.session_state["up-add-new"] = not detected_company
        if not detected_company:
            st.session_state["up-newname"] = (
                profile.suggested_company_name if profile and profile.suggested_company_name
                else _prettify(stem)
            )
        if profile and profile.period.value:
            st.session_state["up-label"] = profile.period.value
        if profile and profile.suggested_title:
            st.session_state["up-title"] = profile.suggested_title
        detected_language = profile.language.value if profile else "unknown"
        st.session_state["up-language"] = (
            detected_language if detected_language in {"en", "es"} else "en"
        )

    if uploaded is None:
        st.info("Choose a file to begin. Nothing is written until you confirm Add to corpus.")
        return

    profile = st.session_state.get("up-profile") or {}
    if profile.get("_error"):
        st.warning(f"Content classification was inconclusive ({profile['_error']}). "
                   "Confirm the editable metadata below.")
    else:
        company_signals = profile.get("companies") or []
        type_signal = profile.get("doc_type") or {}
        language_signal = profile.get("language") or {}
        company_text = ", ".join(
            f"{signal.get('value')} ({float(signal.get('confidence') or 0):.0%})"
            for signal in company_signals
        ) or "no known company"
        st.success(
            f"Detected: {company_text} · {taxonomy.label_for(type_signal.get('value'))} · "
            f"{language_signal.get('value') or 'unknown'}"
        )
        st.caption("Suggestions come from this file only. Review them before indexing.")

    # --- Companies (one or more corpora) ---
    # A drop-in can belong to several corpora at once — e.g. an in-house paper covering multiple
    # names should be findable in each company's corpus. First selected = primary (owns the
    # on-disk copy + doc_id); every selection becomes a searchable membership.
    st.subheader("1. Company coverage")
    selected = st.multiselect(
        "Known companies covered", list(facets.companies), key="up-companies",
        format_func=lambda slug: configured.get(slug, {}).get(
            "company", slug.replace("_", " ").upper()),
        help="Select every company materially covered by the document. The first owns the file; "
             "all selections become searchable company memberships.")
    targets: list[dict] = [
        {"slug": c, "company": c, "industry": facets.company_industry.get(c)} for c in selected]

    if st.checkbox("The document covers companies not listed", key="up-add-new"):
        displays = st.text_input(
            "New company names", key="up-newname",
            placeholder="e.g. Arca Continental, Becle, Gruma",
            help="Comma-separated. Each company is created as a searchable facet immediately.",
        )
        ind_options = sorted(set(facets.industries) | {"food", "retail", "beverage",
                                                        "conglomerate", "materials"})
        ind_pick = st.selectbox("Industry for new companies", ind_options + [_NEW_INDUSTRY],
                                key="up-newind")
        new_industry = (st.text_input("New industry", key="up-newind-txt").strip().lower()
                        if ind_pick == _NEW_INDUSTRY else ind_pick)
        new_names = [name.strip() for name in displays.split(",") if name.strip()]
        for display in new_names:
            new_slug = slugify(display)
            targets.append({"slug": new_slug, "company": display,
                            "industry": new_industry})
        if new_names:
            st.caption("New facets: " + ", ".join(f"`{slugify(name)}`" for name in new_names))

    # --- Doc type + metadata (doc type pre-selected from detection) ---
    # With no file dropped yet, open on the configured default class rather than whatever happens
    # to be first in the taxonomy list (press_release leads it, for inference-ordering reasons).
    st.subheader("2. Document details")
    st.session_state.setdefault("up-doctype", taxonomy.label_for(taxonomy.default_key))
    doc_label = st.selectbox("Document type", taxonomy.labels(), key="up-doctype")
    doc_type_key = taxonomy.key_for_label(doc_label)
    st.session_state.setdefault("up-language", "en")
    language = st.selectbox(
        "Document language", ["en", "es"], key="up-language",
        help="Detected from document text; confirm if the document is mixed-language.",
    )
    title = st.text_input("Title (optional)", key="up-title")
    label = st.text_input(
        "Reporting period or label", key="up-label",
        placeholder="e.g. 2025-2T or strategy-memo",
        help="Used for period filtering. If it collides with an existing filing, Alpha Go creates "
             "a separate filename instead of overwriting it.")

    st.subheader("3. Confirm")
    if not st.button("Add to corpus", key="up-submit", type="primary"):
        return
    if uploaded is None:
        st.warning("Choose a file to upload first.")
        return
    if not targets:
        st.warning("Pick at least one company (or add a new one).")
        return

    with st.spinner("Parsing + indexing…"):
        try:
            stats = add_uploaded_document(
                store, config, targets=targets,
                doc_type_key=doc_type_key, title=title, label=label,
                filename=uploaded.name, data=uploaded.getvalue(),
                embedder=retriever.get_embedder(),
                storage_dir=ESTATE_BRIDGE.uploads_dir / "alpha-go",
                estate_bridge=ESTATE_BRIDGE,
                language=language,
            )
        except Exception as exc:  # noqa: BLE001 — surface a clean message, not a stack trace
            st.error(f"Upload failed: {type(exc).__name__}: {exc}")
            return

    corpora = stats.get("companies") or [stats.get("company")]
    where = " + ".join(f"**{c}**" for c in corpora)
    period_bit = f" · {stats['period']}" if stats.get("period") else ""
    st.session_state["up-last-msg"] = (
        f"Indexed {stats['chunks']} chunk(s) into {where} "
        f"({taxonomy.label_for(stats['doc_type'])}{period_bit}). Searchable now."
        + (" Registered in the shared document estate."
           if stats.get("estate_registered") else "")
        + (f" Saved separately as `{stats['label']}` to protect an existing document."
           if stats.get("renamed_for_collision") else ""))
    if not stats.get("estate_registered"):
        st.error(
            "The document is searchable in this Alpha Go session, but shared-estate "
            f"registration failed: {stats.get('estate_error') or 'unknown error'}"
        )
        return
    # Refresh the retriever's cached vector matrix + the sidebar facet cache, then rerun so the
    # new company/doc-type appear in the filters this session. The PDF reader has its own cached
    # manifest map; clear it too so the just-uploaded original opens without a process restart.
    # Clear the fresh-file marker so the next upload re-detects cleanly.
    from app.components import pdf_view

    st.session_state.pop("up-last-file", None)
    retriever.invalidate_cache()
    pdf_view.invalidate_manifest_cache()
    st.cache_data.clear()
    st.rerun()
