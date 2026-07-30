"""Extract bank capital/returns ratios from the CNBV annual-report narrative facts.

Mexican bank annual filings (`ar_pros_*` taxonomy) don't tag NIM/CET1/efficiency as numeric XBRL —
they print them inside narrative HTML facts (e.g. "Capital Fundamental como porcentaje de activos de
riesgo ponderado 14.94% …", "Índice de eficiencia 35.30%"). This reads those narrative facts out of
the raw instance JSON and pulls the ratios with **strict** patterns.

Design rule (matches the math-gate philosophy): emit a value ONLY when a high-confidence, tightly
anchored pattern matches; otherwise leave it absent. A blank lands on the worklist — a *wrong* ratio
must never ship. Values are returned as percentages (e.g. 14.94), latest reported period.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# label anchor → metric key. Each anchor must be immediately followed (within a short window) by a
# percentage; we take the FIRST percentage after the anchor (CNBV prints latest year first).
# Only the genuinely-non-derivable ratios — ROE/ROTE are computed natively (ni/equity, ni/tangible
# book) and must NOT be overwritten by a less-reliable prose grab.
_PATTERNS: list[tuple[str, str]] = [
    ("cet1", r"capital\s+fundamental\s+como\s+porcentaje\s+de\s+activos?\s+de\s+riesgo\s+ponderado"),
    ("nim", r"margen\s+de\s+inter[eé]s\s+neto"),
    ("efficiency_ratio", r"[ií]ndice\s+de\s+eficiencia(?:\s+operativa)?"),
    ("cost_of_risk", r"costo\s+de\s+riesgo"),
]

# a percentage token: "14.94%" / "14.94 %" / "1 6.32 %" (CNBV sometimes splits digits with spaces)
_PCT = r"(\d{1,3}(?:[ .]\d{1,2})?)\s*%"
_WINDOW = 80  # chars after the anchor within which the percentage must appear


def _clean(html: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = re.sub(r"&#x[0-9a-fA-F]+;|&nbsp;|&[a-zA-Z]+;", " ", txt)
    return re.sub(r"\s+", " ", txt)


def _narrative_text(doc: dict) -> str:
    """Concatenate every narrative (HTML) fact value in the instance."""
    hpi = doc.get("HechosPorId") or {}
    parts = []
    for fact in hpi.values():
        val = fact.get("Valor")
        if isinstance(val, str) and ("<" in val or len(val) > 120):
            parts.append(val)
    return _clean(" ".join(parts))


def _num(pct: str) -> float | None:
    s = pct.replace(" ", "")  # "1 6.32" → "16.32"
    try:
        return float(s)
    except ValueError:
        return None


def extract_bank_ratios(json_path: str | Path) -> dict[str, float]:
    """Return {metric_key: pct_value} for the ratios found with high confidence in ``json_path``."""
    try:
        from src.download.bmv_xbrl import _read_raw_text
        doc = json.loads(_read_raw_text(json_path))
    except Exception:
        return {}
    text = _narrative_text(doc)
    low = text.lower()
    out: dict[str, float] = {}
    for key, anchor in _PATTERNS:
        for m in re.finditer(anchor, low):
            window = text[m.end():m.end() + _WINDOW]
            pm = re.search(_PCT, window)
            if not pm:
                continue
            v = _num(pm.group(1))
            # sanity band per metric — outside → not confident, skip (leave blank).
            if v is None or not _plausible(key, v):
                continue
            out[key] = v
            break
    return out


_BANDS = {
    "cet1": (5, 30), "icap": (8, 35), "nim": (1, 15), "efficiency_ratio": (20, 90),
    "cost_of_risk": (0, 12), "roe": (0, 45), "rote": (0, 10),
}


def _plausible(key: str, v: float) -> bool:
    lo, hi = _BANDS.get(key, (0, 100))
    return lo <= v <= hi


def latest_annual_json(reports_dir: str | Path) -> Path | None:
    """Pick the most recent annual (`*-FY`) instance JSON under reports_dir/xbrl."""
    xdir = Path(reports_dir) / "xbrl"
    if not xdir.is_dir():
        return None
    # base instances only (exclude the *_facts.json artifacts); raw filings are
    # gzip-compressed (*-FY.json.gz), with legacy plaintext (*-FY.json) still accepted.
    cands = [p for p in xdir.glob("*-FY.json*")
             if "_facts" not in p.name and p.name.endswith((".json", ".json.gz"))]
    if not cands:
        return None
    return sorted(cands)[-1]  # lexical sort → latest FY last
