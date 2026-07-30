from scripts.fetch_regional_reports import period_from_row, select_quarterlies


def test_regional_period_from_feed_row():
    assert period_from_row({
        "titulo": "Regional",
        "subtitulo": "2nd Quarter / 2026",
    }) == "2026-2T"


def test_regional_selector_keeps_latest_primary_report_per_period():
    rows = [
        {
            "id_archivo": 10, "id_empresa": 2, "id_clase_documento": 16,
            "titulo": "Regional", "subtitulo": "1st Quarter / 2024",
            "esp": "files/old.pdf", "ing": "",
        },
        {
            "id_archivo": 20, "id_empresa": 2, "id_clase_documento": 16,
            "titulo": "Regional", "subtitulo": "1st Quarter / 2024",
            "esp": "files/new.pdf", "ing": "",
        },
        {
            "id_archivo": 30, "id_empresa": 2, "id_clase_documento": 12,
            "titulo": "Presentation", "subtitulo": "1st Quarter / 2024",
            "esp": "files/deck.pdf", "ing": "",
        },
    ]

    assert select_quarterlies(rows, 2020) == [{
        **rows[1],
        "period": "2024-1T",
        "url": "https://www.regional.mx/files/new.pdf",
    }]
