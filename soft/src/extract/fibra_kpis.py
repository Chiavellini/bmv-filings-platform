"""Extract FIBRA (Mexican REIT) operating KPIs from the quarterly MD&A narrative.

FFO/AFFO/NOI/occupancy aren't tagged XBRL — FIBRAs print them in the `_mdna.html` prose. Parsing
these safely is the hard part: the FIRST mention of "FFO" is usually a glossary *definition*
("FFO): Es el flujo…"), figures appear both per-quarter and annual, and some FIBRAs (e.g. a recent
portfolio) explicitly DON'T report AFFO. So the rules here are deliberately conservative:

- A currency KPI is emitted only when the label is immediately followed by a **value verb**
  (de / se ubicó en / se elevó a / fue de / totalizó / alcanzó) and a "$X millones" figure — this
  skips definition sentences.
- When both a quarterly and an annual figure are present, prefer the **annual** one (LTM proxy for
  P/FFO); if only a quarterly figure is found, skip (an LTM multiple on one quarter would be wrong).
- Occupancy is a clean single value ("ocupación del X%").
- Anything ambiguous → omit (blank → worklist). A wrong FFO would corrupt a headline multiple, so
  "no value" always beats "maybe-wrong value".

Values are returned in millions (currency) or percent (occupancy).
"""
from __future__ import annotations

import re
from pathlib import Path

_VALUE_VERB = r"(?:de|se\s+ubic[oó]|se\s+elev[oó]\s+a|fue\s+de|totaliz[oó]|alcanz[oó]|ascendi[oó]\s+a)"
_MILLONES = r"\$?\s*([\d]{1,3}(?:[,\s]?\d{3})*(?:\.\d+)?)\s*millones"
_ANNUAL_CTX = r"(?:en\s+el\s+a[nñ]o|acumulad|en\s+20\d\d|doce\s+meses|del\s+a[nñ]o)"


def _clean(html: str) -> str:
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"&#x[0-9a-fA-F]+;|&nbsp;|&[a-zA-Z]+;", " ", t)
    return re.sub(r"\s+", " ", t)


def _to_millions(s: str) -> float | None:
    try:
        return float(s.replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def _currency_kpi(text: str, label: str) -> float | None:
    """Find `<label> <verb> $X millones`, preferring an annual-context figure."""
    quarterly = None
    for m in re.finditer(rf"\b{label}\b[^.]{{0,60}}?{_VALUE_VERB}\s+{_MILLONES}([^.]{{0,40}})",
                         text, re.I):
        val = _to_millions(m.group(1))
        if val is None or val <= 0:
            continue
        tail = m.group(2) or ""
        if re.search(_ANNUAL_CTX, tail, re.I):
            return val  # annual figure — best for an LTM multiple
        quarterly = quarterly or val
    return None  # only a quarterly figure (or none) → skip; LTM on one quarter would be wrong


def _occupancy(text: str) -> float | None:
    m = re.search(r"ocupaci[oó]n\s+(?:del?\s+|promedio\s+(?:de\s+)?)?(\d{1,3}(?:\.\d+)?)\s*%",
                  text, re.I)
    if m:
        v = float(m.group(1))
        if 0 < v <= 100:
            return v
    return None


def extract_fibra_kpis(mdna_html_path: str | Path) -> dict[str, float]:
    """Return {kpi: value} extracted with high confidence from a FIBRA `_mdna.html`."""
    try:
        text = _clean(Path(mdna_html_path).read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    out: dict[str, float] = {}
    for key, label in (("ffo", "FFO"), ("affo", "AFFO"), ("noi", "NOI")):
        v = _currency_kpi(text, label)
        if v is not None:
            out[key] = v
    occ = _occupancy(text)
    if occ is not None:
        out["occupancy"] = occ
    return out


def latest_mdna(reports_dir: str | Path) -> Path | None:
    """Most recent quarterly `_mdna.html` under reports_dir/xbrl (largest = richest, latest period)."""
    xdir = Path(reports_dir) / "xbrl"
    if not xdir.is_dir():
        return None
    cands = [p for p in xdir.glob("*_mdna.html") if p.stat().st_size > 2000]
    if not cands:
        return None
    return sorted(cands)[-1]  # lexical → latest period last
