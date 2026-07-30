"""Canonical document-type taxonomy — friendly labels + keyword inference.

AlphaSense-style doc-type scoping. The taxonomy lives in ``configs/alpha_go.yaml`` under
``doc_types:`` (a list of ``{key, label, keywords}``) plus a global ``default_doc_type``. A
document's type is inferred by matching a taxonomy entry's keyword against the filename/text;
with no match it falls back to the default. Legacy index values (``release``/``report``) map to a
canonical key so pre-existing data reads with friendly labels without a migration.

The storage/filter/facet layers are value-agnostic (``documents.doc_type TEXT`` +
``doc_type IN (...)``), so richer types "just work" once inferred here or chosen on upload.
"""
from __future__ import annotations

from dataclasses import dataclass

# Fallback taxonomy if config omits `doc_types:` — keeps the module usable standalone/in tests.
_BUILTIN: list[dict] = [
    {"key": "news_article", "label": "News", "keywords": []},
    {"key": "press_release", "label": "Press release",
     "keywords": ["comunicado", "prensa", "press release", "news release"]},
    {"key": "quarterly_release", "label": "Quarterly release",
     "keywords": ["release", "quarter", "trimestr", "earnings", "resultados",
                  "1q", "2q", "3q", "4q", "1t", "2t", "3t", "4t"]},
    {"key": "annual_report", "label": "Annual report",
     "keywords": ["annual", "10-k", "20-f", "informe anual", "reporte anual", "anual"]},
    {"key": "transcript", "label": "Earnings-call transcript",
     "keywords": ["transcript", "transcripcion", "earnings call", "conference call", "webcast"]},
    {"key": "presentation", "label": "Investor presentation",
     "keywords": ["presentation", "presentacion", "investor day", "slides", "deck"]},
    {"key": "analyst_research", "label": "Analyst / sector research",
     "keywords": ["equity research", "analyst report", "initiation", "initiating coverage",
                  "price target", "rating"]},
    {"key": "relevant_event", "label": "Relevant / material event",
     "keywords": ["evento relevante", "hecho relevante", "material event"]},
    {"key": "shareholder_meeting", "label": "Shareholder meeting",
     "keywords": ["asamblea", "shareholder meeting", "annual general meeting"]},
    {"key": "debt_prospectus", "label": "Debt / prospectus",
     "keywords": ["prospectus", "prospecto", "deuda", "debt offering"]},
    {"key": "sustainability_report", "label": "Sustainability report",
     "keywords": ["sustainability", "sustentabilidad", "esg report", "informe integrado"]},
    {"key": "regulatory_filing", "label": "Regulatory filing",
     "keywords": ["filing", "6-k", "8-k", "folleto", "cnbv", "bmv", "sec"]},
    {"key": "internal", "label": "Internal / Other", "keywords": []},
]
_DEFAULT_KEY = "quarterly_release"

# Legacy doc_type values in older indexes → canonical key (all were quarterly press releases).
_LEGACY = {"release": "quarterly_release", "report": "quarterly_release"}


@dataclass(frozen=True)
class DocType:
    key: str
    label: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Taxonomy:
    types: tuple[DocType, ...]
    default_key: str

    def keys(self) -> list[str]:
        return [t.key for t in self.types]

    def labels(self) -> list[str]:
        return [t.label for t in self.types]

    def canonical(self, key: str) -> str:
        """Map a raw/legacy key to its canonical taxonomy key (unchanged if unknown)."""
        return _LEGACY.get(key, key)

    def label_for(self, key: str) -> str:
        """Friendly label for a doc_type key (legacy-aware); unknown keys returned verbatim."""
        canon = self.canonical(key)
        for t in self.types:
            if t.key == canon:
                return t.label
        return key

    def key_for_label(self, label: str) -> str:
        for t in self.types:
            if t.label == label:
                return t.key
        return label

    def infer(self, text: str) -> str:
        """Infer a canonical key: first taxonomy entry whose keyword appears in ``text``."""
        low = (text or "").lower()
        for t in self.types:
            for kw in t.keywords:
                if kw and kw in low:
                    return t.key
        return self.default_key


def load_taxonomy(config: dict | None = None) -> Taxonomy:
    """Build the :class:`Taxonomy` from config's ``doc_types``/``default_doc_type`` (or builtin)."""
    cfg = config or {}
    raw = cfg.get("doc_types") or _BUILTIN
    types = tuple(
        DocType(
            key=str(d["key"]),
            label=str(d.get("label", d["key"])),
            keywords=tuple(str(k).lower() for k in (d.get("keywords") or [])),
        )
        for d in raw
    )
    default_key = str(cfg.get("default_doc_type") or "")
    if not any(t.key == default_key for t in types):
        default_key = _DEFAULT_KEY if any(t.key == _DEFAULT_KEY for t in types) else types[0].key
    return Taxonomy(types=types, default_key=default_key)
