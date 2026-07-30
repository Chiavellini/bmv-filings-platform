"""Tests for the Cloudflare-aware curl_cffi impersonation fallback in downloader.py.

Some IR hosts (e.g. organizacionsoriana.com) sit behind a Cloudflare TLS-fingerprint
bot wall that 403s plain HTTP clients. The downloader detects such a block and retries
the fetch with a browser TLS fingerprint via curl_cffi, or jumps straight to it when a
host is flagged with ``impersonate`` in its config. These tests exercise that routing
without hitting the network (``_impersonate_get`` / ``_impersonate_download`` are
monkeypatched) so they never depend on curl_cffi being installed.
"""
import pytest
import requests

from src.download import downloader
from src.download.downloader import _CurlResponse, _is_cloudflare_block


class _Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, headers=None, text="", cookies=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.cookies = cookies or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(str(self.status_code), response=self)


# ---------------------------------------------------------------------------
# _is_cloudflare_block detection
# ---------------------------------------------------------------------------

def test_cf_block_detected_via_server_header():
    assert _is_cloudflare_block(_Resp(403, headers={"Server": "cloudflare"})) is True


def test_cf_block_detected_via_cf_ray_header():
    assert _is_cloudflare_block(_Resp(503, headers={"cf-ray": "a1b2c3"})) is True


def test_cf_block_detected_via_cf_bm_cookie():
    assert _is_cloudflare_block(_Resp(429, cookies={"__cf_bm": "x"})) is True


def test_cf_block_detected_via_body_marker():
    body = "<h1>Sorry, you have been blocked</h1>"
    assert _is_cloudflare_block(_Resp(403, text=body)) is True


def test_ordinary_403_is_not_cf_block():
    assert _is_cloudflare_block(_Resp(403, text="Forbidden")) is False


def test_404_is_not_cf_block():
    assert _is_cloudflare_block(_Resp(404, headers={"Server": "cloudflare"})) is False


def test_200_from_cloudflare_is_not_a_block():
    assert _is_cloudflare_block(_Resp(200, headers={"Server": "cloudflare"})) is False


def test_none_response_is_not_cf_block():
    assert _is_cloudflare_block(None) is False


# ---------------------------------------------------------------------------
# _session_get routing
# ---------------------------------------------------------------------------

class _Session:
    """Session whose .get returns a canned response (or raises)."""

    def __init__(self, response=None, exc=None):
        self.headers = {}
        self._response = response
        self._exc = exc
        self.get_calls = 0

    def get(self, url, **kwargs):
        self.get_calls += 1
        if self._exc is not None:
            raise self._exc
        return self._response


def test_session_get_reactive_falls_back_on_cf_block(monkeypatch):
    captured = {}

    def fake_impersonate_get(url, **kwargs):
        captured["url"] = url
        captured["profile"] = kwargs.get("profile")
        return _CurlResponse(text="IMPERSONATED", url=url, status_code=200)

    monkeypatch.setattr(downloader, "_impersonate_get", fake_impersonate_get)

    session = _Session(response=_Resp(403, headers={"Server": "cloudflare"}, text="blocked"))
    resp = downloader._session_get(session, "https://x.mx/r", timeout=30, verify_ssl=True)

    assert resp.text == "IMPERSONATED"
    assert captured["url"] == "https://x.mx/r"
    assert captured["profile"] == downloader._DEFAULT_IMPERSONATE
    assert session.get_calls == 1  # tried requests once before falling back


def test_session_get_proactive_skips_requests(monkeypatch):
    def fake_impersonate_get(url, **kwargs):
        return _CurlResponse(text="IMPERSONATED", url=url, status_code=200)

    monkeypatch.setattr(downloader, "_impersonate_get", fake_impersonate_get)

    session = _Session()
    session._impersonate_profile = "chrome"
    resp = downloader._session_get(session, "https://x.mx/r", timeout=30, verify_ssl=True)

    assert resp.text == "IMPERSONATED"
    assert session.get_calls == 0  # requests path skipped entirely


def test_session_get_ordinary_403_does_not_impersonate(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not impersonate on a non-Cloudflare 403")

    monkeypatch.setattr(downloader, "_impersonate_get", boom)

    session = _Session(response=_Resp(403, text="Forbidden"))
    with pytest.raises(requests.exceptions.HTTPError):
        downloader._session_get(session, "https://x.mx/r", timeout=30, verify_ssl=True)


def test_session_get_tls_error_still_uses_curl(monkeypatch):
    """The pre-existing TLS->curl fallback must be unaffected by the CF path."""
    def fake_curl_get(session, url, **kwargs):
        return _CurlResponse(text="VIA_CURL", url=url, status_code=200)

    def boom(*a, **k):
        raise AssertionError("TLS errors must not route to impersonation")

    monkeypatch.setattr(downloader, "_curl_get", fake_curl_get)
    monkeypatch.setattr(downloader, "_impersonate_get", boom)

    exc = requests.exceptions.SSLError("tlsv1 alert protocol version")
    session = _Session(exc=exc)
    resp = downloader._session_get(session, "https://x.mx/r", timeout=30, verify_ssl=True)
    assert resp.text == "VIA_CURL"


# ---------------------------------------------------------------------------
# _download_pdf routing
# ---------------------------------------------------------------------------

_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


def test_download_pdf_reactive_falls_back_on_cf_block(monkeypatch, tmp_path):
    def fake_impersonate_download(url, dest, **kwargs):
        dest.write_bytes(_PDF_BYTES)

    monkeypatch.setattr(downloader, "_impersonate_download", fake_impersonate_download)

    session = _Session(response=_Resp(403, headers={"Server": "cloudflare"}, text="blocked"))
    dest = tmp_path / "2024-1T.pdf"
    out = downloader._download_pdf(session, "https://x.mx/r.pdf", dest)

    assert out == dest
    assert dest.read_bytes() == _PDF_BYTES


def test_download_pdf_proactive_skips_requests(monkeypatch, tmp_path):
    def fake_impersonate_download(url, dest, **kwargs):
        dest.write_bytes(_PDF_BYTES)

    monkeypatch.setattr(downloader, "_impersonate_download", fake_impersonate_download)

    session = _Session()
    session._impersonate_profile = "chrome"
    dest = tmp_path / "2024-1T.pdf"
    out = downloader._download_pdf(session, "https://x.mx/r.pdf", dest)

    assert out == dest
    assert session.get_calls == 0


def test_download_pdf_ordinary_403_does_not_impersonate(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("must not impersonate on a non-Cloudflare 403")

    monkeypatch.setattr(downloader, "_impersonate_download", boom)

    session = _Session(response=_Resp(403, text="Forbidden"))
    with pytest.raises(requests.exceptions.HTTPError):
        downloader._download_pdf(session, "https://x.mx/r.pdf", tmp_path / "x.pdf")
