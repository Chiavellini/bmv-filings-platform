"""Native macro fetcher — free Mexican macro series (no Bloomberg).

- **Banxico SIE** (policy rate, USDMXN FIX, INPC inflation): JSON API, free token via
  ``BANXICO_SIE_TOKEN`` (register at banxico.org.mx). Header ``Bmx-Token``.
- **INEGI** (GDP growth): BIE indicator API, free token via ``INEGI_TOKEN``.

Series ids live in ``configs/soft.yaml::macro_sources`` keyed by the macro row key used in the spec
(``gdp_growth``/``policy_rate``/``usdmxn``/``inflation``). A missing token or unmapped key is skipped
gracefully → that macro cell falls to the Bloomberg residual.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.shared.paths import PROJECT_ROOT

_CACHE = PROJECT_ROOT / ".cache" / "macro"
_BANXICO_URL = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/{ids}/datos/oportuno"
_INEGI_URL = ("https://www.inegi.org.mx/app/api/indicadores/desarrolladores/jsonxml/INDICATOR/"
              "{ind}/es/0700/false/BIE/2.0/{token}?type=json")


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _get(url: str, verify_ssl: bool, headers: dict | None = None) -> str | None:
    from src.download.downloader import _make_session, _session_get
    try:
        resp = _session_get(_make_session(extra_headers=headers), url, timeout=20,
                            verify_ssl=verify_ssl, extra_headers=headers)
        return resp.text if hasattr(resp, "text") else str(resp)
    except Exception:
        return None


def fetch_banxico(series_id: str, token: str, *, verify_ssl: bool = True) -> float | None:
    """Latest value of a Banxico SIE series (e.g. SF61745 policy rate, SF43718 USDMXN FIX)."""
    _CACHE.mkdir(parents=True, exist_ok=True)
    cache = _CACHE / f"banxico_{series_id}.json"
    text = cache.read_text(encoding="utf-8") if cache.exists() and cache.stat().st_size > 20 else None
    if text is None:
        text = _get(_BANXICO_URL.format(ids=series_id), verify_ssl, {"Bmx-Token": token})
        if text:
            cache.write_text(text, encoding="utf-8")
    if not text:
        return None
    try:
        series = json.loads(text)["bmx"]["series"][0]["datos"]
        return _num(series[-1]["dato"]) if series else None
    except (KeyError, IndexError, ValueError):
        return None


def fetch_inegi(indicator_id: str, token: str, *, verify_ssl: bool = True) -> float | None:
    """Latest value of an INEGI BIE indicator (e.g. GDP growth)."""
    _CACHE.mkdir(parents=True, exist_ok=True)
    cache = _CACHE / f"inegi_{indicator_id}.json"
    text = cache.read_text(encoding="utf-8") if cache.exists() and cache.stat().st_size > 20 else None
    if text is None:
        text = _get(_INEGI_URL.format(ind=indicator_id, token=token), verify_ssl)
        if text:
            cache.write_text(text, encoding="utf-8")
    if not text:
        return None
    try:
        obs = json.loads(text)["Series"][0]["OBSERVATIONS"]
        return _num(obs[-1]["OBS_VALUE"]) if obs else None
    except (KeyError, IndexError, ValueError):
        return None


def fetch_macro(macro_keys: list[str], sources: dict, *, verify_ssl: bool = True) -> dict:
    """Resolve the requested macro keys from Banxico/INEGI. ``sources`` maps each key to
    ``{provider: banxico|inegi, id: <series/indicator id>}`` (from configs/soft.yaml).
    Returns {key: value} for those that resolved (missing token/id → skipped)."""
    bmx_token = os.environ.get("BANXICO_SIE_TOKEN", "")
    inegi_token = os.environ.get("INEGI_TOKEN", "")
    out: dict = {}
    for key in macro_keys:
        src = sources.get(key) or {}
        provider, sid = src.get("provider"), src.get("id")
        if not sid:
            continue
        if provider == "banxico" and bmx_token:
            v = fetch_banxico(sid, bmx_token, verify_ssl=verify_ssl)
        elif provider == "inegi" and inegi_token:
            v = fetch_inegi(sid, inegi_token, verify_ssl=verify_ssl)
        else:
            v = None
        if v is not None:
            out[key] = v
    return out
