"""llm_crosscheck.py — advisory second-model verification of extracted metrics.

Check #2 of the three-check robustness model (deterministic engine → independent
LLM cross-check → manual verification). Unlike the Tier-4 fallback
(``llm_extract``), which only fills metrics the engine MISSED, this module
re-extracts the metrics the engine already PRODUCED — independently, by label,
with no regex patterns required — and compares the two values. That makes it
the only accuracy signal available on zero-shot companies with no ground truth:
the **two-model agreement rate**.

Strictly ADVISORY: results ride along in ``conf_map`` / the validation report
and never change a gate verdict (see test_llm_crosscheck.py's advisory
guarantee). Default provider is DeepSeek (``deepseek-chat``) via the
OpenAI-compatible path in ``llm_providers``; degrades silently to "not run"
when no API key resolves.

Screening mirrors llm_extract: values must carry a verbatim ``evidence`` quote
locatable in the document (anti-hallucination); unlocatable or null answers are
recorded as *uncheckable* (``agree: None``), never as disagreement.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from src.extract.llm_extract import _evidence_present, _normalize, _system_prompt
from src.extract.llm_providers import ProviderConfig, complete_json, resolve_provider

_CACHE_DIR = Path(".llm_cache/crosscheck")

# unit -> (relative tolerance, absolute floor in the metric's own units).
# Mirrors compare_extractions.TOLERANCE/ABS_FLOOR semantics: agree when within
# EITHER bound. pct is absolute percentage points; count is exact.
_AGREE_TOL: dict[str, tuple[float, float]] = {
    "currency": (0.02, 0.5),
    "area": (0.005, 0.5),
    "pct": (0.0, 0.5),
    "count": (0.0, 0.0),
    "ratio": (0.02, 0.01),
    "per_share": (0.02, 0.01),
}


def values_agree(engine: float, llm: float, unit: str) -> bool:
    """True when the two independent readings match within the unit's tolerance."""
    rel, floor = _AGREE_TOL.get(unit, (0.02, 0.5))
    diff = abs(float(engine) - float(llm))
    if unit == "pct":
        return diff <= floor
    if unit == "count":
        return diff == 0
    if diff <= floor:
        return True
    base = max(abs(float(engine)), abs(float(llm)))
    return base > 0 and (diff / base) <= rel


def _cache_key(model: str, keys: list[str], text: str, system: str) -> str:
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    system_hash = hashlib.sha256(system.encode("utf-8")).hexdigest()
    payload = f"crosscheck|{model}|{','.join(sorted(keys))}|{text_hash}|{system_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def crosscheck_metrics(
    found: dict,                    # {key: MetricRow} — the engine's output for one period
    text: str,
    cfg: dict | None = None,
    *,
    provider: ProviderConfig | None = None,
    cache_dir: Path = _CACHE_DIR,
    complete_fn=None,               # test seam: (system, user) -> dict
    max_keys: int = 40,
) -> dict[str, dict]:
    """Cross-check one period's extracted metrics; return {key: result}.

    Result shape: ``{"model": str, "value": float|None, "agree": bool|None,
    "engine_value": float, "evidence": str}``. ``agree is None`` means the model
    could not check the cell (null answer or unlocatable evidence) — callers
    must not count that as disagreement. Returns ``{}`` when the check cannot
    run at all (no provider key, empty text, nothing to check).
    """
    rows = {k: r for k, r in found.items()
            if getattr(r, "current", None) is not None}
    if not rows or not text.strip():
        return {}
    if complete_fn is None and provider is None:
        provider = resolve_provider((cfg or {}).get("llm_crosscheck"))
        if provider is None:
            return {}
    model = provider.model if provider else "test"

    keys = sorted(rows)[:max_keys]
    system = _system_prompt(cfg) + (
        " You are cross-checking previously extracted figures; you are NOT shown "
        "the extracted values. Answer with a single JSON object mapping each "
        "requested key to {\"current\": <number or null>, \"evidence\": \"<verbatim quote>\"}."
    )
    brief = "\n".join(
        f"- {k} ({rows[k].unit}): {rows[k].label_es}" for k in keys
    )
    user = (
        "Find each metric below in the report text. Return ONLY the JSON object.\n\n"
        f"METRICS:\n{brief}\n\n=== REPORT TEXT ===\n{text}"
    )

    key = _cache_key(model, keys, text, system)
    data = None
    cache_path = cache_dir / f"{key}.json"
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
    if data is None:
        try:
            if complete_fn is not None:
                data = complete_fn(system=system, user=user) or {}
            else:
                data = complete_json(provider, system=system, user=user) or {}
        except Exception as exc:  # advisory: never break the pipeline
            print(f"llm_crosscheck: call failed ({exc}); skipping.", file=sys.stderr)
            return {}
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            print(f"llm_crosscheck: cache write failed: {exc}", file=sys.stderr)

    normalized_text = _normalize(text)
    out: dict[str, dict] = {}
    for k in keys:
        row = rows[k]
        entry = data.get(k) if isinstance(data, dict) else None
        result = {"model": model, "engine_value": float(row.current),
                  "value": None, "agree": None, "evidence": ""}
        if isinstance(entry, dict) and entry.get("current") is not None:
            evidence = str(entry.get("evidence") or "")
            if _evidence_present(evidence, normalized_text):
                try:
                    llm_val = float(entry["current"])
                except (TypeError, ValueError):
                    llm_val = None
                if llm_val is not None:
                    result["value"] = llm_val
                    result["evidence"] = evidence[:120]
                    result["agree"] = values_agree(row.current, llm_val, row.unit)
        out[k] = result
    return out


def agreement_summary(checks: dict) -> dict:
    """Aggregate {(period, key): result} → {checked, agreed, disagreed, unchecked, rate}."""
    agreed = sum(1 for r in checks.values() if r.get("agree") is True)
    disagreed = sum(1 for r in checks.values() if r.get("agree") is False)
    unchecked = sum(1 for r in checks.values() if r.get("agree") is None)
    checked = agreed + disagreed
    return {
        "checked": checked,
        "agreed": agreed,
        "disagreed": disagreed,
        "unchecked": unchecked,
        "rate": (agreed / checked) if checked else None,
    }
