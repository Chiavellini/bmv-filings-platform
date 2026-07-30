"""Shared LLM provider chokepoint — Anthropic and OpenAI-compatible (DeepSeek).

One ``complete()`` seam so both the sentiment refine and the RAG Q&A route through a single,
monkeypatchable call instead of three bare ``anthropic.Anthropic()`` constructions. Providers
are lazy-imported and optional — nothing here runs unless a caller passes a resolved
:class:`ProviderConfig` (which requires a real API key in the environment).

``resolve_provider(block)`` reads a config block (the ``qa`` or ``sentiment`` section), fills in
per-provider defaults (base URL, model, key env var), pulls the key from the environment, and
returns ``None`` when no key is present so callers degrade to the offline path / "enable" hint.

DeepSeek exposes an OpenAI-compatible API, so it shares the ``openai`` SDK path with a
``base_url`` override.

Note: the actual DeepSeek call is exercised live in ``analyze(..., refine=True)``; unit tests
monkeypatch :func:`complete` so nothing here touches the network under ``pytest``.
"""
from __future__ import annotations

import os
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

    ``block`` is a ``qa`` or ``sentiment`` section. Recognized keys: ``provider`` (default
    ``anthropic``), ``model``, ``base_url``, ``api_key_env``. The API key is read from the
    environment (``.env`` is loaded first, without overriding already-set vars). An unknown
    provider name is treated as OpenAI-compatible — it then needs an explicit ``base_url``.
    Does **not** check any ``enabled``/``engine`` gate; the caller owns that.
    """
    load_env()
    block = block or {}
    name = str(block.get("provider") or "anthropic").lower()
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


def complete(provider: ProviderConfig, *, system: str, user: str, max_tokens: int) -> str:
    """Single LLM call for either SDK path. Raises on SDK/network error (callers catch)."""
    if provider.kind == "anthropic":
        import anthropic

        kwargs = {"api_key": provider.api_key}
        if provider.base_url:
            kwargs["base_url"] = provider.base_url
        client = anthropic.Anthropic(**kwargs)
        msg = client.messages.create(
            model=provider.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(getattr(b, "text", "") for b in msg.content).strip()

    # OpenAI-compatible (DeepSeek / OpenAI)
    import openai

    client = openai.OpenAI(api_key=provider.api_key, base_url=provider.base_url)
    resp = client.chat.completions.create(
        model=provider.model, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return (resp.choices[0].message.content or "").strip()
