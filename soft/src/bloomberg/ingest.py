"""Parse a filled Bloomberg template CSV into a validated :class:`BloombergPack`.

Validation is intentionally light but honest: it reports every blank/unparseable cell (as a
warning list, not a hard failure) so the workbook can still build with what is present, and it
runs a couple of plausibility checks (positive price/shares). The caller decides whether missing
cells are acceptable for the requested blocks.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from src.bloomberg.schema import BloombergPack

# The expected CSV header (mirrors template.COLUMNS; re-declared to avoid an import cycle).
_EXPECTED = ["entity", "entity_kind", "field", "period", "label", "unit", "bbg_hint", "value"]


@dataclass
class IngestResult:
    pack: BloombergPack
    missing: list[str] = field(default_factory=list)   # human-readable "entity/field" that were blank
    warnings: list[str] = field(default_factory=list)


def _to_float(raw: str) -> float | None:
    s = (raw or "").strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_pack(path: str | Path, slug: str | None = None) -> IngestResult:
    p = Path(path)
    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    if not rows:
        raise ValueError(f"{p}: empty template")

    header = list(rows[0].keys())
    if header != _EXPECTED:
        raise ValueError(f"{p}: unexpected columns {header}; expected {_EXPECTED}")

    inferred_slug = slug
    pack = BloombergPack(slug=inferred_slug or "")
    missing: list[str] = []
    warnings: list[str] = []

    for r in rows:
        kind = (r["entity_kind"] or "").strip()
        entity = (r["entity"] or "").strip()
        fld = (r["field"] or "").strip()
        period = (r["period"] or "").strip()
        val = _to_float(r["value"])

        if val is None:
            missing.append(f"{entity}/{fld}" + (f"@{period}" if period else ""))
            continue

        if kind == "subject":
            if inferred_slug is None and pack.slug == "":
                pack.slug = entity
            pack.subject[fld] = val
        elif kind == "peer":
            pack.peers.setdefault(entity, {})[fld] = val
        elif kind == "segment":
            pack.segment_multiples[entity] = val
        elif kind == "history":
            try:
                pack.history[int(period)] = val
            except ValueError:
                warnings.append(f"history row with non-year period {period!r}")
        elif kind == "timeseries":
            try:
                yr = int(period)
            except ValueError:
                warnings.append(f"timeseries row with non-year period {period!r}")
                continue
            # Map the terminal-facing field name to the engine's fund.annual key.
            engine_field = "operating_income" if fld == "ebit" else fld
            pack.timeseries.setdefault(yr, {})[engine_field] = val
        elif kind == "macro":
            pack.macro[entity] = val
        else:
            warnings.append(f"unknown entity_kind {kind!r} for {entity}/{fld}")

    # Plausibility checks (non-fatal).
    px = pack.subject.get("px_last")
    if px is not None and px <= 0:
        warnings.append(f"subject px_last is non-positive ({px})")
    sh = pack.subject.get("shares_out")
    if sh is not None and sh <= 0:
        warnings.append(f"subject shares_out is non-positive ({sh})")

    if slug and pack.slug and pack.slug != slug:
        warnings.append(f"pack slug {pack.slug!r} != expected {slug!r}")
        pack.slug = slug

    pack.suspect_placeholder, pack.placeholder_reasons = detect_placeholder(pack)
    if pack.suspect_placeholder:
        warnings.append("pack looks like PLACEHOLDER data: " + "; ".join(pack.placeholder_reasons))

    return IngestResult(pack=pack, missing=missing, warnings=warnings)


def _is_round(v: float, step: float) -> bool:
    """True if ``v`` is an exact multiple of ``step`` (e.g. a hand-typed round number)."""
    return abs(v / step - round(v / step)) < 1e-9


def detect_placeholder(pack) -> tuple[bool, list[str]]:
    """Heuristically flag a residual Bloomberg pack that is hand-typed DUMMY data rather than real
    terminal values. Real Bloomberg exports vary per company and carry cents/basis-point precision;
    template placeholders are uniform, whole, and half-rounded. We fire on strong, low-false-positive
    tells and require the near-definitive one (identical yields across many names) OR ≥2 weaker ones.
    """
    reasons: list[str] = []

    def across_entities(fld):
        vals = [pack.subject[fld]] if fld in pack.subject else []
        vals += [row[fld] for row in pack.peers.values() if fld in row]
        return vals

    # A (near-definitive): the same dividend yield typed for 3+ different companies.
    dvds = across_entities("dvd_yield")
    strong = len(dvds) >= 3 and len(set(dvds)) == 1
    if strong:
        reasons.append(f"dividend yield identical ({dvds[0]}) across {len(dvds)} companies")

    # B: every price is a whole number (real quotes carry cents).
    pxs = across_entities("px_last")
    if len(pxs) >= 3 and all(_is_round(p, 1.0) for p in pxs):
        reasons.append(f"all {len(pxs)} prices are whole numbers")

    # C: subject bank ratios all land on a 0.5 grid (real filing ratios don't, e.g. 15.46).
    ratios = [pack.subject[k] for k in ("nim", "cet1", "efficiency_ratio", "roe", "rote",
                                        "cost_of_risk") if k in pack.subject]
    if len(ratios) >= 2 and all(_is_round(r, 0.5) for r in ratios):
        reasons.append(f"{len(ratios)} bank ratios all round to 0.5")

    # D: every share count is a round 500-mn step.
    shs = across_entities("shares_out")
    if len(shs) >= 3 and all(_is_round(s, 500.0) for s in shs):
        reasons.append(f"all {len(shs)} share counts are round")

    # E (near-definitive): the subject pack is dominated by hand-typed round whole numbers. Real
    # terminal exports carry cents/decimals; ≥6 operational fields all landing on whole integers is a
    # scaffold fill (Fibra Uptown's 14000/12000/24000/3900/… REIT template — where D/A can't fire on a
    # single-subject pack). Catches the REIT/industrial dummy packs the peer-based tells miss.
    _op = ("ffo", "affo", "noi", "ebitda_ltm", "net_debt", "nav_ps", "gla", "occupancy", "ltv",
           "shares_out", "book_value", "tangible_book")
    whole = [pack.subject[f] for f in _op if isinstance(pack.subject.get(f), (int, float))
             and _is_round(pack.subject[f], 1.0)]
    dummy_scaffold = len(whole) >= 6
    if dummy_scaffold:
        reasons.append(f"{len(whole)} subject operational fields are round whole numbers")

    return (strong or dummy_scaffold or len(reasons) >= 2), reasons
