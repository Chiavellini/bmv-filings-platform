"""Ask panel — AlphaSense-style generative search over the local corpus, honestly.

Layout: threads on the left; on the right the agent pills, the question box, and the thread's
turns. Every turn shows its **Research plan** (scope chips, the sub-queries that ran, the
documents read) before its answer; every ``[n]`` in the answer is a chip that opens the cited
passage inline. Without an LLM key the same pipeline returns the numbered **evidence pack**,
clearly labelled — the panel is useful offline and the LLM only ever adds the synthesis.

All orchestration is in :mod:`src.qa.ask` / :mod:`src.qa.redline`; persistence in
:mod:`src.state.threads`. This module is the Streamlit view and is rendered against a minimal
stand-in in the tests, so optional widgets are looked up through :func:`panels._widget`.
"""
from __future__ import annotations

import html
from pathlib import Path

import yaml

from app.components import panels
from app.components.highlight import spans_to_html
from app.components.panels import _PENDING_SCOPE, _pick, _sentiment_badge, _widget

_Q_KEY = "ask-q"                 # question box (widget key)
_THREAD_KEY = "ask_thread"       # current thread id (plain state)
_VIEW_KEY = "ask_view"           # "ask" | "redline"
_PREFILL_KEY = "_ask_prefill"    # staged question text (set before the widget is built)
_PICKED_KEY = "ask_picked"       # doc_ids ticked in the Search rail for a multi-doc question

_CHIP_STYLE = ("display:inline-block;margin:0 .3rem .3rem 0;padding:.05rem .5rem;"
               "border-radius:10px;border:1px solid #2aa198;color:#2aa198;font-size:.78rem")
_CITE_STYLE = ("display:inline-block;margin:0 .15rem;padding:0 .4rem;border-radius:8px;"
               "background:#1f3a3a;color:#7fd4c8;font-size:.75rem;font-weight:600")
_ANSWER_STYLE = ("padding:.8rem 1rem;border:1px solid #2a2f3a;border-left:3px solid #2aa198;"
                 "background:#12151b;color:#e6e6e6;border-radius:6px;line-height:1.6")
_EVIDENCE_STYLE = ("padding:.6rem .9rem;border:1px solid #2a2f3a;border-left:3px solid #b58900;"
                   "background:#171a21;color:#e6e6e6;border-radius:6px;line-height:1.55;"
                   "margin:.15rem 0 .4rem 0")
_ADDED_STYLE = "border-left:3px solid #3fb950;padding:.3rem .7rem;margin:.2rem 0;background:#12181b"
_REMOVED_STYLE = ("border-left:3px solid #f85149;padding:.3rem .7rem;margin:.2rem 0;"
                  "background:#1b1214;text-decoration:line-through;color:#bbb")
_CHANGED_STYLE = "border-left:3px solid #d29922;padding:.3rem .7rem;margin:.2rem 0;background:#1b1712"


# --------------------------------------------------------------------------------------------
# Config + state helpers
# --------------------------------------------------------------------------------------------
def load_agents(config: "dict | None" = None) -> list[dict]:
    """The agent templates from ``configs/agents.yaml`` (empty list when absent/invalid)."""
    try:
        from src.shared.paths import CONFIGS_DIR
        path = Path((config or {}).get("agents_path") or CONFIGS_DIR / "agents.yaml")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return [a for a in (data.get("agents") or []) if isinstance(a, dict) and a.get("key")]
    except Exception:  # noqa: BLE001 — agents are optional sugar
        return []


def fill_template(agent: dict, *, company: "str | None") -> str:
    return " ".join(str(agent.get("template") or "").format(
        company=company or "the company").split())


def _thread_store(st, path=None):
    store = st.session_state.get("_thread_store")
    if store is None:
        from src.state.threads import ThreadStore, default_threads_path

        store = ThreadStore(path or default_threads_path())
        st.session_state["_thread_store"] = store
    return store


def _stage(st, updates: dict) -> None:
    staged = dict(st.session_state.get(_PENDING_SCOPE) or {})
    staged.update(updates)
    st.session_state[_PENDING_SCOPE] = staged


def citation_records(result) -> list[dict]:
    """Serializable citations for a turn: cited hits (LLM mode) or every hit (evidence mode)."""
    hits = list(result.hits)
    if result.answer is not None and result.answer.citations:
        wanted = {c.marker for c in result.answer.citations}
        pairs = [(i, h) for i, h in enumerate(hits, start=1) if i in wanted]
    else:
        pairs = list(enumerate(hits, start=1))
    out = []
    for n, h in pairs:
        snip = getattr(h, "snippet", None)
        out.append({
            "marker": n, "doc_id": h.doc_id, "chunk_id": getattr(h, "chunk_id", ""),
            "title": " ".join((h.title or "").split()), "company": h.company,
            "period": h.period, "doc_type": h.doc_type, "markdown_path": h.markdown_path,
            "char_start": int(h.char_start), "char_end": int(h.char_end),
            "snippet": (snip.text if snip is not None else ""),
            "spans": [list(s) for s in (getattr(snip, "spans", None) or [])],
        })
    return out


def plan_record(plan) -> dict:
    return {
        "question": plan.question, "mode": plan.mode, "provider": plan.provider,
        "chunks_read": plan.chunks_read,
        "scope": [{"kind": c.kind, "label": c.label} for c in plan.scope],
        "sub_queries": list(plan.sub_queries),
        "docs_read": [{"doc_id": d.doc_id, "company": d.company, "period": d.period,
                       "doc_type": d.doc_type, "title": d.title, "hits": d.hits}
                      for d in plan.docs_read],
    }


# --------------------------------------------------------------------------------------------
# Rendering pieces
# --------------------------------------------------------------------------------------------
def _chips(labels: list[str]) -> str:
    return "".join(f"<span style='{_CHIP_STYLE}'>{html.escape(str(x))}</span>" for x in labels)


def _render_plan(st, plan: dict, *, doc_type_label, key: str) -> None:
    expander = _widget(st, "expander")
    label = (f"Research plan — {len(plan.get('sub_queries') or [])} sub-quer"
             f"{'y' if len(plan.get('sub_queries') or []) == 1 else 'ies'} · "
             f"{len(plan.get('docs_read') or [])} document(s) read · "
             f"{'LLM synthesis' if plan.get('mode') == 'llm' else 'evidence pack (no LLM)'}")
    ctx = expander(label, expanded=False) if expander is not None else None
    dl = doc_type_label if callable(doc_type_label) else (lambda d: d)
    body = ctx if ctx is not None else _Null()
    with body:
        st.markdown("**Scope** " + _chips([c["label"] for c in plan.get("scope") or []]),
                    unsafe_allow_html=True)
        st.markdown("**Searching**")
        for sq in plan.get("sub_queries") or []:
            st.markdown(f"- 🔍 {sq}")
        docs = plan.get("docs_read") or []
        if docs:
            st.markdown(f"**Reading** — {plan.get('chunks_read', 0)} passage(s)")
            for d in docs:
                st.markdown(f"- 📄 {d['company'].upper()} · {d['period'] or '—'} · "
                            f"{dl(d['doc_type'])} · {d['title']} ({d['hits']})")
        else:
            st.markdown("**Reading** — nothing matched the sub-queries in this scope.")
        if plan.get("provider"):
            st.caption(f"Provider: {plan['provider']}")


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _answer_html(text: str) -> str:
    """Answer text with ``[n]`` rendered as chips; everything else escaped, newlines kept."""
    from src.qa.ask import answer_segments

    parts = []
    for run, marker in answer_segments(text):
        if marker is not None:
            parts.append(f"<span style='{_CITE_STYLE}' title='source {marker}'>[{marker}]</span>")
        else:
            parts.append(html.escape(run).replace("\n", "<br>"))
    return "".join(parts)


def _render_sources(st, citations: list[dict], *, key: str, doc_type_label, mode: str) -> None:
    """Numbered sources with a Read-in-context button each; the opened one renders inline."""
    if not citations:
        return
    dl = doc_type_label if callable(doc_type_label) else (lambda d: d)
    st.markdown("**Sources**" if mode == "llm" else "**Evidence pack** — the passages an "
                "answer would be built from, ranked by the research plan")
    open_key = f"ask-open-{key}"
    for c in citations:
        head = (f"[{c['marker']}] {c['company'].upper()} · {c['period'] or '—'} · "
                f"{dl(c['doc_type'])}")
        if mode != "llm" and c.get("snippet"):
            spans = [tuple(s) for s in c.get("spans") or []]
            st.markdown(f"<div style='{_EVIDENCE_STYLE}'><b>{html.escape(head)}</b><br>"
                        f"{spans_to_html(c['snippet'], spans)}</div>", unsafe_allow_html=True)
        else:
            st.markdown(f"<span style='{_CITE_STYLE}'>[{c['marker']}]</span> "
                        f"{html.escape(head)} — {html.escape(c['title'])}", unsafe_allow_html=True)
        if st.button("Read in context ›", key=f"{open_key}-{c['marker']}"):
            st.session_state[open_key] = c["marker"]
    opened = st.session_state.get(open_key)
    if opened:
        c = next((x for x in citations if x["marker"] == opened), None)
        if c is not None:
            if st.button("Close passage", key=f"{open_key}-close"):
                st.session_state.pop(open_key, None)
                st.rerun()
            else:
                terms = []
                if c.get("snippet") and c.get("spans"):
                    terms = [c["snippet"][a:b] for a, b in c["spans"] if b > a][:4]
                panels._render_context(st, markdown_path=c["markdown_path"],
                                       char_start=c["char_start"], char_end=c["char_end"],
                                       key=f"ask-ctx-{key}-{c['marker']}", terms=terms)


def _render_turn(st, turn, *, doc_type_label) -> None:
    st.markdown(f"<div style='text-align:right'><span style='display:inline-block;"
                f"padding:.4rem .8rem;border-radius:10px;background:#1c2230;color:#e6e6e6'>"
                f"{html.escape(turn.question)}</span></div>", unsafe_allow_html=True)
    _render_plan(st, turn.plan, doc_type_label=doc_type_label, key=str(turn.turn_id))
    if turn.mode == "llm" and turn.answer_text:
        from src.qa.prompts import NOT_FOUND

        if turn.answer_text.strip() == NOT_FOUND:
            st.warning("Not found in the corpus — the retrieved passages do not answer this. "
                       "Widen the scope or rephrase.")
        else:
            st.markdown(f"<div style='{_ANSWER_STYLE}'>{_answer_html(turn.answer_text)}</div>",
                        unsafe_allow_html=True)
            st.caption("Generated from the numbered sources only; every claim carries its [n]. "
                       "Verify against the passage before quoting.")
    elif turn.mode != "llm":
        st.info("No LLM configured — showing the evidence pack instead of a synthesized answer. "
                "Enable `qa.enabled: true` in configs/alpha_go.yaml and set the provider key in "
                ".env to get cited answers.")
    _render_sources(st, turn.citations, key=str(turn.turn_id), doc_type_label=doc_type_label,
                    mode=turn.mode)


# --------------------------------------------------------------------------------------------
# Redline view (the no-LLM agent)
# --------------------------------------------------------------------------------------------
def _canonical_periods(periods: list[str]) -> list[str]:
    import re

    return [p for p in periods if re.fullmatch(r"\d{4}-(?:[1-4]T|FY)", p or "")]


def _render_redline(st, *, store, retriever, config, facets, scope_companies: list,
                    company_label, doc_type_label) -> None:
    from src.qa import redline as rl_mod
    from src.qa.ask import llm_ready
    from src.shared.report_index import period_sort_key

    st.markdown("**Quarter-over-quarter redline** — what changed between two filings of one "
                "company: added, removed and reworded sentences, figures first, with the tone "
                "tally of each side. Deterministic; the optional LLM narration cites the items.")
    companies = list(getattr(facets, "companies", []) or [])
    if not companies:
        st.caption("No companies in the index.")
        return
    default_co = scope_companies[0] if scope_companies and scope_companies[0] in companies else companies[0]
    selectbox = _widget(st, "selectbox")
    cl = company_label if callable(company_label) else (lambda c: str(c).upper())
    if selectbox is not None:
        company = selectbox("Company", companies, index=companies.index(default_co),
                            format_func=cl, key="rl-company")
    else:
        company = default_co
    periods = _canonical_periods(list((getattr(facets, "company_periods", {}) or {}).get(company, [])))
    periods = sorted(periods, key=period_sort_key)
    if len(periods) < 2:
        st.caption(f"{cl(company)} has fewer than two canonical periods on file — nothing to diff.")
        return
    if selectbox is not None:
        c1, c2 = st.columns(2)
        with c1:
            older = selectbox("Prior filing", periods, index=len(periods) - 2, key="rl-old")
        with c2:
            newer = selectbox("Latest filing", periods, index=len(periods) - 1, key="rl-new")
    else:
        older, newer = periods[-2], periods[-1]
    if older == newer:
        st.caption("Pick two different periods.")
        return

    def _row(period):
        rows = store.document_rows(
            "EXISTS (SELECT 1 FROM document_companies dc WHERE dc.doc_id = documents.doc_id "
            "AND dc.company = ?) AND documents.period = ?", [company, period])
        return rows[0] if rows else None

    r_old, r_new = _row(older), _row(newer)
    if r_old is None or r_new is None:
        st.caption("One of the periods has no indexed document.")
        return
    memo = st.session_state.setdefault("_redline_memo", {})
    mkey = (r_old["doc_id"], r_new["doc_id"])
    if mkey not in memo:
        boiler = None
        try:
            fn = getattr(retriever, "_boilerplate_model", None)
            boiler = fn() if callable(fn) else None
        except Exception:  # noqa: BLE001
            boiler = None
        memo[mkey] = rl_mod.redline(panels._doc_text(r_old["markdown_path"]),
                                    panels._doc_text(r_new["markdown_path"]),
                                    old_label=f"{cl(company)} {older}",
                                    new_label=f"{cl(company)} {newer}", boilerplate=boiler)
        if len(memo) > 8:
            for k in list(memo)[:-8]:
                memo.pop(k, None)
    rl = memo[mkey]

    st.markdown(f"**{rl.old_label} → {rl.new_label}** — {len(rl.changed)} reworded · "
                f"{len(rl.added)} added · {len(rl.removed)} removed · {rl.unchanged} unchanged")
    st.caption(f"Tone tally — prior: 🟢{rl.old_tone.positive} 🟡{rl.old_tone.neutral} "
               f"🔴{rl.old_tone.negative} (net {rl.old_tone.net:+d}) · latest: "
               f"🟢{rl.new_tone.positive} 🟡{rl.new_tone.neutral} 🔴{rl.new_tone.negative} "
               f"(net {rl.new_tone.net:+d})")

    if llm_ready(config) is not None:
        if st.button("✨ Narrate the changes (LLM, cites items)", key="rl-narrate"):
            memo[mkey] = rl_mod.narrate(rl, config)
            rl = memo[mkey]
    if rl.narrative:
        st.markdown(f"<div style='{_ANSWER_STYLE}'>{_answer_html(rl.narrative)}</div>",
                    unsafe_allow_html=True)
        st.caption("Items are numbered below in the order: reworded, added, removed.")

    numbered = {id(c): n for n, c in rl_mod.numbered_items(rl)}

    def _item(c, style):
        n = numbered.get(id(c))
        badge = _sentiment_badge(c.tone, "lexicon tone of the sentence")
        tag = f"<span style='{_CITE_STYLE}'>[{n}]</span> " if n else ""
        fig = " 🔢" if getattr(c, "numbers_changed", False) else ""
        if c.kind == "changed":
            body = (f"<div style='color:#999;text-decoration:line-through'>"
                    f"{html.escape(c.old_text or '')}</div><div>{html.escape(c.text)}</div>")
        else:
            body = html.escape(c.text)
        st.markdown(f"<div style='{style}'>{tag}{badge}{fig} {body}</div>", unsafe_allow_html=True)

    if rl.changed:
        st.markdown("**Reworded** (figures first)")
        for c in rl.changed:
            _item(c, _CHANGED_STYLE)
    if rl.added:
        st.markdown("**Added in the latest filing**")
        for c in rl.added:
            _item(c, _ADDED_STYLE)
    if rl.removed:
        st.markdown("**Removed since the prior filing**")
        for c in rl.removed:
            _item(c, _REMOVED_STYLE)
    if rl.total == 0:
        st.info("No sentence-level differences beyond boilerplate.")


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------
def render_ask(st, retriever, store, *, query: str, filters, config: "dict | None", facets,
               company_label=None, doc_type_label=None, threads_path=None) -> None:
    """The Ask mode of the Explore page."""
    from src.qa.ask import llm_ready, run_ask

    threads = _thread_store(st, threads_path)
    cfg = config or {}
    cl = company_label if callable(company_label) else (lambda c: str(c).upper())
    scope_companies = list(getattr(filters, "companies", None) or [])

    left, main = st.columns([1, 2.8], gap="medium")

    # ---- threads rail -------------------------------------------------------------------
    with left:
        st.markdown("**Threads**")
        if st.button("＋ New thread", key="ask-new", use_container_width=True):
            st.session_state.pop(_THREAD_KEY, None)
            st.session_state[_VIEW_KEY] = "ask"
            st.rerun()
        current = st.session_state.get(_THREAD_KEY)
        for t in threads.list_threads():
            title = t.title if len(t.title) <= 60 else t.title[:57].rstrip() + "…"
            label = f"{title} · {t.turns} turn{'s' if t.turns != 1 else ''}"
            if st.button(label, key=f"ask-thread-{t.thread_id}", use_container_width=True,
                         type="primary" if t.thread_id == current else "secondary"):
                st.session_state[_THREAD_KEY] = t.thread_id
                st.session_state[_VIEW_KEY] = "ask"
                st.rerun()
        if current and st.button("Delete this thread", key="ask-delete"):
            threads.delete_thread(current)
            st.session_state.pop(_THREAD_KEY, None)
            st.rerun()
        st.caption("Threads are saved locally (SQLite) and survive restarts.")

    with main:
        provider = llm_ready(cfg)
        if provider is None:
            st.caption("🔎 Evidence mode — no LLM key configured. Questions still run the research "
                       "plan and return the ranked evidence pack. Set `qa.enabled: true` + the key "
                       "in .env for cited answers.")
        else:
            st.caption(f"✨ Cited answers via {provider.provider} · {provider.model}. Every claim "
                       "carries a [n] that opens the passage.")

        # ---- agents ------------------------------------------------------------------
        agents = load_agents(cfg)
        if agents:
            st.markdown("**Agents** — one-click questions, scoped to the sidebar")
            cols = st.columns(min(3, len(agents)))
            for i, agent in enumerate(agents):
                with cols[i % len(cols)]:
                    if st.button(agent["name"], key=f"agent-{agent['key']}",
                                 help=agent.get("description"), use_container_width=True):
                        if agent.get("mode") == "redline":
                            st.session_state[_VIEW_KEY] = "redline"
                        else:
                            st.session_state[_VIEW_KEY] = "ask"
                            company = cl(scope_companies[0]) if len(scope_companies) == 1 else None
                            if agent.get("needs_company") and company is None:
                                st.session_state["_ask_notice"] = (
                                    f"“{agent['name']}” works best scoped to one company — pick "
                                    "one in the sidebar (2 · Companies). Running on the current "
                                    "scope instead.")
                            st.session_state[_PREFILL_KEY] = fill_template(agent, company=company)
                        st.rerun()

        if st.session_state.get(_VIEW_KEY) == "redline":
            if st.button("← Back to questions", key="rl-back"):
                st.session_state[_VIEW_KEY] = "ask"
                st.rerun()
            _render_redline(st, store=store, retriever=retriever, config=cfg, facets=facets,
                            scope_companies=scope_companies, company_label=company_label,
                            doc_type_label=doc_type_label)
            return

        notice = st.session_state.pop("_ask_notice", None)
        if notice:
            st.info(notice)

        # ---- question box (prefill BEFORE the widget is built) ------------------------
        prefill = st.session_state.pop(_PREFILL_KEY, None)
        if prefill is not None:
            st.session_state[_Q_KEY] = prefill
        elif _Q_KEY not in st.session_state and (query or "").strip():
            st.session_state[_Q_KEY] = query.strip()
        current = st.session_state.get(_THREAD_KEY)
        turns = threads.turns(current) if current else []
        text_area = _widget(st, "text_area")
        if text_area is not None:
            question = text_area("Ask a follow-up…" if turns else "Ask the corpus a question",
                                 key=_Q_KEY, height=80,
                                 placeholder="e.g. How has management's tone on pricing pressure "
                                             "changed over the last year?")
        else:
            question = st.session_state.get(_Q_KEY, "")
        from src.qa.ask import scope_chips
        st.markdown("Scope: " + _chips([c.label for c in scope_chips(
            filters, company_label=company_label, doc_type_label=doc_type_label)]),
            unsafe_allow_html=True)
        ask_label = "Ask follow-up" if turns else "Ask"
        if st.button(ask_label, key="ask-run", type="primary") and (question or "").strip():
            q = " ".join(question.split())
            if not current:
                thread = threads.create_thread(q, scope={
                    "companies": scope_companies,
                    "doc_types": list(getattr(filters, "doc_types", None) or []),
                    "doc_ids": list(getattr(filters, "doc_ids", None) or [])})
                current = thread.thread_id
                st.session_state[_THREAD_KEY] = current
            spinner = _widget(st, "spinner")
            ctx = spinner("Planning, searching, reading…") if spinner is not None else _Null()
            with ctx:
                result = run_ask(q, retriever, config=cfg, filters=filters,
                                 history=threads.history(current), company_label=company_label,
                                 doc_type_label=doc_type_label)
            threads.add_turn(current, question=q,
                             answer_text=(result.answer.text if result.answer else None),
                             mode=result.plan.mode, plan=plan_record(result.plan),
                             citations=citation_records(result))
            turns = threads.turns(current)
            # Clear the box for the next follow-up (the widget is rebuilt next run).
            _stage(st, {_Q_KEY: ""})
            st.rerun()

        # ---- the thread -------------------------------------------------------------
        for turn in turns:
            st.divider()
            _render_turn(st, turn, doc_type_label=doc_type_label)
        if not turns and not current:
            st.caption("Ask anything about the corpus in scope. The research plan is shown before "
                       "the answer, and every citation opens its passage.")
