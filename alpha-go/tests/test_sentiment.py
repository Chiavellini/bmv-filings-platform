"""Phase 4 — hybrid sentiment: lexicon scoring, negation, and disabled-by-default LLM.

Plus the Track-C additions: bilingual (Spanish auto-detection) scoring and a macro-F1 floor
that runs the offline engine over the labeled gold set (regressions can only lower it).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.qa import sentiment
from src.qa.sentiment import SentimentResult, analyze, refine_with_llm, score_sentiment
from src.qa.sentiment_eval import load_labels, score

_ROOT = Path(__file__).resolve().parents[1]
_LABELS = _ROOT / "eval" / "sentiment_labels.yaml"
_GOLD = _ROOT / "eval" / "sentiment_gold.yaml"    # independent, held-out (dev|test) annotations


def test_positive_passage():
    r = score_sentiment("Net sales rose with strong, record growth and margin expansion.")
    assert r.label == "positive"
    assert r.score > 0
    assert r.engine == "lexicon"


def test_negative_passage():
    r = score_sentiment("Results declined amid pricing pressure, weak demand and an impairment.")
    assert r.label == "negative"
    assert r.score < 0


def test_neutral_when_no_cues():
    r = score_sentiment("The company operates retail stores across several regions.")
    assert r.label == "neutral"
    assert r.score == 0.0
    assert "no tonal cues" in r.rationale


def test_negation_flips_positive_to_negative():
    # "not strong" → the positive word is negated.
    assert score_sentiment("Demand was not strong this quarter.").label == "negative"


def test_negation_flips_negative_to_positive():
    # "no pressure" → the negative word is negated.
    assert score_sentiment("There was no pressure on margins.").label == "positive"


def test_contraction_negator():
    # "didn't decline" → negative word negated → positive lean.
    assert score_sentiment("Volumes didn't decline this period.").score >= 0


def test_increase_in_tax_is_negative():
    # User-reported misclassification: "increase" is not good news when taxes are what grew.
    r = score_sentiment(
        "New taxes were procured for sugar: which includes an increase in the excise tax "
        "on sugar-sweetened beverages from Ps. 1.64 to Ps. 3.08 per liter."
    )
    assert r.label == "negative"


def test_direction_polarity_follows_object():
    assert score_sentiment("The company reduced operating costs this year.").label == "positive"
    assert score_sentiment("Total revenue increased during the quarter.").label == "positive"
    assert score_sentiment("Operating expenses increased sharply.").label == "negative"
    assert score_sentiment("Gross margin declined in the period.").label == "negative"


def test_direction_without_object_keeps_own_sign():
    assert score_sentiment("The business accelerated meaningfully.").score > 0
    assert score_sentiment("Activity dropped during the period.").score < 0


def test_single_cue_in_long_passage_reads_neutral():
    filler = "The company operates stores and distribution centers across the region. " * 8
    r = score_sentiment(filler + "Results were strong.")
    assert r.label == "neutral"          # one cue in ~100 tokens is not a polar read
    assert "treated as neutral" in r.rationale


def test_single_cue_in_short_text_still_polar():
    assert score_sentiment("Results were strong.").label == "positive"


def test_rationale_names_drivers():
    r = score_sentiment("Strong growth and record gains drove the quarter.")
    assert "e.g." in r.rationale


# --- Spanish (auto-detected language) -------------------------------------------------------

def test_spanish_positive():
    r = score_sentiment("Los ingresos crecieron 11.6% y la utilidad neta alcanzo un nivel record.")
    assert r.label == "positive"
    assert r.engine == "lexicon"


def test_spanish_negative():
    r = score_sentiment("Las ventas cayeron y se registro una perdida por el deterioro del margen.")
    assert r.label == "negative"


def test_spanish_neutral_factual():
    r = score_sentiment("En 2017 la Compania opera 61 tiendas y arrenda locales comerciales a terceros.")
    assert r.label == "neutral"


def test_spanish_negation_flips():
    # "sin presion" / "no ... debilidad" → negated negatives read positive.
    assert score_sentiment("No se observo debilidad ni presion sobre los margenes.").label == "positive"


def test_spanish_direction_follows_object():
    # "el incremento del margen" → up × positive object → positive.
    assert score_sentiment("El trimestre mostro un incremento del margen operativo.").label == "positive"
    # "aumento de costos" → up × negative object → negative.
    assert score_sentiment("El periodo reflejo un aumento de los costos de operacion.").label == "negative"


def test_accented_words_are_folded():
    # Real corpus keeps accents; the folded tokenizer must still match ASCII lexicon keys.
    assert score_sentiment("Los márgenes se debilitaron por la presión inflacionaria.").label == "negative"


def test_spanish_scored_not_silently_neutral():
    # The whole point of Track C: a clearly-toned Spanish passage is NOT read neutral.
    assert score_sentiment("El EBITDA se redujo 21% durante el trimestre.").label == "negative"


# --- context guards (Track C honest-eval redo) ----------------------------------------------

def test_accounting_boilerplate_reads_neutral():
    # Cue words in accounting-policy prose are terminology, not tone: no false negative on "perdida".
    r = score_sentiment(
        "No se reconoce ninguna ganancia o perdida en la compra, venta o cancelacion de "
        "los instrumentos de capital propios de la Entidad.")
    assert r.label == "neutral"
    r = score_sentiment(
        "Las cuentas por cobrar se miden al costo amortizado menos la provision por deterioro.")
    assert r.label == "neutral"


def test_risk_management_objective_reads_neutral():
    # "minimizar el riesgo ... mayor certidumbre" fires positive as a bag of cues — guard it.
    r = score_sentiment(
        "El objetivo de la Compania es minimizar el riesgo de variacion en los precios de sus "
        "insumos, brindando mayor certidumbre y visibilidad.")
    assert r.label == "neutral"


def test_training_csr_reads_neutral():
    # HR/training CSR ("crecimiento de colaboradores", "horas de capacitacion") is not tonal.
    r = score_sentiment(
        "Soriana Universidad apoya el crecimiento de sus colaboradores; se impartieron mas de "
        "754 mil horas de capacitacion en el trimestre.")
    assert r.label == "neutral"


def test_deleveraging_reads_positive():
    assert score_sentiment(
        "There was a reduction of $9.554 billion pesos in net debt, 45% in twelve months."
    ).label == "positive"
    assert score_sentiment("The company continued deleveraging during the year.").label == "positive"


def test_rising_debt_reads_negative():
    assert score_sentiment(
        "Net debt increased to Ps. 4,826 million, a significant increment over the prior year."
    ).label == "negative"


def test_operating_leverage_is_not_debt():
    # "apalancamiento operativo" (operating leverage) must NOT trip the rising-debt guard.
    assert score_sentiment(
        "El margen EBITDA aumento 50pb por un mayor efecto de apalancamiento operativo y el "
        "aumento de las ventas.").label == "positive"


def test_offset_clause_defers_to_dominant_prior():
    # "favorable ... partially offset by [negative]" → the dominant prior clause wins.
    assert score_sentiment(
        "Favorable PET prices and expense savings were partially offset by unfavorable FX."
    ).label == "positive"


def test_vague_forward_looking_reads_neutral():
    assert score_sentiment(
        "We are confident that these pillars will continue to drive sustainable growth and "
        "create value for our stakeholders.").label == "neutral"


def test_concrete_optimism_stays_positive():
    # Concrete evidence (figures / strong result word) survives the forward-looking guard.
    assert score_sentiment(
        "El programa de ahorro en costos genero aproximadamente $500 millones en el trimestre."
    ).label == "positive"
    assert score_sentiment("Net sales grew 10% to a record level this quarter.").label == "positive"


# --- gold-set regression floors -------------------------------------------------------------

@pytest.mark.skipif(
    not _GOLD.is_file(),
    reason=(
        "external sentiment evaluation bundle not attached: "
        "missing eval/sentiment_gold.yaml"
    ),
)
def test_held_out_test_split_floor():
    """Honest floor on the INDEPENDENTLY-labeled held-out TEST split (never tuned against).
    Round-2 expanded the gold to 400 and minted a FRESH 200-snippet test. The track-c-improve2
    pass (generalizable domain/context rules tuned only on the dev split — award/recognition &
    payment-mechanics boilerplate guards, es directional gaps, two-clause concession dominance)
    lifted dev to acc 0.965 / macro-F1 0.940 and the fresh TEST to acc 0.755 / macro-F1 0.693
    (from the prior honest 0.750 / 0.687). Floor set just below the achieved TEST level."""
    snippets = load_labels(_GOLD, split="test")
    assert len(snippets) >= 190, "held-out test split shrank unexpectedly"
    report = score(score_sentiment, snippets, split="test")
    assert report.macro_f1 >= 0.685, f"held-out macro-F1 regressed to {report.macro_f1}"
    assert report.accuracy >= 0.75, f"held-out accuracy regressed to {report.accuracy}"


@pytest.mark.skipif(
    not _LABELS.is_file(),
    reason=(
        "external sentiment evaluation bundle not attached: "
        "missing eval/sentiment_labels.yaml"
    ),
)
def test_gold_set_macro_f1_floor():
    """Harness over the labeled gold set. Locked below the achieved baseline (macro-F1 0.968,
    accuracy 0.970 on 2026-07-07) so future lexicon changes can only raise it, never regress."""
    snippets = load_labels(_LABELS)
    assert len(snippets) >= 60, "gold set shrank unexpectedly"
    report = score(score_sentiment, snippets)
    assert report.macro_f1 >= 0.96, f"macro-F1 regressed to {report.macro_f1}"
    assert report.accuracy >= 0.96, f"accuracy regressed to {report.accuracy}"


def test_llm_refine_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # qa disabled → no LLM call, returns None regardless of key.
    assert refine_with_llm("strong growth", {"qa": {"enabled": False}}) is None
    assert refine_with_llm("strong growth", {}) is None
    assert refine_with_llm("strong growth", None) is None


def test_llm_refine_needs_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # enabled but no key → still None (never attempts a call).
    assert refine_with_llm("strong growth", {"qa": {"enabled": True}}) is None


def test_analyze_falls_back_to_lexicon(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = analyze("Record growth and strong momentum.", {"qa": {"enabled": True}}, refine=True)
    assert isinstance(r, SentimentResult)
    assert r.engine == "lexicon"      # LLM unavailable → lexicon result
    assert r.label == "positive"


# --- opt-in DeepSeek LLM tier (round-2; hermetic — provider is mocked) -----------------------

def _fake_provider():
    from src.qa.llm import ProviderConfig
    return ProviderConfig(kind="openai", provider="deepseek", model="deepseek-chat",
                          base_url="https://api.deepseek.com", api_key="test-key")


def test_llm_tier_off_by_default_is_hermetic(monkeypatch):
    # engine=lexicon (the committed default) → no provider resolution, no network, returns None.
    import src.qa.llm as llm
    monkeypatch.setattr(llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network touched")))
    assert refine_with_llm("Sales grew 10%.", {"sentiment": {"engine": "lexicon"}}) is None
    assert sentiment.llm_enabled({"sentiment": {"engine": "lexicon"}}) is False


def test_llm_tier_used_when_enabled(monkeypatch):
    import src.qa.llm as llm
    monkeypatch.setattr(llm, "resolve_provider", lambda block: _fake_provider())
    monkeypatch.setattr(llm, "complete",
                        lambda provider, *, system, user, max_tokens: "negative")
    cfg = {"sentiment": {"engine": "llm", "cache": False}}
    assert sentiment.llm_enabled(cfg) is True
    r = refine_with_llm("Margins contracted on higher input costs.", cfg)
    assert r is not None and r.label == "negative" and r.engine == "llm"
    assert analyze("Margins contracted on higher input costs.", cfg, refine=True).engine == "llm"


def test_llm_tier_falls_back_on_error(monkeypatch):
    import src.qa.llm as llm
    monkeypatch.setattr(llm, "resolve_provider", lambda block: _fake_provider())
    monkeypatch.setattr(llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")))
    cfg = {"sentiment": {"engine": "llm", "cache": False}}
    assert refine_with_llm("x", cfg) is None                                   # degrades to None
    assert analyze("Sales grew 10%.", cfg, refine=True).engine == "lexicon"    # caller falls back
