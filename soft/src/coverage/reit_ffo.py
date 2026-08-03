"""REIT FFO reconstruction from cached BMV XBRL — net-new logic (never vendor_sync'd).

FFO = ProfitLoss − fair_value_adjustment + depreciation_and_amortisation, where
``fair_value_adjustment`` is the IAS 40 investment-property mark-to-market line already
identified as the reason P/E / Net margin / NI CAGR are ``na_template`` for fibras and
real_estate (``configs/metric_applicability.yaml``) — FFO is the metric that backs the
distortion back out. Concept candidates + measured hit-rates: ``configs/reit_ffo_concepts.yaml``.

blank-not-wrong (this is why FFO was deferred — a wrong FFO corrupts P/FFO): a value ships only
when the fiscal year's ProfitLoss AND at least one fair-value concept are both present. When two
independent concepts (a property-specific mx-cor tag and the generic ifrs-full cash-flow add-back)
are both present and agree within 5%, the value ships "corroborated"; a single tag alone still
ships (these tags are individually reliable, unlike the deferred MD&A prose parser — see the
hit-rate table in the yaml), but is flagged single-tag in its source note. Two present tags that
DISAGREE beyond 5%, or no tag at all (14/26 REITs measured), blank rather than guess.

The remaining caller-side gate — plausibility (``0 < P/FFO`` within the sector's normal range) —
lives in ``valuation.py`` alongside the other REIT sanity checks, since this module has no market
cap to compute a multiple with.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

import yaml

from src.coverage.fundamentals import (
    _catalog_current_facts,
    _period_label,
    _selected_canonical_fact_files,
)
from src.extract.xbrl_facts import _has_dimensions, _parse_iso, _span_days
from src.shared.paths import REPORTS_DIR

_ROOT = Path(__file__).resolve().parents[2]
_CONCEPTS_PATH = _ROOT / "configs" / "reit_ffo_concepts.yaml"


def _load_concepts() -> dict[str, list[str]]:
    try:
        data = yaml.safe_load(_CONCEPTS_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    return {k: list((v or {}).get("xbrl_concepts") or []) for k, v in data.items()}


_CONCEPTS = _load_concepts()

# A full fiscal year runs ~365 days; reject anything materially shorter (a lone quarter, the
# quarter-preferring case xbrl_facts._pick_entry is built for) or longer (a transition-period
# anomaly). This is why _pick_entry itself isn't reused here — it prefers the shortest duration
# matching a target period_end, which would return Q4 alone instead of the full year.
_MIN_ANNUAL_DAYS = 330
_MAX_ANNUAL_DAYS = 380

# Two independent fair-value tags within this relative tolerance of each other → "corroborated".
_CORROBORATION_TOLERANCE = 0.05


@dataclass
class FfoResult:
    value: float          # FFO in the same units as the input facts (pre-scaling; caller scales)
    year: int             # fiscal year the value covers — FY, NOT a trailing-twelve-month figure
    corroborated: bool     # two independent fair-value tags agreed within tolerance
    note: str             # human-readable provenance for the Cell note


def _annual_fact(facts: dict, concept: str, year: int) -> float | None:
    """The single annual (full fiscal-year) duration fact for ``concept`` ending in ``year``.

    Excludes dimensional (segment) facts — the consolidated total is what FFO needs. A concept
    present but valued exactly 0.0 is treated as absent (mirrors xbrl_facts._PREFER_NONZERO): some
    issuers file a mapped concept as an empty placeholder line while the real figure sits under a
    different concept, and a real zero annual fair-value adjustment is not a plausible FIBRA figure
    to trust blindly either way.
    """
    entries = facts.get(concept) or []
    candidates = []
    for e in entries:
        if _has_dimensions(e):
            continue
        span = _span_days(e)
        if span is None or not (_MIN_ANNUAL_DAYS <= span <= _MAX_ANNUAL_DAYS):
            continue
        end = _parse_iso(e.get("period_end"))
        if end is None or end.year != year:
            continue
        candidates.append(e)
    if not candidates:
        return None
    chosen = min(candidates, key=lambda e: abs((_span_days(e) or 0) - 365))
    value = chosen["value"]
    return value if value != 0 else None


def _first_present(facts: dict, concepts: list[str], year: int) -> float | None:
    for concept in concepts:
        v = _annual_fact(facts, concept, year)
        if v is not None:
            return v
    return None


def compute_ffo(facts: dict, year: int) -> FfoResult | None:
    """FY ``year`` FFO from a company's flat XBRL facts dict (as returned by
    ``src.extract.pipeline._load_facts``), or None if it can't be safely reconstructed.

    Units match the input facts (raw pesos, unscaled) — callers apply the same
    pesos-per-unit scaling they use for every other XBRL-derived figure.
    """
    profit_loss = _first_present(facts, _CONCEPTS.get("profit_loss", []), year)
    if profit_loss is None:
        return None

    fv_gain = _first_present(facts, _CONCEPTS.get("fair_value_gain", []), year)
    fv_loss = _first_present(facts, _CONCEPTS.get("fair_value_loss", []), year)
    # mx-cor concepts are magnitude-only, direction-encoded by name. `fv` below is defined as "the
    # delta to ADD to ProfitLoss" (not "to subtract") — a GAIN must be backed OUT (added as a
    # negative), a LOSS backed IN (added as a positive).
    fv_property = -fv_gain if fv_gain is not None else (fv_loss if fv_loss is not None else None)

    # The generic ifrs-full concept is a SIGNED cash-flow add-back that already carries the same
    # "delta to add" convention as fv_property above — confirmed empirically (Fibra Uno FY2025:
    # property tag -11,694mn vs generic tag -11,522mn, 1.5% apart, SAME sign, no extra flip needed).
    fv_generic = _first_present(facts, _CONCEPTS.get("fair_value_adjustment_generic", []), year)

    if fv_property is not None and fv_generic is not None:
        denom = max(abs(fv_property), abs(fv_generic))
        agree = denom > 0 and abs(fv_property - fv_generic) / denom < _CORROBORATION_TOLERANCE
        if not agree:
            return None  # two independent tags disagree — blank rather than guess which is right
        fv = fv_property
        corroborated = True
        note = "FFO: property-specific + generic fair-value tags corroborate"
    elif fv_property is not None:
        fv, corroborated, note = fv_property, False, "FFO: property-specific fair-value tag only"
    elif fv_generic is not None:
        fv, corroborated, note = fv_generic, False, "FFO: generic fair-value tag only"
    else:
        return None  # no fair-value tag found at all — can't distinguish "genuinely zero" from
                     # "tagged differently" (e.g. a PP&E-holding hotel/tower FIBRA); blank, not a guess

    da = _first_present(facts, _CONCEPTS.get("depreciation_and_amortisation", []), year) or 0.0
    ffo = profit_loss + fv + da
    return FfoResult(value=ffo, year=year, corroborated=corroborated,
                      note=f"{note}, FY{year}")


# Same annual-filing filename marker as applicability._ANNUAL_MARKERS — "-4T" (industrial fiscal
# year-end quarter) or "-FY" (bank/financials full-year statement). FIBRAs file -4T.
_ANNUAL_MARKERS = ("-4T", "-FY")


def _latest_annual_filing(slug: str) -> tuple[Path, int] | None:
    """(path, year) for the most recent cached annual XBRL filing for ``slug``, or None."""
    xdir = REPORTS_DIR / slug / "xbrl"
    if not xdir.is_dir():
        return None
    canonical: list[tuple[int, Path]] = []
    for path in _selected_canonical_fact_files(REPORTS_DIR / slug):
        period = _period_label(path.name)
        if period.endswith(("-4T", "-FY")):
            canonical.append((int(period[:4]), path))
    if canonical:
        year, path = max(canonical, key=lambda item: item[0])
        return path, year
    if _catalog_current_facts(REPORTS_DIR / slug) is not None:
        return None  # managed estate: never fall back to a superseded raw alias

    # Standalone legacy cache without canonical facts: newest mtime per year.
    candidates: dict[int, Path] = {}
    annual_name = re.compile(r"(20\d{2})(?:-4T|-FY)\.json(?:\.gz)?$")
    for path in xdir.iterdir():
        match = annual_name.search(path.name)
        if not match:
            continue
        year = int(match.group(1))
        existing = candidates.get(year)
        if existing is None or (
            path.stat().st_mtime_ns, path.name
        ) > (
            existing.stat().st_mtime_ns, existing.name
        ):
            candidates[year] = path
    return (candidates[max(candidates)], max(candidates)) if candidates else None


def _load_slug_config(slug: str) -> dict:
    cfg_path = _ROOT / "configs" / f"{slug}.yaml"
    if not cfg_path.exists():
        return {}
    try:
        return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def ffo_for_slug(slug: str) -> FfoResult | None:
    """FY FFO for ``slug`` from its most recent cached annual XBRL filing, scaled to the company's
    configured project unit (matching every other XBRL-derived figure in the engine) — or None if
    no annual filing is cached or FFO can't be safely reconstructed (see :func:`compute_ffo`).
    """
    from src.extract.pipeline import _load_facts
    from src.extract.xbrl_facts import pesos_per_unit_for_facts

    found = _latest_annual_filing(slug)
    if found is None:
        return None
    path, year = found
    if path.name.endswith("_facts.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            facts = payload.get("facts") if isinstance(payload, dict) else None
        except (OSError, ValueError):
            facts = None
    else:
        facts = _load_facts(path)
    if not facts:
        return None
    result = compute_ffo(facts, year)
    if result is None:
        return None
    ppu = pesos_per_unit_for_facts(_load_slug_config(slug), facts)
    return FfoResult(value=result.value / ppu, year=result.year,
                      corroborated=result.corroborated, note=result.note)
