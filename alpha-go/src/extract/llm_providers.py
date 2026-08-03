"""Shared LLM provider chokepoint — Anthropic and OpenAI-compatible (DeepSeek).

One seam so the Tier-4 fallback and the advisory cross-check route through a single,
monkeypatchable call. Providers are lazy-imported and optional — nothing here runs unless a
caller passes a resolved :class:`ProviderConfig` (which requires a real API key in the
environment).

``resolve_provider(block)`` reads a config block (e.g. the ``llm_crosscheck`` section), fills in
per-provider defaults (base URL, model, key env var), pulls the key from the environment, and
returns ``None`` when no key is present so callers degrade silently to "not run".

DeepSeek exposes an OpenAI-compatible API, so it shares the ``openai`` SDK path with a
``base_url`` override. ``complete_json`` is the structured path: OpenAI-compatible providers
use ``response_format={"type": "json_object"}`` (the prompt MUST mention "JSON" per the API
contract); Anthropic uses a plain call and relies on the caller's JSON-only instruction.

Adapted from ``alpha-go/src/qa/llm.py`` (keep the preset table in sync).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from src.shared.env import load_env

# preset -> (kind, base_url, default_key_env, default_model)
# ``kind`` selects the SDK path; ``anthropic`` uses the Messages API, everything else the
# OpenAI-compatible Chat Completions API.
_PRESETS: dict[str, tuple[str, str | None, str, str]] = {
    "anthropic": ("anthropic", None, "ANTHROPIC_API_KEY", "claude-haiku-4-5"),
    "deepseek": ("openai", "https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "openai": ("openai", None, "OPENAI_API_KEY", "gpt-4o-mini"),
}


@dataclass(frozen=True)
class ProviderConfig:
    kind: str            # "anthropic" | "openai"  (SDK path)
    provider: str        # the configured name, e.g. "deepseek"
    model: str
    base_url: str | None
    api_key: str


def resolve_provider(block: dict | None) -> ProviderConfig | None:
    """Resolve provider/model/base_url/key from a config block, or ``None`` if unkeyed.

    Recognized keys: ``provider`` (default ``deepseek`` — this module exists for the
    cross-check), ``model``, ``base_url``, ``api_key_env``. The API key is read from the
    environment (``.env`` is loaded first, without overriding already-set vars). An unknown
    provider name is treated as OpenAI-compatible — it then needs an explicit ``base_url``.
    Does **not** check any ``enabled`` gate; the caller owns that.
    """
    load_env()
    block = block or {}
    name = str(block.get("provider") or "deepseek").lower()
    preset = _PRESETS.get(name)
    if preset is not None:
        kind, base_url, key_env, model = preset
    else:                                   # unknown → assume OpenAI-compatible
        kind, base_url, key_env, model = "openai", None, "OPENAI_API_KEY", ""

    base_url = block.get("base_url", base_url)
    key_env = block.get("api_key_env", key_env)
    model = block.get("model") or model
    api_key = os.environ.get(key_env)
    if not api_key or not model:
        return None
    return ProviderConfig(kind=kind, provider=name, model=model, base_url=base_url,
                          api_key=api_key)


def complete_json(provider: ProviderConfig, *, system: str, user: str,
                  max_tokens: int = 2048, temperature: float | None = None) -> dict:
    """One LLM call that must yield a JSON object; returns ``{}`` on unparseable output.

    Raises on SDK/network error (callers catch). The ``system``/``user`` text should
    instruct the model to answer with a single JSON object (and must contain the word
    "JSON" for the OpenAI-compatible ``json_object`` response format to be accepted).

    ``temperature`` is omitted from the request unless a caller passes one, so the
    existing extraction callers keep their provider-default sampling untouched. Study
    callers that must be reproducible (earnings' AI reader) pin it to 0.
    """
    extra = {} if temperature is None else {"temperature": temperature}
    if provider.kind == "anthropic":
        import anthropic

        kwargs = {"api_key": provider.api_key}
        if provider.base_url:
            kwargs["base_url"] = provider.base_url
        client = anthropic.Anthropic(**kwargs)
        msg = client.messages.create(
            model=provider.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}], **extra,
        )
        text = "".join(getattr(b, "text", "") for b in msg.content).strip()
    else:
        import openai

        client = openai.OpenAI(api_key=provider.api_key, base_url=provider.base_url)
        resp = client.chat.completions.create(
            model=provider.model, max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}], **extra,
        )
        text = (resp.choices[0].message.content or "").strip()
    return _parse_json_object(text)


def _parse_json_object(text: str) -> dict:
    """Best-effort parse of a JSON object from model output (handles ```json fences)."""
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}
