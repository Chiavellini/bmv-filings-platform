"""Document-agnostic query normalization for the semantic retrieval channel.

This is a small standards glossary, not a corpus-learned synonym table.  It expands common
financial acronyms before embedding so a general multilingual model receives their meaning
(``FX`` → ``foreign exchange``) rather than an opaque two-letter token.  The lexical channel
continues to search the user's exact text and reports curated expansions separately.
"""
from __future__ import annotations

import re
import unicodedata

_FINANCIAL_ACRONYMS = {
    "fx": "foreign exchange currency exchange rates",
    "capex": "capital expenditures",
    "opex": "operating expenses",
    "yoy": "year over year",
    "qoq": "quarter over quarter",
    "npl": "non performing loans",
    "nim": "net interest margin",
    "roe": "return on equity",
    "roa": "return on assets",
    "eps": "earnings per share",
}

# Deliberately small, reviewable ES/EN bridges for common financial *phrases*.  These are semantic
# hints only: they never change the literal Command-F count, and all broader lexical equivalents
# remain in the separately labelled curated-discovery lane.  The multilingual embedding model is
# still responsible for unseen wording; this table makes common short queries less opaque.
_BILINGUAL_FINANCIAL_HINTS = {
    "foreign exchange": "tipo de cambio efecto cambiario",
    "exchange rate": "tipo de cambio efecto cambiario",
    "tipo de cambio": "foreign exchange exchange rate currency translation",
    "efecto cambiario": "foreign exchange currency translation",
    "revenue": "ingresos ventas netas",
    "revenues": "ingresos ventas netas",
    "ingresos": "revenue net sales",
    "ventas": "revenue net sales",
    "net sales": "ventas netas ingresos",
    "ventas netas": "net sales revenue ingresos",
    "margin pressure": "presion sobre margenes",
    "presion sobre margenes": "margin pressure",
    "debt reduction": "reduccion de deuda desapalancamiento",
    "reduccion de deuda": "debt reduction deleveraging",
    "passenger": "pasajeros trafico de pasajeros",
    "passengers": "pasajeros trafico de pasajeros",
    "passenger traffic": "trafico de pasajeros pasajeros",
    "pasajero": "passenger passengers",
    "pasajeros": "passenger passengers passenger traffic",
    "trafico de pasajeros": "passenger traffic passengers",
}


def _fold(value: str) -> str:
    """Accent-insensitive comparison while preserving the user's original semantic query."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(c)
    )


def semantic_query_text(query: str) -> str:
    """Append deterministic ES/EN financial hints for the multilingual semantic channel.

    The returned string is only embedded; keyword matching and exact mention counts still receive
    the untouched user query.  That separation makes a translation useful without letting it
    manufacture literal evidence.
    """
    raw = " ".join((query or "").split())
    if not raw:
        return ""
    tokens = set(re.findall(r"[a-z0-9]+", raw.lower()))
    expansions = [_FINANCIAL_ACRONYMS[token]
                  for token in _FINANCIAL_ACRONYMS if token in tokens]
    folded = _fold(raw)
    for phrase, hint in _BILINGUAL_FINANCIAL_HINTS.items():
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", folded):
            expansions.append(hint)
    # Preserve deterministic order but avoid repeatedly weighting a synonym when query wording
    # triggers more than one bridge (for example "foreign exchange rate").
    unique = list(dict.fromkeys(expansions))
    return raw if not unique else f"{raw} {' '.join(unique)}"
