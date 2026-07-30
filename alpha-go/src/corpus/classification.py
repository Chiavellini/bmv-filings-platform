"""Content-first classification for newly uploaded financial documents.

The classifier is deliberately corpus-agnostic: it scores evidence in the extracted document
before considering the filename.  It does not train on the installed company corpus and it never
mutates a vocabulary after seeing a document.  The UI may still ask the user to confirm low
confidence fields, but a misleading filename is no longer the primary source of truth.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from src.corpus.doc_types import Taxonomy
from src.shared.report_index import infer_period_label

_WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", re.UNICODE)
_ES = frozenset({
    "de", "del", "la", "las", "el", "los", "en", "para", "por", "con", "que", "una",
    "un", "al", "se", "sus", "resultados", "trimestre", "ingresos", "utilidad", "millones",
    "financiera", "financiero", "cambio",
})
_EN = frozenset({
    "the", "of", "and", "in", "for", "to", "with", "that", "a", "an", "our", "results",
    "quarter", "revenue", "income", "million", "financial", "exchange",
})
_COMPANY_SUFFIX = re.compile(
    r"\b(?:s\.?\s*a\.?\s*b?\.?\s*de\s*c\.?\s*v\.?|inc\.?|corp(?:oration)?|plc|ltd\.?|"
    r"grupo|group|banco|bank|holdings?)\b",
    re.I,
)
_GENERIC_HEADINGS = {
    "annual report", "quarterly report", "financial information", "informacion financiera",
    "reporte anual", "informe anual", "resultados trimestrales", "press release",
}


def _norm(value: str) -> str:
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", value or "") if not unicodedata.combining(c)
    )
    return " ".join(re.findall(r"[a-z0-9]+", folded.lower()))


@dataclass(frozen=True)
class ClassificationSignal:
    value: str | None
    confidence: float
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DocumentClassification:
    language: ClassificationSignal
    company: ClassificationSignal
    companies: tuple[ClassificationSignal, ...]
    doc_type: ClassificationSignal
    period: ClassificationSignal
    suggested_company_name: str | None = None
    suggested_title: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def detect_language(text: str) -> ClassificationSignal:
    """Detect English/Spanish from content-word evidence; return ``unknown`` when ambiguous."""
    words = [_norm(w) for w in _WORD.findall((text or "")[:80_000])]
    es = sum(w in _ES for w in words)
    en = sum(w in _EN for w in words)
    total = es + en
    if total < 3:
        return ClassificationSignal("unknown", 0.0, ("insufficient language evidence",))
    winner, loser, label = (es, en, "es") if es >= en else (en, es, "en")
    confidence = min(0.99, 0.5 + (winner - loser) / max(2 * total, 1))
    return ClassificationSignal(
        label, round(confidence, 3), (f"content tokens: es={es}, en={en}",)
    )


def _company_display(company) -> tuple[str, str]:
    if isinstance(company, dict):
        slug = str(company.get("slug") or company.get("company") or "")
        display = str(company.get("company") or company.get("name") or slug)
        return slug, display
    return str(company), str(company)


def classify_companies(
    filename: str, text: str, companies: Iterable[str | dict],
) -> tuple[ClassificationSignal, ...]:
    """Resolve every materially covered known company, best-supported first.

    A single-company filing normally has one name repeated throughout.  A broker/sector report
    can cover several names with comparable frequency; returning only the top name silently makes
    that document disappear from the other companies' scoped searches.  Candidates therefore
    need at least three content mentions and at least 25% of the leading company's support before
    they are auto-selected.  A filename match can break a weak single-company tie, but can never
    manufacture extra memberships on its own.
    """
    body = _norm((text or "")[:120_000])
    early_body = body[:30_000]
    file_text = _norm(Path(filename).stem)
    scored: list[tuple[float, int, str, list[str]]] = []
    for raw in companies:
        slug, display = _company_display(raw)
        names = {_norm(slug.replace("_", " ")), _norm(display)}
        names.discard("")
        content_hits = sum(
            len(re.findall(rf"(?:^|\s){re.escape(name)}(?:\s|$)", body)) for name in names
        )
        early_hits = sum(
            len(re.findall(rf"(?:^|\s){re.escape(name)}(?:\s|$)", early_body))
            for name in names
        )
        filename_hit = any(
            re.search(rf"(?:^|\s){re.escape(name)}(?:\s|$)", file_text) for name in names
        )
        score = min(content_hits, 20) * 3.0 + min(early_hits, 10) + (
            1.0 if filename_hit else 0.0
        )
        if score:
            evidence = []
            if content_hits:
                evidence.append(f"{content_hits} content mention(s)")
            if filename_hit:
                evidence.append("filename mention")
            scored.append((score, content_hits, slug, evidence))
    if not scored:
        return ()
    scored.sort(key=lambda item: (-item[0], item[2]))
    best_score, best_hits = scored[0][0], scored[0][1]
    materially_covered = [
        item for item in scored
        if item[1] >= 3 and item[1] >= max(3, best_hits * 0.25)
    ]
    selected = materially_covered or [scored[0]]
    signals = []
    for score, _hits, slug, evidence in selected:
        confidence = min(0.99, 0.55 + 0.44 * score / max(best_score, 1))
        signals.append(ClassificationSignal(slug, round(confidence, 3), tuple(evidence)))
    return tuple(signals)


def classify_company(
    filename: str, text: str, companies: Iterable[str | dict],
) -> ClassificationSignal:
    """Backward-compatible best known company signal."""
    matches = classify_companies(filename, text, companies)
    return matches[0] if matches else ClassificationSignal(
        None, 0.0, ("no known company identified",)
    )


def suggest_new_company_name(text: str) -> str | None:
    """Best content-header candidate for a company not present in the known-company list."""
    for raw in (text or "").splitlines()[:80]:
        line = " ".join(raw.strip(" #*|\t").split())
        normalized = _norm(line)
        if not 4 <= len(line) <= 100 or normalized in _GENERIC_HEADINGS:
            continue
        if _COMPANY_SUFFIX.search(line):
            return line
    return None


def suggest_document_title(filename: str, text: str) -> str | None:
    """Pick the first human heading, ignoring parser page markers and the raw filename."""
    file_norm = _norm(Path(filename).stem)
    for raw in (text or "").splitlines()[:120]:
        line = " ".join(raw.strip(" #*|\t").split())
        normalized = _norm(line)
        if not 5 <= len(line) <= 140:
            continue
        if (normalized == file_norm or normalized in _GENERIC_HEADINGS
                or normalized.startswith("pagina ") or "===== " in raw
                or line.lower().endswith((".pdf", ".html", ".htm", ".md", ".txt"))
                or line.lower().startswith(("http://", "https://"))):
            continue
        return line
    return None


def classify_doc_type(filename: str, text: str, taxonomy: Taxonomy) -> ClassificationSignal:
    """Score taxonomy keywords in document content first, filename second."""
    body = _norm((text or "")[:100_000])
    file_text = _norm(Path(filename).stem)
    scored: list[tuple[float, str, list[str]]] = []
    for doc_type in taxonomy.types:
        content_terms: list[str] = []
        filename_terms: list[str] = []
        for keyword in doc_type.keywords:
            key = _norm(keyword)
            if not key:
                continue
            if re.search(rf"(?:^|\s){re.escape(key)}(?:\s|$)", body):
                content_terms.append(keyword)
            if re.search(rf"(?:^|\s){re.escape(key)}(?:\s|$)", file_text):
                filename_terms.append(keyword)
        # Multi-word phrases carry more information than generic bare tokens: "equity research"
        # should outweigh incidental repetitions of "quarter" in a sector initiation report.
        content_unique = list(dict.fromkeys(content_terms))
        filename_unique = list(dict.fromkeys(filename_terms))
        score = sum(3.0 + min(2, len(_norm(term).split()) - 1) * 1.5
                    for term in content_unique)
        score += sum(1.0 + min(2, len(_norm(term).split()) - 1) * 0.5
                     for term in filename_unique)
        if score:
            evidence = ([f"content: {', '.join(list(dict.fromkeys(content_terms))[:4])}"]
                        if content_terms else [])
            if filename_terms:
                evidence.append(
                    f"filename: {', '.join(list(dict.fromkeys(filename_terms))[:3])}"
                )
            scored.append((score, doc_type.key, evidence))
    if not scored:
        return ClassificationSignal(
            taxonomy.default_key, 0.25, ("no class signal; configured default",)
        )
    scored.sort(key=lambda item: (-item[0], item[1]))
    best = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    confidence = min(0.99, 0.5 + (best[0] - runner) / max(best[0] * 2, 1))
    return ClassificationSignal(best[1], round(confidence, 3), tuple(best[2]))


def classify_period(filename: str, text: str) -> ClassificationSignal:
    """Infer reporting period from content first, then filename."""
    content_period = infer_period_label((text or "")[:20_000])
    filename_period = infer_period_label(Path(filename).stem)
    if content_period:
        evidence = ["content period"]
        if filename_period == content_period:
            evidence.append("filename agrees")
        return ClassificationSignal(
            content_period, 0.95 if filename_period == content_period else 0.85, tuple(evidence)
        )
    if filename_period:
        return ClassificationSignal(filename_period, 0.6, ("filename period",))
    return ClassificationSignal(None, 0.0, ("no period identified",))


def classify_document(
    filename: str,
    text: str,
    *,
    companies: Iterable[str | dict],
    taxonomy: Taxonomy,
) -> DocumentClassification:
    """Classify an unseen document without consulting or modifying search dictionaries."""
    company_matches = classify_companies(filename, text, companies)
    company = company_matches[0] if company_matches else ClassificationSignal(
        None, 0.0, ("no known company identified",)
    )
    return DocumentClassification(
        language=detect_language(text),
        company=company,
        companies=company_matches,
        doc_type=classify_doc_type(filename, text, taxonomy),
        period=classify_period(filename, text),
        suggested_company_name=(suggest_new_company_name(text) if company.value is None else None),
        suggested_title=suggest_document_title(filename, text),
    )
