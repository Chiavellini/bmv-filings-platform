"""universe_gap.py — reconcile the authoritative BMV equity roster + the XBRL archive against our
covered UNIVERSE, and emit the coverage-expansion worklist.

The PRIMARY output is a four-bucket reconciliation of `data/bmv_equity_roster.csv` (the curated list
of ~140 BMV-listed equity tickers) against `gen_universe.UNIVERSE`:

* **Bucket A** — roster name NOT covered, but present in the free BMV XBRL archive → onboard with
  full data.
* **Bucket B** — roster name NOT covered and NOT in the archive → Yahoo-only fallback (6 price
  metrics; the rest honest N/A). Some are suspended/delisted → mostly-blank rows.
* **Bucket C** — already covered (matched by clave, config `xbrl_ticker`, slug, or an alias).
* **Bucket D** — our covered claves NOT on the equity roster (legitimate extras: FIBRAs the roster
  omits, plus sports/private/delisted names).

A SECONDARY section lists XBRL-archive issuers that are neither covered nor on the roster and are not
trust/ETF vehicles — i.e. operating filers the roster may have missed (TELMEX, AHMSA, …).

Fetching the archive index needs network (and `dangerouslyDisableSandbox` in this sandbox); pass
`--html <saved.html>` to parse a saved page offline (also how the tests run).

Run:
    PYTHONPATH=. python scripts/universe_gap.py --html data/cache/bmv_archive.html   # offline
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.gen_universe import UNIVERSE, slugify  # noqa: E402
from src.download.bmv_xbrl import (  # noqa: E402
    XbrlFiling,
    fetch_archive_index,
    parse_archive_index,
)

_ROOT = Path(__file__).resolve().parents[1]
_OUT = _ROOT / "outputs" / "_reconcile" / "universe_gap.md"
_ROSTER = _ROOT / "data" / "bmv_equity_roster.csv"


# --------------------------------------------------------------------------------------------------
# Classification heuristic (name-based). Proposals only — the user confirms the final sector bucket.
# --------------------------------------------------------------------------------------------------
_REIT_HINTS = ("fibra", "fibrapl", "cbfi")
_INSURER_HINTS = ("seguro", "aseguradora", "afore", "pensiones", "reaseg", "fianzas")
_BROKER_HINTS = ("casa de bolsa", "bolsa", "valores", "inversora", "broker", "grupo bmv")
_BANK_HINTS = ("banco", "banca", "grupo financiero", "financiera", "arrendadora", "hipotecaria",
               "sofom", "sofom,", "credito", "crédito", "casa de cambio", "banorte", "inbursa")

# Known ETFs / tracker certificate series (issued by Nafin or a casa de bolsa) — NOT operating
# companies. They lack the CK/CC/PI suffix so the structural rule below misses them; list explicitly.
_ETF_TRACKERS = {"NAFTRAC", "ANGELD", "DIABLOI", "QVGMEX", "SMARTRC", "MILATRC", "MEXTRAC", "TRACISHRS"}


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def is_trust_vehicle(razon_social: str, ticker: str = "") -> bool:
    """A CKD/CERPI trust-certificate series — tickers end CK/CC/PI. NOTE: deliberately does NOT use
    the trustee legal name ("BANCA MÚLTIPLE"), which mis-flagged real operating banks (Santander/BBVA/
    Banorte issue under that same name); the ticker suffix is the reliable signal."""
    tick = _norm(ticker)
    return tick.endswith("ck") or tick.endswith("cc") or tick.endswith("pi")


def is_etf_tracker(ticker: str = "") -> bool:
    return _norm(ticker).upper() in _ETF_TRACKERS or ticker.strip().upper() in _ETF_TRACKERS


def classify(razon_social: str, ticker: str = "") -> tuple[str, str]:
    """Propose ``(template, sector)`` from an issuer's legal name (+ ticker). Best-effort.

    Non-operating vehicles are separated: CKD/CERPI → ``("trust","ckd_cerpi")``; ETF/tracker →
    ``("etf","tracker")`` — both "likely skip"."""
    name = _norm(razon_social)
    tick = _norm(ticker)
    if is_etf_tracker(ticker):
        return ("etf", "tracker")
    if is_trust_vehicle(razon_social, ticker):
        return ("trust", "ckd_cerpi")
    is_fideicomiso_inmob = "fideicomiso" in name and (
        "inmobili" in name or "bienes ra" in name or "/f" in name or name.startswith("f/"))
    if any(h in name for h in _REIT_HINTS) or tick.startswith("fib") or is_fideicomiso_inmob:
        return ("reit", "fibras")
    if any(h in name for h in _INSURER_HINTS):
        return ("financials", "insurers_afores")
    if any(h in name for h in _BROKER_HINTS):
        return ("financials", "brokers_exchange")
    if any(h in name for h in _BANK_HINTS):
        return ("financials", "banks")
    return ("industrial", "misc_industrial")


_NON_OPERATING = {"trust", "etf"}


# --------------------------------------------------------------------------------------------------
# Covered-set + roster
# --------------------------------------------------------------------------------------------------
def existing_claves() -> set[str]:
    """Uppercased claves already in UNIVERSE (the covered set)."""
    return {clave.strip().upper()
            for _sector, (_t, members) in UNIVERSE.items() for clave, _n in members}


def _config_xbrl_tickers() -> set[str]:
    """The `ir_website.xbrl_ticker` value of every config (a company's clave may differ from the
    archive ticker — e.g. slug `liverpool` files under `LIVEPOL`)."""
    out: set[str] = set()
    cfg_dir = _ROOT / "configs"
    if not cfg_dir.is_dir():
        return out
    for cfg in cfg_dir.glob("*.yaml"):
        for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s.startswith("xbrl_ticker:"):
                out.add(s.split(":", 1)[1].strip().strip("\"'").upper())
    return out


# Archive/operating tickers we ALREADY cover under a different clave (operating subsidiary vs the
# holding company we list, or an alternate series). Without these, the secondary off-roster scan
# false-flags them: Banorte→GFNORTE, Compartamos→GENTERA, Sigma Foods→SIGMA.
_ARCHIVE_ALIASES = {"BANORTE", "COMPART", "SIGMAF", "AERO"}


def covered_tokens() -> set[str]:
    """Every token that means "we already cover this": UNIVERSE claves ∪ config xbrl_tickers ∪ slugs
    ∪ known operating-subsidiary aliases."""
    toks = existing_claves() | _config_xbrl_tickers() | set(_ARCHIVE_ALIASES)
    for _sector, (_t, members) in UNIVERSE.items():
        for _clave, name in members:
            toks.add(slugify(name).upper())
    return toks


@dataclass
class RosterEntry:
    clave: str
    name: str
    status: str          # covered | add_xbrl | add_yahoo (a hint; recomputed here)
    sector: str
    template: str
    in_archive_hint: str
    alias: str


def load_roster(path: Path = _ROSTER) -> list[RosterEntry]:
    """Parse data/bmv_equity_roster.csv (skips `#` comment lines)."""
    lines = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    out: list[RosterEntry] = []
    for r in csv.DictReader(lines):
        out.append(RosterEntry(
            clave=(r.get("clave") or "").strip().upper(),
            name=(r.get("name") or "").strip(),
            status=(r.get("status") or "").strip(),
            sector=(r.get("sector") or "").strip(),
            template=(r.get("template") or "").strip(),
            in_archive_hint=(r.get("in_xbrl_archive") or "").strip(),
            alias=(r.get("alias") or "").strip().upper(),
        ))
    return out


# --------------------------------------------------------------------------------------------------
@dataclass
class Candidate:
    clave: str
    razon_social: str
    quarterly: int
    annual: int
    period_lo: str
    period_hi: str
    template: str
    sector: str


def _archive_by_ticker(filings: list[XbrlFiling]) -> dict[str, list[XbrlFiling]]:
    by: dict[str, list[XbrlFiling]] = {}
    for f in filings:
        by.setdefault(f.ticker.strip().upper(), []).append(f)
    return by


def gap_candidates(filings: list[XbrlFiling], covered: set[str] | None = None) -> list[Candidate]:
    """Archive issuers not in ``covered``, most-data-first. Pure over its inputs (unit-testable)."""
    covered = existing_claves() if covered is None else covered
    cands: list[Candidate] = []
    for clave, fs in _archive_by_ticker(filings).items():
        if clave in covered:
            continue
        periods = sorted(f.period for f in fs)
        quarterly = sum(1 for f in fs if f.kind == "quarterly")
        annual = sum(1 for f in fs if f.kind == "annual")
        razon = next((f.razon_social for f in fs if f.razon_social), "")
        template, sector = classify(razon, clave)
        cands.append(Candidate(clave, razon, quarterly, annual,
                               periods[0], periods[-1], template, sector))
    cands.sort(key=lambda c: (-(c.quarterly + c.annual), c.clave))
    return cands


@dataclass
class Reconciliation:
    bucket_a: list[RosterEntry] = field(default_factory=list)   # missing, in archive
    bucket_b: list[RosterEntry] = field(default_factory=list)   # missing, no archive
    bucket_c: list[RosterEntry] = field(default_factory=list)   # covered
    bucket_d: list[str] = field(default_factory=list)           # our claves not on roster
    off_roster_ops: list[Candidate] = field(default_factory=list)  # archive operating, not covered/roster


def reconcile(filings: list[XbrlFiling], roster: list[RosterEntry] | None = None,
              covered: set[str] | None = None) -> Reconciliation:
    """Four-bucket reconciliation of the roster + a secondary off-roster operating list."""
    roster = load_roster() if roster is None else roster
    covered = covered_tokens() if covered is None else covered
    archive = set(_archive_by_ticker(filings).keys())
    rec = Reconciliation()
    roster_claves: set[str] = set()
    for e in roster:
        roster_claves.add(e.clave)
        if e.clave in covered or (e.alias and e.alias in covered):
            rec.bucket_c.append(e)
        elif e.clave in archive:
            rec.bucket_a.append(e)
        else:
            rec.bucket_b.append(e)
    rec.bucket_d = sorted(existing_claves() - roster_claves)
    # secondary: archive issuers that are operating (not trust/etf), not covered, not on the roster.
    for c in gap_candidates(filings, covered):
        if c.template not in _NON_OPERATING and c.clave not in roster_claves:
            rec.off_roster_ops.append(c)
    return rec


# --------------------------------------------------------------------------------------------------
def _archive_qcount(filings: list[XbrlFiling]) -> dict[str, int]:
    return {t: len(fs) for t, fs in _archive_by_ticker(filings).items()}


def render_md(rec: Reconciliation, covered_n: int, qcount: dict[str, int]) -> str:
    def a_row(e: RosterEntry) -> str:
        n = qcount.get(e.clave, 0)
        return f"| {e.clave} | {e.name} | {n} | {e.sector} | {e.template} |"

    def b_row(e: RosterEntry) -> str:
        return f"| {e.clave} | {e.name} | {e.sector} | {e.template} |"

    lines = [
        "# BMV coverage reconciliation — roster vs. covered universe",
        "",
        f"Covered today: **{covered_n}** claves in `gen_universe.UNIVERSE`. Authoritative equity roster "
        f"(`data/bmv_equity_roster.csv`): **{len(rec.bucket_a) + len(rec.bucket_b) + len(rec.bucket_c)}** "
        "tickers. Reconciliation:",
        "",
        f"- **Bucket A — add now (in XBRL archive): {len(rec.bucket_a)}**",
        f"- **Bucket B — Yahoo-only (no XBRL): {len(rec.bucket_b)}**",
        f"- **Bucket C — already covered: {len(rec.bucket_c)}**",
        f"- **Bucket D — our extras not on the equity roster (FIBRAs etc.): {len(rec.bucket_d)}**",
        f"- Secondary — archive operating issuers not covered & not on roster: {len(rec.off_roster_ops)}",
        "",
        "## Bucket A — onboard with full free XBRL",
        "",
        "| clave | name | filings | proposed sector | template |",
        "|---|---|--:|---|---|",
        *[a_row(e) for e in rec.bucket_a],
        "",
        "## Bucket B — Yahoo-only fallback (some are suspended/delisted → mostly-blank)",
        "",
        "| clave | name | proposed sector | template |",
        "|---|---|---|---|",
        *[b_row(e) for e in rec.bucket_b],
        "",
        "## Bucket D — our covered names not on the equity roster (kept)",
        "",
        ", ".join(rec.bucket_d) or "(none)",
        "",
        "## Secondary — archive operating issuers to consider (not on the equity roster)",
        "",
        "| clave | issuer | filings | template | sector |",
        "|---|---|--:|---|---|",
        *[f"| {c.clave} | {c.razon_social[:48]} | {c.quarterly + c.annual} | {c.template} | {c.sector} |"
          for c in rec.off_roster_ops],
        "",
    ]
    return "\n".join(lines)


def run(html_path: Path | None = None, *, verify_ssl: bool = False,
        cache_html: Path | None = None, out_path: Path = _OUT) -> Reconciliation:
    if html_path is not None:
        filings = parse_archive_index(Path(html_path).read_text(encoding="utf-8"))
    else:
        filings = fetch_archive_index(verify_ssl=verify_ssl, cache_html_path=cache_html)
    rec = reconcile(filings)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_md(rec, len(existing_claves()), _archive_qcount(filings)),
                        encoding="utf-8")
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", type=Path, help="Parse a saved archive HTML page (offline)")
    ap.add_argument("--cache-html", type=Path, help="Save the fetched archive page here for reuse")
    ap.add_argument("--verify-ssl", action="store_true", help="Verify TLS (default off in this env)")
    ap.add_argument("--out", type=Path, default=_OUT, help="Markdown worklist output path")
    args = ap.parse_args(argv)

    rec = run(args.html, verify_ssl=args.verify_ssl, cache_html=args.cache_html, out_path=args.out)
    print(f"\nCovered: {len(existing_claves())} · roster reconciliation → {args.out}", file=sys.stderr)
    print(f"  Bucket A (add now, XBRL):   {len(rec.bucket_a):>3}  {[e.clave for e in rec.bucket_a]}")
    print(f"  Bucket B (Yahoo-only):      {len(rec.bucket_b):>3}  {[e.clave for e in rec.bucket_b]}")
    print(f"  Bucket C (already covered): {len(rec.bucket_c):>3}")
    print(f"  Bucket D (our extras):      {len(rec.bucket_d):>3}")
    print(f"  Off-roster archive ops:     {len(rec.off_roster_ops):>3}  "
          f"{[c.clave for c in rec.off_roster_ops][:15]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
