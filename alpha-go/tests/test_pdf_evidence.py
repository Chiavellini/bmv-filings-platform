"""PDF evidence crops are tied to the exact matched source occurrence."""
from __future__ import annotations

from app.components import pdf_view


def _pdf(path):
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 96), "Revenue increased strongly in the quarter.")
    doc.save(path)
    doc.close()


def test_mention_preview_renders_highlighted_crop_for_mapped_pdf_span(tmp_path, monkeypatch):
    pdf = tmp_path / "source.pdf"
    _pdf(pdf)
    md = tmp_path / "source.md"
    text = "===== Página 1 =====\nRevenue increased strongly in the quarter."
    md.write_text(text, encoding="utf-8")
    offset = text.index("Revenue")
    monkeypatch.setattr(pdf_view, "pdf_path_for", lambda _doc_id: str(pdf))
    pdf_view._render_crop.cache_clear()

    preview = pdf_view.mention_preview("acme/q1", str(md), offset, ["Revenue"])

    assert preview is not None
    assert preview.page == 1
    assert preview.highlights >= 1
    assert preview.png.startswith(b"\x89PNG")


def test_mention_preview_refuses_unlocated_phrase(tmp_path, monkeypatch):
    pdf = tmp_path / "source.pdf"
    _pdf(pdf)
    md = tmp_path / "source.md"
    text = "===== Página 1 =====\nNo matching source phrase."
    md.write_text(text, encoding="utf-8")
    monkeypatch.setattr(pdf_view, "pdf_path_for", lambda _doc_id: str(pdf))
    pdf_view._render_crop.cache_clear()

    assert pdf_view.mention_preview("acme/q1", str(md), text.index("No"), ["No matching"]) is None


def test_mention_highlights_use_light_sentiment_tints():
    assert pdf_view.highlight_color("positive") == "#dcfce7"
    assert pdf_view.highlight_color("neutral") == "#fef3c7"
    assert pdf_view.highlight_color("negative") == "#fee2e2"
