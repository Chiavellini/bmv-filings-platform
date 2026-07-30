#!/usr/bin/env python3
"""
llm_extract.py — Tier 4: LLM fallback for metrics the deterministic tiers miss.

This is the last resort in the cascade: only the residual metrics that XBRL
facts, table cells, and regex all failed to produce are sent to the model
(`claude-haiku-4-5`, with a JSON-schema-constrained response). It is OFF by
default — gated behind ``use_llm`` / ``--llm`` and an ``ANTHROPIC_API_KEY`` — and
every returned value is screened before acceptance:

  1. Anti-hallucination gate: the model must quote source text it can locate;
     an ``evidence`` snippet that isn't a substring of the document is dropped.
  2. Validator cross-check: a candidate that breaks an accounting identity it
     participates in (gross-profit, EBITDA, net-debt, …) is dropped.

Surviving values are tagged ``[llm]`` so validator.score_confidence scores them
at the lowest tier (0.3, or 0.5 if they pass an applicable cross-check). The
deterministic tiers always win — Tier 4 only fills gaps.

Responses are cached (content-addressed under ``.llm_cache/``) so repeated and
test runs are deterministic and free.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

from src.model.financial_model import MetricDef
from src.shared.validator import validate, _IDENTITY_RULES

MODEL = "claude-haiku-4-5"
_CACHE_DIR = Path(".llm_cache")

def _system_prompt(cfg: dict | None = None) -> str:
    """Unit- and currency-aware system prompt built from ``company.unit``/``currency``.

    The unit clause must match the company config: a millions-unit company
    (herdez, soriana, gmexico, orbia) prompted for thousands would produce
    values 1000x off that the downstream gates cannot always catch.
    """
    from src.extract.xbrl_facts import iso_currency_for, pesos_per_unit_for

    ppu = pesos_per_unit_for(cfg)
    currency = iso_currency_for(cfg) or "MXN"
    if ppu >= 1e6:
        unit_clause = (
            f"CURRENCY values must be returned in MILLIONS of {currency} "
            "(millones), matching the financial-statement tables: e.g. if the "
            'text says "$213.9 millones", return 213.9. '
        )
    else:
        unit_clause = (
            f"CURRENCY values must be returned in THOUSANDS of {currency} "
            "(miles), matching the financial-statement tables: e.g. if the text "
            'says "$213.9 millones", return 213900; if a table row shows '
            '"589,053", return 589053. '
        )
    return (
        "You extract financial metrics from a Mexican company's quarterly report "
        "(text is Spanish or English). Return only values you can locate verbatim in "
        "the provided text; use null when a metric is not stated. Do not infer, "
        "compute, or estimate. " + unit_clause +
        "Percentages and counts are returned as their plain number. For each "
        "metric you fill, include a short verbatim 'evidence' quote copied exactly from "
        "the text."
    )


# ---------------------------------------------------------------------------
# Schema + prompt
# ---------------------------------------------------------------------------

def _build_schema(defs: list[MetricDef]) -> dict:
    props = {}
    for m in defs:
        props[m.key] = {
            "type": ["object", "null"],
            "properties": {
                "current": {"type": ["number", "null"]},
                "prior": {"type": ["number", "null"]},
                "evidence": {"type": "string"},
            },
            "required": ["current", "evidence"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": props,
        "required": [m.key for m in defs],
        "additionalProperties": False,
    }


def _metric_brief(defs: list[MetricDef]) -> str:
    lines = []
    for m in defs:
        lines.append(f"- {m.key} ({m.unit}): {m.label_es} / {m.label}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def _cache_key(model: str, keys: list[str], text: str, system: str = "") -> str:
    # ``system`` participates so a unit/currency prompt change (company.unit)
    # can never serve a cached answer produced under a different scale.
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    system_hash = hashlib.sha256(system.encode("utf-8")).hexdigest()
    payload = f"{model}|{','.join(sorted(keys))}|{text_hash}|{system_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_get(cache_dir: Path, key: str) -> dict | None:
    path = cache_dir / f"{key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _cache_put(cache_dir: Path, key: str, data: dict) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{key}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        print(f"llm_extract: cache write failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Model call (the only place that touches the network)
# ---------------------------------------------------------------------------

def _anthropic_complete(*, model: str, system: str, user: str, schema: dict) -> dict:
    import anthropic  # lazy import: keeps the dependency optional + tests offline

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=[{"type": "text", "text": system}],
        messages=[{
            "role": "user",
            "content": [{
                "type": "text",
                "text": user,
                "cache_control": {"type": "ephemeral"},  # large document → cache
            }],
        }],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    if getattr(response, "stop_reason", None) == "refusal":
        return {}
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Evidence gate
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def _evidence_present(evidence: str, normalized_text: str) -> bool:
    ev = _normalize(evidence or "")
    if len(ev) < 4:        # too short to be a meaningful, locatable quote
        return False
    return ev in normalized_text


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def llm_extract(
    missing_defs: list[MetricDef],
    text: str,
    already_found: dict,
    *,
    cfg: dict | None = None,
    model: str = MODEL,
    cache_dir: Path = _CACHE_DIR,
    complete_fn=None,
) -> dict:
    """Tier 4: {metric_key: MetricRow} for residual metrics, screened for trust.

    Returns {} when there is nothing to ask, the text is empty, or no
    ANTHROPIC_API_KEY is set (and no test ``complete_fn`` was injected).
    """
    from src.extract.extract_metrics import MetricRow   # local: avoid import cycle

    defs = [m for m in missing_defs if m.key not in already_found]
    if not defs or not text.strip():
        return {}
    if complete_fn is None and not os.environ.get("ANTHROPIC_API_KEY"):
        print("llm_extract: ANTHROPIC_API_KEY not set; skipping Tier 4.", file=sys.stderr)
        return {}

    keys = [m.key for m in defs]
    system = _system_prompt(cfg)
    key = _cache_key(model, keys, text, system)
    data = _cache_get(cache_dir, key)
    if data is None:
        fn = complete_fn or _anthropic_complete
        user = (
            "Extract these metrics from the report text below.\n\n"
            f"METRICS:\n{_metric_brief(defs)}\n\n=== REPORT TEXT ===\n{text}"
        )
        data = fn(model=model, system=system, user=user, schema=_build_schema(defs))
        _cache_put(cache_dir, key, data or {})

    normalized_text = _normalize(text)
    by_key = {m.key: m for m in defs}
    candidates: dict = {}
    for k, entry in (data or {}).items():
        mdef = by_key.get(k)
        if not mdef or not isinstance(entry, dict):
            continue
        current = entry.get("current")
        if current is None:
            continue
        if not _evidence_present(entry.get("evidence", ""), normalized_text):
            continue   # anti-hallucination: unlocatable quote
        candidates[k] = MetricRow(
            metric=k,
            label_es=mdef.label_es,
            current=float(current),
            prior=float(entry["prior"]) if entry.get("prior") is not None else None,
            var_pct=None,
            unit=mdef.unit,
            source_line=f"[llm] {str(entry.get('evidence', ''))[:80]}".strip(),
        )

    return _drop_failing_crosschecks(candidates, already_found)


def _drop_failing_crosschecks(candidates: dict, already_found: dict) -> dict:
    """Remove candidates that break an accounting identity they participate in."""
    if not candidates:
        return candidates
    merged = {**already_found, **candidates}
    failed = {r.rule for r in validate(merged) if not r.passed}
    if not failed:
        return candidates
    kept = {}
    for k, row in candidates.items():
        breaks_rule = any(
            rule in failed and k in members
            for rule, members in _IDENTITY_RULES.items()
        )
        if not breaks_rule:
            kept[k] = row
        else:
            print(f"llm_extract: dropped {k} (failed cross-check)", file=sys.stderr)
    return kept
