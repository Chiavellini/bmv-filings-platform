"""Shared LLM provider layer — provider/key resolution and cache (all offline)."""
from __future__ import annotations

import pytest

from src.qa import llm, llm_cache


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Neutralize the .env loader so tests see only the env vars they set explicitly."""
    monkeypatch.setattr(llm, "load_env", lambda: None)


def test_resolve_anthropic_default(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.resolve_provider({}) is None                      # no key → None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    p = llm.resolve_provider({})
    assert p is not None and p.kind == "anthropic"
    assert p.base_url is None and p.model == "claude-haiku-4-5" and p.api_key == "k"


def test_resolve_deepseek(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    p = llm.resolve_provider({"provider": "deepseek"})
    assert p is not None
    assert p.kind == "openai"                                    # OpenAI-compatible SDK path
    assert p.provider == "deepseek"
    assert p.base_url == "https://api.deepseek.com"
    assert p.model == "deepseek-chat" and p.api_key == "sk-test"


def test_resolve_missing_key_returns_none(monkeypatch):
    monkeypatch.delenv("NOPE_KEY_XYZ", raising=False)
    # A provider whose configured key env var is unset resolves to None (callers fall back).
    assert llm.resolve_provider({"provider": "deepseek", "api_key_env": "NOPE_KEY_XYZ"}) is None


def test_resolve_respects_overrides(monkeypatch):
    monkeypatch.setenv("MY_KEY", "k")
    p = llm.resolve_provider({"provider": "deepseek", "model": "deepseek-reasoner",
                              "base_url": "https://example.test", "api_key_env": "MY_KEY"})
    assert p.model == "deepseek-reasoner" and p.base_url == "https://example.test"


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_cache, "_CACHE_ROOT", tmp_path)
    assert llm_cache.get("sentiment", "m", "text") is None       # miss
    llm_cache.put("sentiment", "m", "text", {"label": "positive"})
    assert llm_cache.get("sentiment", "m", "text") == {"label": "positive"}
    # keyed by (model, text): a different model is a distinct entry
    assert llm_cache.get("sentiment", "other", "text") is None
