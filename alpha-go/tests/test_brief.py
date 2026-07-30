from src.search.brief import EvidenceBrief, EvidenceItem
from app.components.panels import _brief_passage_text


def test_brief_deduplicates_and_exports():
    item = EvidenceItem("bimbo/2024-1T", "bimbo", "2024-1T", "report", "Q1", "margin improved", "data/corpus/bimbo/2024-1T.md")
    brief = EvidenceBrief(query="margin")
    brief.add(item)
    brief.add(item)
    assert len(brief.items) == 1
    assert "company,period" in brief.to_csv()
    assert "margin improved" in brief.to_html()


def test_brief_passage_accepts_sentiment_colored_offsets(tmp_path):
    source = tmp_path / "document.md"
    source.write_text("zero one two three", encoding="utf-8")

    text = _brief_passage_text(
        str(source),
        [(5, ["one"], "#dcfce7"), (13, ["three"])],
    )

    assert text.startswith("one two three")
    assert text.endswith("three")
