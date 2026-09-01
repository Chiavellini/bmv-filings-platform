"""Resolve an Estate issuer identity to its optional extraction config."""
from __future__ import annotations

from pathlib import Path
import re


def _candidate(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def resolve_company_config_slug(project_root: Path, issuer_slug: str) -> str:
    """Return the config slug while preserving the canonical Estate slug.

    Most issuers use the same slug in both places.  A few historical model
    configs use the exchange ticker instead (for example ``tiendas_3b`` in the
    Estate and ``tbbb.yaml`` in the model layer).
    """
    config_dir = project_root / "configs"
    if (config_dir / f"{issuer_slug}.yaml").is_file():
        return issuer_slug

    from src.acquisition.registry import load_issuer_registry

    registry = load_issuer_registry(config_dir / "issuers.yaml")
    try:
        issuer = registry.get(issuer_slug)
    except KeyError:
        return issuer_slug
    for value in (issuer.market_ticker, issuer.ticker):
        candidate = _candidate(value or "")
        if candidate and (config_dir / f"{candidate}.yaml").is_file():
            return candidate
    return issuer_slug


__all__ = ["resolve_company_config_slug"]
