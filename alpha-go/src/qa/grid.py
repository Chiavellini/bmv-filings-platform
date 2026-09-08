"""Grid — Ask, batched over a document set.

Rows are documents, columns are questions, and every cell is one run of the Phase-C Ask
pipeline scoped to a single document::

    run_ask(question, retriever, filters=SearchFilters(doc_ids=[doc_id]))

Nothing about retrieval, synthesis, citation or caching is re-implemented here — :mod:`src.qa.ask`
already decomposes the question, fuses the sub-queries, falls back to the numbered evidence pack
without a provider key and disk-caches the synthesis. This module adds only what batching needs:

* a **budget** that cannot be talked past. ``limits()`` clamps config to hard ceilings and
  :func:`plan_run` refuses — rather than truncating — a matrix bigger than the cell cap, so an
  accidental hundred-call run is unreachable from the UI.
* an **honest cell status**. ``run_ask`` never raises: a provider failure comes back looking
  exactly like the offline evidence pack. :func:`run_cell` compares against
  :func:`src.qa.ask.llm_ready` so a broken call is reported as ``error`` and never dressed up as
  a legitimate evidence cell.
* a **resumable sequential runner**. :func:`run_batch` is a generator yielding one finished cell
  at a time, so the caller can persist each before the next starts and a stopped run keeps
  everything already done.

Execution is deliberately serial. The ``IndexStore`` connection, the cached vector matrix and the
embedding model are all shared, lazily-built, single-threaded objects; fanning a grid across
threads would buy seconds on a ≤24-cell run in exchange for touching the retriever.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field

from src.qa import ask
from src.search.filters import SearchFilters

# --- Hard ceilings. Config may lower these; nothing can raise them. --------------------------
HARD_MAX_ROWS = 12
HARD_MAX_COLUMNS = 6
HARD_MAX_CELLS = 24
HARD_MAX_CHUNK = 8

_DEFAULT_CHUNK = 4          # cells per script run, so a long batch never freezes the page
_DEFAULT_CONFIRM = 8        # above this many real LLM calls, the run needs a second, explicit act
_DEFAULT_TOP_K = 6          # a cell searches ONE document, so Ask's corpus-wide 8 is wasteful
_SUMMARY_CHARS = 240

# Cell status vocabulary. "ok" is the only one that carries content.
OK = "ok"
EMPTY = "empty"
NOT_FOUND = "not_found"
NO_HITS = "no_hits"
ERROR = "error"
_ANSWERED = frozenset({OK, NOT_FOUND, NO_HITS})

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MARKER_RE = re.compile(r"\[(\d+)\]")


class GridLimitError(ValueError):
    """Raised when a run would exceed the cell ceiling. The runner refuses; it never truncates."""


@dataclass(frozen=True)
class GridLimits:
    max_rows: int = HARD_MAX_ROWS
    max_columns: int = HARD_MAX_COLUMNS
    max_cells: int = HARD_MAX_CELLS
    chunk_size: int = _DEFAULT_CHUNK
    confirm_threshold: int = _DEFAULT_CONFIRM
    top_k: int = _DEFAULT_TOP_K


def limits(config: "dict | None" = None) -> GridLimits:
    """``config['grid']`` clamped by the hard ceilings; anything unparseable falls back."""
    block = (config or {}).get("grid") or {}

    def _one(key: str, default: int, ceiling: int) -> int:
        try:
            value = int(block[key])
        except (KeyError, TypeError, ValueError):
            return default
        return max(1, min(value, ceiling))

    return GridLimits(
        max_rows=_one("max_rows", HARD_MAX_ROWS, HARD_MAX_ROWS),
        max_columns=_one("max_columns", HARD_MAX_COLUMNS, HARD_MAX_COLUMNS),
        max_cells=_one("max_cells", HARD_MAX_CELLS, HARD_MAX_CELLS),
        chunk_size=_one("chunk_size", _DEFAULT_CHUNK, HARD_MAX_CHUNK),
        confirm_threshold=_one("confirm_threshold", _DEFAULT_CONFIRM, HARD_MAX_CELLS),
        top_k=_one("top_k", _DEFAULT_TOP_K, 20),
    )


# --------------------------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------------------------
def normalize_question(text: str) -> str:
    return " ".join((text or "").split())[:300]


def question_key(text: str) -> str:
    """A stable, content-derived id for a question column.

    Content-derived on purpose: removing a column and adding the same question back reuses its
    key, so the already-answered cells come straight back and cost nothing.
    """
    norm = normalize_question(text).casefold()
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:6]
    slug = _SLUG_RE.sub("-", norm).strip("-")[:32].strip("-")
    return f"{slug}-{digest}" if slug else digest


@dataclass(frozen=True)
class GridQuestion:
    key: str
    text: str


def make_question(text: str) -> GridQuestion:
    norm = normalize_question(text)
    return GridQuestion(key=question_key(norm), text=norm)


@dataclass(frozen=True)
class GridRow:
    doc_id: str
    company: str
    period: "str | None"
    doc_type: str
    title: str


def row_from_choice(choice) -> GridRow:
    """A :class:`src.search.facets.DocumentChoice` (or any hit-like) as a grid row."""
    return GridRow(doc_id=choice.doc_id, company=choice.company,
                   period=getattr(choice, "period", None), doc_type=choice.doc_type,
                   title=" ".join((getattr(choice, "title", "") or "").split()))


def rows_from_choices(choices, *, lim: "GridLimits | None" = None) -> "tuple[list, int]":
    """``(rows, dropped)`` — capped at ``max_rows``, reporting how many were left out."""
    lim = lim or limits()
    picked = [row_from_choice(c) for c in (choices or [])]
    return picked[:lim.max_rows], max(0, len(picked) - lim.max_rows)


@dataclass
class GridSpec:
    rows: list = field(default_factory=list)        # GridRow
    questions: list = field(default_factory=list)   # GridQuestion

    @property
    def total_cells(self) -> int:
        return len(self.rows) * len(self.questions)

    def coords(self) -> "list[tuple[str, str]]":
        """Every ``(doc_id, question_key)`` pair, row-major — the order a run executes in."""
        return [(r.doc_id, q.key) for r in self.rows for q in self.questions]

    def question(self, key: str) -> "GridQuestion | None":
        return next((q for q in self.questions if q.key == key), None)

    def row(self, doc_id: str) -> "GridRow | None":
        return next((r for r in self.rows if r.doc_id == doc_id), None)


@dataclass
class GridCell:
    doc_id: str
    question_key: str
    status: str = EMPTY
    mode: str = "evidence"                          # "llm" | "evidence"
    answer_text: "str | None" = None                # None whenever there is no synthesis
    citations: list = field(default_factory=list)   # ask.citation_records shape
    plan: dict = field(default_factory=dict)        # ask.plan_record shape
    provider: "str | None" = None
    cached: bool = False
    error: "str | None" = None

    @property
    def answered(self) -> bool:
        return self.status in _ANSWERED


@dataclass
class GridPlan:
    """What a run would do, decided before a single call is made."""
    total_cells: int = 0
    already_done: int = 0
    to_run: list = field(default_factory=list)      # [(doc_id, question_key)]
    mode: str = "evidence"                          # "llm" | "evidence"
    provider: "str | None" = None
    blocked: "str | None" = None                    # set => refuse; to_run is empty
    confirm_threshold: int = _DEFAULT_CONFIRM

    @property
    def llm_calls(self) -> int:
        """The honest number: zero without a provider, however many cells are queued."""
        return len(self.to_run) if self.mode == "llm" else 0

    @property
    def needs_confirmation(self) -> bool:
        """Big spends take a second, deliberate act — see the panel's confirmation checkbox."""
        return self.llm_calls > self.confirm_threshold


def plan_run(spec: GridSpec, existing: "dict | None" = None, *, config: "dict | None" = None,
             lim: "GridLimits | None" = None, only: "str | None" = None) -> GridPlan:
    """Decide the work. ``only``: ``None`` → unanswered + failed, ``"failed"``, ``"all"``.

    Refuses (via ``blocked``) rather than truncating when the matrix exceeds the cell cap, so the
    caller cannot accidentally run a subset it did not ask for.
    """
    lim = lim or limits(config)
    cells = existing or {}
    provider = ask.llm_ready(config)
    plan = GridPlan(total_cells=spec.total_cells,
                    mode="llm" if provider is not None else "evidence",
                    provider=(f"{provider.provider} · {provider.model}" if provider else None),
                    confirm_threshold=lim.confirm_threshold)
    plan.already_done = sum(1 for c in spec.coords()
                            if (cell := cells.get(c)) is not None and cell.answered)

    if spec.total_cells > lim.max_cells:
        plan.blocked = (f"{len(spec.rows)} document(s) × {len(spec.questions)} question(s) = "
                        f"{spec.total_cells} cells, over the {lim.max_cells}-cell cap. "
                        f"Remove a question or narrow the document selection.")
        return plan

    for coord in spec.coords():
        cell = cells.get(coord)
        if only == "all":
            plan.to_run.append(coord)
        elif only == "failed":
            if cell is not None and cell.status == ERROR:
                plan.to_run.append(coord)
        elif cell is None or not cell.answered:
            plan.to_run.append(coord)
    return plan


# --------------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------------
def cell_filters(doc_id: str) -> SearchFilters:
    """The document IS the scope.

    Company/period/type are deliberately *not* carried over from the sidebar: the row set was
    already resolved under that scope when the grid was built, so re-applying live filters could
    only contradict a chosen row and turn it into a false "not mentioned".
    """
    return SearchFilters(doc_ids=[doc_id])


def cell_config(config: "dict | None", lim: "GridLimits | None" = None) -> dict:
    """``config`` with ``qa.top_k`` narrowed for a single-document cell (no mutation)."""
    lim = lim or limits(config)
    cfg = dict(config or {})
    cfg["qa"] = {**(cfg.get("qa") or {}), "top_k": lim.top_k}
    return cfg


def _citation_records(result) -> list:
    """Reuse the Ask panel's serializer so a grid cell's sources render through its code path."""
    from app.components.ask import citation_records  # lazy: keeps this module import-light

    return citation_records(result)


def _plan_record(plan) -> dict:
    from app.components.ask import plan_record

    return plan_record(plan)


def run_cell(question: GridQuestion, row: GridRow, retriever, *, config: "dict | None" = None,
             lim: "GridLimits | None" = None, company_label=None, doc_type_label=None,
             use_cache: bool = True) -> GridCell:
    """One cell: Ask, scoped to one document, with an honest status."""
    lim = lim or limits(config)
    wanted_llm = ask.llm_ready(config) is not None
    result = ask.run_ask(question.text, retriever, config=cell_config(config, lim),
                         filters=cell_filters(row.doc_id), company_label=company_label,
                         doc_type_label=doc_type_label, use_cache=use_cache)
    cell = GridCell(doc_id=row.doc_id, question_key=question.key, mode=result.plan.mode,
                    provider=result.plan.provider, cached=bool(result.cached),
                    plan=_plan_record(result.plan), citations=_citation_records(result))

    # run_ask swallows provider failures and returns the evidence pack, which is indistinguishable
    # from the offline path unless we remember that a provider WAS resolvable. Without this the
    # grid would quietly present broken calls as legitimate evidence cells.
    if wanted_llm and result.plan.mode == "evidence":
        reason = (result.plan.provider or "")
        cell.status = ERROR
        cell.error = reason.partition("(unreachable: ")[2].rstrip(")") or "provider unreachable"
        return cell

    if not result.hits:
        cell.status = NO_HITS
        return cell
    if result.not_found:
        cell.status = NOT_FOUND
        return cell
    cell.status = OK
    if result.answer is not None:
        cell.answer_text = result.answer.text
    return cell


def run_batch(spec: GridSpec, coords: list, retriever, *, config: "dict | None" = None,
              lim: "GridLimits | None" = None, company_label=None, doc_type_label=None):
    """Yield one finished :class:`GridCell` per coordinate, in order, one at a time.

    A generator so the caller persists each cell as it lands: stopping mid-run keeps everything
    already computed, and resuming re-plans only what is still missing.
    """
    lim = lim or limits(config)
    if len(coords) > lim.max_cells:
        raise GridLimitError(f"{len(coords)} cells requested, cap is {lim.max_cells}")
    for doc_id, qkey in coords:
        row, question = spec.row(doc_id), spec.question(qkey)
        if row is None or question is None:      # the grid changed under a stale coord
            continue
        yield run_cell(question, row, retriever, config=config, lim=lim,
                       company_label=company_label, doc_type_label=doc_type_label)


# --------------------------------------------------------------------------------------------
# Presentation-neutral text + export
# --------------------------------------------------------------------------------------------
def cell_summary(cell: "GridCell | None", *, max_chars: int = _SUMMARY_CHARS,
                 strip_markers: bool = False) -> str:
    """The short text a cell shows: the synthesis, or the top passage, or an honest dash."""
    if cell is None or cell.status == EMPTY:
        return ""
    if cell.status == ERROR:
        return f"— could not run ({cell.error})"
    if cell.status == NO_HITS:
        return "— not mentioned in this document"
    if cell.status == NOT_FOUND:
        return "— not found in this document"
    text = cell.answer_text or ""
    if not text and cell.citations:
        text = cell.citations[0].get("snippet", "") or ""
    text = " ".join(text.split())
    if strip_markers:
        text = " ".join(_MARKER_RE.sub("", text).split())
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"


def _csv_value(cell: "GridCell | None") -> str:
    """A cell as CSV text. Evidence cells are labelled so an export can never read as an answer."""
    if cell is None or cell.status == EMPTY:
        return ""
    body = cell_summary(cell, max_chars=10_000)
    if cell.status == OK and cell.answer_text is None:
        return f"[evidence] {body}"
    return body


def to_matrix_csv(spec: GridSpec, cells: "dict | None" = None) -> str:
    """One row per document, one column per question, plus the mode each cell ran in."""
    cells = cells or {}
    out = io.StringIO()
    header = (["doc_id", "company", "period", "doc_type", "title"]
              + [q.text for q in spec.questions] + ["grid_mode"])
    writer = csv.writer(out)
    writer.writerow(header)
    for row in spec.rows:
        picked = [cells.get((row.doc_id, q.key)) for q in spec.questions]
        modes = {c.mode for c in picked if c is not None and c.status != EMPTY}
        writer.writerow([row.doc_id, row.company, row.period or "", row.doc_type, row.title]
                        + [_csv_value(c) for c in picked]
                        + ["llm" if modes == {"llm"} else "evidence" if modes else ""])
    return out.getvalue()


def to_citations_csv(spec: GridSpec, cells: "dict | None" = None) -> str:
    """One row per citation, so every cell's evidence is auditable outside the app."""
    cells = cells or {}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["doc_id", "question", "marker", "cited_doc_id", "chunk_id", "company",
                     "period", "doc_type", "title", "snippet"])
    for row in spec.rows:
        for question in spec.questions:
            cell = cells.get((row.doc_id, question.key))
            for cit in (cell.citations if cell is not None else []):
                writer.writerow([row.doc_id, question.text, cit.get("marker", ""),
                                 cit.get("doc_id", ""), cit.get("chunk_id", ""),
                                 cit.get("company", ""), cit.get("period") or "",
                                 cit.get("doc_type", ""), cit.get("title", ""),
                                 " ".join((cit.get("snippet") or "").split())])
    return out.getvalue()


def load_presets(config: "dict | None" = None) -> list:
    """Question sets from ``configs/grid_presets.yaml`` (empty list when absent/invalid)."""
    from pathlib import Path

    import yaml

    try:
        from src.shared.paths import CONFIGS_DIR
        path = Path((config or {}).get("grid_presets_path") or CONFIGS_DIR / "grid_presets.yaml")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return [p for p in (data.get("presets") or [])
                if isinstance(p, dict) and p.get("key") and p.get("questions")]
    except Exception:  # noqa: BLE001 — presets are optional sugar, exactly like agents.yaml
        return []
