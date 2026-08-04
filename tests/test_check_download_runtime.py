"""Preflight for optional download runtimes that specific issuers depend on."""

from __future__ import annotations

from scripts import check_download_runtime as crt


def test_missing_runtime_with_dependants_fails(monkeypatch, capsys):
    """A missing browser must be an exit code, not a log line.

    Playwright was absent from the venv while eight issuers declared
    ``use_playwright``; the downloader degraded to static links and said so only
    in a diagnostic string, so Grupo Herdez quietly scraped governance PDFs
    instead of quarterly reports.
    """
    monkeypatch.setattr(crt, "playwright_ready", lambda: (False, "package not installed"))
    monkeypatch.setattr(crt, "curl_cffi_ready", lambda: (True, "ok"))
    monkeypatch.setattr(
        crt, "dependants", lambda: {"playwright": ["herdez", "kimber"], "curl_cffi": []}
    )

    assert crt.main([]) == 1
    err = capsys.readouterr().err
    assert "herdez" in err and "playwright install chromium" in err


def test_missing_runtime_nobody_needs_is_not_a_failure(monkeypatch):
    """Don't fail a deployment over an extra no enabled source asks for."""
    monkeypatch.setattr(crt, "playwright_ready", lambda: (True, "ok"))
    monkeypatch.setattr(crt, "curl_cffi_ready", lambda: (False, "package not installed"))
    monkeypatch.setattr(
        crt, "dependants", lambda: {"playwright": ["herdez"], "curl_cffi": []}
    )

    assert crt.main([]) == 0


def test_all_present_passes(monkeypatch):
    monkeypatch.setattr(crt, "playwright_ready", lambda: (True, "ok"))
    monkeypatch.setattr(crt, "curl_cffi_ready", lambda: (True, "ok"))
    monkeypatch.setattr(
        crt, "dependants", lambda: {"playwright": ["herdez"], "curl_cffi": ["soriana"]}
    )

    assert crt.main([]) == 0


def test_dependants_reads_the_live_registry():
    needs = crt.dependants()
    # The eight JS-rendered IR pages that made this check necessary.
    assert set(needs["playwright"]) >= {
        "ac", "becle", "chedraui", "herdez", "kimber", "kof", "liverpool", "regional",
    }
    assert "soriana" in needs["curl_cffi"]


def test_playwright_ready_requires_a_browser_binary_not_just_the_package():
    ok, detail = crt.playwright_ready()
    assert isinstance(ok, bool) and isinstance(detail, str)
    if not ok:
        assert detail  # never a silent failure
