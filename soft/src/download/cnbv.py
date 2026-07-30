"""CNBV bank-capital ratios (ICAP_BM) — authoritative CET1/ICAP per banca-múltiple bank.

RECONSTRUCTED 2026-07-28: the original module was lost to a ``vendor_sync --sync``
that predated its ``NET_NEW`` protection (it is now listed there). Rebuilt against
``tests/test_cnbv.py`` (the surviving spec) and validated by re-parsing the cached
``data/cnbv/ICAP_BM_202604.pdf`` to reproduce ``data/cnbv/bank_capital.json``
exactly (53 banks, identical values).

CNBV publishes a monthly one-page PDF (``ICAP_BM_<YYYYMM>.pdf``) with each bank's
CCB / CCF (CET1) / ICAP capital ratios. ``fetch_and_cache`` downloads the most
recent month available (they lag ~1-2 months), parses it, and writes
``data/cnbv/bank_capital.json``; the per-company build reads that cache offline via
``bank_capital_for_slug``. These are ground truth and OVERRIDE any prose grab.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import unicodedata
from pathlib import Path

SOFT_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = SOFT_ROOT / "data" / "cnbv"
CACHE_FILE = CACHE_DIR / "bank_capital.json"

_URL_TMPL = "https://portafolioinfo.cnbv.gob.mx/PortafolioInformacion/ICAP_BM_{period}.pdf"

# Universe slug → CNBV table name (normalized, see _norm). Only banca-múltiple
# banks appear in the ICAP table; holding-company slugs map to their bank
# (GFNorte → Banorte, Gentera → Compartamos, Regional → Banregio, ...).
SLUG_TO_CNBV = {
    "gfnorte": "banorte",
    "banco_del_bajio": "banco del bajio",
    "regional": "banregio",
    "gfinbursa": "inbursa",
    "gentera": "compartamos",
    "gf_multiva": "multiva",
    "invex": "invex",
    "actinver": "actinver",
    "banco_santander_mexico": "santander",
    "monex": "monex",
    "bbva_mexico": "bbva mexico",
}

# Plausibility band for a real bank's CET1 (%): a mis-parsed line (wrong column,
# stray decimal) lands outside and is dropped rather than shipped. Small niche
# banks legitimately print very high ratios (Banco Bineo 266%), so the ceiling
# is generous; nothing real prints below ~1%.
_CET1_BAND = (1.0, 500.0)


def _norm(name: str) -> str:
    """Accent-folded, lowercased, whitespace-collapsed bank name."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


# One table row on a single layout line: name, CCB, CCF, ICAP, optional category.
_ROW_RE = re.compile(
    r"^\s*(?P<name>[^\d]+?)\s+"
    r"(?P<ccb>\d{1,3}\.\d{2})\s+"
    r"(?P<ccf>\d{1,3}\.\d{2})\s+"
    r"(?P<icap>\d{1,3}\.\d{2})"
    r"(?:\s+(?:I{1,3}|IV|V))?\s*$"
)
_NUM_LINE_RE = re.compile(r"^\d{1,3}\.\d{2}$")
_CAT_LINE_RE = re.compile(r"^(?:I{1,3}|IV|V)$")


def parse_icap_text(text: str) -> dict[str, dict]:
    """Parse layout-extracted ICAP_BM text → {normalized name: {ccb, cet1, icap}}.

    CCF is the Coeficiente de Capital Fundamental — the CET1 ratio. Handles both
    one-line rows ("Banorte  19.34  12.75  19.74  I") and the split shape some
    extractors produce (name line followed by three value lines). Aggregate
    ("Total …") rows and out-of-band CET1 values are dropped, never shipped.
    """
    banks: dict[str, dict] = {}

    def _add(raw_name: str, ccb: float, ccf: float, icap: float) -> None:
        name = _norm(raw_name)
        if (not name or name.startswith(("total", "institucion", "ccb", "ccf", "icap"))
                or "%" in name):
            return
        if not (_CET1_BAND[0] <= ccf <= _CET1_BAND[1]):
            return
        banks[name] = {"ccb": ccb, "cet1": ccf, "icap": icap}

    lines = [l.strip() for l in text.splitlines()]
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _ROW_RE.match(line)
        if m:
            _add(m.group("name"), float(m.group("ccb")), float(m.group("ccf")),
                 float(m.group("icap")))
            i += 1
            continue
        # Split shape: a non-numeric name line followed by three ratio lines.
        if (line and not _NUM_LINE_RE.match(line) and not _CAT_LINE_RE.match(line)
                and i + 3 < len(lines)
                and _NUM_LINE_RE.match(lines[i + 1]) and _NUM_LINE_RE.match(lines[i + 2])
                and _NUM_LINE_RE.match(lines[i + 3])):
            _add(line, float(lines[i + 1]), float(lines[i + 2]), float(lines[i + 3]))
            i += 4
            continue
        i += 1
    return banks


def parse_icap_pdf(pdf_path: Path) -> dict[str, dict]:
    """Parse an ICAP_BM PDF (pymupdf text extraction) via :func:`parse_icap_text`."""
    import fitz  # pymupdf

    doc = fitz.open(pdf_path)
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    return parse_icap_text(text)


def _candidate_periods(months_back: int) -> list[str]:
    today = _dt.date.today().replace(day=1)
    out = []
    d = today
    for _ in range(months_back + 1):
        out.append(f"{d.year}{d.month:02d}")
        d = (d - _dt.timedelta(days=1)).replace(day=1)
    return out


def fetch_and_cache(*, verify_ssl: bool = False, months_back: int = 6) -> dict | None:
    """Download the newest available ICAP_BM report, parse, and cache.

    Returns the cache payload ``{"period", "source", "banks"}`` or ``None`` when
    no report could be fetched/parsed. Never raises on network errors.
    """
    import requests

    for period in _candidate_periods(months_back):
        url = _URL_TMPL.format(period=period)
        try:
            resp = requests.get(url, timeout=60, verify=verify_ssl)
        except requests.RequestException:
            continue
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF-"):
            continue
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path = CACHE_DIR / f"ICAP_BM_{period}.pdf"
        pdf_path.write_bytes(resp.content)
        try:
            banks = parse_icap_pdf(pdf_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[cnbv] parse failed for {pdf_path.name}: {exc}", file=sys.stderr)
            continue
        if not banks:
            continue
        payload = {"period": period, "source": url, "banks": banks}
        CACHE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                              encoding="utf-8")
        return payload
    return None


def load_bank_capital() -> dict | None:
    """Read the cached payload (offline path used by the per-company build)."""
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def bank_capital_for_slug(slug: str, banks: dict | None = None) -> dict | None:
    """{ccb, cet1, icap} for a universe slug via SLUG_TO_CNBV, or None.

    ``banks`` (a parsed table) is injectable for tests; the default reads the
    on-disk cache written by :func:`fetch_and_cache`.
    """
    name = SLUG_TO_CNBV.get(slug)
    if not name:
        return None
    if banks is None:
        payload = load_bank_capital()
        banks = (payload or {}).get("banks") or {}
    return banks.get(name)
