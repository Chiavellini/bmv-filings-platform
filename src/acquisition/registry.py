"""Load the root-owned issuer and acquisition-source registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from src.acquisition.models import (
    AcquisitionSource,
    IssuerSpec,
    ProjectMembership,
)
from src.shared.paths import PROJECT_ROOT


DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "configs" / "issuers.yaml"
_PROJECTS = ("alpha_go", "soft", "earnings")
_SOURCE_FIELDS = {
    "key",
    "kind",
    "enabled",
    "url",
    "xbrl_ticker",
    "pdf_link_pattern",
    "direct_url_templates",
    "year_api_urls",
    "use_playwright",
    "max_reports",
    "floor_year",
    "coverage_from_period",
    "live_verified_period",
    "live_verified_on",
    "live_verified_url",
    "strict_pdf_link_pattern",
    "delay_ms",
    "impersonate",
}


class IssuerRegistryError(ValueError):
    """Raised when the canonical issuer registry is malformed."""


@dataclass(frozen=True, slots=True)
class IssuerRegistry:
    """Validated, immutable collection of canonical issuer specifications."""

    version: int
    issuers: tuple[IssuerSpec, ...]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise IssuerRegistryError("registry version must be positive")
        _validate_unique_identities(self.issuers)

    @property
    def by_slug(self) -> dict[str, IssuerSpec]:
        return {issuer.slug: issuer for issuer in self.issuers}

    @property
    def by_ticker(self) -> dict[str, IssuerSpec]:
        return {issuer.ticker.upper(): issuer for issuer in self.issuers}

    def get(self, slug: str) -> IssuerSpec:
        """Return an issuer by canonical slug."""

        try:
            return self.by_slug[slug.strip().lower()]
        except KeyError as exc:
            raise KeyError(f"unknown issuer slug: {slug!r}") from exc

    def find(self, identifier: str) -> IssuerSpec:
        """Resolve a slug, canonical BMV ticker, or configured market ticker."""

        token = identifier.strip()
        if not token:
            raise KeyError("issuer identifier cannot be empty")
        slug_match = self.by_slug.get(token.lower())
        if slug_match is not None:
            return slug_match
        ticker = token.upper()
        for issuer in self.issuers:
            if ticker == issuer.ticker.upper():
                return issuer
            if issuer.market_ticker and ticker == issuer.market_ticker.upper():
                return issuer
        raise KeyError(f"unknown issuer identifier: {identifier!r}")

    def for_project(self, project: str) -> tuple[IssuerSpec, ...]:
        """Return issuers enabled for one consuming subproject."""

        return tuple(issuer for issuer in self.issuers if issuer.is_member(project))

    def enabled_sources(
        self,
        *,
        project: str | None = None,
        kind: str | None = None,
    ) -> tuple[tuple[IssuerSpec, AcquisitionSource], ...]:
        """Return enabled issuer/source bindings, optionally filtered."""

        issuers: Iterable[IssuerSpec] = (
            self.for_project(project) if project is not None else self.issuers
        )
        return tuple(
            (issuer, source)
            for issuer in issuers
            for source in issuer.sources
            if source.enabled and (kind is None or source.kind == kind)
        )


def load_issuer_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> IssuerRegistry:
    """Load and validate the root canonical issuer registry."""

    registry_path = Path(path)
    try:
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise IssuerRegistryError(f"cannot read registry {registry_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise IssuerRegistryError(f"invalid YAML in {registry_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise IssuerRegistryError("registry root must be a mapping")

    version = _required_int(raw, "version", "registry")
    defaults = _mapping(raw.get("defaults", {}), "defaults")
    membership_defaults = _memberships(
        defaults.get("memberships", {}),
        base=ProjectMembership(),
        owner="defaults",
    )
    default_exchange = _optional_text(defaults.get("exchange"), "defaults.exchange") or "BMV"
    default_currency = _optional_text(defaults.get("currency"), "defaults.currency") or "MXN"

    groups = _mapping(raw.get("groups"), "groups")
    rows: list[dict[str, Any]] = []
    for sector, group_raw in groups.items():
        if not isinstance(sector, str) or not sector.strip():
            raise IssuerRegistryError("group names must be non-empty strings")
        group = _mapping(group_raw, f"group {sector}")
        template = _required_text(group, "template", f"group {sector}")
        members = group.get("issuers")
        if not isinstance(members, list) or not members:
            raise IssuerRegistryError(f"group {sector}: issuers must be a non-empty list")
        for index, member_raw in enumerate(members):
            member = dict(_mapping(member_raw, f"group {sector} issuer {index}"))
            member.setdefault("sector", sector)
            member.setdefault("template", template)
            rows.append(member)

    overlays = _mapping(raw.get("overlays", {}), "overlays")
    row_slugs = {
        _required_text(row, "slug", "issuer")
        for row in rows
    }
    unknown_overlays = sorted(set(overlays) - row_slugs)
    if unknown_overlays:
        raise IssuerRegistryError(
            f"overlays reference unknown issuer slugs: {', '.join(unknown_overlays)}"
        )
    membership_sets = _project_membership_sets(raw.get("memberships", {}), row_slugs)

    issuers: list[IssuerSpec] = []
    for row in rows:
        slug = _required_text(row, "slug", "issuer")
        overlay = _mapping(overlays.get(slug, {}), f"overlay {slug}")
        merged = _merge_issuer(row, overlay)
        try:
            issuer_membership_base = ProjectMembership(
                **{
                    project: (
                        slug in membership_sets[project]
                        or getattr(membership_defaults, project)
                    )
                    for project in _PROJECTS
                }
            )
            issuer = _issuer_from_raw(
                merged,
                default_exchange=default_exchange,
                default_currency=default_currency,
                default_memberships=issuer_membership_base,
            )
        except (TypeError, ValueError) as exc:
            raise IssuerRegistryError(f"{slug}: {exc}") from exc
        issuers.append(issuer)

    registry = IssuerRegistry(version=version, issuers=tuple(issuers))
    if not registry.issuers:
        raise IssuerRegistryError("registry must contain at least one issuer")
    return registry


def _issuer_from_raw(
    raw: Mapping[str, Any],
    *,
    default_exchange: str,
    default_currency: str,
    default_memberships: ProjectMembership,
) -> IssuerSpec:
    slug = _required_text(raw, "slug", "issuer")
    ticker = _required_text(raw, "ticker", slug).upper()
    memberships = _memberships(
        raw.get("memberships", {}),
        base=default_memberships,
        owner=slug,
    )
    sources = _sources_from_raw(raw, ticker=ticker, owner=slug)
    return IssuerSpec(
        slug=slug,
        ticker=ticker,
        name=_required_text(raw, "name", slug),
        sector=_required_text(raw, "sector", slug),
        template=_required_text(raw, "template", slug),
        exchange=_optional_text(raw.get("exchange"), f"{slug}.exchange") or default_exchange,
        language=_optional_text(raw.get("language"), f"{slug}.language"),
        currency=(
            _optional_text(raw.get("currency"), f"{slug}.currency") or default_currency
        ).upper(),
        unit=_optional_text(raw.get("unit"), f"{slug}.unit"),
        market_ticker=_optional_text(raw.get("market_ticker"), f"{slug}.market_ticker"),
        memberships=memberships,
        sources=sources,
        active=_optional_bool(raw.get("active"), f"{slug}.active", True),
        listed_from=_optional_date(raw.get("listed_from"), f"{slug}.listed_from"),
        listed_to=_optional_date(raw.get("listed_to"), f"{slug}.listed_to"),
        fiscal_year_end_month=(
            _nullable_int(
                raw.get("fiscal_year_end_month"), f"{slug}.fiscal_year_end_month"
            )
            if raw.get("fiscal_year_end_month") is not None
            else 12
        ),
        filing_grace_days=(
            _nullable_int(raw.get("filing_grace_days"), f"{slug}.filing_grace_days")
            if raw.get("filing_grace_days") is not None
            else 45
        ),
    )


def _sources_from_raw(
    raw: Mapping[str, Any],
    *,
    ticker: str,
    owner: str,
) -> tuple[AcquisitionSource, ...]:
    ir = _mapping(raw.get("ir", {}), f"{owner}.ir")
    xbrl_ticker = (
        _optional_text(raw.get("xbrl_ticker"), f"{owner}.xbrl_ticker")
        or _optional_text(ir.get("xbrl_ticker"), f"{owner}.ir.xbrl_ticker")
        or ticker
    )
    xbrl_enabled = _optional_bool(raw.get("xbrl_enabled"), f"{owner}.xbrl_enabled", True)
    sources = [
        AcquisitionSource(
            key="bmv_xbrl",
            kind="bmv_xbrl",
            enabled=xbrl_enabled,
            xbrl_ticker=xbrl_ticker,
        )
    ]
    if ir:
        sources.append(
            _source_from_mapping(
                ir,
                owner=f"{owner}.ir",
                default_key="ir",
                default_kind="investor_relations",
            )
        )

    extra_sources = raw.get("sources", [])
    if not isinstance(extra_sources, list):
        raise IssuerRegistryError(f"{owner}.sources must be a list")
    for index, source_raw in enumerate(extra_sources):
        source = _mapping(source_raw, f"{owner}.sources[{index}]")
        sources.append(
            _source_from_mapping(
                source,
                owner=f"{owner}.sources[{index}]",
                default_key=None,
                default_kind=None,
            )
        )
    return tuple(sources)


def _source_from_mapping(
    raw: Mapping[str, Any],
    *,
    owner: str,
    default_key: str | None,
    default_kind: str | None,
) -> AcquisitionSource:
    key = _optional_text(raw.get("key"), f"{owner}.key") or default_key
    kind = _optional_text(raw.get("kind"), f"{owner}.kind") or default_kind
    if key is None or kind is None:
        raise IssuerRegistryError(f"{owner}: key and kind are required")
    options = {key: value for key, value in raw.items() if key not in _SOURCE_FIELDS}
    return AcquisitionSource(
        key=key,
        kind=kind,
        enabled=_optional_bool(raw.get("enabled"), f"{owner}.enabled", True),
        url=_optional_text(raw.get("url"), f"{owner}.url"),
        xbrl_ticker=_optional_text(raw.get("xbrl_ticker"), f"{owner}.xbrl_ticker"),
        pdf_link_pattern=_optional_text(
            raw.get("pdf_link_pattern"), f"{owner}.pdf_link_pattern"
        ),
        direct_url_templates=_string_tuple(
            raw.get("direct_url_templates", []), f"{owner}.direct_url_templates"
        ),
        year_api_urls=_string_tuple(
            raw.get("year_api_urls", []), f"{owner}.year_api_urls"
        ),
        use_playwright=_nullable_bool(
            raw.get("use_playwright"), f"{owner}.use_playwright"
        ),
        max_reports=_nullable_int(raw.get("max_reports"), f"{owner}.max_reports"),
        floor_year=_nullable_int(raw.get("floor_year"), f"{owner}.floor_year"),
        coverage_from_period=_optional_text(
            raw.get("coverage_from_period"),
            f"{owner}.coverage_from_period",
        ),
        live_verified_period=_optional_text(
            raw.get("live_verified_period"),
            f"{owner}.live_verified_period",
        ),
        live_verified_on=_optional_date(
            raw.get("live_verified_on"),
            f"{owner}.live_verified_on",
        ),
        live_verified_url=_optional_text(
            raw.get("live_verified_url"),
            f"{owner}.live_verified_url",
        ),
        strict_pdf_link_pattern=_optional_bool(
            raw.get("strict_pdf_link_pattern"),
            f"{owner}.strict_pdf_link_pattern",
            False,
        ),
        delay_ms=_nullable_int(raw.get("delay_ms"), f"{owner}.delay_ms"),
        impersonate=_optional_text(raw.get("impersonate"), f"{owner}.impersonate"),
        options=options,
    )


def _merge_issuer(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if key == "memberships":
            memberships = dict(_mapping(merged.get("memberships", {}), "memberships"))
            memberships.update(_mapping(value, "memberships overlay"))
            merged[key] = memberships
        else:
            merged[key] = value
    return merged


def _validate_unique_identities(issuers: tuple[IssuerSpec, ...]) -> None:
    _raise_duplicates((issuer.slug for issuer in issuers), "slug", normalize=str.lower)
    _raise_duplicates((issuer.ticker for issuer in issuers), "ticker", normalize=str.upper)

    market_owners: dict[str, str] = {}
    for issuer in issuers:
        for ticker in {issuer.ticker, issuer.market_ticker} - {None}:
            normalized = ticker.upper()
            previous = market_owners.get(normalized)
            if previous is not None and previous != issuer.slug:
                raise IssuerRegistryError(
                    f"duplicate market ticker {normalized!r}: {previous}, {issuer.slug}"
                )
            market_owners[normalized] = issuer.slug


def _raise_duplicates(
    values: Iterable[str],
    label: str,
    *,
    normalize,
) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        normalized = normalize(value)
        if normalized in seen:
            duplicates.add(normalized)
        seen.add(normalized)
    if duplicates:
        rendered = ", ".join(sorted(duplicates))
        raise IssuerRegistryError(f"duplicate issuer {label}: {rendered}")


def _memberships(
    raw: object,
    *,
    base: ProjectMembership,
    owner: str,
) -> ProjectMembership:
    values = _mapping(raw, f"{owner}.memberships")
    unknown = sorted(set(values) - set(_PROJECTS))
    if unknown:
        raise IssuerRegistryError(
            f"{owner}.memberships has unknown projects: {', '.join(unknown)}"
        )
    resolved = {
        project: _optional_bool(
            values.get(project),
            f"{owner}.memberships.{project}",
            getattr(base, project),
        )
        for project in _PROJECTS
    }
    return ProjectMembership(**resolved)


def _project_membership_sets(
    raw: object,
    known_slugs: set[str],
) -> dict[str, set[str]]:
    values = _mapping(raw, "memberships")
    unknown_projects = sorted(set(values) - set(_PROJECTS))
    if unknown_projects:
        raise IssuerRegistryError(
            f"memberships has unknown projects: {', '.join(unknown_projects)}"
        )
    result: dict[str, set[str]] = {}
    for project in _PROJECTS:
        entries = values.get(project, [])
        if not isinstance(entries, list):
            raise IssuerRegistryError(f"memberships.{project} must be a list")
        slugs: list[str] = []
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                raise IssuerRegistryError(
                    f"memberships.{project} entries must be non-empty strings"
                )
            slugs.append(entry.strip())
        if len(slugs) != len(set(slugs)):
            raise IssuerRegistryError(f"memberships.{project} contains duplicate slugs")
        unknown_slugs = sorted(set(slugs) - known_slugs)
        if unknown_slugs:
            raise IssuerRegistryError(
                f"memberships.{project} references unknown issuers: "
                f"{', '.join(unknown_slugs)}"
            )
        result[project] = set(slugs)
    return result


def _mapping(raw: object, owner: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise IssuerRegistryError(f"{owner} must be a mapping")
    return raw


def _required_text(raw: Mapping[str, Any], key: str, owner: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IssuerRegistryError(f"{owner}.{key} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, owner: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise IssuerRegistryError(f"{owner} must be a non-empty string")
    return value.strip()


def _required_int(raw: Mapping[str, Any], key: str, owner: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise IssuerRegistryError(f"{owner}.{key} must be an integer")
    return value


def _nullable_int(value: object, owner: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise IssuerRegistryError(f"{owner} must be an integer")
    return value


def _optional_bool(value: object, owner: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise IssuerRegistryError(f"{owner} must be a boolean")
    return value


def _nullable_bool(value: object, owner: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise IssuerRegistryError(f"{owner} must be a boolean")
    return value


def _optional_date(value: object, owner: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if not 1800 <= value <= 2200:
            raise IssuerRegistryError(f"{owner} year is out of range")
        return date(value, 1, 1)
    if not isinstance(value, str):
        raise IssuerRegistryError(f"{owner} must be an ISO date or year")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IssuerRegistryError(f"{owner} must be an ISO date") from exc


def _string_tuple(value: object, owner: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise IssuerRegistryError(f"{owner} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise IssuerRegistryError(f"{owner} entries must be non-empty strings")
        result.append(item.strip())
    return tuple(result)
