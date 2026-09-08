"""Dashboard panels — render functions for the alpha-go Streamlit app.

Thin view layer over the stable service interfaces: ``HybridRetriever`` (search), ``IndexStore``
(document lookup), and the vendored extraction cascade (financials). Each renders results and
keeps logic minimal; the highlight HTML is produced by ``app.components.highlight``.
"""
from __future__ import annotations

import functools
import html
from contextlib import nullcontext
from pathlib import Path

from app.components.highlight import snippet_html, spans_to_html


def _brief_state(st):
    from src.search.brief import EvidenceBrief
    brief = st.session_state.get("_evidence_brief")
    if brief is None:
        brief = EvidenceBrief()
        st.session_state["_evidence_brief"] = brief
    return brief


def _brief_passage_text(
    markdown_path: str, offset_phrases: "list[tuple]"
) -> str:
    """Collect export passages from 2- or 3-field reader offsets."""
    source = _doc_text(markdown_path)
    return "\n\n".join(
        source[int(entry[0]):int(entry[0]) + 1200]
        for entry in offset_phrases
        if entry
    )


def _render_brief_controls(st) -> None:
    brief = st.session_state.get("_evidence_brief")
    if not brief or not brief.items:
        return
    with st.expander(f"Evidence brief · {len(brief.items)} passage(s)"):
        st.caption("Selected passages are kept locally and exported without an LLM.")
        st.download_button("Download HTML brief", brief.to_html(),
                           file_name="alpha-go-evidence-brief.html", mime="text/html",
                           key="brief-html")
        st.download_button("Download CSV", brief.to_csv(),
                           file_name="alpha-go-evidence.csv", mime="text/csv",
                           key="brief-csv")

_SENTIMENT_COLORS = {"positive": "#3fb950", "negative": "#f85149", "neutral": "#888"}


# Sized to hold a whole local corpus: the linear view lexical-scans EVERY document per query,
# so a small cache would thrash and re-read files on each Streamlit rerun.
@functools.lru_cache(maxsize=1024)
def _doc_text(markdown_path: str) -> str:
    """Source markdown for a hit (cached; empty string when unreadable)."""
    try:
        return open(markdown_path, encoding="utf-8").read()
    except OSError:
        return ""


@functools.lru_cache(maxsize=512)
def _doc_matches_cached(markdown_path: str, terms_key: "tuple[str, ...]"):
    """Full-document match spans for a (doc, terms) pair, computed once per process.

    Streamlit reruns the whole script on every click and renders even collapsed expander
    bodies, so without this every Prev/Next press re-scanned every rendered document once
    per occurrence.
    """
    from app.components.doc_matches import find_matches

    return find_matches(_doc_text(markdown_path), list(terms_key))


def _sentiment_badge(label: str, rationale: str) -> str:
    color = _SENTIMENT_COLORS.get(label, "#888")
    safe_rationale = html.escape(rationale, quote=True)
    safe_label = html.escape(label)
    return (f"<span title='{safe_rationale}' style='color:{color};border:1px solid {color};"
            f"border-radius:4px;padding:0 .35rem;font-size:.75rem'>{safe_label}</span>")


def _related_tag() -> str:
    return ("<span title='No keyword match — surfaced by semantic similarity' "
            "style='color:#8b949e;border:1px solid #30363d;border-radius:4px;"
            "padding:0 .35rem;font-size:.75rem'>related</span>")


# Theme-consistent dark cards (the app theme lives in .streamlit/config.toml).
_SNIPPET_STYLE = (
    "padding:.6rem .9rem;border:1px solid #2a2f3a;border-left:3px solid #2aa198;"
    "background:#171a21;color:#e6e6e6;border-radius:6px;line-height:1.55;"
)
_DOC_STYLE = (
    "padding:1rem;border:1px solid #2a2f3a;border-radius:6px;background:#12151b;"
    "color:#dcdcdc;white-space:pre-wrap;font-family:ui-monospace,monospace;"
    "font-size:.85rem;max-height:60vh;overflow:auto;line-height:1.5;"
)


@functools.lru_cache(maxsize=8)
def _on_disk_md_count(corpus_root: str) -> int:
    """Total ``.md`` files physically present under the corpus root (cached per process)."""
    try:
        return sum(1 for _ in Path(corpus_root).rglob("*.md"))
    except OSError:
        return 0


def _coverage_note(store, doc_rows) -> str:
    """Honest index-coverage caption: how many corpus docs were scanned vs left un-indexed.

    The literal scan can only see INDEXED documents (``doc_rows``). Un-indexed ``.md`` on disk (e.g.
    the Spanish companies this English build intentionally excludes) are outside the scan, so a
    mention count is capped by index coverage — say so instead of silently undercounting.
    """
    scanned = len(doc_rows)
    indexed_total = store.count("documents")
    return f"scanned {scanned} of {indexed_total} indexed corpus document(s)"


def partition_hits(hits: list, *, semantic_ok: bool = False) -> "tuple[list, bool]":
    """Results to display, and whether they are semantic-only ("related") hits.

    With a real embedding model (``semantic_ok``), semantic similarity is meaningful, so ALL hits
    are kept — concept ranking is the primary surface, and a passage that ranks by meaning shows
    even without a keyword match. ``related_only`` is True when NONE matched a keyword.

    On the offline hashing embedder (``semantic_ok`` False — the default, and the test fixture),
    semantic-only hits are noise: keyword-matched hits win outright, and semantic-only ones show
    only when no keyword hit exists.
    """
    kw = [h for h in hits if getattr(h, "keyword_match", True)]
    if semantic_ok:
        return list(hits), not kw
    if kw:
        return kw, False
    return list(hits), True


def _highlight_terms(query: str) -> list[str]:
    """The terms the mentions view counts and highlights — Ctrl+F-faithful.

    A multi-word query is kept as ONE phrase (interior stopwords preserved) so ``find_matches``
    counts it as a contiguous phrase, not ``word1`` OR ``word2`` (the biggest Ctrl+F divergence).
    Synonym-dictionary aliases are appended as extra phrases so alias matches still surface.
    """
    from src.index.keyword_index import search_phrases

    return search_phrases(query)


def _literal_terms(query: str) -> list[str]:
    """The literal query PHRASE only (no analog aliases) — the Mentions band's scan terms.

    Same phrase normalization as :func:`_highlight_terms` but WITHOUT the synonym/analog aliases,
    so Mentions counts only true literal occurrences; the curated analogs are surfaced separately.
    """
    from src.index.keyword_index import query_phrase

    phrase = query_phrase(query)
    return [phrase] if phrase else []


def _widget(st, name: str):
    """A Streamlit API by name, or ``None`` when the host (or the test stand-in) lacks it.

    The panels are rendered against a minimal ``_FakeSt`` in the unit tests, so every
    newer/optional widget is looked up this way and skipped when absent.
    """
    return getattr(st, name, None)


_TONES_KEY = "reader-tones"          # session flag: underline sentence sentiment in the reader


def _tones_enabled(st) -> bool:
    return bool(st.session_state.get(_TONES_KEY, True))


@functools.lru_cache(maxsize=2048)
def _window_tones_cached(win_text: str) -> "tuple[tuple[int, int, str], ...]":
    """Sentence tone spans for one reader window (memoized: reruns re-render the same window)."""
    from src.qa.topics import sentence_tones

    return tuple(sentence_tones(win_text))


def _render_context(st, *, markdown_path: str, char_start: int, char_end: int,
                    key: str, terms: list[str]) -> None:
    """Inline windowed document view anchored at a match, with match n/N Prev/Next nav.

    Positive/negative sentences are underlined green/red (lexicon-scored, per sentence) when the
    reader's "Sentiment underlines" toggle is on — the search-term ``<mark>`` stays a separate
    channel, so a hit inside a negative sentence reads as a yellow mark on a red underline.
    """
    from app.components.doc_matches import active_index, merge_spans, window_view

    text = _doc_text(markdown_path)
    if not text:
        st.caption("Source document unavailable.")
        return

    spans = merge_spans(list(_doc_matches_cached(markdown_path, tuple(terms)))
                        + [(char_start, char_end)])
    state_key = f"m-{key}"
    idx = st.session_state.get(state_key)
    if idx is None:
        idx = active_index(spans, char_start)
    idx = max(0, min(idx, len(spans) - 1))

    if len(spans) > 1:
        col_prev, col_count, col_next = st.columns([1, 2, 1])
        if col_prev.button("◀ Prev", key=f"prev-{key}"):
            idx = (idx - 1) % len(spans)
        if col_next.button("Next ▶", key=f"next-{key}"):
            idx = (idx + 1) % len(spans)
        col_count.markdown(f"<div style='text-align:center'>match <b>{idx + 1} of "
                           f"{len(spans)}</b></div>", unsafe_allow_html=True)
    st.session_state[state_key] = idx

    # A short lead-in keeps the active (highlighted) match inside the first screenful of the
    # scrollable block — with a long lead-in the match sits below the fold and Prev/Next
    # looks like it does nothing.
    win_text, win_spans, win_active, win_start = window_view(text, spans, idx,
                                                             before=300, after=3500)
    tones = list(_window_tones_cached(win_text)) if _tones_enabled(st) else None
    html = spans_to_html(win_text, win_spans, active=win_active, tones=tones)
    st.markdown(f"<div style='{_DOC_STYLE}'>{html}</div>", unsafe_allow_html=True)
    tone_note = ""
    if tones:
        pos = sum(1 for _, _, lab in tones if lab == "positive")
        neg = len(tones) - pos
        tone_note = f" · underlined: {pos} positive / {neg} negative sentence(s)"
    st.caption(f"chars {win_start:,}–{win_start + len(win_text):,} of {len(text):,}{tone_note} · "
               f"{markdown_path}")


# ---------------------------------------------------------------------------------------------
# Smart Summary (AlphaSense-style) for the selected document: key takeaways + topics + sections.
# ---------------------------------------------------------------------------------------------
_TOPIC_PILL_STYLE = (
    "display:inline-block;margin:0 .35rem .35rem 0;padding:.1rem .55rem;border-radius:12px;"
    "border:1px solid #2aa198;color:#2aa198;font-size:.8rem"
)


def _smart_summary_memo(st, *, retriever, doc_id: str, markdown_path: str, company: str,
                        period: "str | None", title: str, max_sentences: int = 5):
    """Compute (once per doc per session) the extractive summary, topics and sections."""
    memo = st.session_state.setdefault("_smart_summary_memo", {})
    if doc_id in memo:
        return memo[doc_id]
    from src.qa import summarize
    from src.qa.topics import document_sections, document_topics

    text = _doc_text(markdown_path)
    embedder = None
    boiler = None
    try:
        get_embedder = getattr(retriever, "get_embedder", None)
        embedder = get_embedder() if callable(get_embedder) else None
    except Exception:  # noqa: BLE001 — centrality is optional; extractive core still works
        embedder = None
    try:
        boiler_fn = getattr(retriever, "_boilerplate_model", None)
        boiler = boiler_fn() if callable(boiler_fn) else None
    except Exception:  # noqa: BLE001
        boiler = None
    summ = summarize.summarize_document(
        text, doc_id=doc_id, title=title, company=company, period=period,
        markdown_path=markdown_path, embedder=embedder, boilerplate=boiler,
        max_sentences=max_sentences + 1,
    )
    # A markdown heading can pass the extractive salience filter (it is short, central and
    # front-loaded) but it is a title, not a takeaway — the Sections list already shows it.
    summ.points = [p for p in summ.points if not p.text.lstrip().startswith("#")][:max_sentences]
    result = {"summary": summ, "topics": document_topics(text),
              "sections": document_sections(text)}
    if len(memo) > 32:
        memo.clear()
    memo[doc_id] = result
    return result


def _render_smart_summary(st, *, retriever, doc_id: str, markdown_path: str, company: str,
                          period: "str | None", title: str, terms: "list[str]") -> None:
    """Collapsible Smart Summary at the top of the reader pane.

    Key takeaways are the offline extractive summary (each with a tone badge and a "Read in
    context" jump); Topics are curated concepts the document literally discusses, ranked by
    count, each a button that opens the reader at the first occurrence; Sections list the
    document's own headings. Everything is deterministic and labelled as such.
    """
    expander = _widget(st, "expander")
    if expander is None:
        return
    data = _smart_summary_memo(st, retriever=retriever, doc_id=doc_id,
                               markdown_path=markdown_path, company=company, period=period,
                               title=title)
    summ, topics, sections = data["summary"], data["topics"], data["sections"]
    if not (summ.points or topics or sections):
        return
    label = "🧠 Smart Summary — " + " · ".join(
        s for s, ok in (("Key takeaways", bool(summ.points)), (f"{len(topics)} topics", bool(topics)),
                        (f"{len(sections)} sections", bool(sections))) if ok)
    with expander(label, expanded=True):
        st.caption("Offline & deterministic: extractive key takeaways (lexicon tone), literal "
                   "topic counts from the curated concept dictionary, and the document's own "
                   "headings. No LLM.")
        jump_key = f"topic-jump-{doc_id}"
        col_take, col_topics = st.columns([1.6, 1], gap="medium")
        with col_take:
            if summ.points:
                st.markdown("**Key takeaways**")
                for i, p in enumerate(summ.points):
                    badge = _sentiment_badge(p.sentiment, "lexicon tone of this passage")
                    st.markdown(f"<div style='{_SUMMARY_STYLE}'>{badge} &nbsp;"
                                f"{spans_to_html(p.text, [])}</div>", unsafe_allow_html=True)
                    if st.button("Read in context", key=f"take-{doc_id}-{i}"):
                        st.session_state[jump_key] = (p.char_start, p.char_end, [])
        with col_topics:
            if topics:
                st.markdown("**Topics**")
                st.caption("Literal occurrence counts · click to jump to the first mention")
                for t in topics:
                    if st.button(f"{t.label} · {t.count}", key=f"topic-{doc_id}-{t.label}",
                                 use_container_width=True):
                        st.session_state[jump_key] = (t.first_offset, t.first_offset + 1,
                                                      list(t.phrases) or [t.label])
            if sections:
                st.markdown("**Sections**")
                for j, s in enumerate(sections):
                    if st.button(s.title, key=f"sec-{doc_id}-{j}", use_container_width=True):
                        st.session_state[jump_key] = (s.offset, s.offset + 1, [])
        jump = st.session_state.get(jump_key)
        if jump:
            cs, ce, jump_terms = jump
            st.markdown("**In context**")
            if st.button("Close", key=f"topic-close-{doc_id}"):
                st.session_state.pop(jump_key, None)
                st.rerun()
            else:
                _render_context(st, markdown_path=markdown_path, char_start=cs, char_end=ce,
                                key=f"topic-{doc_id}", terms=list(jump_terms) or list(terms))


def _open_document(st, *, doc_id: str, markdown_path: str,
                   offset_phrases: "list[tuple[int, list[str]]]", fallback, key: str,
                   terms: "list[str] | None" = None) -> None:
    """Lazy "open the source document" control (Summary tab) — prefers the natural document state.

    Mounts the reader only when the user clicks (Streamlit re-renders every result each rerun, so
    gating on ``session_state`` avoids loading a document per result). Tries the real PDF, then the
    original HTML filing, and only then the ``fallback`` (markdown) for the rare source-less doc.
    """
    from app.components import html_view, pdf_view

    has_doc = pdf_view.pdf_path_for(doc_id) is not None or html_view.html_path_for(doc_id) is not None
    state_key = f"opendoc-{key}"
    opened = st.session_state.get(state_key, False)
    label = ("📕 Close document" if opened
             else "📄 Open document" if has_doc else "📖 Read in context")
    if st.button(label, key=f"openbtn-{key}"):
        opened = not opened
        st.session_state[state_key] = opened
    if not opened:
        return
    if pdf_view.render(st, doc_id=doc_id, markdown_path=markdown_path,
                       offset_phrases=offset_phrases, key=key):
        return
    # terms for the HTML highlighter: caller-supplied, else the phrases from offset_phrases.
    html_terms = terms if terms is not None else [p for _, ps in offset_phrases for p in ps]
    if html_view.render(st, doc_id=doc_id, terms=html_terms, key=key):
        return
    fallback()


# Retrieval pool: fetch more chunks than documents shown so per-doc grouping still fills a
# page; occurrences shown inline per document before the rest collapse into an expander.
_POOL_FACTOR, _POOL_MAX = 3, 150
_INLINE_OCCURRENCES = 5


def _render_reader(st, *, doc_id: str, markdown_path: str,
                   offset_phrases: "list[tuple[int, list[str]]]", terms: list[str],
                   key: str) -> None:
    """Show a document in its NATURAL state in the reader pane — never parsed markdown.

    PDF-backed filing → the real PDF (hits highlighted, opened at the hit page). HTML-sourced
    filing (FEMSA MD&A) → the original filing HTML rendered inline. Neither available (the few
    source-less periods) → a plain note.
    """
    from app.components import html_view, pdf_view

    if pdf_view.render(st, doc_id=doc_id, markdown_path=markdown_path,
                       offset_phrases=offset_phrases, key=key):
        return
    if html_view.render(st, doc_id=doc_id, terms=terms, key=key):
        return
    st.caption("Indexed source-text view")
    _render_context(st, markdown_path=markdown_path,
                    char_start=offset_phrases[0][0] if offset_phrases else 0,
                    char_end=(offset_phrases[0][0] + 1) if offset_phrases else 1,
                    key=f"fallback-{key}", terms=terms)


def _doc_row(st, *, doc_id: str, company: str, period: str, doc_type: str,
             count: "int | None", selected: bool, via: "list[str] | None" = None,
             companies: "list[str] | None" = None) -> None:
    """One selectable report in the left rail — metadata only (no snippet text).

    Mentions show an occurrence count; analog rows show "via <aliases>" (the curated analog term(s)
    that surfaced the doc). A document shared across corpora shows its extra companies inline.
    """
    if via:
        tail = "via " + ", ".join(via)
    elif count is not None:
        tail = f"{count} mention" + ("s" if count != 1 else "")
    else:
        tail = "match"
    # A drop-in can belong to several corpora — surface the others so the shared doc reads as one.
    extra = [c for c in (companies or []) if c != company]
    also = f" · also {', '.join(c.upper() for c in extra)}" if extra else ""
    label = f"{company.upper()}{also} · {period or '—'} · {doc_type} · {tail}"
    checkbox = _widget(st, "checkbox")
    if checkbox is not None:
        col_pick, col_row = st.columns([0.14, 1], gap="small")
        with col_pick:
            picked = set(st.session_state.get(_ASK_PICKED_KEY) or [])
            if checkbox("pick", key=f"pick-{doc_id}", value=doc_id in picked,
                        label_visibility="collapsed", help="Select for a multi-document question"):
                picked.add(doc_id)
            else:
                picked.discard(doc_id)
            st.session_state[_ASK_PICKED_KEY] = sorted(picked)
        target = col_row
    else:
        target = st
    if target.button(label, key=f"row-{doc_id}", use_container_width=True,
                     type="primary" if selected else "secondary"):
        st.session_state["reader_doc"] = doc_id
        st.rerun()


_RELATED_CAP = 5          # semantic-only fallback cards shown for a no-keyword query


def _filters_key(filters) -> tuple:
    return (tuple(getattr(filters, "doc_ids", []) or []),
            tuple(filters.companies), tuple(filters.doc_types), filters.period_from,
            filters.period_to, tuple(getattr(filters, "industries", []) or []))


@functools.lru_cache(maxsize=512)
def _mention_tones_cached(markdown_path: str, spans: "tuple[tuple[int, int], ...]"):
    from src.search.mention_analytics import mention_tones

    return tuple(mention_tones(_doc_text(markdown_path), list(spans)))


def _render_mention_sentiment(st, *, markdown_path: str,
                              spans: "list[tuple[int, int]]",
                              total_matches: "int | None" = None) -> None:
    """Visible, auditable sentiment for each exact lexical mention in the selected document."""
    if not spans:
        return
    from src.search.mention_analytics import tone_counts

    tones = list(_mention_tones_cached(markdown_path, tuple(spans)))
    counts = tone_counts(tones)
    st.markdown(
        "**Mention sentiment** — "
        f"🟢 {counts['positive']} positive · "
        f"🟡 {counts['neutral']} neutral · "
        f"🔴 {counts['negative']} negative"
    )
    capped = (
        f" Showing the first {len(tones)} of {total_matches} matches."
        if total_matches and total_matches > len(tones) else ""
    )
    st.caption(
        "Each exact match is scored independently from its surrounding passage; "
        f"hover a badge for the scoring rationale.{capped}"
    )
    with st.expander(f"Inspect {len(tones)} mention judgment(s)"):
        for i, tone in enumerate(tones, 1):
            excerpt = " ".join(tone.context.split())
            if len(excerpt) > 360:
                excerpt = excerpt[:357].rstrip() + "…"
            badge = _sentiment_badge(tone.label, tone.rationale)
            st.markdown(
                f"{i}. {badge} &nbsp; score `{tone.score:+.3f}` — "
                f"{html.escape(excerpt)}",
                unsafe_allow_html=True,
            )


_EVIDENCE_CARD_BATCH = 12


def _render_mention_evidence(st, *, doc_id: str, markdown_path: str,
                             spans: "list[tuple[int, int]]", total_matches: "int | None",
                             key: str, terms: "list[str]") -> bool:
    """Render independent, source-verifiable cards for the mentions in one selected document.

    A card uses an actual cropped PDF page only when the exact span can be mapped and located on
    that PDF.  HTML/Markdown sources and unmatched PDF text receive a plainly labelled text card;
    this avoids presenting an unverified screenshot as evidence.  Cards are incrementally loaded
    because a literal corpus scan can legitimately find hundreds of mentions in one filing.
    """
    if not spans:
        return False
    from app.components import pdf_view
    from src.search.mention_analytics import tone_counts

    source_text = _doc_text(markdown_path)
    tones = list(_mention_tones_cached(markdown_path, tuple(spans)))
    counts = tone_counts(tones)
    state_key = f"evidence-shown-{key}"
    shown = min(len(tones), int(st.session_state.get(state_key, _EVIDENCE_CARD_BATCH)))
    st.session_state[state_key] = shown
    total = total_matches if total_matches is not None else len(tones)
    st.markdown(f"**Mention evidence** — showing {shown} of {total}")
    st.caption(
        f"🟢 {counts['positive']} positive · 🟡 {counts['neutral']} neutral · "
        f"🔴 {counts['negative']} negative. Each card is one independently scored occurrence."
    )

    def render_card(i, tone) -> None:
        phrase = source_text[tone.start:tone.end].strip() or (terms[0] if terms else "match")
        preview = pdf_view.mention_preview(
            doc_id, markdown_path, tone.start, [phrase], sentiment=tone.label,
        )
        container = (st.container(border=True) if hasattr(st, "container") else nullcontext())
        with container:
            if preview is not None:
                st.image(preview.png, use_container_width=True)
                st.caption(f"PDF evidence · page {preview.page} · {preview.filename}")
            else:
                st.caption("Text evidence · original page crop unavailable for this source")
            lo = max(0, tone.start - 240)
            rel_start, rel_end = tone.start - lo, tone.end - lo
            passage = spans_to_html(tone.context, [(rel_start, rel_end)])
            st.markdown(f"<div style='{_SNIPPET_STYLE}'>{passage}</div>",
                        unsafe_allow_html=True)
            st.markdown(
                f"{i}. {_sentiment_badge(tone.label, tone.rationale)} "
                f"&nbsp; score `{tone.score:+.3f}`",
                unsafe_allow_html=True,
            )

    # A two-column evidence gallery is legible on the wide dashboard but degrades to the same
    # sequential cards in the lightweight test renderer.
    cols = st.columns(2, gap="small")
    for i, tone in enumerate(tones[:shown], 1):
        with cols[(i - 1) % len(cols)]:
            render_card(i, tone)

    if shown < len(tones):
        remaining = len(tones) - shown
        if st.button(f"Show more mentions (+{min(_EVIDENCE_CARD_BATCH, remaining)})",
                     key=f"evidence-more-{key}", use_container_width=True):
            st.session_state[state_key] = min(len(tones), shown + _EVIDENCE_CARD_BATCH)
            st.rerun()

    # The full filing remains available on demand, but is no longer the primary result surface.
    color_by_tone = {"positive": "#3fb950", "negative": "#f85149", "neutral": "#f5c518"}
    offsets = [
        (tone.start, [source_text[tone.start:tone.end]], color_by_tone.get(tone.label, "#f5c518"))
        for tone in tones[:shown] if source_text[tone.start:tone.end].strip()
    ]
    _open_document(
        st, doc_id=doc_id, markdown_path=markdown_path, offset_phrases=offsets,
        fallback=lambda: _render_context(
            st, markdown_path=markdown_path, char_start=tones[0].start, char_end=tones[0].end,
            key=f"evidence-context-{key}", terms=terms,
        ),
        key=f"evidence-{key}", terms=terms,
    )
    return True


# ---------------------------------------------------------------------------------------------
# Company card (AlphaSense-style issuer overview) — the reader pane's second mode.
# ---------------------------------------------------------------------------------------------
_PANE_KEY = "pane-mode"
_PANE_OPTIONS = ("Document", "Company")
_STATUS_ICON = {"received": "✅", "expected": "🗓️", "missing": "⚠️"}


def _company_memo(st, *, store, slug: str, catalog, display_name: "str | None"):
    memo = st.session_state.setdefault("_company_memo", {})
    if slug in memo:
        return memo[slug]
    from src.search.company import company_profile

    profile = company_profile(store, slug, catalog=catalog, display_name=display_name)
    if len(memo) > 32:
        memo.clear()
    memo[slug] = profile
    return profile


def _company_trend(st, *, store, slug: str, query: str):
    """Mentions of the current query per period for one company (memoized like Trends)."""
    from src.search.trends import mention_trend

    key = (query, ("company", slug), False)
    memo = st.session_state.setdefault("_trend_memo", {})
    if key in memo:
        return memo[key]
    try:
        points = mention_trend(store, query, companies=[slug], expand_synonyms=False)
    except Exception:  # noqa: BLE001 — a stand-in store without memberships: no sparkline
        points = None
    if len(memo) > 16:
        memo.clear()
    memo[key] = points
    return points


def _render_company_card(st, *, store, slug: str, display_name: "str | None", query: str,
                         specs: dict, app_config: "dict | None", doc_type_label) -> None:
    """The issuer overview: identity, holdings, coverage, mention trend, calendar, events,
    financials and the other companies in the current results. Index-derived only."""
    from src.search.company import (
        companies_in_results, coverage_items, expected_reports, trend_change,
    )

    cfg = app_config or {}
    profile = _company_memo(st, store=store, slug=slug, catalog=cfg.get("company_catalog"),
                            display_name=display_name)
    label = (doc_type_label if callable(doc_type_label) else (lambda k: k))

    ident = " · ".join(x for x in (
        f"**{profile.name}**", (f"`{profile.ticker}`" if profile.ticker else None),
        (str(profile.industry).replace("_", " ").title() if profile.industry else None)) if x)
    st.markdown(ident)
    st.caption("Company card — derived from the local index and the BMV catalog only; no market "
               "data. Coverage is measured against configs/bmv_corpus.yaml.")

    metric = _widget(st, "metric")
    facts = [("Documents", profile.docs_total), ("Filings", profile.filings_total),
             ("News", profile.news_total), ("Latest period", profile.latest_period or "—"),
             ("Periods", len(profile.periods))]
    cols = st.columns(len(facts))
    for col, (name, value) in zip(cols, facts):
        with col:
            if metric is not None:
                metric(name, value)
            else:
                st.markdown(f"**{name}**: {value}")

    col_l, col_r = st.columns([1, 1], gap="medium")
    with col_l:
        st.markdown("**Holdings by document type**")
        for dt, n in profile.docs_by_type.items():
            st.markdown(f"- {label(dt)} — {n}")
        if profile.languages:
            st.caption("Languages: " + ", ".join(f"{k} {v}" for k, v in profile.languages.items()))

        st.markdown("**Coverage vs contract**")
        progress = _widget(st, "progress")
        for item in coverage_items(profile, cfg.get("coverage_contract")):
            tick = "✅" if item.met else "◻️"
            text = f"{tick} {item.label}: {item.have} / {item.target}"
            if progress is not None:
                progress(item.ratio, text=text)
            else:
                st.markdown(text)

    with col_r:
        st.markdown(f"**Document trend** — mentions of “{query.strip()}” per period")
        points = _company_trend(st, store=store, slug=slug, query=query) if query.strip() else []
        if points:
            import pandas as pd
            from src.shared.report_index import period_sort_key

            df = pd.DataFrame([{"period": p.period, "mentions": p.mentions} for p in points
                               if p.period != "Undated"])
            if not df.empty:
                df = df.groupby("period", as_index=True).sum()
                df = df.loc[sorted(df.index, key=period_sort_key)]
                chart = _widget(st, "bar_chart")
                if chart is not None:
                    chart(df, height=160)
                last, prev, pct = trend_change(points)
                delta = (f"{pct:+.0f}% vs prior period" if pct is not None
                         else ("new this period" if last and not prev else "no change"))
                st.caption(f"{int(df['mentions'].sum())} mention(s) across {len(df)} period(s) · "
                           f"latest {last} · {delta}")
        elif points is None:
            st.caption("Trend unavailable for this store.")
        else:
            st.caption("No mentions of the current query for this company.")

        st.markdown("**Reporting calendar** (estimated BMV deadlines)")
        for rep in expected_reports(profile.periods):
            icon = _STATUS_ICON.get(rep.status, "")
            st.markdown(f"- {icon} {rep.period} · {rep.deadline.isoformat()} · {rep.status}")

    if profile.recent_events:
        st.markdown("**Recent events & releases**")
        for ev in profile.recent_events:
            when = ev.date or ev.period or "—"
            text = f"{when} · {label(ev.doc_type)} · {ev.title or ev.doc_id}"
            if st.button(text, key=f"event-{ev.doc_id}", use_container_width=True):
                st.session_state["reader_doc"] = ev.doc_id
                staged = dict(st.session_state.get(_PENDING_SCOPE) or {})
                staged[_PANE_KEY] = "Document"
                st.session_state[_PENDING_SCOPE] = staged
                st.rerun()

    st.markdown("**Financials** (vendored extraction cascade over the parsed prose)")
    fin_key = f"fin-open-{slug}"
    if st.button("Load financials" if not st.session_state.get(fin_key) else "Hide financials",
                 key=f"fin-btn-{slug}"):
        st.session_state[fin_key] = not st.session_state.get(fin_key, False)
        st.rerun()
    if st.session_state.get(fin_key):
        try:
            from app.components import financials
            financials.render_financials_table(st, cfg, slug)
        except Exception as exc:  # noqa: BLE001 — the cascade must never take the card down
            st.caption(f"Financials unavailable ({type(exc).__name__}: {exc}).")

    others = companies_in_results(specs)
    if len(others) > 1:
        st.markdown(f"**Companies in these results** — {len(others)}")
        rows = [{"Company": r["company"].upper(), "Documents": r["documents"],
                 "Mentions": r["mentions"], "Lanes": r["lanes"]} for r in others]
        dataframe = _widget(st, "dataframe")
        if dataframe is not None:
            dataframe(rows, hide_index=True, use_container_width=True)
        else:
            for r in rows:
                st.markdown(f"- {r['Company']} · {r['Documents']} doc(s) · {r['Mentions']} mention(s)")
        download = _widget(st, "download_button")
        if download is not None:
            import csv
            import io

            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
            download("Download CSV", buf.getvalue(), file_name="alpha-go-companies.csv",
                     mime="text/csv", key="companies-csv")


_SORT_OPTIONS = ("Most mentions", "Newest first", "Oldest first")
_VIEW_OPTIONS = ("Detailed", "Table")

# Sidebar filter widget keys owned by app/streamlit_app.py. The panel writes them only through
# the explicit scope shortcuts below ("Only …", zero-result expansions) — each write is followed
# by st.rerun() so the sidebar widgets pick the value up on their next instantiation.
_FLT_INDUSTRIES, _FLT_COMPANIES, _FLT_DOCTYPES, _FLT_DOCUMENTS, _FLT_PERIOD = (
    "flt-industries", "flt-companies", "flt-doctypes", "flt-documents", "flt-period")


def _pick(st, label: str, options: "tuple[str, ...]", *, key: str, default: str,
          help: "str | None" = None) -> str:
    """Single-choice control: pills when available, else radio, else the default (tests)."""
    pills = _widget(st, "pills")
    if pills is not None:
        chosen = pills(label, list(options), selection_mode="single", default=default, key=key,
                       help=help, label_visibility="collapsed")
        return chosen or default
    radio = _widget(st, "radio")
    if radio is not None:
        return radio(label, list(options), index=list(options).index(default), key=key,
                     horizontal=True, label_visibility="collapsed", help=help)
    return default


def _sort_doc_groups(groups: list, mode: str) -> list:
    from app.components.linear_results import sort_groups

    if not groups:
        return []
    if mode == "Most mentions":
        return sorted(groups, key=lambda g: (-g.total_matches, g.company, g.period or ""))
    return sort_groups(groups, newest_first=(mode == "Newest first"))


_PENDING_SCOPE = "_pending_scope"     # staged sidebar-widget writes, applied by the app next run
_PENDING_QUERY = "_pending_query"     # staged query-box rewrite, applied by the app next run


def scope_updates(*, companies: "list[str] | None" = None,
                  doc_type_labels: "list[str] | None" = None, clear_period: bool = False,
                  clear_documents: bool = False, clear_all: bool = False) -> dict:
    """Sidebar-key updates for a scope shortcut (pure). A ``None`` value means "drop the key".

    Streamlit forbids writing a widget's session key after that widget was instantiated in the
    current run, and the sidebar is drawn before the results — so the panel never writes these
    keys itself; it stages them under ``_PENDING_SCOPE`` and the app applies them at the top
    of the next run (``app.streamlit_app._apply_pending_state``).
    """
    out: dict = {}
    if clear_all:
        out.update({_FLT_INDUSTRIES: [], _FLT_COMPANIES: [], _FLT_DOCTYPES: [],
                    _FLT_DOCUMENTS: [], _FLT_PERIOD: None})
    if companies is not None:
        out[_FLT_COMPANIES] = list(companies)
        out[_FLT_DOCUMENTS] = []
    if doc_type_labels is not None:
        out[_FLT_DOCTYPES] = list(doc_type_labels)
        out[_FLT_DOCUMENTS] = []
    if clear_period:
        out[_FLT_PERIOD] = None
    if clear_documents:
        out[_FLT_DOCUMENTS] = []
    return out


def apply_pending_state(session_state) -> None:
    """Apply staged scope/query writes to ``session_state`` (call BEFORE the widgets are built)."""
    pending = session_state.pop(_PENDING_SCOPE, None) or {}
    for k, v in pending.items():
        if v is None:
            session_state.pop(k, None)
        else:
            session_state[k] = v
    q = session_state.pop(_PENDING_QUERY, None)
    if q is not None:
        session_state["query"] = q


def _set_scope(st, **kwargs) -> None:
    """Stage a sidebar scope change (see :func:`scope_updates`) and rerun."""
    staged = dict(st.session_state.get(_PENDING_SCOPE) or {})
    staged.update(scope_updates(**kwargs))
    st.session_state[_PENDING_SCOPE] = staged
    st.rerun()


def _render_zero_result_expansions(st, *, query: str, filters, metric_terms: list,
                                   alias_terms: list) -> None:
    """AlphaSense-style one-click ways out of an empty result: widen scope or try wording."""
    actions = []
    if filters is not None:
        if getattr(filters, "period_from", None) or getattr(filters, "period_to", None):
            actions.append(("Show all periods", dict(clear_period=True)))
        if getattr(filters, "doc_ids", None):
            actions.append(("Drop the specific-document filter", dict(clear_documents=True)))
        if getattr(filters, "doc_types", None):
            actions.append(("All document types", dict(doc_type_labels=[])))
        if getattr(filters, "companies", None) or getattr(filters, "industries", None):
            actions.append(("Search the whole corpus", dict(clear_all=True)))
    if actions:
        st.caption("Expand results:")
        cols = st.columns(len(actions))
        for col, (label, kwargs) in zip(cols, actions):
            with col:
                if st.button(label, key=f"expand-{label}", use_container_width=True):
                    _set_scope(st, **kwargs)
    suggestions = list(dict.fromkeys([*metric_terms, *alias_terms]))[:6]
    if suggestions:
        st.caption("Try related wording instead:")
        cols = st.columns(len(suggestions))
        for col, term in zip(cols, suggestions):
            with col:
                if st.button(term, key=f"try-{term}", use_container_width=True):
                    st.session_state[_PENDING_QUERY] = term
                    st.rerun()


_ASK_PICKED_KEY = "ask_picked"        # doc_ids ticked in the rail for a multi-document question


def _stage_ask(st, *, doc_ids: list, question: str) -> None:
    """Switch to the Ask mode scoped to ``doc_ids`` with ``question`` pre-filled, next run."""
    staged = dict(st.session_state.get(_PENDING_SCOPE) or {})
    staged.update({"explore-mode": "Ask", _FLT_DOCUMENTS: list(doc_ids)})
    st.session_state[_PENDING_SCOPE] = staged
    st.session_state["_ask_prefill"] = question
    st.rerun()


def _render_scope_chips(st, *, company: str, doc_type: str, doc_type_label,
                        doc_id: "str | None" = None, query: str = "") -> None:
    """AlphaSense's per-facet "Only" shortcut, applied to the selected document's facets, plus
    "Ask about this document" (AlphaSense's chat-with-selected-content)."""
    label = doc_type_label(doc_type) if callable(doc_type_label) else doc_type
    if doc_id:
        if st.button("✨ Ask about this document", key=f"ask-doc-{doc_id}",
                     help="Open the Ask mode scoped to just this document",
                     use_container_width=True):
            _stage_ask(st, doc_ids=[doc_id], question=(query or "").strip())
    c1, c2, c3 = st.columns(3, gap="small")
    with c1:
        if st.button(f"Only {company.upper()}", key=f"only-co-{company}",
                     help="Scope the search to this company", use_container_width=True):
            _set_scope(st, companies=[company])
    with c2:
        if st.button(f"Only {label}", key=f"only-dt-{doc_type}",
                     help="Scope the search to this document type", use_container_width=True):
            _set_scope(st, doc_type_labels=[label])
    with c3:
        if st.button("Hits across this company's " + str(label).lower(),
                     key=f"only-codt-{company}-{doc_type}",
                     help="Same query, scoped to this company and document type",
                     use_container_width=True):
            _set_scope(st, companies=[company], doc_type_labels=[label])


def _render_results_table(st, *, rows: list, sel: str) -> None:
    """Table view of the result rail (AlphaSense's second list mode); selecting a row opens it."""
    dataframe = _widget(st, "dataframe")
    if dataframe is None:
        return
    import pandas as pd

    df = pd.DataFrame(rows, columns=["Company", "Period", "Type", "Lane", "Mentions", "Tone",
                                     "doc_id"])
    try:
        event = dataframe(
            df.drop(columns=["doc_id"]), hide_index=True, use_container_width=True,
            on_select="rerun", selection_mode="single-row", key="results-table",
        )
        picked = (event.selection.rows if event is not None and hasattr(event, "selection")
                  else [])
    except TypeError:            # an older Streamlit without on_select — static table only
        dataframe(df.drop(columns=["doc_id"]), hide_index=True, use_container_width=True)
        picked = []
    if picked:
        doc_id = df.iloc[picked[0]]["doc_id"]
        if doc_id != sel:
            st.session_state["reader_doc"] = doc_id
            st.rerun()


def _tone_summary(markdown_path: str, spans: list) -> str:
    """Compact 🟢/🔴 tone tally for a document's mention spans (table view column)."""
    if not spans:
        return "—"
    from src.search.mention_analytics import tone_counts

    counts = tone_counts(list(_mention_tones_cached(markdown_path, tuple(spans[:60]))))
    return f"🟢{counts['positive']} 🟡{counts['neutral']} 🔴{counts['negative']}"


def render_search(st, retriever, *, query: str, filters, limit: int = 20,
                  search_cfg: "dict | None" = None, doc_type_label=None,
                  app_config: "dict | None" = None) -> None:
    """Search results — linear occurrence view: documents in chronological order, every
    keyword occurrence shown in document order (k/N), read-in-context inline.

    ``doc_type_label`` maps a raw doc_type to the sidebar's display label (the taxonomy's
    ``label_for``) so the "Only <type>" scope chips write values the sidebar widget accepts.
    """
    if not query or not query.strip():
        st.caption("Type a query above to search the corpus.")
        return

    _render_brief_controls(st)

    cfg = search_cfg or {}
    weights = {"keyword_weight": float(cfg.get("keyword_weight", 0.5)),
               "semantic_weight": float(cfg.get("semantic_weight", 0.5))}

    # One search + occurrence scan per (query, filters, limit) — facet-independent reruns
    # (opening an expander, pressing Prev/Next, switching tabs) reuse the memo instead of
    # re-searching and re-scanning every document.
    memo_key = (query, _filters_key(filters), limit, tuple(sorted(weights.items())))
    memo = st.session_state.get("_search_memo")
    if memo and memo["key"] == memo_key:
        hits = memo["hits"]
        doc_rows = memo["doc_rows"]
    else:
        hits = retriever.search(query, filters=filters,
                                limit=min(limit * _POOL_FACTOR, _POOL_MAX), **weights)
        # Corpus-wide universe = EVERY (scoped) document, not the retriever's capped pool nor the
        # FTS-token set: the linear view lexical-scans each doc, so a mention the ranking never
        # surfaced is still counted. Docs with zero precise literal matches are dropped downstream.
        where, params = (filters.to_sql() if filters and not filters.is_empty() else ("", []))
        doc_rows = retriever.store.document_rows(where, params)
        st.session_state["_search_memo"] = {"key": memo_key, "hits": hits,
                                             "doc_rows": doc_rows, "groups": {}}
        st.session_state["_docs_shown"] = limit          # new search → reset pagination
        st.session_state.pop("reader_doc", None)          # new search → auto-open the top result
        # New search → drop match-nav positions so old navigation state can't bleed into
        # the new result set (the viewer would otherwise open at an arbitrary match).
        for k in [k for k in st.session_state if k.startswith("m-") or k.startswith("topic-jump-")]:
            del st.session_state[k]
        # A shareable link (?q=…&doc=…) names the document to open; honour it on the first
        # search only, then fall back to the normal "top result" behaviour.
        pending = st.session_state.pop("_pending_reader_doc", None)
        if pending:
            st.session_state["reader_doc"] = pending

    from src.index.keyword_index import (
        analog_synonym_phrases,
        bilingual_equivalent_phrases,
        bilingual_search_phrases,
        metric_synonym_phrases,
    )
    from src.qa.sentiment import sentiment_color

    from app.components.linear_results import group_hits

    # The primary finder is literal plus vetted direct translations.  Broader curated metric
    # wording remains in the clearly separate related-wording lane below.
    literal_terms = bilingual_search_phrases(query)
    bilingual_terms = bilingual_equivalent_phrases(query)
    metric_terms = metric_synonym_phrases(query)
    alias_terms = analog_synonym_phrases(query)

    # Three unconditional corpus-wide scans with explicit provenance. Exact query text, curated
    # metric translations, and looser domain analogs must never collapse into one count: the
    # Banorte/FX incident demonstrated that a larger expanded count is not proof of exact recall.
    docs_shown = st.session_state.get("_docs_shown", limit)
    gcache = st.session_state["_search_memo"].setdefault("groups", {})
    if docs_shown in gcache:
        literal_groups, metric_groups, alias_groups = gcache[docs_shown]
    else:
        literal_groups = group_hits([], terms=literal_terms, doc_text_fn=_doc_text,
                                    doc_rows=doc_rows, max_docs=docs_shown)
        metric_groups = (group_hits([], terms=metric_terms, doc_text_fn=_doc_text,
                                    doc_rows=doc_rows, max_docs=docs_shown) if metric_terms else [])
        alias_groups = (group_hits([], terms=alias_terms, doc_text_fn=_doc_text,
                                   doc_rows=doc_rows, max_docs=docs_shown) if alias_terms else [])
        gcache[docs_shown] = (literal_groups, metric_groups, alias_groups)

    any_groups = bool(literal_groups or metric_groups or alias_groups)
    sort_mode = "Oldest first"
    view_mode = "Detailed"
    if any_groups:
        ctl_sort, ctl_view, ctl_tone = st.columns([2, 1.2, 1.2], gap="small")
        with ctl_sort:
            sort_mode = _pick(st, "Sort", _SORT_OPTIONS, key="sort-mode", default="Most mentions",
                              help="Order of the result list")
        with ctl_view:
            view_mode = _pick(st, "View", _VIEW_OPTIONS, key="view-mode", default="Detailed",
                              help="Detailed rows or a sortable table")
        with ctl_tone:
            toggle = _widget(st, "toggle")
            if toggle is not None:
                st.session_state.setdefault(_TONES_KEY, True)
                toggle("Sentiment underlines", key=_TONES_KEY,
                       help="Underline positive (green) / negative (red) sentences in the reader")
    literal_groups = _sort_doc_groups(literal_groups, sort_mode)
    metric_groups = _sort_doc_groups(metric_groups, sort_mode)
    alias_groups = _sort_doc_groups(alias_groups, sort_mode)

    true_docs = len(literal_groups)
    true_occ = sum(g.total_matches for g in literal_groups)
    mention_groups = [g for g in literal_groups if g.occurrences]
    literal_ids = {g.doc_id for g in literal_groups}
    expanded_docs = len(metric_groups)
    expanded_only_groups = [
        g for g in metric_groups if g.occurrences and g.doc_id not in literal_ids
    ]
    expanded_ids = {g.doc_id for g in metric_groups}
    # Analogs = docs surfaced by a curated analog ALIAS but NOT the literal query phrase — the analog
    # engine's output (analogs.yaml, polysemy-gated), NOT generic embedding similarity. So a query
    # for "PET resin" surfaces resin/plastic docs; "convenience store" surfaces OXXO/proximity docs
    # that don't say the phrase — never a merely topically-similar company (Bimbo/Walmex).
    analog_groups = [
        g for g in alias_groups
        if g.occurrences and g.doc_id not in literal_ids and g.doc_id not in expanded_ids
    ]

    def _spec(g, kind: str) -> dict:
        # per occurrence: (doc offset, matched substrings, sentiment color of that passage)
        offs = [(occ.spans[0][0],
                 [occ.snippet.text[a:b] for a, b in getattr(occ.snippet, "spans", [])],
                 sentiment_color(occ.snippet.text))
                for occ in g.occurrences if occ.spans]
        via = list(dict.fromkeys(
            occ.snippet.text[a:b] for occ in g.occurrences
            for a, b in getattr(occ.snippet, "spans", [])))[:3] if kind in {"expanded", "analog"} else []
        return dict(kind=kind, company=g.company, period=g.period, doc_type=g.doc_type,
                    title=g.title, markdown_path=g.markdown_path, offset_phrases=offs, via=via,
                    match_spans=[span for occ in g.occurrences for span in occ.spans],
                    total_matches=g.total_matches,
                    highlight_terms=(alias_terms if kind == "analog" else
                                     metric_terms if kind == "expanded" else literal_terms))

    specs = {g.doc_id: _spec(g, "mention") for g in mention_groups}
    for g in expanded_only_groups:
        specs.setdefault(g.doc_id, _spec(g, "expanded"))
    for g in analog_groups:
        specs.setdefault(g.doc_id, _spec(g, "analog"))

    # Keep generic semantic neighbors in a separate lane. They are only meaningful when the
    # configured local embedding model is actually available, never when the hashing fallback is
    # active or when a test/dummy retriever has no semantic capability.
    try:
        semantic_ok = bool(getattr(retriever, "semantic_available", False))
    except Exception:
        semantic_ok = False
    concept_specs = {}
    if semantic_ok:
        for hit in hits:
            if getattr(hit, "keyword_match", True) or hit.doc_id in specs:
                continue
            if hit.doc_id in concept_specs or len(concept_specs) >= _RELATED_CAP:
                continue
            concept_specs[hit.doc_id] = dict(
                kind="concept", company=hit.company, period=hit.period, doc_type=hit.doc_type,
                title=hit.title, markdown_path=hit.markdown_path,
                offset_phrases=[(hit.char_start, [hit.snippet.text])], via=[],
                match_spans=[],
                total_matches=None,
                highlight_terms=[], companies=getattr(hit, "companies", None) or [hit.company],
            )
    specs.update(concept_specs)

    ordered_ids = ([g.doc_id for g in mention_groups]
                   + [g.doc_id for g in expanded_only_groups]
                   + [g.doc_id for g in analog_groups]
                   + list(concept_specs))
    if not ordered_ids:
        st.warning("No matches. Try broader terms or clear the sidebar filters.")
        _render_zero_result_expansions(st, query=query, filters=filters,
                                       metric_terms=metric_terms, alias_terms=alias_terms)
        return

    # Auto-open the top result; the user's pick persists across reruns (reset only on a new search).
    # A document picked outside the result set (an event from the company card) opens too — as
    # a plain "browse" entry with no mention evidence, the way AlphaSense opens any filing.
    sel = st.session_state.get("reader_doc")
    if sel not in specs:
        browse_row = next((r for r in doc_rows if r["doc_id"] == sel), None) if sel else None
        if browse_row is not None:
            specs[sel] = dict(
                kind="browse", company=browse_row["company"], period=browse_row["period"],
                doc_type=browse_row["doc_type"], title=browse_row["title"],
                markdown_path=browse_row["markdown_path"], offset_phrases=[(0, [])], via=[],
                match_spans=[], total_matches=None, highlight_terms=literal_terms,
                companies=[browse_row["company"]],
            )
        else:
            sel = ordered_ids[0]
            st.session_state["reader_doc"] = sel

    # ---- Two-pane AlphaSense layout: the results list (left) + the actual document (right) ----
    left, right = st.columns([1, 2.4], gap="medium")

    table_mode = view_mode == "Table" and _widget(st, "dataframe") is not None
    with left:
        picked = [d for d in (st.session_state.get(_ASK_PICKED_KEY) or []) if d in specs]
        if picked:
            if st.button(f"✨ Ask about {len(picked)} selected document(s)", key="ask-picked",
                         type="primary", use_container_width=True):
                _stage_ask(st, doc_ids=picked, question=query.strip())
        if table_mode:
            st.markdown(
                f"**Mentions** ({'bilingual exact' if bilingual_terms else 'exact'}) — "
                f"{true_occ} across {true_docs} document(s)"
            )
            st.caption(_coverage_note(retriever.store, doc_rows))
            lane_name = {"mention": "Mentions", "expanded": "Related", "analog": "Analog",
                         "concept": "Concept"}
            table_rows = []
            for doc_id in ordered_ids:
                e = specs[doc_id]
                table_rows.append([
                    e["company"].upper(), e["period"] or "—",
                    (doc_type_label(e["doc_type"]) if callable(doc_type_label) else e["doc_type"]),
                    lane_name.get(e["kind"], e["kind"]),
                    e["total_matches"] if e["kind"] == "mention" else None,
                    _tone_summary(e["markdown_path"], e.get("match_spans", [])),
                    doc_id,
                ])
            _render_results_table(st, rows=table_rows, sel=sel)
            if true_docs > len(mention_groups):
                if st.button(f"Show more (+{min(limit, true_docs - len(mention_groups))})",
                             key="more-docs", use_container_width=True):
                    st.session_state["_docs_shown"] = docs_shown + limit
                    st.rerun()
        elif mention_groups:
            capped = (f" · top {len(mention_groups)} of {true_docs} by relevance"
                      if true_docs > len(mention_groups) else "")
            st.markdown(
                f"**Mentions** ({'bilingual exact' if bilingual_terms else 'exact'}) — "
                f"{true_occ} across {true_docs} document(s){capped}"
            )
            if bilingual_terms:
                st.caption("Direct ES/EN equivalents included: " + ", ".join(bilingual_terms))
            st.caption(_coverage_note(retriever.store, doc_rows))
            for g in mention_groups:
                _doc_row(st, doc_id=g.doc_id, company=g.company, period=g.period,
                         doc_type=g.doc_type, count=g.total_matches, selected=g.doc_id == sel,
                         companies=g.companies)
            if true_docs > len(mention_groups):
                if st.button(f"Show more (+{min(limit, true_docs - len(mention_groups))})",
                             key="more-docs", use_container_width=True):
                    st.session_state["_docs_shown"] = docs_shown + limit
                    st.rerun()
        elif not (metric_groups or analog_groups or concept_specs):
            st.caption("No literal mentions found.")

        if metric_groups and not table_mode:
            if mention_groups:
                st.divider()
            st.markdown(
                f"**Related wording** (curated discovery) — {expanded_docs} document(s)"
            )
            st.caption(
                "These documents contain curated equivalent wording, not the literal query. "
                "Their occurrences are not added to the Mentions count. Expanded with: "
                + ", ".join(metric_terms)
            )
            if expanded_docs > len(expanded_only_groups):
                st.caption(
                    f"{expanded_docs - len(expanded_only_groups)} document(s) also contain the "
                    "exact query and remain in the Exact lane."
                )
            for g in expanded_only_groups:
                _doc_row(st, doc_id=g.doc_id, company=g.company, period=g.period,
                         doc_type=g.doc_type, count=None, via=specs[g.doc_id]["via"],
                         selected=g.doc_id == sel, companies=g.companies)

        if analog_groups and not table_mode:
            if mention_groups or metric_groups:
                st.divider()
            st.markdown("**Analogs** — curated domain analogs (your analogs.yaml), not the "
                        "literal term")
            for g in analog_groups:
                _doc_row(st, doc_id=g.doc_id, company=g.company, period=g.period,
                         doc_type=g.doc_type, count=None, via=specs[g.doc_id]["via"],
                         selected=g.doc_id == sel, companies=g.companies)
        if concept_specs and not table_mode:
            st.divider()
            st.markdown("**Concepts** — semantic matches from the local embedding model")
            for doc_id, e in concept_specs.items():
                _doc_row(st, doc_id=doc_id, company=e["company"], period=e["period"],
                         doc_type=e["doc_type"], count=None, selected=doc_id == sel,
                         companies=e["companies"])

    with right:
        e = specs[sel]
        selected_row = next((r for r in doc_rows if r["doc_id"] == sel), None)
        type_label = (doc_type_label(e["doc_type"]) if callable(doc_type_label) else e["doc_type"])
        title = " ".join(str(e.get("title") or "").split())
        head_l, head_r = st.columns([2.2, 1], gap="small")
        with head_l:
            st.markdown(f"**{e['company'].upper()}** · {e['period'] or '—'} · {type_label}"
                        + (f" — {title}" if title and title != sel else ""))
        with head_r:
            pane = _pick(st, "Pane", _PANE_OPTIONS, key=_PANE_KEY, default="Document",
                         help="Read the document, or see the issuer's company card")
        if pane == "Company":
            _render_company_card(st, store=retriever.store, slug=e["company"],
                                 display_name=None, query=query, specs=specs,
                                 app_config=app_config, doc_type_label=doc_type_label)
            return
        _render_scope_chips(st, company=e["company"], doc_type=e["doc_type"],
                            doc_type_label=doc_type_label, doc_id=sel, query=query)
        if e["kind"] == "browse":
            st.caption("Opened from the company card — no query evidence in this document; the "
                       "reader shows it from the top.")
        _render_smart_summary(st, retriever=retriever, doc_id=sel,
                              markdown_path=e["markdown_path"], company=e["company"],
                              period=e["period"], title=title or sel, terms=e["highlight_terms"])
        if (selected_row is not None and "source_format" in selected_row.keys()
                and selected_row["source_format"] == "news" and selected_row["source_url"]):
            if hasattr(st, "link_button"):
                st.link_button("Open publisher article ↗", selected_row["source_url"],
                               use_container_width=True)
        if e["kind"] == "analog":
            via = ", ".join(e["via"]) or "an analog term"
            st.caption(f"🔗 Analog match (via {via}) — surfaced by your analog dictionary, not the "
                       "literal query. The analog term is highlighted below.")
        elif e["kind"] == "expanded":
            via = ", ".join(e["via"]) or "a curated synonym"
            st.caption(
                f"Related-wording match (via {via}) — a curated discovery aid, not an exact "
                "mention or a semantic inference."
            )
        elif e["kind"] == "concept":
            st.caption("Concept match — ranked by the local embedding model; no literal mention verified.")
        has_evidence = _render_mention_evidence(
            st, doc_id=sel, markdown_path=e["markdown_path"],
            spans=e.get("match_spans", []), total_matches=e.get("total_matches"),
            key=sel, terms=e["highlight_terms"],
        )
        if not has_evidence:
            _open_document(
                st, doc_id=sel, markdown_path=e["markdown_path"],
                offset_phrases=e["offset_phrases"],
                fallback=lambda: _render_context(
                    st, markdown_path=e["markdown_path"],
                    char_start=e["offset_phrases"][0][0] if e["offset_phrases"] else 0,
                    char_end=(e["offset_phrases"][0][0] + 1) if e["offset_phrases"] else 1,
                    key=f"concept-context-{sel}", terms=e["highlight_terms"],
                ),
                key=f"concept-{sel}", terms=e["highlight_terms"],
            )
        if st.button("Add passage to evidence brief", key=f"brief-add-{sel}"):
            from src.search.brief import EvidenceItem
            text = _brief_passage_text(
                e["markdown_path"], e["offset_phrases"]
            )
            brief = _brief_state(st)
            brief.query = query.strip()
            brief.add(EvidenceItem(
                doc_id=sel, company=e["company"], period=e["period"], doc_type=e["doc_type"],
                title=e.get("title", sel), text=text or "Selected passage",
                markdown_path=e["markdown_path"],
                source_path=(selected_row["source_path"] if selected_row and "source_path" in selected_row.keys() else None),
                char_start=e["offset_phrases"][0][0] if e["offset_phrases"] else None,
            ))
            st.rerun()


_SUMMARY_STYLE = (
    "padding:.6rem .9rem;border:1px solid #2a2f3a;border-left:3px solid #b58900;"
    "background:#171a21;color:#e6e6e6;border-radius:6px;line-height:1.55;margin:.15rem 0 .5rem 0;"
)


def _render_summary_points(st, points, *, key_prefix: str) -> None:
    """Shared renderer for a list of SummaryPoint — bullet + sentiment badge + Read-in-context."""
    for i, p in enumerate(points):
        marker = f"<b>[{p.marker}]</b> " if p.marker else "•&nbsp;"
        badge = _sentiment_badge(p.sentiment, "lexicon tone of this passage")
        st.markdown(f"<span style='color:#8b949e;font-size:.8rem'>{p.company.upper()} · "
                    f"{p.period or '—'}</span>  {badge}", unsafe_allow_html=True)
        st.markdown(f"<div style='{_SUMMARY_STYLE}'>{marker}{spans_to_html(p.text, [])}</div>",
                    unsafe_allow_html=True)
        if p.markdown_path:
            _open_document(
                st, doc_id=p.doc_id, markdown_path=p.markdown_path,
                offset_phrases=[(p.char_start, [p.text])],
                fallback=lambda p=p, i=i: _render_context(
                    st, markdown_path=p.markdown_path, char_start=p.char_start,
                    char_end=p.char_end, key=f"{key_prefix}-{i}", terms=[]),
                key=f"{key_prefix}-{i}")


def render_summary(st, retriever, store, *, config: dict, facets, query: str = "",
                   filters=None) -> None:
    """Summary panel — extractive "key takeaways" of one document, or a thematic summary of a query.

    Zero-typing: pick a company + period from the index facets. When a query is present, also offer
    a thematic summary over the retrieved passages. The LLM tier ("✨ Sharpen") appears only when
    ``summary.engine`` is llm/hybrid and the provider key resolves.
    """
    from src.qa import summarize

    st.caption("Extractive key-takeaways — offline & deterministic. "
               "Flip `summary.engine` to `hybrid` in the config for the optional LLM tier.")

    max_sentences = int((config.get("summary") or {}).get("max_sentences", 6))
    embedder = retriever.get_embedder()
    boiler = retriever._boilerplate_model()
    llm_on = summarize.llm_enabled(config)

    # --- Per-document summary -------------------------------------------------------------
    companies = list(facets.companies) if hasattr(facets, "companies") else []
    col_co, col_pd = st.columns(2)
    company = col_co.selectbox("Company", companies, key="sum-company") if companies else None
    periods = facets.periods_for([company]) if (company and hasattr(facets, "periods_for")) else []
    period = col_pd.selectbox("Period", list(reversed(periods)), key="sum-period") if periods else None

    if company and period:
        rows = store.document_rows(
            "EXISTS (SELECT 1 FROM document_companies dc WHERE dc.doc_id = documents.doc_id "
            "AND dc.company = ?) AND documents.period = ?", [company, period])
        if not rows:
            st.info("No indexed document for that company/period.")
        else:
            row = rows[0]
            memo_key = ("doc", company, period, max_sentences)
            memo = st.session_state.get("_summary_memo")
            if not (memo and memo.get("key") == memo_key):
                text = _doc_text(row["markdown_path"])
                summ = summarize.summarize_document(
                    text, doc_id=row["doc_id"], title=row["title"], company=company,
                    period=period, markdown_path=row["markdown_path"], embedder=embedder,
                    boilerplate=boiler, max_sentences=max_sentences)
                st.session_state["_summary_memo"] = {"key": memo_key, "summary": summ}
                memo = st.session_state["_summary_memo"]
            summ = memo["summary"]

            if llm_on and st.button("✨ Sharpen with LLM", key="sum-llm-doc"):
                summ = summarize.summarize_llm(summ, config)
            if summ.narrative:
                st.markdown(summ.narrative)
            if summ.points:
                st.markdown(f"**Key takeaways · {company.upper()} {period}**")
                _render_summary_points(st, summ.points, key_prefix="sumdoc")
            else:
                st.info("No salient passages found for this document.")

    # --- Thematic (query-scoped) summary --------------------------------------------------
    if query and query.strip():
        st.divider()
        if st.button(f"Summarize results for “{query.strip()}”", key="sum-thematic"):
            hits = retriever.search(query, filters=filters, limit=max(max_sentences * 2, 10))
            summ = summarize.summarize_hits(hits, query=query.strip(), embedder=embedder,
                                            boilerplate=boiler, max_sentences=max_sentences)
            if summ.points:
                st.markdown(f"**Across the corpus · “{query.strip()}”**")
                _render_summary_points(st, summ.points, key_prefix="sumtheme")
            else:
                st.info("No passages to summarize for that query.")


def render_financials(st, config, facets) -> None:
    """Financials panel — per-company metric table (+ accuracy vs ground truth when wired)."""
    from app.components import financials

    companies = list(facets.companies) if hasattr(facets, "companies") else []
    if not companies:
        st.caption("No companies in the index yet.")
        return
    slug = st.selectbox("Company", companies, key="fin-company")
    financials.render_financials_table(st, config, slug)


def render_trends(st, store, *, term: str, companies: "list[str] | None" = None,
                  filters=None, suggestions: "list[str] | None" = None) -> None:
    """Trends panel — mentions of a term per quarter per company, with mean sentiment.

    Zero typing: the term is picked from pills — the current search query (if any) first,
    then the suggested concepts.
    """
    from src.search.trends import mention_trend

    options: list[str] = []
    if term and term.strip():
        options.append(term.strip())
    options += [s for s in (suggestions or []) if s not in options]
    if not options:
        st.caption("Search above or pick a suggestion to chart mentions per quarter.")
        return

    picked = st.pills("Chart mentions of", options, selection_mode="single",
                      default=options[0])
    if not picked:
        st.caption("Pick a term above to chart how often it is mentioned per quarter.")
        return

    evidence_mode = st.pills(
        "Count evidence",
        ["Exact / direct translation", "Exact + curated expansions"],
        selection_mode="single",
        default="Exact / direct translation",
        help=(
            "Direct Spanish/English translations are part of the Command-F count. Curated expansions are optional related wording "
            "and should not be interpreted as literal mentions."
        ),
    )
    expand_synonyms = evidence_mode == "Exact + curated expansions"

    # Memoized per (term, companies): the FTS scan + per-chunk sentiment is repeated on
    # every rerun otherwise (tab bodies render even when another tab is active).
    scope_key = _filters_key(filters) if filters is not None else tuple(companies or [])
    trend_key = (picked, scope_key, expand_synonyms)
    trend_memo = st.session_state.setdefault("_trend_memo", {})
    if trend_key in trend_memo:
        points = trend_memo[trend_key]
    else:
        points = mention_trend(
            store, picked, companies=companies or None, filters=filters,
            expand_synonyms=expand_synonyms,
        )
        if len(trend_memo) > 16:
            trend_memo.clear()
        trend_memo[trend_key] = points
    if not points:
        st.warning("No mentions found. Try broader terms or clear the company filter.")
        return

    import pandas as pd

    df = pd.DataFrame([{"company": p.company, "period": p.period,
                        "mentions": p.mentions, "sentiment": p.sentiment,
                        "positive": p.positive, "neutral": p.neutral,
                        "negative": p.negative} for p in points])
    evidence_label = (
        "exact + related-wording occurrence(s)" if expand_synonyms
        else "exact / direct-translation mention(s)"
    )
    st.caption(f"{df['mentions'].sum()} {evidence_label} across "
               f"{df['company'].nunique()} company(ies) · {df['period'].nunique()} period(s)")
    if expand_synonyms:
        from src.index.keyword_index import metric_synonym_phrases

        expanded = metric_synonym_phrases(picked)
        st.info(
            "Related-wording mode is broader than Command-F and can be dominated by recurring "
            "tables or accounting language. It currently includes: " + ", ".join(expanded)
        )
    st.markdown("**Mentions per period**")
    st.bar_chart(df.pivot_table(index="period", columns="company",
                                values="mentions", aggfunc="sum").fillna(0))
    st.markdown("**Sentiment distribution of the mentions**")
    tone_df = df.groupby("period")[["positive", "neutral", "negative"]].sum()
    st.bar_chart(
        tone_df,
        color=["#3fb950", "#f5c518", "#f85149"],
        stack=True,
    )
    st.markdown("**Mean sentiment per period** (lexicon score, −1 to 1)")
    st.line_chart(df.pivot_table(index="period", columns="company",
                                 values="sentiment", aggfunc="mean"))
