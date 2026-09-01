"""Metric contracts promoted from the analyst's existing Segments models.

The base financial registry remains universal.  This module layers the
company-specific rows already certified by the update pipeline onto that base
without making one company's metrics the global default.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from src.model.financial_model import MetricDef, apply_config


CATALOG_PATH = Path(__file__).resolve().parents[2] / "configs" / "analyst_model_metrics.yaml"


@lru_cache(maxsize=1)
def load_analyst_model_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    """Return the validated company -> model-metric catalog."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if payload.get("version") != 1 or not isinstance(payload.get("companies"), dict):
        raise ValueError(f"invalid analyst model metric catalog: {path}")
    return payload


def analyst_model_company_slugs() -> tuple[str, ...]:
    return tuple(sorted(load_analyst_model_catalog()["companies"]))


def analyst_model_spec(company_slug: str) -> dict[str, Any]:
    value = load_analyst_model_catalog()["companies"].get(company_slug, {})
    return value if isinstance(value, dict) else {}


def analyst_model_extractor(company_slug: str) -> str | None:
    value = analyst_model_spec(company_slug).get("extractor")
    return str(value).strip() if value else None


def analyst_model_metric_keys(company_slug: str) -> tuple[str, ...]:
    """Return the company rows owned by the supplemental Segments extractor."""
    rows = analyst_model_spec(company_slug).get("metrics") or []
    return tuple(
        str(row["key"])
        for row in rows
        if isinstance(row, dict) and row.get("key")
    )


def apply_analyst_model_metrics(
    metrics: Iterable[MetricDef], company_slug: str
) -> list[MetricDef]:
    """Add only missing rows known from one of the analyst's models."""
    current = list(metrics)
    existing = {metric.key for metric in current}
    rows = analyst_model_spec(company_slug).get("metrics") or []
    missing = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("key") or row["key"] in existing:
            continue
        normalized = dict(row)
        normalized.setdefault("section", "segment")
        normalized.setdefault("label_es", normalized.get("label", normalized["key"]))
        missing.append(normalized)
    if not missing:
        return current
    return apply_config(current, {"custom_metrics": missing})


__all__ = [
    "CATALOG_PATH",
    "analyst_model_company_slugs",
    "analyst_model_extractor",
    "analyst_model_metric_keys",
    "analyst_model_spec",
    "apply_analyst_model_metrics",
    "load_analyst_model_catalog",
]
