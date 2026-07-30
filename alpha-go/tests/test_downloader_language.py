"""Tests for English-only link filtering in the downloader (Phase 1)."""
from __future__ import annotations

import re

from src.download.downloader import (
    _PdfAnchor,
    _build_lang_spec,
    _filter_anchors_by_language,
    _is_english_only,
    _LANG_FILTER,
    _select_pdf_links,
)


def _anchor(url, filename="", text=""):
    return _PdfAnchor(url=url, filename=filename, text=text)


# Representative mixed-language anchor set (the kind a bilingual IR site yields).
EN_ANCHORS = [
    _anchor("https://co.com/en/q1-2024-results.pdf", "q1-2024-results.pdf", "First Quarter 2024 Results"),
    _anchor("https://co.com/files/1Q24_earnings_release.pdf", "1Q24_earnings_release.pdf", "Earnings Release"),
    _anchor("https://co.com/en/reports/2023-q4.pdf", "2023-q4.pdf", "Report"),
]
ES_ANCHORS = [
    _anchor("https://co.com/es/reporte-1T24.pdf", "reporte-1T24.pdf", "Reporte Trimestral 1T24"),
    _anchor("https://co.com/files/informe_resultados_2023.pdf", "informe_resultados_2023.pdf", "Informe de Resultados"),
    _anchor("https://co.com/comunicado-trimestre.pdf", "comunicado-trimestre.pdf", "Comunicado"),
]


def test_is_english_only():
    assert _is_english_only("en")
    assert _is_english_only("English")
    assert not _is_english_only("es")
    assert not _is_english_only(None)


def test_build_lang_spec():
    assert _build_lang_spec("es", None, None) is None       # Spanish → no-op
    assert _build_lang_spec(None, None, None) is None        # untagged → no-op
    en = _build_lang_spec("en", None, None)
    assert en == {"include": None, "exclude": None}          # heuristic on
    ov = _build_lang_spec("es", r"/en/", r"/es/")            # explicit override wins even for es
    assert ov["include"].search("/en/") and ov["exclude"].search("/es/")


def test_filter_keeps_english_drops_spanish():
    spec = _build_lang_spec("en", None, None)
    kept = _filter_anchors_by_language(EN_ANCHORS + ES_ANCHORS, spec)
    urls = {a.url for a in kept}
    assert urls == {a.url for a in EN_ANCHORS}               # all EN kept, all ES dropped


def test_filter_noop_when_spec_none():
    both = EN_ANCHORS + ES_ANCHORS
    assert _filter_anchors_by_language(both, None) is both   # Spanish/untagged unaffected


def test_filter_keeps_bilingual_under_en_path():
    # A Spanish-worded filename under an /en/ path has both signals → kept (EN wins).
    a = _anchor("https://co.com/en/reporte-1T24.pdf", "reporte-1T24.pdf", "Reporte")
    spec = _build_lang_spec("en", None, None)
    assert _filter_anchors_by_language([a], spec) == [a]


def test_include_exclude_override():
    spec = _build_lang_spec(None, r"/en/", None)
    kept = _filter_anchors_by_language(EN_ANCHORS + ES_ANCHORS, spec)
    assert all("/en/" in a.url for a in kept)
    assert {a.url for a in kept} == {a.url for a in EN_ANCHORS if "/en/" in a.url}


def test_select_pdf_links_applies_contextvar_filter():
    token = _LANG_FILTER.set(_build_lang_spec("en", None, None))
    try:
        links = _select_pdf_links(EN_ANCHORS + ES_ANCHORS, None)
        assert links  # found english reports
        assert all("/es/" not in u and "reporte" not in u and "informe" not in u
                   and "comunicado" not in u for u in links)
    finally:
        _LANG_FILTER.reset(token)


def test_select_pdf_links_no_filter_by_default():
    # Without an active spec, Spanish anchors are selectable (back-compat).
    links = _select_pdf_links(ES_ANCHORS, None)
    assert links


def test_download_context_is_restored_between_companies():
    from src.download.downloader import _DOC_KIND, _scoped_download_context

    @_scoped_download_context
    def probe(*, language=None, lang_include=None, lang_exclude=None, doc_kind="quarterly"):
        return _LANG_FILTER.get(), _DOC_KIND.get()

    outer_lang = _LANG_FILTER.get()
    outer_kind = _DOC_KIND.get()
    active_lang, active_kind = probe(language="en", doc_kind="annual")
    assert active_lang == {"include": None, "exclude": None}
    assert active_kind == "annual"
    assert _LANG_FILTER.get() == outer_lang
    assert _DOC_KIND.get() == outer_kind
