#!/usr/bin/env python3
"""
parse_tables.py — Tier 2 extraction: structured table cells via pdfplumber.

The regex engine (Tier 3) hunts numeric columns inside flattened layout text,
which forces brittle hacks: footnote-digit skips (``_FN``), "skip to after the
first %" column jumps, and hard-coded column order that breaks when the 2026
IFRS-16 columns reverse. Reading real table cells sidesteps all of that: a row
is matched by its leftmost label, and the value is simply the first numeric cell
to its right.

This tier is for PDF-only periods (2016–2020) and any metric XBRL doesn't tag.
It reuses the project's semantic search dictionary plus per-metric aliases and
labels, so row-label variants are handled in one shared matching layer.

Table cells are read literally; the printed scale varies by company (some print
full pesos, others thousands or millions), so callers pass a per-company
``table_scale`` to convert monetary cells into the metric's expected unit — the
Tier 2 analogue of the regex layer's per-pattern ``multiplier``.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from src.extract.table_periods import (
    normalize_target_period,
    parse_header_token,
    select_value_for_period,
)
from src.model.financial_model import MetricDef

_MATCH_THRESHOLD = 0.85
_LINE_TOL = 3.0   # vertical px tolerance when clustering words into a row

# A cell "looks numeric" if it starts with a digit/sign/paren/currency and is
# composed only of number characters. Rejects words; tolerates 1,234.5 / (94,052) / 14.3%.
_NUMERIC_RE = re.compile(r"^[\(\-\$]?\s?\d[\d,\.\s]*\%?\)?$")


def _norm(text: str) -> str:
    """Lower-case, strip accents, drop trailing footnote/punctuation, collapse spaces."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().strip()
    text = re.sub(r"[\.:]+$", "", text)          # trailing dots/colons
    text = re.sub(r"\s+", " ", text)
    return text


def _looks_numeric(token: str) -> bool:
    t = token.strip()
    if not t or t in ("-", "—"):
        return False
    if not _NUMERIC_RE.match(t):
        return False
    # reject a lone footnote digit ("1"); require a separator or 2+ digits
    digits = re.sub(r"\D", "", t)
    return len(digits) >= 2 or any(c in t for c in ".,()%")


def _label_score(norm_label: str, norm_alias: str) -> float:
    """1.0 for a prefix match; otherwise ratio over the alias-length head."""
    if not norm_alias:
        return 0.0
    if norm_label.startswith(norm_alias):
        return 1.0
    head = norm_label[: len(norm_alias)]
    return SequenceMatcher(None, head, norm_alias).ratio()


def _aliases_for(mdef: MetricDef) -> list[str]:
    """Row labels to look for: explicit aliases, else the display labels."""
    aliases = list(mdef.aliases)
    if not aliases:
        aliases = [mdef.label_es, mdef.label]
    return [_norm(a) for a in aliases if a]


# ---------------------------------------------------------------------------
# Candidate rows: (raw_label, [numeric_cell_strings])
# ---------------------------------------------------------------------------

def _rows_from_extract_tables(page) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []
    try:
        tables = page.extract_tables() or []
    except Exception:
        return rows
    for table in tables:
        for cells in table:
            cells = [c for c in cells if c not in (None, "")]
            if not cells:
                continue
            label = str(cells[0]).replace("\n", " ").strip()
            nums = [str(c).strip() for c in cells[1:] if _looks_numeric(str(c))]
            if label and nums:
                rows.append((label, nums))
    return rows


def _rows_from_words(page) -> list[tuple[str, list[str]]]:
    """Cluster words into visual lines, then split label words from numeric cells."""
    try:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
    except Exception:
        return []

    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        placed = False
        for line in lines:
            if abs(line[0]["top"] - w["top"]) <= _LINE_TOL:
                line.append(w)
                placed = True
                break
        if not placed:
            lines.append([w])

    rows: list[tuple[str, list[str]]] = []
    for line in lines:
        toks = [t["text"] for t in sorted(line, key=lambda t: t["x0"])]
        # label = leading non-numeric tokens; cells = numeric tokens after them
        i = 0
        while i < len(toks) and not _looks_numeric(toks[i]):
            i += 1
        label = " ".join(toks[:i]).strip()
        cells = [t for t in toks[i:] if _looks_numeric(t)]
        if label and cells:
            rows.append((label, cells))
    return rows


# ---------------------------------------------------------------------------
# Header-aware blocks: (label, [(col_index, cell_str)]) + a per-table header row
#
# These carry the original column index for each numeric cell so a matched data
# cell can be aligned to its period header (1T26 / 1T25 / Acum / Var%). The flat
# `_rows_from_*` functions above are left untouched (still used by the evidence
# cache, the measurement harness, and existing tests); flattening a block's
# cells in order reproduces their output exactly, so the positional fallback is
# behaviorally identical to the legacy path.
# ---------------------------------------------------------------------------

# (label, [(col_index, numeric_cell_str)])
BlockRow = tuple[str, list[tuple[int, str]]]


@dataclass(frozen=True)
class TableBlock:
    """One table: a period-header row (by column index) plus its data rows."""

    header_by_col: dict[int, str]
    rows: list[BlockRow]


def _is_period_header(text: str) -> bool:
    """True if a header cell parses to a concrete period (quarter or year)."""
    header = parse_header_token(text)
    return header is not None and (header.quarter is not None or header.year is not None)


def _find_header_row(table) -> int | None:
    """Index of the first grid row carrying ≥2 period-bearing header cells."""
    for idx, row in enumerate(table):
        hits = sum(1 for c in row if c not in (None, "") and _is_period_header(str(c)))
        if hits >= 2:
            return idx
    return None


def _blocks_from_extract_tables(page) -> list[TableBlock]:
    blocks: list[TableBlock] = []
    try:
        tables = page.extract_tables() or []
    except Exception:
        return blocks
    for table in tables:
        block = _grid_to_block(table)
        if block is not None:
            blocks.append(block)
    return blocks


def _grid_to_block(table) -> TableBlock | None:
    header_idx = _find_header_row(table)
    header_by_col: dict[int, str] = {}
    if header_idx is not None:
        for col, cell in enumerate(table[header_idx]):
            if cell not in (None, ""):
                header_by_col[col] = str(cell).replace("\n", " ").strip()

    # Every row is a data-row candidate; period-header rows simply yield no
    # numeric cells (so they drop out naturally), exactly as the flat path does.
    rows: list[BlockRow] = []
    for raw_cells in table:
        nonempty = [(col, c) for col, c in enumerate(raw_cells) if c not in (None, "")]
        if not nonempty:
            continue
        label = str(nonempty[0][1]).replace("\n", " ").strip()
        cell_pairs = [(col, str(c).strip()) for col, c in nonempty[1:] if _looks_numeric(str(c))]
        if label and cell_pairs:
            rows.append((label, cell_pairs))
    if not rows:
        return None
    return TableBlock(header_by_col=header_by_col, rows=rows)


def _cluster_columns(line: list[dict], gap: float = 8.0) -> list[dict]:
    """Group horizontally-adjacent words into column cells by x-gap."""
    cols: list[list[dict]] = []
    current: list[dict] = []
    for tok in sorted(line, key=lambda t: t["x0"]):
        if current and tok["x0"] - current[-1]["x1"] > gap:
            cols.append(current)
            current = []
        current.append(tok)
    if current:
        cols.append(current)
    out: list[dict] = []
    for group in cols:
        x0 = min(t["x0"] for t in group)
        x1 = max(t["x1"] for t in group)
        out.append({"x0": x0, "x1": x1, "xc": (x0 + x1) / 2, "text": " ".join(t["text"] for t in group)})
    return out


def _find_word_header_columns(lines: list[list[dict]]) -> list[dict]:
    """First clustered line carrying ≥2 period columns → its column cells."""
    for line in lines:
        cols = _cluster_columns(line)
        if sum(1 for c in cols if _is_period_header(c["text"])) >= 2:
            return cols
    return []


def _nearest_header_col(token: dict, header_cols: list[dict], tol: float = 25.0) -> int | None:
    """Index of the header column whose x-range best fits ``token`` (or None)."""
    xc = (token["x0"] + token["x1"]) / 2
    best_idx: int | None = None
    best_dist = tol
    for idx, col in enumerate(header_cols):
        if col["x0"] - tol <= xc <= col["x1"] + tol:
            dist = abs(xc - col["xc"])
            if dist <= best_dist:
                best_dist = dist
                best_idx = idx
    return best_idx


def _cluster_lines(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        for line in lines:
            if abs(line[0]["top"] - w["top"]) <= _LINE_TOL:
                line.append(w)
                break
        else:
            lines.append([w])
    return lines


def _blocks_from_words(page) -> list[TableBlock]:
    """Word-cluster fallback that keeps each numeric cell's header column.

    The (label, ordered numeric values) split is identical to ``_rows_from_words``;
    we additionally align each numeric token to a detected period-header column by
    x-position. Tokens that don't fall under a header column get a private index
    that no header maps to, so selection cleanly ignores them (and the positional
    fallback still sees every value).
    """
    try:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
    except Exception:
        return []
    lines = _cluster_lines(words)
    header_cols = _find_word_header_columns(lines)

    rows: list[BlockRow] = []
    unmatched = -1
    for line in lines:
        toks = sorted(line, key=lambda t: t["x0"])
        i = 0
        while i < len(toks) and not _looks_numeric(toks[i]["text"]):
            i += 1
        label = " ".join(t["text"] for t in toks[:i]).strip()
        num_toks = [t for t in toks[i:] if _looks_numeric(t["text"])]
        if not (label and num_toks):
            continue
        cell_pairs: list[tuple[int, str]] = []
        for tok in num_toks:
            col = _nearest_header_col(tok, header_cols) if header_cols else None
            if col is None:
                col = unmatched
                unmatched -= 1
            cell_pairs.append((col, tok["text"]))
        rows.append((label, cell_pairs))
    if not rows:
        return []
    header_by_col = {idx: col["text"] for idx, col in enumerate(header_cols)}
    return [TableBlock(header_by_col=header_by_col, rows=rows)]


# ---------------------------------------------------------------------------
# Matching: candidate rows → metrics  (pure, PDF-free — directly unit-testable)
# ---------------------------------------------------------------------------

MONETARY_UNITS = {"currency", "miles_mxn"}


def _snap_scale(ratio: float, candidates: list[float]) -> float:
    """Nearest candidate scale to ``ratio`` in log space (orders of magnitude)."""
    import math

    return min(candidates, key=lambda c: abs(math.log10(ratio) - math.log10(c)))


def resolve_table_scale(
    raw_rows: dict,
    xbrl_found: dict,
    cfg: dict | None,
    metric_defs: list[MetricDef],
    doc=None,
) -> float:
    """Infer the Tier-2 monetary ``table_scale`` when config doesn't set one.

    Generalization lever: a new company prints its tables in pesos / thousands /
    millions, and historically that required a hand-set ``table_scale`` per
    company. This recovers it automatically so an unseen filer scales correctly
    with zero config. The decision is deliberately conservative — it only returns
    a NON-1.0 scale on strong, agreeing evidence; otherwise it returns 1.0
    (today's default), so companies that already work at 1.0 (walmex, sport) are
    unaffected.

    Signals, in priority order:
      1. **XBRL cross-check (primary, unit-agnostic).** For any monetary metric
         present BOTH as a raw table cell (read at scale 1.0) and as an
         XBRL-sourced value in ``xbrl_found`` (already in the metric's stored
         unit), ``xbrl_value / raw_value`` is the true scale; snap it to the
         company's candidate set. Requires ≥2 metrics agreeing on the same
         non-1.0 scale (one metric can be a YTD/row fluke).
      2. **Caption.** If no XBRL overlap, an explicit table caption
         ("miles"/"millones") via ``doc.scale`` gives the printed unit; translate
         to the stored unit with ``pesos_per_unit_for``.
      3. **Default 1.0.** No positive evidence → today's behavior.

    Note: ``raw_rows`` are read at scale 1.0, so a returned scale ``s`` means the
    caller should re-read (or multiply) monetary cells by ``s``.
    """
    if cfg is not None and "table_scale" in cfg:
        return float(cfg["table_scale"])
    import os
    if os.environ.get("AUTO_SCALE") == "0":  # escape hatch: pre-auto-scale behavior
        return 1.0

    from src.extract.xbrl_facts import pesos_per_unit_for

    ppu = pesos_per_unit_for(cfg)
    # Candidate scales = printed-unit pesos / stored-unit pesos, for the three
    # common printed units {pesos, thousands, millions}. For a millions-stored
    # company that's {1e-6, 1e-3, 1}; for a miles_mxn company {1e-3, 1, 1e3}.
    candidates = sorted({p / ppu for p in (1.0, 1e3, 1e6)})
    unit_by_key = {m.key: m.unit for m in metric_defs}

    # --- Signal 1: XBRL cross-check -----------------------------------------
    votes: dict[float, int] = {}
    for key, raw_row in raw_rows.items():
        if unit_by_key.get(key) not in MONETARY_UNITS:
            continue
        xrow = xbrl_found.get(key)
        if xrow is None or "[xbrl]" not in (getattr(xrow, "source_line", "") or ""):
            continue
        raw_val = getattr(raw_row, "current", None)
        xbrl_val = getattr(xrow, "current", None)
        if not raw_val or abs(raw_val) <= 0.5 or not xbrl_val:
            continue
        snapped = _snap_scale(abs(xbrl_val) / abs(raw_val), candidates)
        votes[snapped] = votes.get(snapped, 0) + 1
    if votes:
        best = max(votes, key=lambda s: (votes[s], -abs(s - 1.0)))
        if best == 1.0:
            return 1.0
        if votes[best] >= 2:  # strong agreement required for a non-default scale
            return best

    # --- Signal 2: explicit caption -----------------------------------------
    if doc is not None and getattr(doc, "scale_source", None) == "header":
        printed_pesos = float(getattr(doc, "scale", 1.0)) * 1e6  # millions-relative
        cand = printed_pesos / ppu
        # only trust it if it lands on a real candidate
        if any(abs(cand - c) < c * 1e-6 for c in candidates):
            return cand

    return 1.0


def match_metrics_from_rows(
    candidates: list[tuple[str, list[str]]],
    metric_defs: list[MetricDef],
    *,
    table_scale: float = 1.0,
    skip_keys: frozenset = frozenset(),
) -> dict:
    """Match (label, numeric_cells) rows to metrics by fuzzy label match.

    First numeric cell after a matched row label = current, second = prior
    (mirrors extract_metrics._build_row's convention).

    table_scale: multiplier applied to monetary cells so a literal table number
        lands in the metric's expected unit (e.g. lacomer prints full pesos but
        the registry/actuales use millions → table_scale=1e-6). The regex layer
        encodes this per-pattern via PatternSpec.multiplier; Tier 2 reads raw
        cells, so it needs the per-company scale supplied here.
    skip_keys: metric keys to NOT attempt (e.g. metrics that have a section-aware
        regex path — trust the tuned regex rather than a flat cell grab).
    """
    from src.extract.extract_metrics import MetricRow, parse_number   # local: avoid import cycle

    from src.extract.semantic_search import SemanticMatcher

    matcher = SemanticMatcher(metric_defs, skip_keys=skip_keys)
    defs_by_key = {m.key: m for m in metric_defs}
    found: dict = {}
    match_cache: dict[str, object] = {}
    for raw_lbl, cells in candidates:
        if raw_lbl in match_cache:
            match = match_cache[raw_lbl]
        else:
            match = matcher.best_match(raw_lbl, threshold=_MATCH_THRESHOLD)
            match_cache[raw_lbl] = match
        if match is None or match.metric_key in found:
            continue
        mdef = defs_by_key[match.metric_key]
        if mdef.unit in MONETARY_UNITS and (
            "%" in raw_lbl or any("%" in cell for cell in cells[:3])
        ):
            continue
        vals = [parse_number(c) for c in cells]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        if table_scale != 1.0 and mdef.unit in MONETARY_UNITS:
            vals = [v * table_scale for v in vals]
        found[match.metric_key] = MetricRow(
            metric=match.metric_key,
            label_es=mdef.label_es,
            current=vals[0],
            prior=vals[1] if len(vals) >= 2 else None,
            var_pct=None,
            unit=mdef.unit,
            source_line=f"[table] {raw_lbl} {' '.join(cells[:3])}".strip()[:100],
        )
    return found


def match_metrics_from_blocks(
    blocks: list[TableBlock],
    metric_defs: list[MetricDef],
    *,
    table_scale: float = 1.0,
    skip_keys: frozenset = frozenset(),
    period: str | None = None,
) -> dict:
    """Header-aware variant of :func:`match_metrics_from_rows`.

    When ``period`` is given and a row's table has a parseable period header, the
    value is taken from the column matching the target quarter (prior = same
    quarter, prior year). Otherwise — no period, no header, or no column match —
    it falls back to the legacy positional rule (first numeric = current, second
    = prior), so behavior is unchanged for tables without period headers.
    """
    from src.extract.extract_metrics import MetricRow, parse_number   # local: avoid import cycle

    from src.extract.semantic_search import SemanticMatcher, consolidation_rank

    matcher = SemanticMatcher(metric_defs, skip_keys=skip_keys)
    defs_by_key = {m.key: m for m in metric_defs}
    target = normalize_target_period(period)
    # When several rows map to one metric, prefer the most-consolidated label
    # (generic tiebreak). `chosen_rank` tracks the winning row's rank per key:
    # a later row only displaces the incumbent if its rank is STRICTLY higher, so
    # equal-rank ties keep first-match-wins (identical to prior behavior). With
    # CONSOLIDATE=0 every rank is 0 → pure first-match-wins.
    _rank_on = os.environ.get("CONSOLIDATE") != "0"
    chosen_rank: dict = {}
    found: dict = {}
    match_cache: dict[str, object] = {}
    for block in blocks:
        for raw_lbl, cell_pairs in block.rows:
            if raw_lbl in match_cache:
                match = match_cache[raw_lbl]
            else:
                match = matcher.best_match(raw_lbl, threshold=_MATCH_THRESHOLD)
                match_cache[raw_lbl] = match
            if match is None:
                continue
            rank = consolidation_rank(raw_lbl) if _rank_on else 0
            if match.metric_key in found and chosen_rank.get(match.metric_key, 0) >= rank:
                continue
            mdef = defs_by_key[match.metric_key]
            cell_values = [c for _, c in cell_pairs]
            if mdef.unit in MONETARY_UNITS and (
                "%" in raw_lbl or any("%" in c for c in cell_values[:3])
            ):
                continue

            current = prior = None
            if target is not None and block.header_by_col:
                cur_str, prior_str = select_value_for_period(block.header_by_col, cell_pairs, target)
                if cur_str is not None:
                    current = parse_number(cur_str)
                    prior = parse_number(prior_str) if prior_str is not None else None
            if current is None:   # no period match (or unparseable) → positional fallback
                nums = [v for v in (parse_number(c) for c in cell_values) if v is not None]
                if not nums:
                    continue
                current = nums[0]
                prior = nums[1] if len(nums) >= 2 else None

            if table_scale != 1.0 and mdef.unit in MONETARY_UNITS:
                current = current * table_scale
                prior = prior * table_scale if prior is not None else None
            found[match.metric_key] = MetricRow(
                metric=match.metric_key,
                label_es=mdef.label_es,
                current=current,
                prior=prior,
                var_pct=None,
                unit=mdef.unit,
                source_line=f"[table] {raw_lbl} {' '.join(cell_values[:3])}".strip()[:100],
            )
            chosen_rank[match.metric_key] = rank
    return found


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _parse_blocks_uncached(pdf_path: Path) -> list[TableBlock]:
    """Open a PDF and extract its table blocks (the expensive pdfplumber step).

    Returns [] on any open/parse failure (matches the prior inline behavior).
    """
    try:
        import pdfplumber
    except ImportError:
        print("parse_tables: pdfplumber not available", file=sys.stderr)
        return []
    blocks: list[TableBlock] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                blocks.extend(_blocks_from_extract_tables(page))
                blocks.extend(_blocks_from_words(page))
    except Exception as exc:
        print(f"parse_tables: failed to open {pdf_path.name}: {exc}", file=sys.stderr)
        return []
    return blocks


def _blocks_for_pdf(pdf_path: Path) -> list[TableBlock]:
    """Cached wrapper around _parse_blocks_uncached (keyed on file identity).

    The cache stores only parsed structure, so callers see identical blocks
    whether or not it hit; it just skips re-running pdfplumber. Fails open.
    """
    from src.parse.parse_cache import cached_blocks

    return cached_blocks(pdf_path, lambda: _parse_blocks_uncached(pdf_path))


def extract_from_tables(
    pdf_path,
    metric_defs: list[MetricDef],
    *,
    table_scale: float = 1.0,
    cfg: dict | None = None,
    period: str | None = None,
) -> dict:
    """Tier 2: {metric_key: MetricRow} for metrics found as table rows in a PDF.

    table_scale: per-company monetary scale (see match_metrics_from_rows).
    cfg: company config; if it defines `sections`, the metrics listed there have a
        section-aware regex path, so Tier 2 skips them (a flat cell grab can't
        disambiguate the consolidated row from a per-segment row).
    period: the document's period label (e.g. "2026-1T" or "1Q16A"). When given,
        values are selected by matching column headers to the target quarter;
        when omitted or unparseable, the legacy positional rule applies.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        return {}

    # Metrics to NOT attempt in Tier 2: those owned by a section-aware regex path,
    # plus any the company config lists under `tier2_skip` (prose-only metrics where
    # a flat cell grab only mis-fires — e.g. lacomer total_stores, where a prose
    # sentence "tiendas: 6 City Market, 6 Freskos y 11 ..." reads as a bogus row).
    skip_keys = frozenset(
        [k for s in (cfg or {}).get("sections", []) for k in s.get("metrics", [])]
        + list((cfg or {}).get("tier2_skip", []))
    )

    blocks = _blocks_for_pdf(pdf_path)

    return match_metrics_from_blocks(
        blocks, metric_defs, table_scale=table_scale, skip_keys=skip_keys, period=period
    )
