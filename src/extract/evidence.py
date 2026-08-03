"""Evidence-backed generic extraction.

This module adds an opt-in extraction path that treats every source as a
candidate generator. The resolver accepts only source-backed values above a
confidence threshold and returns diagnostics for everything else.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from src.extract.extract_metrics import MetricRow, extract_metrics, extract_metrics_segmented
from src.model.financial_model import MetricDef, compute_derived_metrics


@dataclass
class DocumentTable:
    page: int
    rows: list[tuple[str, list[str]]]
    source: str = "pdfplumber"
    # Header-aware extras (schema 2). Empty for hand-built / legacy-cache tables,
    # which then fall back to positional value selection.
    header_by_col: dict[int, str] = field(default_factory=dict)
    block_rows: list = field(default_factory=list)  # [(label, [(col_index, cell_str)])]


@dataclass
class DocumentModel:
    period: str
    text: str = ""
    pdf_path: str | None = None
    tables: list[DocumentTable] = field(default_factory=list)
    scale: float = 1.0
    scale_source: str = "default"


@dataclass
class MetricRequest:
    key: str
    label: str
    concept: str
    unit: str
    aliases: list[str] = field(default_factory=list)
    segment: str | None = None
    derived: bool = False


@dataclass
class CandidateValue:
    metric: str
    value: float | None
    prior: float | None
    var_pct: float | None
    unit: str
    source: str
    evidence: str
    confidence: float
    page: int | None = None
    reason: str = ""


@dataclass
class ExtractionResult:
    period: str
    accepted: dict[str, MetricRow]
    candidates: list[CandidateValue]
    diagnostics: list[dict]

    def wide_row(self) -> dict:
        row = {"period": self.period}
        for key, metric in self.accepted.items():
            row[key] = metric.current
        return row

    def candidates_rows(self) -> list[dict]:
        return [asdict(c) | {"period": self.period} for c in self.candidates]


_CANONICAL_CONCEPTS = [
    ("fx_effect", r"\b(?:fx|foreign exchange|tipo de cambio|conversion|conversi[oó]n)\b"),
    ("gross_profit", r"\b(?:gross profit|utilidad bruta)\b"),
    ("operating_income", r"\b(?:operating income|operating profit|ebit|utilidad de operaci[oó]n|uafir)\b"),
    ("ebitda", r"\b(?:ebitda|uafida|uafirda)\b"),
    ("volume", r"\b(?:volume|volumen|tons?|toneladas?)\b"),
    ("net_sales", r"\b(?:net sales|ventas netas|total sales|revenue|ingresos)\b"),
]

_DERIVED_LABEL_RE = re.compile(
    r"\b(?:yoy|margin|margen|bps|average price|precio promedio|check|as %|como %)\b",
    re.IGNORECASE,
)


def metric_requests_from_defs(metric_defs: list[MetricDef]) -> list[MetricRequest]:
    requests: list[MetricRequest] = []
    for mdef in metric_defs:
        aliases = [mdef.label, mdef.label_es, *mdef.aliases]
        requests.append(MetricRequest(
            key=mdef.key,
            label=mdef.label,
            concept=_concept_for_key(mdef.key, mdef.label),
            unit=mdef.unit,
            aliases=[a for a in aliases if a],
            segment=_segment_for_key(mdef.key),
            derived=bool(mdef.calc and not mdef.patterns),
        ))
    return requests


def metric_requests_from_labels(labels: Iterable[str]) -> list[MetricRequest]:
    """Best-effort parser for arbitrary user outline labels."""
    requests: list[MetricRequest] = []
    for label in labels:
        clean = label.strip()
        if not clean:
            continue
        concept = _concept_for_label(clean)
        key = _slug_key(clean)
        requests.append(MetricRequest(
            key=key,
            label=clean,
            concept=concept,
            unit="pct" if "margin" in clean.lower() or "%" in clean else "currency",
            aliases=[clean],
            segment=_segment_from_label(clean, concept),
            derived=bool(_DERIVED_LABEL_RE.search(clean)),
        ))
    return requests


def metric_defs_from_labels(labels: Iterable[str]) -> list[MetricDef]:
    """Create lightweight metric definitions for an arbitrary user metric list.

    These defs are intended for the evidence extractor, not the legacy regex
    engine. The original label becomes the primary table alias.
    """
    defs: list[MetricDef] = []
    seen: set[str] = set()
    for request in metric_requests_from_labels(labels):
        if request.derived:
            continue
        key = request.key
        if key in seen:
            key = f"{key}_{len(seen) + 1}"
        seen.add(key)
        defs.append(MetricDef(
            key=key,
            label=request.label,
            label_es=request.label,
            section="kpi" if request.concept in {"volume", "fx_effect"} else "income",
            unit=request.unit,
            patterns=[],
            aliases=request.aliases,
        ))
    return defs


def build_document_model(src, *, cache_dir: Path | None = None) -> DocumentModel:
    """Create a reusable document model from a PeriodSource-like object."""
    period = getattr(src, "period", "")
    text = getattr(src, "text", "") or ""
    pdf_path = getattr(src, "pdf_path", None)
    doc = getattr(src, "doc", None)
    tables: list[DocumentTable] = []

    if pdf_path:
        tables = _tables_from_pdf_cached(Path(pdf_path), cache_dir=cache_dir)

    return DocumentModel(
        period=period,
        text=text,
        pdf_path=str(pdf_path) if pdf_path else None,
        tables=tables,
        scale=float(getattr(doc, "scale", 1.0) or 1.0),
        scale_source=getattr(doc, "scale_source", "default") if doc else "default",
    )


def extract_with_evidence(
    src,
    metric_defs: list[MetricDef],
    cfg: dict | None = None,
    *,
    metric_keys: list[str] | None = None,
    confidence_threshold: float = 0.75,
    use_llm_labeling: bool = False,
    cache_dir: Path | None = None,
) -> ExtractionResult:
    """Extract one period with accepted values, candidates, and diagnostics."""
    cfg = cfg or {}
    wanted = set(metric_keys or [m.key for m in metric_defs])
    defs = [m for m in metric_defs if m.key in wanted]
    requests = [r for r in metric_requests_from_defs(defs) if not r.derived]
    doc = build_document_model(src, cache_dir=cache_dir)

    candidates: list[CandidateValue] = []
    candidates.extend(_xbrl_candidates(src, defs, cfg))
    candidates.extend(_custom_candidates(src, defs, cfg))
    candidates.extend(_regex_candidates(src, defs, cfg))
    candidates.extend(_table_candidates(doc, requests, defs, cfg))
    if use_llm_labeling:
        candidates.extend(_llm_candidates(src, defs, {c.metric for c in candidates}, cfg))

    accepted, diagnostics = _resolve_candidates(
        doc.period, defs, candidates, confidence_threshold=confidence_threshold
    )

    for key, row in compute_derived_metrics(accepted, metric_defs).items():
        if key in wanted:
            accepted.setdefault(key, row)

    return ExtractionResult(
        period=doc.period,
        accepted={k: v for k, v in accepted.items() if k in wanted},
        candidates=candidates,
        diagnostics=diagnostics,
    )


def write_diagnostics(path: Path, results: list[ExtractionResult]) -> None:
    payload = {
        "periods": [
            {
                "period": result.period,
                "diagnostics": result.diagnostics,
            }
            for result in results
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_candidates_csv(path: Path, results: list[ExtractionResult]) -> None:
    import pandas as pd

    rows: list[dict] = []
    for result in results:
        rows.extend(result.candidates_rows())
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def _resolve_candidates(
    period: str,
    metric_defs: list[MetricDef],
    candidates: list[CandidateValue],
    *,
    confidence_threshold: float,
) -> tuple[dict[str, MetricRow], list[dict]]:
    by_key: dict[str, list[CandidateValue]] = {}
    for candidate in candidates:
        if candidate.value is None:
            continue
        by_key.setdefault(candidate.metric, []).append(candidate)

    defs = {m.key: m for m in metric_defs}
    accepted: dict[str, MetricRow] = {}
    diagnostics: list[dict] = []
    for mdef in metric_defs:
        ranked = sorted(by_key.get(mdef.key, []), key=lambda c: c.confidence, reverse=True)
        if not ranked:
            diagnostics.append({
                "period": period,
                "metric": mdef.key,
                "status": "missing",
                "message": "No source-backed candidate found.",
            })
            continue
        best = ranked[0]
        if best.confidence < confidence_threshold:
            diagnostics.append({
                "period": period,
                "metric": mdef.key,
                "status": "low_confidence",
                "best_confidence": best.confidence,
                "evidence": best.evidence,
            })
            continue
        accepted[mdef.key] = MetricRow(
            metric=mdef.key,
            label_es=defs[mdef.key].label_es,
            current=best.value,
            prior=best.prior,
            var_pct=best.var_pct,
            unit=best.unit,
            source_line=f"[{best.source}] {best.evidence}"[:120],
        )
        if len(ranked) > 1 and ranked[1].confidence >= confidence_threshold:
            diagnostics.append({
                "period": period,
                "metric": mdef.key,
                "status": "accepted_with_alternatives",
                "accepted_confidence": best.confidence,
                "alternative_confidence": ranked[1].confidence,
            })
    return accepted, diagnostics


def _xbrl_candidates(src, metric_defs: list[MetricDef], cfg: dict) -> list[CandidateValue]:
    facts = getattr(src, "facts", None)
    if not facts:
        return []
    try:
        from src.extract.tiered_extract import period_end_from_label
        from src.extract.xbrl_facts import (
            extract_from_xbrl,
            fact_value_divisor_for,
            iso_currency_for,
        )
        period_end = getattr(src, "period_end", None) or period_end_from_label(getattr(src, "period", None))
        currency_mode = (((cfg or {}).get("xbrl") or {}).get("currency_mode") or "native")
        expected_currency = None if currency_mode == "convert_to_mxn" else iso_currency_for(cfg)
        rows = extract_from_xbrl(
            facts,
            metric_defs,
            period_end,
            fact_value_divisor_for(cfg, facts, currency_mode=currency_mode),
            expected_currency=expected_currency,
        )
    except Exception as exc:
        print(f"evidence: xbrl candidates failed: {exc}", file=sys.stderr)
        return []
    return [_candidate_from_row(key, row, source="xbrl", confidence=0.95)
            for key, row in rows.items()]


def _custom_candidates(src, metric_defs: list[MetricDef], cfg: dict) -> list[CandidateValue]:
    """Candidates from the company's custom extractor (any registry entry).

    Dispatches through tiered_extract._CUSTOM_EXTRACTORS so the evidence path
    covers every custom extractor — previously only gruma was wired here and
    ``--evidence`` silently lost the other companies' deterministic tiers.
    """
    custom = cfg.get("custom_extractor")
    if not getattr(src, "text", None) or not custom:
        return []
    from src.extract.tiered_extract import _CUSTOM_EXTRACTORS
    spec = _CUSTOM_EXTRACTORS.get(custom)
    if spec is None:
        print(f"evidence: unknown custom_extractor '{custom}'", file=sys.stderr)
        return []
    mod_path, fn_name, wants_period, wants_pdf = spec
    try:
        import importlib
        fn = getattr(importlib.import_module(mod_path), fn_name)
        args = [src.text, metric_defs]
        if wants_period:
            args.append(getattr(src, "period", None))
        kwargs = {"pdf_path": getattr(src, "pdf_path", None)} if wants_pdf else {}
        rows = fn(*args, **kwargs)
    except Exception as exc:
        print(f"evidence: custom extractor '{custom}' failed: {exc}", file=sys.stderr)
        return []
    return [_candidate_from_row(key, row, source="custom_table", confidence=0.90)
            for key, row in rows.items()]


def _regex_candidates(src, metric_defs: list[MetricDef], cfg: dict) -> list[CandidateValue]:
    text = getattr(src, "text", "") or ""
    if not text:
        return []
    rows = extract_metrics_segmented(text, metric_defs, cfg) if cfg.get("sections") else extract_metrics(text, metric_defs)
    out: list[CandidateValue] = []
    for key, row in rows.items():
        source = _source_tag(row.source_line)
        confidence = {"table": 0.78, "prose": 0.55, "calc": 0.90}.get(source, 0.60)
        out.append(_candidate_from_row(key, row, source=source, confidence=confidence))
    return out


def _table_candidates(
    doc: DocumentModel,
    requests: list[MetricRequest],
    metric_defs: list[MetricDef],
    cfg: dict,
) -> list[CandidateValue]:
    from src.extract.extract_metrics import parse_number
    from src.extract.semantic_search import SemanticMatcher
    from src.extract.table_periods import normalize_target_period, select_value_for_period

    defs = {m.key: m for m in metric_defs}
    request_keys = {request.key for request in requests}
    request_defs = [m for m in metric_defs if m.key in request_keys]
    matcher = SemanticMatcher(request_defs)
    table_scale = float(cfg.get("table_scale", 1.0) or 1.0)
    target = normalize_target_period(getattr(doc, "period", None))
    monetary = {"currency", "miles_mxn"}
    out: list[CandidateValue] = []
    for table in doc.tables:
        # Prefer column-indexed rows (enables period selection); legacy/hand-built
        # tables without block_rows use sequential indices and an empty header,
        # which falls through to positional selection.
        indexed_rows = table.block_rows or [
            (label, list(enumerate(cells))) for label, cells in table.rows
        ]
        for raw_label, cell_pairs in indexed_rows:
            match = matcher.best_match(raw_label, threshold=0.85)
            if match is None:
                continue
            cells = [c for _, c in cell_pairs]
            if defs[match.metric_key].unit in monetary and (
                "%" in raw_label or any("%" in cell for cell in cells[:3])
            ):
                continue
            value = prior = None
            if target is not None and table.header_by_col:
                cur_str, prior_str = select_value_for_period(table.header_by_col, cell_pairs, target)
                if cur_str is not None:
                    value = parse_number(cur_str)
                    prior = parse_number(prior_str) if prior_str is not None else None
            if value is None:   # no period match → positional fallback
                vals = [v for v in (parse_number(cell) for cell in cells) if v is not None]
                if not vals:
                    continue
                value = vals[0]
                prior = vals[1] if len(vals) >= 2 else None
            if defs[match.metric_key].unit in monetary and table_scale != 1.0:
                value *= table_scale
                prior = prior * table_scale if prior is not None else None
            out.append(CandidateValue(
                metric=match.metric_key,
                value=value,
                prior=prior,
                var_pct=None,
                unit=defs[match.metric_key].unit,
                source="table_cell",
                evidence=f"{raw_label} {' '.join(cells[:4])}".strip()[:160],
                confidence=min(0.95, 0.72 + match.score * 0.20),
                page=table.page,
                reason=match.reason,
            ))
    return out


def _llm_candidates(src, metric_defs: list[MetricDef], already_seen: set[str],
                    cfg: dict | None = None) -> list[CandidateValue]:
    text = getattr(src, "text", "") or ""
    missing = [m for m in metric_defs if m.key not in already_seen and m.patterns]
    if not missing:
        return []
    try:
        from src.extract.llm_extract import llm_extract
        rows = llm_extract(missing, text, {}, cfg=cfg)
    except Exception as exc:
        print(f"evidence: llm candidates failed: {exc}", file=sys.stderr)
        return []
    return [_candidate_from_row(key, row, source="llm", confidence=0.50)
            for key, row in rows.items()]


def _candidate_from_row(
    key: str,
    row: MetricRow,
    *,
    source: str,
    confidence: float,
) -> CandidateValue:
    return CandidateValue(
        metric=key,
        value=row.current,
        prior=row.prior,
        var_pct=row.var_pct,
        unit=row.unit,
        source=source,
        evidence=(row.source_line or "")[:160],
        confidence=confidence,
    )


def _tables_from_pdf_cached(pdf_path: Path, *, cache_dir: Path | None) -> list[DocumentTable]:
    if cache_dir is None:
        cache_dir = Path(".parse_cache")
    try:
        stat = pdf_path.stat()
        # ":v2" bumps the cache key so legacy (header-less) entries are bypassed.
        cache_key = hashlib.sha256(
            f"{pdf_path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}:v2".encode()
        ).hexdigest()
        cache_path = cache_dir / f"{cache_key}.tables.json"
        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return [
                DocumentTable(
                    page=item["page"],
                    rows=[tuple(row) for row in item["rows"]],
                    source=item.get("source", "pdfplumber"),
                    # JSON object keys are strings → restore int column indices.
                    header_by_col={int(k): v for k, v in item.get("header_by_col", {}).items()},
                    block_rows=[(label, [tuple(p) for p in pairs])
                                for label, pairs in item.get("block_rows", [])],
                )
                for item in data
            ]
    except Exception:
        cache_path = None

    tables: list[DocumentTable] = []
    try:
        import pdfplumber
        from src.extract.parse_tables import _blocks_from_extract_tables, _blocks_from_words
        with pdfplumber.open(pdf_path) as pdf:
            for index, page in enumerate(pdf.pages, start=1):
                for block in _blocks_from_extract_tables(page) + _blocks_from_words(page):
                    flat = [(label, [c for _, c in pairs]) for label, pairs in block.rows]
                    if flat:
                        tables.append(DocumentTable(
                            page=index,
                            rows=flat,
                            header_by_col=block.header_by_col,
                            block_rows=block.rows,
                        ))
    except Exception as exc:
        print(f"evidence: table parse failed: {exc}", file=sys.stderr)
        return []

    if cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps([asdict(t) for t in tables], ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return tables


def _source_tag(source_line: str) -> str:
    match = re.match(r"\[([^\]]+)\]", source_line or "")
    return (match.group(1) if match else "regex").replace("gruma_table", "custom_table")


def _concept_for_key(key: str, label: str = "") -> str:
    if key == "revenue" or key.startswith("net_sales") or key.startswith("revenue"):
        return "net_sales"
    if key.startswith("volume"):
        return "volume"
    for concept in ("gross_profit", "operating_income", "ebitda"):
        if key == concept or key.startswith(f"{concept}_"):
            return concept
    return _concept_for_label(label or key)


def _concept_for_label(label: str) -> str:
    folded = _fold(label)
    for concept, pattern in _CANONICAL_CONCEPTS:
        if re.search(pattern, folded, re.IGNORECASE):
            return concept
    return _slug_key(label)


def _segment_for_key(key: str) -> str | None:
    for prefix in ("net_sales_", "revenue_", "volume_", "gross_profit_", "operating_income_", "ebitda_"):
        if key.startswith(prefix):
            return key[len(prefix):]
    return None


def _segment_from_label(label: str, concept: str) -> str | None:
    folded = _fold(label)
    folded = re.sub(_CANONICAL_PATTERN_FOR(concept), "", folded, flags=re.IGNORECASE).strip()
    folded = re.sub(r"\b(?:net|sales|total|as|of|consolidated|margin|volume|thousand|tons?)\b", "", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    return folded or None


def _CANONICAL_PATTERN_FOR(concept: str) -> str:
    for name, pattern in _CANONICAL_CONCEPTS:
        if name == concept:
            return pattern
    return re.escape(concept)


def _slug_key(label: str) -> str:
    folded = _fold(label)
    folded = re.sub(r"[^a-z0-9]+", "_", folded).strip("_")
    return folded or "metric"


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in normalized if not unicodedata.combining(c))
    return ascii_text.lower()
