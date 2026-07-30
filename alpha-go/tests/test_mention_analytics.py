from src.search.mention_analytics import mention_tone, mention_tones, tone_counts


def test_each_mention_is_scored_independently():
    text = (
        "FX pressure significantly reduced margins and hurt profitability. "
        + ("ordinary context " * 50)
        + "FX benefits improved margins and delivered strong growth."
    )
    first = text.index("FX")
    second = text.rindex("FX")
    tones = mention_tones(text, [(first, first + 2), (second, second + 2)])

    assert [tone.start for tone in tones] == [first, second]
    assert tones[0].label == "negative"
    assert tones[1].label == "positive"
    assert tone_counts(tones) == {"positive": 1, "neutral": 0, "negative": 1}


def test_mention_context_is_bounded_at_document_edges():
    tone = mention_tone("FX improved growth.", 0, 2)
    assert tone.context == "FX improved growth."
    assert tone.start == 0 and tone.end == 2
