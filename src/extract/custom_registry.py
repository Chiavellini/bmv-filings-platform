"""Single dispatch registry for deterministic company extractors."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


STATEMENT_TIER = "statement"


def scope_custom_rows(rows: dict, cfg: dict | None) -> dict:
    """Keep supplemental outputs without hiding their calculation inputs."""
    keys = set((cfg or {}).get("_custom_extractor_metric_keys") or ())
    if not keys:
        return rows
    return {key: row for key, row in rows.items() if key in keys}


@dataclass(frozen=True, slots=True)
class CustomExtractor:
    module: str
    function: str
    wants_period: bool = True
    wants_pdf_path: bool = True


CUSTOM_EXTRACTORS: dict[str, CustomExtractor] = {
    "ac": CustomExtractor("src.extract.ac", "extract_ac"),
    "becle": CustomExtractor("src.extract.becle", "extract_becle_segments", wants_pdf_path=False),
    "femsa": CustomExtractor("src.extract.femsa", "extract_femsa"),
    "gmexico": CustomExtractor("src.extract.gmexico", "extract_gmexico"),
    "gruma": CustomExtractor("src.extract.gruma", "extract_gruma_appendix", False, False),
    "herdez": CustomExtractor("src.extract.herdez", "extract_herdez"),
    "kimber": CustomExtractor("src.extract.kimber", "extract_kimber"),
    "kof": CustomExtractor("src.extract.kof", "extract_kof"),
    "lab": CustomExtractor("src.extract.lab", "extract_lab_release", True, False),
    "liverpool": CustomExtractor("src.extract.liverpool", "extract_liverpool_release", True, False),
    "orbia": CustomExtractor("src.extract.orbia", "extract_orbia_release", True, False),
    "soriana": CustomExtractor("src.extract.soriana", "extract_soriana"),
    "tbbb": CustomExtractor("src.extract.tbbb", "extract_tbbb"),
}


def resolve(slug: str) -> CustomExtractor:
    try:
        return CUSTOM_EXTRACTORS[slug]
    except KeyError as exc:
        known = ", ".join(sorted(CUSTOM_EXTRACTORS))
        raise KeyError(f"unknown custom extractor {slug!r}; known: {known}") from exc


def run(slug: str, text: str, metric_defs: list, *, period=None, pdf_path=None) -> Any:
    spec = resolve(slug)
    function = getattr(importlib.import_module(spec.module), spec.function)
    args = [text, metric_defs]
    if spec.wants_period:
        args.append(period)
    kwargs = {"pdf_path": pdf_path} if spec.wants_pdf_path else {}
    return function(*args, **kwargs)


__all__ = [
    "CUSTOM_EXTRACTORS",
    "CustomExtractor",
    "STATEMENT_TIER",
    "resolve",
    "run",
    "scope_custom_rows",
]
