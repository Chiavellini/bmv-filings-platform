"""One resolver for "where do this company's reports come from".

Three config surfaces grew up describing the same thing with the same field
names:

1. ``configs/<slug>.yaml`` → ``ir_website:`` — 24 hand-tuned per-company files,
   read by the extraction pipeline and ``scripts/fetch_company_reports.py``.
2. ``configs/issuers.yaml`` → ``overlays.<slug>.ir`` — the same 24 bindings
   again, read by the acquisition engine.
3. ``configs/issuers.yaml`` → ``groups.*`` — the canonical 179-issuer roster,
   which carries a ``ticker`` for every issuer and nothing else.

Callers used to read exactly one of them, so a company without a per-company
file had no source at all — even though the roster knows its ticker, and a
ticker is all the BMV XBRL archive needs. ``resolve_company_source`` merges the
three, most specific first, so every roster issuer resolves to *something*.

The important consequence: ``ir_url`` is optional. An issuer with only a roster
entry still gets ``xbrl_ticker``, which is enough for
``src.extract.pipeline._from_bmv_xbrl`` to cover ~2021 onward from the
regulator's own filings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.shared.paths import CONFIGS_DIR


__all__ = [
    "CompanySource",
    "UnknownCompanyError",
    "resolve_company_source",
]


class UnknownCompanyError(KeyError):
    """Raised when a slug matches neither a per-company config nor the roster."""


@dataclass(frozen=True)
class CompanySource:
    """Everything the download layers need to fetch one company's reports."""

    slug: str
    ticker: str | None = None
    name: str | None = None
    #: IR page to crawl. ``None`` means "no IR binding" — not an error; the
    #: XBRL archive still applies.
    ir_url: str | None = None
    pdf_link_pattern: str | None = None
    strict_pdf_link_pattern: bool = False
    use_playwright: bool | None = None
    browser_first: bool = False
    year_api_urls: tuple[str, ...] = ()
    direct_url_templates: tuple[str, ...] = ()
    impersonate: str | None = None
    delay_ms: int | None = None
    max_reports: int | None = None
    floor_year: int | None = None
    xbrl_ticker: str | None = None
    #: First fiscal quarter the issuer could have reported (``YYYY-nT``),
    #: derived from the registry's ``listed_from``. Bounds "expected periods"
    #: so a 2021 IPO does not report twenty phantom gaps.
    coverage_from_period: str | None = None
    #: First quarter the *IR source* is known to cover. This is a property of
    #: the binding, not of the issuer: the onboarding compiler sets it to the
    #: oldest quarter it actually verified. It must never be used as the
    #: expected-period floor — doing so would declare a company with one
    #: verified quarter to be at 100% coverage.
    ir_coverage_from_period: str | None = None
    #: Which surfaces contributed, most specific first. Diagnostic only.
    provenance: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_ir_binding(self) -> bool:
        return bool(self.ir_url)

    @property
    def has_xbrl(self) -> bool:
        return bool(self.xbrl_ticker)

    def ir_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``src.download.downloader.download_from_ir``.

        Only keys with a resolved value are included, so the downloader's own
        defaults still apply to everything unset.
        """
        kwargs: dict[str, Any] = {}
        if self.pdf_link_pattern:
            kwargs["file_pattern"] = self.pdf_link_pattern
        if self.delay_ms is not None:
            kwargs["delay_ms"] = int(self.delay_ms)
        if self.use_playwright is not None:
            kwargs["use_playwright"] = bool(self.use_playwright)
        if self.year_api_urls:
            kwargs["year_api_urls"] = list(self.year_api_urls)
        if self.browser_first:
            kwargs["browser_first"] = True
        if self.impersonate:
            kwargs["impersonate"] = self.impersonate
        return kwargs


# Fields copied verbatim from an ``ir_website:`` / ``ir:`` mapping. The two
# surfaces use identical names, which is what makes this a merge and not a
# translation.
_IR_TEXT_FIELDS = ("pdf_link_pattern", "impersonate", "xbrl_ticker")
_IR_INT_FIELDS = ("delay_ms", "max_reports", "floor_year")
_IR_BOOL_FIELDS = ("browser_first", "strict_pdf_link_pattern")
_IR_LIST_FIELDS = ("year_api_urls", "direct_url_templates")


def _as_tuple(value: object) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if item)
    return ()


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _layer_from_ir_mapping(ir: dict, *, url_key: str = "url") -> dict[str, Any]:
    """Normalize one ``ir_website:``/``ir:`` mapping into CompanySource fields."""
    layer: dict[str, Any] = {}
    url = ir.get(url_key)
    if isinstance(url, str) and url.strip():
        layer["ir_url"] = url.strip()
    for name in _IR_TEXT_FIELDS:
        value = ir.get(name)
        if isinstance(value, str) and value.strip():
            layer[name] = value.strip()
    for name in _IR_INT_FIELDS:
        parsed = _as_int(ir.get(name))
        if parsed is not None:
            layer[name] = parsed
    for name in _IR_BOOL_FIELDS:
        if ir.get(name) is not None:
            layer[name] = bool(ir[name])
    for name in _IR_LIST_FIELDS:
        values = _as_tuple(ir.get(name))
        if values:
            layer[name] = values
    # Tri-state: True / False / None(auto-fallback). Only None means "unset".
    if ir.get("use_playwright") is not None:
        layer["use_playwright"] = bool(ir["use_playwright"])
    return layer


def _per_company_layer(slug: str, configs_dir: Path) -> dict[str, Any] | None:
    """Layer 1 — ``configs/<slug>.yaml``, the hand-tuned per-company file."""
    path = configs_dir / f"{slug}.yaml"
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    ir = raw.get("ir_website")
    layer = _layer_from_ir_mapping(ir if isinstance(ir, dict) else {})
    company = raw.get("company")
    if isinstance(company, dict):
        name = company.get("name")
        if isinstance(name, str) and name.strip():
            layer["name"] = name.strip()
    return layer


def _registry_layers(slug: str, registry_path: Path | None) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Layers 2 and 3 — the ``ir:`` overlay and the roster entry.

    Returns ``(overlay_layer, roster_layer)``, or ``None`` when the slug is not
    in the registry at all.
    """
    from src.acquisition.registry import (
        DEFAULT_REGISTRY_PATH,
        IssuerRegistryError,
        load_issuer_registry,
    )

    try:
        registry = load_issuer_registry(registry_path or DEFAULT_REGISTRY_PATH)
    except (IssuerRegistryError, OSError):
        return None

    # ``data/reports/`` is keyed by the alpha-go ticker slug for 19 issuers
    # (``amx``, ``gcarso``, ``fmty``, …) while the registry uses the canonical
    # slug (``america_movil``, ``grupo_carso``, ``fibra_mty``). Try the whole
    # alias group so either spelling resolves.
    issuer = None
    for candidate in _identity_candidates(slug):
        try:
            issuer = registry.get(candidate)
            break
        except KeyError:
            continue
    if issuer is None:
        return None

    roster: dict[str, Any] = {"ticker": issuer.ticker, "name": issuer.name}
    if issuer.listed_from is not None:
        roster["coverage_from_period"] = _period_for_date(issuer.listed_from)

    overlay: dict[str, Any] = {}
    for source in issuer.sources:
        if source.kind == "bmv_xbrl" and source.enabled and source.xbrl_ticker:
            # The registry defaults this to the roster ticker for every issuer,
            # which is precisely why an unconfigured company still resolves.
            roster.setdefault("xbrl_ticker", source.xbrl_ticker)
        elif source.kind == "investor_relations" and source.enabled:
            overlay.update(
                _layer_from_ir_mapping(
                    {
                        "url": source.url,
                        "pdf_link_pattern": source.pdf_link_pattern,
                        "strict_pdf_link_pattern": source.strict_pdf_link_pattern,
                        "use_playwright": source.use_playwright,
                        "year_api_urls": list(source.year_api_urls),
                        "direct_url_templates": list(source.direct_url_templates),
                        "impersonate": source.impersonate,
                        "delay_ms": source.delay_ms,
                        "max_reports": source.max_reports,
                        "floor_year": source.floor_year,
                        "xbrl_ticker": source.xbrl_ticker,
                    }
                )
            )
            if source.coverage_from_period:
                # Deliberately NOT coverage_from_period: this describes how far
                # back the binding was verified, not when the issuer listed.
                overlay["ir_coverage_from_period"] = source.coverage_from_period
    return overlay, roster


def _identity_candidates(slug: str) -> tuple[str, ...]:
    """``slug`` plus every configured alias for the same issuer."""
    try:
        from src.shared.company_aliases import expand_company_aliases

        expanded = expand_company_aliases(slug)
    except Exception:  # noqa: BLE001 — a malformed alias file must not break resolution
        return (slug,)
    # Keep the caller's own spelling first, then the canonical identity.
    return (slug, *(name for name in expanded if name != slug))


def _period_for_date(value) -> str:
    """Calendar quarter containing ``value``, as ``YYYY-nT``."""
    return f"{value.year}-{(value.month - 1) // 3 + 1}T"


def resolve_company_source(
    slug: str,
    *,
    configs_dir: Path | None = None,
    registry_path: Path | None = None,
) -> CompanySource:
    """Merge every config surface that describes ``slug``, most specific first.

    Raises :class:`UnknownCompanyError` only when the slug appears in none of
    them — a company with a roster entry but no IR binding resolves fine and
    simply has ``ir_url is None``.
    """
    configs_dir = Path(configs_dir) if configs_dir is not None else CONFIGS_DIR

    per_company = _per_company_layer(slug, configs_dir)
    registry_layers = _registry_layers(slug, registry_path)

    if per_company is None and registry_layers is None:
        raise UnknownCompanyError(
            f"{slug!r} has no configs/{slug}.yaml and no entry in the issuer registry"
        )

    overlay, roster = registry_layers if registry_layers is not None else ({}, {})

    # Least specific first so more specific layers overwrite.
    merged: dict[str, Any] = {}
    provenance: list[str] = []
    for label, layer in (
        ("roster", roster),
        ("registry_overlay", overlay),
        ("per_company_config", per_company or {}),
    ):
        if layer:
            merged.update({k: v for k, v in layer.items() if v not in (None, (), "")})
            provenance.append(label)

    # A ticker is enough for the regulator source; fall back to it when no
    # surface named an explicit xbrl_ticker.
    if not merged.get("xbrl_ticker") and merged.get("ticker"):
        merged["xbrl_ticker"] = merged["ticker"]

    merged.pop("url", None)
    return CompanySource(slug=slug, provenance=tuple(provenance), **merged)
