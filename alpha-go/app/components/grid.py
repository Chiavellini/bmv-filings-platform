"""Grid panel — Ask, batched over a document set.

Rows are the documents in the sidebar scope, columns are questions, and each cell is one run of
the Ask pipeline scoped to that single document. Everything that decides *what runs* lives in
:mod:`src.qa.grid`; this module is the Streamlit view and is rendered against a minimal stand-in
in the tests, so every optional widget is looked up through :func:`panels._widget`.

Two behaviours are load-bearing and deliberate:

* **Nothing computes on a rerun.** A batch runs only while ``_grid_run`` is in session state, and
  that key is written in exactly one place — the Run button. Re-rendering a finished grid never
  touches the retriever, so clicking around costs nothing.
* **A batch runs a few cells per script run, then reruns itself.** A long grid would otherwise
  freeze the page for minutes with no way out. Chunking keeps the progress bar live, makes Stop
  possible, and means an interrupted run keeps every cell it already finished.
"""
from __future__ import annotations

import html

from app.components import panels
from app.components.ask import _CITE_STYLE, _answer_html
from app.components.highlight import spans_to_html
from app.components.panels import _widget

_ASK_PICKED_KEY = "ask_picked"      # the Search rail's checkboxes — the Search → Grid hand-off
_QUESTIONS_KEY = "grid_questions"   # [{"key","text"}] in column order
_ROWS_KEY = "grid_rows"             # [doc_id] in row order
_CELLS_KEY = "_grid_cells"          # {(doc_id, question_key): GridCell}
_RUN_KEY = "_grid_run"              # the active batch; its presence is what authorises work
_OPEN_KEY = "_grid_open"            # (doc_id, question_key, marker) of the passage on screen
_ADD_KEY = "grid-add-q"             # widget key: the new-question box
_CONFIRM_KEY = "grid-confirm"       # widget key: the large-run confirmation

_HEAD_STYLE = ("padding:.3rem .5rem;border-bottom:2px solid #2aa198;color:#7fd4c8;"
               "font-size:.82rem;font-weight:600;line-height:1.35")
_ROWHEAD_STYLE = ("padding:.5rem .4rem;border-top:1px solid #2a2f3a;color:#e6e6e6;"
                  "font-size:.84rem;line-height:1.4")
_CELL_LLM = ("padding:.5rem .6rem;border:1px solid #2a2f3a;border-left:3px solid #2aa198;"
             "background:#12151b;color:#e6e6e6;border-radius:5px;font-size:.82rem;"
             "line-height:1.5;margin:.25rem 0")
_CELL_EVIDENCE = ("padding:.5rem .6rem;border:1px solid #2a2f3a;border-left:3px solid #b58900;"
                  "background:#171a21;color:#e6e6e6;border-radius:5px;font-size:.82rem;"
                  "line-height:1.5;margin:.25rem 0")
_CELL_MUTED = ("padding:.5rem .6rem;border:1px dashed #2a2f3a;background:#0f1116;color:#8b949e;"
               "border-radius:5px;font-size:.82rem;line-height:1.5;margin:.25rem 0")


# --------------------------------------------------------------------------------------------
# Session-state accessors
# --------------------------------------------------------------------------------------------
def _questions(st) -> list:
    from src.qa.grid import GridQuestion

    return [GridQuestion(key=q["key"], text=q["text"])
            for q in (st.session_state.get(_QUESTIONS_KEY) or [])]


def _set_questions(st, questions: list) -> None:
    st.session_state[_QUESTIONS_KEY] = [{"key": q.key, "text": q.text} for q in questions]


def _cells(st) -> dict:
    return st.session_state.setdefault(_CELLS_KEY, {})


def _spec(st, choices: list, lim) -> "object":
    """The grid as it currently stands: chosen documents × chosen questions, in order."""
    from src.qa.grid import GridSpec, row_from_choice

    by_id = {c.doc_id: c for c in choices}
    ordered = [by_id[d] for d in (st.session_state.get(_ROWS_KEY) or []) if d in by_id]
    return GridSpec(rows=[row_from_choice(c) for c in ordered[:lim.max_rows]],
                    questions=_questions(st)[:lim.max_columns])


# --------------------------------------------------------------------------------------------
# Column controls
# --------------------------------------------------------------------------------------------
def _render_presets(st, cfg, lim) -> None:
    from src.qa.grid import load_presets, make_question

    presets = load_presets(cfg)
    if not presets:
        return
    st.markdown("**Question sets** — one click fills the columns")
    cols = st.columns(min(3, len(presets)))
    for i, preset in enumerate(presets):
        with cols[i % len(cols)]:
            if st.button(preset["name"], key=f"grid-preset-{preset['key']}",
                         help=preset.get("description"), use_container_width=True):
                _set_questions(st, [make_question(q)
                                    for q in preset["questions"][:lim.max_columns]])
                st.rerun()
            st.caption(preset.get("description", ""))


def _render_column_editor(st, questions: list, lim) -> None:
    from src.qa.grid import make_question

    text_input = _widget(st, "text_input")
    if text_input is not None:
        typed = text_input("Add a question", key=_ADD_KEY,
                           placeholder="e.g. What is said about refinancing?")
        if st.button("＋ Add column", key="grid-add") and (typed or "").strip():
            if len(questions) >= lim.max_columns:
                st.warning(f"A grid holds at most {lim.max_columns} questions. "
                           "Remove one before adding another.")
            else:
                question = make_question(typed)
                if question.key not in {q.key for q in questions}:
                    _set_questions(st, questions + [question])
                    st.rerun()


def _column_controls(st, questions: list, index: int) -> None:
    """◀ ✕ ▶ under a column header. Removing keeps the cells — the key is content-derived."""
    left, drop, right = st.columns(3)
    with left:
        if index > 0 and st.button("◀", key=f"grid-left-{questions[index].key}",
                                   help="Move this question left"):
            reordered = list(questions)
            reordered[index - 1], reordered[index] = reordered[index], reordered[index - 1]
            _set_questions(st, reordered)
            st.rerun()
    with drop:
        if st.button("✕", key=f"grid-drop-{questions[index].key}",
                     help="Remove this question. Its answers are kept — adding the same "
                          "question back restores them at no cost."):
            _set_questions(st, [q for i, q in enumerate(questions) if i != index])
            st.rerun()
    with right:
        if index < len(questions) - 1 and st.button("▶", key=f"grid-right-{questions[index].key}",
                                                    help="Move this question right"):
            reordered = list(questions)
            reordered[index], reordered[index + 1] = reordered[index + 1], reordered[index]
            _set_questions(st, reordered)
            st.rerun()


# --------------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------------
def _start(st, plan, *, only: "str | None" = None) -> None:
    st.session_state[_RUN_KEY] = {"pending": [list(c) for c in plan.to_run],
                                  "done": 0, "total": len(plan.to_run), "only": only}
    st.rerun()


def _render_run_controls(st, spec, plan, lim, *, provider) -> None:
    """The budget line and the Run buttons — the only place a batch is ever authorised."""
    if plan.blocked:
        st.warning(plan.blocked)
        return
    if not spec.total_cells:
        st.caption("Pick at least one document and add one question to build a grid.")
        return

    calls = ("no LLM key — evidence mode, no API calls" if plan.mode == "evidence"
             else f"{plan.llm_calls} LLM call(s) · {provider.provider} · {provider.model}")
    st.caption(f"**{spec.total_cells} cell(s)** — {plan.already_done} already answered · "
               f"{len(plan.to_run)} to run · {calls}")
    if plan.mode == "llm" and plan.to_run:
        st.caption("Cells already answered are skipped, and a repeat of an identical cell is "
                   "served from the local LLM cache — so some of these may cost nothing.")

    confirmed = True
    if plan.needs_confirmation:
        checkbox = _widget(st, "checkbox")
        # Fails closed: without a checkbox widget a large run simply cannot be started.
        confirmed = bool(checkbox(f"I understand this will make up to {plan.llm_calls} LLM calls",
                                  key=_CONFIRM_KEY)) if checkbox is not None else False

    run, failed, everything = st.columns([1.4, 1, 1])
    with run:
        if st.button(f"▶ Run {len(plan.to_run)} cell(s)", key="grid-run", type="primary",
                     disabled=not plan.to_run, use_container_width=True) and plan.to_run:
            if confirmed:
                _start(st, plan)
            else:
                st.warning("Tick the confirmation above to start a run this size.")
    n_failed = sum(1 for c in _cells(st).values() if c.status == "error")
    with failed:
        if n_failed and st.button(f"↻ Re-run {n_failed} failed", key="grid-rerun-failed",
                                  use_container_width=True):
            _start(st, _rebuild_plan(st, spec, only="failed"), only="failed")
    with everything:
        if plan.already_done and st.button("↻ Re-run everything", key="grid-rerun-all",
                                           use_container_width=True,
                                           help="Re-runs every cell, including answered ones."):
            fresh = _rebuild_plan(st, spec, only="all")
            if not fresh.needs_confirmation or confirmed:
                _start(st, fresh, only="all")
            else:
                st.warning("Tick the confirmation above to re-run the whole grid.")


def _rebuild_plan(st, spec, *, only):
    from src.qa import grid as grid_mod

    return grid_mod.plan_run(spec, _cells(st), config=st.session_state.get("_grid_config"),
                             only=only)


def _advance_run(st, spec, retriever, *, cfg, lim, company_label, doc_type_label) -> bool:
    """Run one chunk of the active batch. Returns True when a batch was in progress."""
    from src.qa import grid as grid_mod

    run = st.session_state.get(_RUN_KEY)
    if not run:
        return False

    done, total = run["done"], run["total"]
    progress = _widget(st, "progress")
    if progress is not None:
        progress(min(1.0, done / total) if total else 1.0,
                 text=f"Running — {done} of {total} cell(s) done")
    else:
        st.caption(f"Running — {done} of {total} cell(s) done")
    if st.button("⏹ Stop", key="grid-stop",
                 help="Stops after the cells already in flight. Everything finished is kept."):
        st.session_state.pop(_RUN_KEY, None)
        st.rerun()
        return True

    pending = [tuple(c) for c in run["pending"]]
    chunk, rest = pending[:lim.chunk_size], pending[lim.chunk_size:]
    cells = _cells(st)
    for cell in grid_mod.run_batch(spec, chunk, retriever, config=cfg, lim=lim,
                                   company_label=company_label, doc_type_label=doc_type_label):
        cells[(cell.doc_id, cell.question_key)] = cell     # persisted the instant it lands
        run["done"] += 1
    run["pending"] = [list(c) for c in rest]
    if rest:
        st.session_state[_RUN_KEY] = run
    else:
        st.session_state.pop(_RUN_KEY, None)
    st.rerun()
    return True


# --------------------------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------------------------
def _cell_html(cell, summary: str) -> str:
    from src.qa import grid as grid_mod

    if cell is None or cell.status == grid_mod.EMPTY:
        return f"<div style='{_CELL_MUTED}'>not run yet</div>"
    if cell.status in (grid_mod.NO_HITS, grid_mod.NOT_FOUND, grid_mod.ERROR):
        return f"<div style='{_CELL_MUTED}'>{html.escape(summary)}</div>"
    if cell.answer_text is not None:
        return f"<div style='{_CELL_LLM}'>{_answer_html(summary)}</div>"
    spans = []
    if cell.citations:
        spans = [tuple(s) for s in (cell.citations[0].get("spans") or [])]
        raw = cell.citations[0].get("snippet") or ""
        if raw and len(raw) <= len(summary):
            return (f"<div style='{_CELL_EVIDENCE}'><span style='{_CITE_STYLE}'>[1]</span> "
                    f"{spans_to_html(raw, spans)}</div>")
    return (f"<div style='{_CELL_EVIDENCE}'><span style='{_CITE_STYLE}'>[1]</span> "
            f"{html.escape(summary)}</div>")


def _render_matrix(st, spec, cells, *, company_label, doc_type_label) -> None:
    from src.qa import grid as grid_mod

    cl = company_label if callable(company_label) else (lambda c: str(c).upper())
    dl = doc_type_label if callable(doc_type_label) else (lambda d: str(d))
    questions = spec.questions
    widths = [1.5] + [1.0] * len(questions)

    header = st.columns(widths)
    with header[0]:
        st.markdown(f"<div style='{_HEAD_STYLE}'>Document</div>", unsafe_allow_html=True)
    for i, question in enumerate(questions):
        with header[i + 1]:
            st.markdown(f"<div style='{_HEAD_STYLE}'>{html.escape(question.text)}</div>",
                        unsafe_allow_html=True)
            _column_controls(st, questions, i)

    for row in spec.rows:
        strip = st.columns(widths)
        with strip[0]:
            st.markdown(f"<div style='{_ROWHEAD_STYLE}'><b>{html.escape(cl(row.company))}</b> · "
                        f"{html.escape(row.period or '—')}<br>"
                        f"<small>{html.escape(dl(row.doc_type))}</small></div>",
                        unsafe_allow_html=True)
        for i, question in enumerate(questions):
            cell = cells.get((row.doc_id, question.key))
            with strip[i + 1]:
                summary = grid_mod.cell_summary(cell)
                st.markdown(_cell_html(cell, summary), unsafe_allow_html=True)
                if cell is not None and cell.citations:
                    expander = _widget(st, "expander")
                    label = f"Sources ({len(cell.citations)})"
                    body = expander(label, expanded=False) if expander is not None else None
                    if body is not None:
                        with body:
                            _render_cell_sources(st, cell, row, question, dl)
                    else:
                        _render_cell_sources(st, cell, row, question, dl)


def _render_cell_sources(st, cell, row, question, dl) -> None:
    for cit in cell.citations:
        head = (f"[{cit['marker']}] {cit['company'].upper()} · {cit['period'] or '—'} · "
                f"{dl(cit['doc_type'])}")
        st.markdown(f"<span style='{_CITE_STYLE}'>[{cit['marker']}]</span> "
                    f"{html.escape(head)}", unsafe_allow_html=True)
        if st.button("Read in context ›",
                     key=f"grid-open-{row.doc_id}-{question.key}-{cit['marker']}"):
            st.session_state[_OPEN_KEY] = (row.doc_id, question.key, cit["marker"])
            st.rerun()


def _render_open_passage(st, spec, cells, *, doc_type_label) -> None:
    """The opened passage, full width BELOW the matrix — a document window inside a 1/6-width
    column is unreadable, and it keeps the reader's own columns out of a nested position."""
    opened = st.session_state.get(_OPEN_KEY)
    if not opened:
        return
    doc_id, qkey, marker = opened
    cell = cells.get((doc_id, qkey))
    cit = next((c for c in (cell.citations if cell else []) if c["marker"] == marker), None)
    if cit is None:
        st.session_state.pop(_OPEN_KEY, None)
        return
    st.divider()
    question = spec.question(qkey)
    st.markdown(f"**Passage [{marker}]** — {html.escape(question.text if question else '')}")
    if st.button("Close passage", key="grid-close"):
        st.session_state.pop(_OPEN_KEY, None)
        st.rerun()
        return
    terms = []
    if cit.get("snippet") and cit.get("spans"):
        terms = [cit["snippet"][a:b] for a, b in cit["spans"] if b > a][:4]
    panels._render_context(st, markdown_path=cit["markdown_path"], char_start=cit["char_start"],
                           char_end=cit["char_end"], key=f"grid-ctx-{doc_id}-{qkey}-{marker}",
                           terms=terms)


def _render_exports(st, spec, cells) -> None:
    from src.qa import grid as grid_mod

    download = _widget(st, "download_button")
    if download is None or not spec.total_cells:
        return
    left, right = st.columns(2)
    with left:
        download("⬇︎ Matrix CSV", grid_mod.to_matrix_csv(spec, cells), file_name="grid.csv",
                 mime="text/csv", key="grid-dl-matrix", use_container_width=True)
    with right:
        download("⬇︎ Citations CSV", grid_mod.to_citations_csv(spec, cells),
                 file_name="grid_citations.csv", mime="text/csv", key="grid-dl-citations",
                 use_container_width=True)
    st.caption("Exports are written locally without an LLM. Evidence cells are labelled "
               "`[evidence]` so an export can never be mistaken for synthesized answers.")


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------
def render_grid(st, retriever, store, *, query: str, filters, config: "dict | None", facets,
                company_label=None, doc_type_label=None) -> None:
    """The Grid mode of the Explore page."""
    from src.qa import grid as grid_mod
    from src.qa.ask import llm_ready

    cfg = config or {}
    st.session_state["_grid_config"] = cfg
    lim = grid_mod.limits(cfg)
    provider = llm_ready(cfg)

    if provider is None:
        st.caption("🔎 Evidence mode — no LLM key configured. Each cell is the best-matching "
                   "passage from that document for that question, **not an answer**. Set "
                   "`qa.enabled: true` in configs/alpha_go.yaml and the provider key in .env "
                   "for cited answers.")
    else:
        st.caption(f"✨ Cited answers via {provider.provider} · {provider.model}. One cell is one "
                   "question asked of one document; every claim carries a [n].")

    choices = facets.documents_for(list(getattr(filters, "companies", None) or []),
                                   list(getattr(filters, "industries", None) or []),
                                   getattr(filters, "period_from", None),
                                   getattr(filters, "period_to", None),
                                   list(getattr(filters, "doc_types", None) or []))
    scoped = list(getattr(filters, "doc_ids", None) or [])
    if scoped:
        choices = [c for c in choices if c.doc_id in set(scoped)]
    if not choices:
        st.info("No documents in the current scope. Widen the sidebar filters to build a grid.")
        return

    _render_rows(st, choices, lim, company_label=company_label, doc_type_label=doc_type_label)
    st.divider()
    questions = _questions(st)
    if not questions:
        _render_presets(st, cfg, lim)
    _render_column_editor(st, questions, lim)

    spec = _spec(st, choices, lim)
    cells = _cells(st)
    plan = grid_mod.plan_run(spec, cells, config=cfg, lim=lim)

    if not _advance_run(st, spec, retriever, cfg=cfg, lim=lim, company_label=company_label,
                        doc_type_label=doc_type_label):
        _render_run_controls(st, spec, plan, lim, provider=provider)

    if spec.total_cells:
        st.divider()
        _render_matrix(st, spec, cells, company_label=company_label,
                       doc_type_label=doc_type_label)
        _render_exports(st, spec, cells)
        _render_open_passage(st, spec, cells, doc_type_label=doc_type_label)


def _render_rows(st, choices: list, lim, *, company_label, doc_type_label) -> None:
    """The document rows, seeded from whatever the Search rail already has ticked."""
    cl = company_label if callable(company_label) else (lambda c: str(c).upper())
    dl = doc_type_label if callable(doc_type_label) else (lambda d: str(d))
    available = {c.doc_id: c for c in choices}

    if _ROWS_KEY not in st.session_state:
        picked = [d for d in (st.session_state.get(_ASK_PICKED_KEY) or []) if d in available]
        st.session_state[_ROWS_KEY] = (picked or list(available))[:lim.max_rows]

    current = [d for d in st.session_state[_ROWS_KEY] if d in available]
    multiselect = _widget(st, "multiselect")
    st.markdown(f"**Documents** — the grid's rows (at most {lim.max_rows})")
    if multiselect is not None:
        def _label(doc_id: str) -> str:
            c = available[doc_id]
            return f"{cl(c.company)} · {c.period or '—'} · {dl(c.doc_type)}"

        chosen = multiselect("Documents", list(available), default=current, format_func=_label,
                             key="grid-rows-pick", label_visibility="collapsed")
        if len(chosen) > lim.max_rows:
            st.warning(f"A grid holds at most {lim.max_rows} documents; using the first "
                       f"{lim.max_rows} of the {len(chosen)} selected.")
        current = list(chosen)[:lim.max_rows]
    st.session_state[_ROWS_KEY] = current
    if not current:
        st.caption("Tick documents in Search, or pick them above, to fill the rows.")
    else:
        st.caption(f"{len(current)} document(s) selected · seeded from the documents ticked in "
                   "Search.")
