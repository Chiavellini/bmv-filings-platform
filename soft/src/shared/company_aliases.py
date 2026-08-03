"""Validated company-slug aliases shared by estate producers and consumers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypeAlias

import yaml

from src.shared.paths import CONFIGS_DIR


DEFAULT_COMPANY_ALIASES_PATH = CONFIGS_DIR / "company_aliases.yaml"

CompanyAliases: TypeAlias = dict[str, tuple[str, ...]]


class CompanyAliasConfigError(ValueError):
    """Raised when ``configs/company_aliases.yaml`` is malformed."""


def load_company_aliases(
    path: str | Path | None = None,
) -> CompanyAliases:
    """Load the canonical-slug-to-aliases mapping in deterministic order.

    Canonical slugs and aliases must be normalized, non-empty strings. An
    identity may occur in exactly one alias group, which keeps expansion
    unambiguous for every producer and consumer of the document estate.
    """

    config_path = Path(path) if path is not None else DEFAULT_COMPANY_ALIASES_PATH
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CompanyAliasConfigError(
            f"cannot read company aliases {config_path}: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise CompanyAliasConfigError(
            f"invalid YAML in company aliases {config_path}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise CompanyAliasConfigError("company aliases root must be a mapping")
    unknown_fields = sorted(str(key) for key in raw if key != "canonical")
    if unknown_fields:
        raise CompanyAliasConfigError(
            "company aliases root has unknown fields: " + ", ".join(unknown_fields)
        )

    canonical_raw = raw.get("canonical")
    if not isinstance(canonical_raw, dict):
        raise CompanyAliasConfigError(
            "company aliases 'canonical' field must be a mapping"
        )

    canonical_names: set[str] = set()
    for value in canonical_raw:
        canonical_names.add(_normalized_slug(value, "canonical slug"))

    parsed: dict[str, tuple[str, ...]] = {}
    alias_owners: dict[str, str] = {}
    for raw_canonical, raw_aliases in canonical_raw.items():
        canonical = _normalized_slug(raw_canonical, "canonical slug")
        if not isinstance(raw_aliases, list):
            raise CompanyAliasConfigError(
                f"aliases for {canonical!r} must be a list"
            )

        aliases: list[str] = []
        seen: set[str] = set()
        for index, value in enumerate(raw_aliases):
            alias = _normalized_slug(value, f"alias {index} for {canonical!r}")
            if alias == canonical:
                raise CompanyAliasConfigError(
                    f"alias {alias!r} duplicates its canonical slug"
                )
            if alias in canonical_names:
                raise CompanyAliasConfigError(
                    f"alias {alias!r} is also a canonical slug"
                )
            if alias in seen:
                raise CompanyAliasConfigError(
                    f"duplicate alias {alias!r} for canonical slug {canonical!r}"
                )
            owner = alias_owners.get(alias)
            if owner is not None:
                raise CompanyAliasConfigError(
                    f"alias {alias!r} belongs to both {owner!r} and {canonical!r}"
                )
            seen.add(alias)
            alias_owners[alias] = canonical
            aliases.append(alias)

        parsed[canonical] = tuple(sorted(aliases))

    return {canonical: parsed[canonical] for canonical in sorted(parsed)}


def expand_company_aliases(
    slug: str,
    aliases_by_canonical: Mapping[str, Sequence[str]] | None = None,
    *,
    path: str | Path | None = None,
) -> tuple[str, ...]:
    """Return a slug's canonical identity followed by all configured aliases.

    ``slug`` may itself be canonical or an alias. Unknown normalized slugs
    expand to a one-item tuple containing themselves.
    """

    candidate = _normalized_slug(slug, "company slug")
    aliases = (
        load_company_aliases(path)
        if aliases_by_canonical is None
        else aliases_by_canonical
    )

    canonical = candidate
    if candidate not in aliases:
        for owner, owner_aliases in aliases.items():
            if candidate in owner_aliases:
                canonical = owner
                break

    configured = aliases.get(canonical)
    if configured is None:
        return (candidate,)
    return (canonical, *tuple(configured))


def _normalized_slug(value: object, owner: str) -> str:
    if not isinstance(value, str) or not value:
        raise CompanyAliasConfigError(f"{owner} must be a non-empty string")
    normalized = value.strip().lower()
    if not normalized:
        raise CompanyAliasConfigError(f"{owner} must be a non-empty string")
    if value != normalized:
        raise CompanyAliasConfigError(
            f"{owner} must be normalized lowercase without surrounding whitespace"
        )
    return normalized
