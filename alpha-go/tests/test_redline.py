"""Quarter-over-quarter redline: sentence diff with reworded pairing, tone tally, LLM narration."""
from __future__ import annotations

from src.qa import redline as rl

_OLD = """# Results

Net sales grew 8% in the quarter driven by traffic in Mexico and Central America.
EBITDA margin expanded 40 basis points to 10.5% on operating leverage and cost discipline.
We opened 120 stores during the period across all formats.
Consumer demand remained resilient throughout the quarter.
The company maintains a strong balance sheet with low leverage.
"""

_NEW = """# Results

Net sales grew 5% in the quarter driven by traffic in Mexico and Central America.
EBITDA margin expanded 40 basis points to 10.5% on operating leverage and cost discipline.
We opened 186 stores during the period across all formats.
Consumer demand softened, and traffic declined as customers became more cautious.
The company maintains a strong balance sheet with low leverage.
"""


def test_redline_pairs_reworded_sentences_and_flags_figure_changes():
    r = rl.redline(_OLD, _NEW, old_label="1T", new_label="2T")
    assert (r.old_label, r.new_label) == ("1T", "2T")
    changed = {c.old_text: c for c in r.changed}
    assert any("grew 8%" in k for k in changed) and any("120 stores" in k for k in changed)
    for c in r.changed:
        assert c.kind == "changed" and c.numbers_changed
        assert _NEW[c.offset:].startswith(c.text)           # offsets anchor into the NEW doc
    # the "demand" sentence changed too much to pair → removed + added
    assert any("resilient" in c.text for c in r.removed)
    assert any("softened" in c.text for c in r.added)
    assert r.unchanged >= 2
    assert r.total == len(r.changed) + len(r.added) + len(r.removed)


def test_tone_tally_shifts_negative_in_the_new_filing():
    r = rl.redline(_OLD, _NEW)
    assert r.new_tone.negative >= r.old_tone.negative
    assert r.new_tone.net <= r.old_tone.net
    removed = [c for c in r.removed if "resilient" in c.text]
    assert removed and removed[0].tone in ("positive", "neutral")


def test_identical_documents_have_no_changes():
    r = rl.redline(_OLD, _OLD)
    assert r.total == 0 and r.unchanged > 0 and r.narrative == ""


def test_numbered_items_order_and_narrate_gating(monkeypatch, tmp_path):
    r = rl.redline(_OLD, _NEW)
    items = rl.numbered_items(r)
    assert [n for n, _ in items] == list(range(1, len(items) + 1))
    assert items[0][1].kind == "changed"
    # disabled → untouched, no call
    monkeypatch.setattr(rl.llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert rl.narrate(r, {"qa": {"enabled": False}}).narrative == ""
    # enabled + key → narrative attached and cached
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(rl.llm_cache, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(rl.llm, "complete", lambda *a, **k: "- Sales growth slowed [1]\n- Demand softened [3]")
    out = rl.narrate(r, {"qa": {"enabled": True, "provider": "deepseek"}})
    assert out.narrative.startswith("- Sales growth slowed [1]")
    monkeypatch.setattr(rl.llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cached")))
    r2 = rl.redline(_OLD, _NEW)
    assert rl.narrate(r2, {"qa": {"enabled": True, "provider": "deepseek"}}).narrative == out.narrative


def test_narrate_failure_keeps_lists(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(rl.llm, "complete", boom)
    r = rl.narrate(rl.redline(_OLD, _NEW), {"qa": {"enabled": True, "provider": "deepseek",
                                                      "cache": False}})
    assert r.narrative == "" and r.total > 0
