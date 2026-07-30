"""Parse ``inputs/<slug>.md`` — the coverage spec that drives a coverage workbook.

The grammar is deliberately close to the parent's segment-outline markdown (``## Section``
headers + ``- item {key}`` lines) so it feels native, but the recognised section names are
valuation-specific. Unknown sections are ignored (forward-compatible with future sheets).

Example (``inputs/walmex.md``):

    # Walmex
    Ticker: WALMEX* MM
    IR: https://www.walmex.mx/en/financial-information/quarterly.html

    ## Settings
    - currency: MXN
    - units: millions
    - history_years: 5

    ## Blocks
    - snapshot_multiples
    - historical_multiples
    - sum_of_the_parts
    - replacement_value
    - financial_analysis
    - macro_sector

    ## Peers
    - CHDRAUI* MM {chedraui}
    - SORIANA B MM {soriana}

    ## Segments
    - Mexico {mexico}
    - Central America {cam}

    ## Macro
    - Mexico GDP growth, % {gdp_growth}
    - Banxico policy rate, % {policy_rate}
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Default block sets per template. `spec.blocks` (an explicit `## Blocks` section) overrides these.
# The industrial default holds only blocks fully fillable from XBRL + Bloomberg. `sum_of_the_parts`
# (needs per-segment EBITDA, which XBRL doesn't break out) and retail-specific `replacement_value`
# are opt-in — add them via an explicit `## Blocks` list once segment inputs exist.
INDUSTRIAL_BLOCKS = [
    "snapshot_multiples",
    "historical_multiples",
    "financial_analysis",
    "macro_sector",
]
# Deeper financial-analysis blocks (docs/ANALYSIS_METRICS.md). Auto-appended to the industrial/
# default template so the whole industrial universe gets them without editing every input file.
# Banks/REITs keep their existing blocks (these are not in their template defaults). A spec opts
# out with `analysis_blocks: off` in its `## Settings`.
ANALYSIS_BLOCKS = [
    "profitability",
    "fcf_liquidity",
    "temporal_ebit",
    "growth",
]
FINANCIALS_BLOCKS = [
    "bank_snapshot",
    "bank_returns",
    "bank_growth",
    "bank_historical",
    # Shared analytical block so a bank row also carries net margin, DuPont, and the universal P/BV in
    # the cross-company matrix (EBITDA/leverage cells stay blank — genuinely N/A for a bank).
    "financial_analysis",
    "macro_sector",
]
# Every block id the engine knows how to render (used to validate an explicit `## Blocks` list).
# Includes the opt-in blocks (sum_of_the_parts, replacement_value) not in any template default.
KNOWN_BLOCKS = (INDUSTRIAL_BLOCKS + ANALYSIS_BLOCKS
                + ["sum_of_the_parts", "replacement_value"] + FINANCIALS_BLOCKS)

# REITs / FIBRAs — FFO/NAV/distribution driven (reit_* blocks) PLUS the shared analytical stack, so a
# Fibra also carries the universal valuation/profitability/growth metrics computed from its own XBRL
# (revenue/NI/equity/assets all extract). The reit_* blocks stay for FFO/NAV; the shared blocks fill
# the cross-company matrix (P/E, EV/EBITDA, P/BV, ROE, margins, CAGRs, liquidity).
REIT_BLOCKS = [
    "reit_snapshot",
    "reit_metrics",
    "reit_historical",
    "snapshot_multiples",
    "financial_analysis",
    "historical_multiples",
    "macro_sector",
]
KNOWN_BLOCKS += REIT_BLOCKS

# Templates → their default block set.
TEMPLATES = {
    "industrial": INDUSTRIAL_BLOCKS,
    "financials": FINANCIALS_BLOCKS,
    "reit": REIT_BLOCKS,
}

_KEY_RE = re.compile(r"\{([a-z0-9_]+)\}\s*$", re.IGNORECASE)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_ITEM_RE = re.compile(r"^\s*-\s+(.+?)\s*$")
_KV_RE = re.compile(r"^([a-z0-9_ ]+?)\s*:\s*(.+?)\s*$", re.IGNORECASE)


@dataclass
class Peer:
    slug: str
    ticker: str
    name: str


@dataclass
class Labelled:
    """A generic key + display label (used for segments and macro rows)."""
    key: str
    label: str


@dataclass
class CoverageSpec:
    slug: str
    name: str
    ticker: str | None = None
    ir_url: str | None = None
    currency: str = "MXN"
    units: str = "millions"
    history_years: int = 5
    template: str = "industrial"
    peer_currency: str = ""   # "mixed" → suppress absolute cross-company comparisons (revenue share)
    timeseries_years: int = 0  # opt-in Bloomberg annual time-series depth (0 = disabled; see schema)
    blocks: list[str] = field(default_factory=lambda: list(INDUSTRIAL_BLOCKS))
    peers: list[Peer] = field(default_factory=list)
    segments: list[Labelled] = field(default_factory=list)
    macro: list[Labelled] = field(default_factory=list)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def _split_key(line: str) -> tuple[str, str | None]:
    """Return (label, key) for an item line; key is the trailing ``{key}`` if present."""
    m = _KEY_RE.search(line)
    if not m:
        return line.strip(), None
    key = m.group(1).lower()
    label = line[: m.start()].strip()
    return label, key


def parse_spec(path: str | Path) -> CoverageSpec:
    """Parse a coverage-spec markdown file into a :class:`CoverageSpec`."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines()

    name: str | None = None
    ticker: str | None = None
    ir_url: str | None = None
    settings: dict[str, str] = {}
    blocks: list[str] = []
    peers: list[Peer] = []
    segments: list[Labelled] = []
    macro: list[Labelled] = []

    section: str | None = None  # normalized current ## section

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue

        h1 = _H1_RE.match(line)
        if h1 and name is None and not line.startswith("##"):
            name = h1.group(1).strip()
            continue

        h2 = _H2_RE.match(line)
        if h2:
            section = _slugify(h2.group(1))
            continue

        # Pre-section header key:value lines (Ticker:, IR:)
        if section is None:
            kv = _KV_RE.match(line)
            if kv:
                k, v = kv.group(1).strip().lower(), kv.group(2).strip()
                if k == "ticker":
                    ticker = v
                elif k in ("ir", "ir_url", "ir website"):
                    ir_url = v
            continue

        item = _ITEM_RE.match(line)
        if not item:
            continue
        body = item.group(1).strip()

        if section == "settings":
            kv = _KV_RE.match(body)
            if kv:
                settings[kv.group(1).strip().lower()] = kv.group(2).strip()
        elif section == "blocks":
            blk = _slugify(body)
            if blk in KNOWN_BLOCKS:
                blocks.append(blk)
        elif section == "peers":
            label, key = _split_key(body)
            slug = key or _slugify(label)
            peers.append(Peer(slug=slug, ticker=label, name=label))
        elif section == "segments":
            label, key = _split_key(body)
            segments.append(Labelled(key=key or _slugify(label), label=label))
        elif section == "macro":
            label, key = _split_key(body)
            macro.append(Labelled(key=key or _slugify(label), label=label))
        # unknown sections: ignored (forward-compatible)

    if name is None:
        raise ValueError(f"{p}: missing '# Company' H1 title")

    slug = _slugify(name)
    history_years = int(settings.get("history_years", 5))
    template = settings.get("template", "industrial").strip().lower()
    if template not in TEMPLATES:
        template = "industrial"
    # Explicit `## Blocks` wins; otherwise use the template's default block set.
    default_blocks = TEMPLATES[template]
    resolved_blocks = blocks or list(default_blocks)
    # Auto-append the deeper analysis blocks to the industrial/default template (banks/REITs keep
    # theirs unchanged) unless the spec opts out via `analysis_blocks: off`. This gives the whole
    # industrial universe the new blocks without editing every inputs/<slug>.md.
    analysis_opt = settings.get("analysis_blocks", "").strip().lower()
    if template in ("industrial", "reit", "financials") and analysis_opt not in ("off", "none", "false", "0", "no"):
        for b in ANALYSIS_BLOCKS:
            if b not in resolved_blocks:
                resolved_blocks.append(b)

    try:
        timeseries_years = max(0, int(settings.get("timeseries_years", 0)))
    except (TypeError, ValueError):
        timeseries_years = 0

    return CoverageSpec(
        slug=slug,
        name=name,
        ticker=ticker,
        ir_url=ir_url,
        currency=settings.get("currency", "MXN"),
        units=settings.get("units", "millions"),
        history_years=history_years,
        template=template,
        peer_currency=settings.get("peer_currency", "").strip().lower(),
        timeseries_years=timeseries_years,
        blocks=resolved_blocks,
        peers=peers,
        segments=segments,
        macro=macro,
    )
