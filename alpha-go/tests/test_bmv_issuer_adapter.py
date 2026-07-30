from __future__ import annotations

from src.sources.bmv_issuer import (
    BmvIssuer, BmvIssuerAdapter, parse_directory_jsonp, parse_events_page,
    parse_financial_page,
)


ISSUER = BmvIssuer("AMX", 6024, "América Móvil", "amx", "telecom")


def test_parse_directory_jsonp():
    payload = 'for(;;);({"response":{"resultado":[{"claveEmisora":"AMX","idEmisora":6024}]}})'
    assert parse_directory_jsonp(payload) == {"AMX": 6024}


def test_financial_page_keeps_annual_pdf_and_excludes_governance_xbrl():
    html = """
    <h2>REPORTES ANUALES</h2><table>
      <tr><td><span>28-Apr-2026 16:15</span></td>
          <td>Informe Anual en formato PDF del año <b>2025</b></td>
          <td><a href="/docs-pub/infoanua/infoanua_1552461_2025_1.pdf">Descargar</a></td></tr>
      <tr><td>29-May-2026</td><td>Código de Mejores Prácticas Corporativas 2025</td>
          <td><a href="/docs-pub/cmpc/cmpc_2025.pdf">Descargar</a></td></tr>
      <tr><td>29-May-2026</td><td>Reporte Anual en formato XBRL 2025</td>
          <td><a href="/docs-pub/xbrl/report.html">Descargar</a></td></tr>
    </table>
    <h2>REPORTE DE SUSTENTABILIDAD</h2><table><tr><td>03-Jun-2025 12:25</td>
      <td>Reporte de Sustentabilidad del año 2024</td>
      <td><a href="/docs-pub/sustenta/sustenta_2024.pdf">Descargar</a></td></tr></table>
    """
    records = parse_financial_page(html, ISSUER)
    assert [(r.doc_type, r.period) for r in records] == [
        ("annual_report", "2025-FY"), ("sustainability_report", "2024-FY")
    ]
    assert records[0].source_record_id == "infoanua_1552461_2025_1.pdf"
    assert records[0].published_at == "2026-04-28T16:15"


def test_events_parser_is_bounded_and_recognizes_annual_report():
    html = """
    <h2>EVENTOS RELEVANTES DE LA EMISORA</h2><table>
      <tr><td>28-04-2026 19:09</td><td>América Móvil presenta Reporte Anual</td>
          <td><a href="x.zip">Descargar</a><a href="/docs-pub/eventemi/eventemi_1.pdf">Adjunto</a></td></tr>
      <tr><td>22-03-2026 12:30</td><td>América Móvil adquiere Desktop</td>
          <td><a href="/docs-pub/eventemi/eventemi_2.pdf">Adjunto</a></td></tr>
    </table><h2>Eventos relevantes de la calificadora</h2><table>
      <tr><td>01-01-2026</td><td>Rating</td><td><a href="rating.pdf">PDF</a></td></tr>
    </table>
    """
    records = parse_events_page(html, ISSUER)
    assert [(r.doc_type, r.period) for r in records] == [
        ("relevant_event", "2025-FY"), ("relevant_event", None)
    ]
    assert records[0].metadata["related_doc_type"] == "annual_report"
    assert all("rating.pdf" not in r.canonical_url for r in records)


def test_adapter_uses_same_contract_for_fixture_pages():
    financial = "<h2>REPORTES ANUALES</h2><table></table>"
    events = """<h2>EVENTOS RELEVANTES DE LA EMISORA</h2><table><tr>
      <td>01-07-2026 10:00</td><td>Material event</td>
      <td><a href='/docs-pub/eventemi/e.pdf'>PDF</a></td></tr></table>"""
    adapter = BmvIssuerAdapter(
        [ISSUER], pages={ISSUER.financial_url: financial, ISSUER.events_url: events}
    )
    records = list(adapter.discover())
    assert len(records) == 1 and records[0].doc_type == "relevant_event"
