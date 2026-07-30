"""Tests for keeping non-report documents out of quarterly-report slots.

Two layers:
  * Conference-call invitations carry a period in their name but are never the report, so
    they are excluded outright by ``_QR_EXCLUDE_RE`` (downloader) — this fixes KOF, whose
    2016-4T / 2019-4T slots were filled by ``KOF-Conference-Call-4Q-….pdf``.
  * Re-transmission notices / material-event filings ("eventos relevantes") may *be* the
    real report (e.g. an audited "cifras dictaminadas" re-transmission), so they are NOT
    dropped — the Wayback recovery just tries them last, so a plain ``/reportes/`` report
    wins when both are archived for a period (this fixes soriana 2016-4T / 2018-1T).
"""
from src.download.downloader import (
    _PdfAnchor,
    _is_excluded_report_asset,
    _looks_like_quarterly_report,
)


def _anchor(name: str, url: str = "") -> _PdfAnchor:
    return _PdfAnchor(url=url or f"https://x.mx/{name}", filename=name, text="")


# Conference-call invitations — excluded outright (never a report).
INVITE_CULPRITS = [
    "KOF-Conference-Call-4Q-2016.pdf",
    "Coca-Cola-FEMSA-KOF-Conference-Call-4Q-2019.pdf",
    "Coca-Cola-FEMSA-KOF-Conferencia-Telefonica-4T-2019.pdf",
    "KOF_Invitation_Conference_Call 1Q15_eng.pdf",
    "KOF_Invitación _Call_2T15_esp.pdf",
    "Grupo-Bimbo-1Q19-Conference-Call-Invite.pdf",
]

# Real reports that must never be excluded by the regex (the no-over-exclusion invariant).
REAL_REPORTS = [
    "2017_02_17_Resultados_Trimestrales_4T16_IR.pdf",
    "2018_04_27InformedelDirector1T18.pdf",
    "1Q24_InfoDir_Eng_Vf.pdf",
    "4th-Quarter-2016-Results.pdf",
    "3Q25_InfDir_InglesVfinal.pdf",
    # An audited re-transmission can be the genuine report — must NOT be regex-excluded.
    "gsw_retransmision_reporte_trimestral_4t19_cifras_dictaminadas.pdf",
    "gsw_resultados_2019_1T.pdf",
]


def test_invite_culprits_are_excluded():
    for name in INVITE_CULPRITS:
        assert _is_excluded_report_asset(_anchor(name)), name
        assert not _looks_like_quarterly_report(_anchor(name)), name


def test_real_reports_not_excluded():
    # The key invariant: the exclude regex must not match real reports — including audited
    # re-transmissions, which are handled by de-prioritisation in Wayback, not exclusion.
    for name in REAL_REPORTS:
        assert not _is_excluded_report_asset(_anchor(name)), name


def test_clean_reports_pass_quarterly_filter_with_realistic_url():
    # Served from a '/reportes/' path, which supplies the report signal.
    for name in ["2017_02_17_Resultados_Trimestrales_4T16_IR.pdf", "1Q24_InfoDir_Eng_Vf.pdf"]:
        a = _PdfAnchor(url=f"https://x.mx/pdf/reportes/es/2016/{name}", filename=name, text="")
        assert _looks_like_quarterly_report(a), name


def test_wayback_prefers_real_report_over_notice(monkeypatch):
    """A notice and the real report both map to 2016-4T; the report must be tried first."""
    from src.download import wayback

    notice = "https://x.mx/pdf/eventos_relevantes/es/2017/2017 Retransmisión de reporte 4T2016.pdf"
    report = "https://x.mx/pdf/reportes/es/2016/2017_02_17_Resultados_Trimestrales_4T16_IR.pdf"
    snaps = [
        {"original": notice, "timestamp": "20180101000000"},  # newer — would win on timestamp alone
        {"original": report, "timestamp": "20170101000000"},
    ]
    monkeypatch.setattr(wayback, "cdx_snapshots", lambda *a, **k: snaps)

    cands = wayback.discover_archived_report_candidates("https://x.mx/ir/", from_year=2016)
    assert "2016-4T" in cands
    urls = cands["2016-4T"]
    # Real report is tried first; the notice is de-prioritised (kept as a fallback, not dropped).
    assert "Resultados_Trimestrales_4T16" in urls[0]
    assert any("Retransmisi" in u for u in urls)


def test_wayback_drops_conference_call_invite(monkeypatch):
    """A conference-call invite for a period is excluded entirely."""
    from src.download import wayback

    invite = "https://x.mx/wp-content/uploads/2019/12/KOF-Conference-Call-4Q-2016.pdf"
    snaps = [{"original": invite, "timestamp": "20170101000000"}]
    monkeypatch.setattr(wayback, "cdx_snapshots", lambda *a, **k: snaps)

    cands = wayback.discover_archived_report_candidates("https://x.mx/ir/", from_year=2016)
    assert all("Conference-Call" not in u for urls in cands.values() for u in urls)
