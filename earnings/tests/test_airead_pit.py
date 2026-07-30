"""AI reader: point-in-time discipline, scrubber completeness, cache determinism.

The reader is the one component that sends study data to an outside model, so
the two things worth pinning are what it is allowed to see (only what was public
before the event) and what it gives away (nothing that identifies the issuer, in
arm B). Both are frozen in study.yaml `hybrid.reader` / `hybrid.contamination`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest

from earnlib import airead as ar

CFG = ar.load_reader_cfg()


# --------------------------------------------------------------------------
# point in time
# --------------------------------------------------------------------------

def _kpi_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "slug": ["acme"] * 5 + ["other"],
        "period": ["2024-4T", "2025-1T", "2025-2T", "2025-3T", "2025-4T",
                   "2025-2T"],
        "revenue": [100.0, 110.0, 120.0, 130.0, 140.0, 999.0],
        "yoy_rev_pct": [1.0, 2.0, 3.0, 4.0, 5.0, 9.0],
        "ebitda_margin": [10.0, 11.0, 12.0, 13.0, 14.0, 90.0],
    })


def _avail() -> dict:
    return {("acme", p): ar.period_end(p) + pd.Timedelta(days=30)
            for p in ("2024-4T", "2025-1T", "2025-2T", "2025-3T", "2025-4T")}


def test_history_excludes_the_current_and_every_later_quarter():
    hist = ar.kpi_history(_kpi_frame(), "acme", "2025-3T", _avail())
    quarters = list(hist["quarter"])
    assert quarters == ["2024-4T", "2025-1T", "2025-2T"]
    assert "2025-3T" not in quarters, "current quarter leaked into the prompt"
    assert "2025-4T" not in quarters, "future quarter leaked into the prompt"


def test_history_is_scoped_to_one_issuer():
    hist = ar.kpi_history(_kpi_frame(), "acme", "2025-3T", _avail())
    assert not (hist.astype(str) == "999").any().any()


def test_history_excludes_model_scores_and_desk_estimates():
    """Channel independence: the reader must not be shown s_ts, the SUEs, or
    the desk's own estimates, or the AI and system 'votes' stop being separate
    evidence."""
    banned = {"s_ts", "s_cs", "sue_revenue", "sue_ebitda", "sue_net_income",
              "est_revenue", "est_ebitda", "beat_revenue_pct"}
    shown = {col for col, _, _ in ar.HISTORY_COLUMNS}
    assert not (banned & shown)


def test_history_is_empty_when_no_quarter_predates_the_event():
    hist = ar.kpi_history(_kpi_frame(), "acme", "2024-4T", _avail())
    assert len(hist) == 0


def test_unmapped_quarters_use_the_period_end_fallback():
    """Metric quarters that never produced a dated event fall back to the
    phase_d convention (period end + 90d), not to 'available immediately'."""
    when = ar.available_at("acme", "2025-2T", {})
    assert when == ar.period_end("2025-2T") + pd.Timedelta(
        days=ar.AVAILABILITY_FALLBACK_DAYS)


def test_prompt_carries_no_forbidden_field_names():
    hist = ar.kpi_history(_kpi_frame(), "acme", "2025-3T", _avail())
    prompt = ar.build_prompt(CFG, arm="N", slug="acme", ticker="ACME",
                             period="2025-3T", sector="Test",
                             text="Revenue rose.", source="mdna", history=hist)
    for banned in ("s_ts", "margin_sue_z", "ar0_cc", "analyst"):
        assert banned not in prompt.user


# --------------------------------------------------------------------------
# the scrubber
# --------------------------------------------------------------------------

def test_scrubber_removes_every_configured_alias():
    aliases = CFG.scrub["aliases"]
    for slug in ("walmex", "liverpool", "becle", "volaris", "femsa"):
        text = " ".join(f"Reporte de {a} del trimestre." for a in aliases[slug])
        scrubbed, stats = ar.scrub_text(text, CFG, slug, slug.upper(), "2025-2T")
        assert stats["alias_hits"] >= len(aliases[slug])
        for alias in aliases[slug]:
            assert str(alias).lower() not in scrubbed.lower(), (slug, alias)


def test_no_alias_is_a_bare_number():
    """A numeric alias (a brand like "1800") would rewrite any amount that
    happens to equal it, breaking the frozen guarantee that the scrubber never
    alters numbers."""
    offenders = {slug: [a for a in aliases if str(a).strip().isdigit()]
                 for slug, aliases in CFG.scrub["aliases"].items()}
    assert not {k: v for k, v in offenders.items() if v}


def test_scrubber_handles_typographic_apostrophes():
    """Reports write Sam's Club with U+2019; a config alias is typed ASCII."""
    scrubbed, _ = ar.scrub_text("Sam’s Club creció 8%.", CFG, "walmex",
                                "WALMEX", "2025-2T")
    assert "Sam" not in scrubbed


def test_scrubber_redacts_urls_and_mailboxes():
    text = ("Detalles en https://tinyurl.com/Becle3Q25Call o escriba a "
            "ir@becle.com.mx hoy.")
    scrubbed, stats = ar.scrub_text(text, CFG, "becle", "CUERVO", "2025-3T")
    assert "becle" not in scrubbed.lower()
    assert stats["url_hits"] >= 2


def test_scrubber_redacts_officer_names_next_to_their_title():
    """Officer names leak the issuer as surely as its brands do — the pilot
    probes named companies off signature and IR-contact blocks. The title is
    kept: "Director General" identifies nobody."""
    text = ("Juan Domingo Beckmann, Director General, y Rodrigo de la Maza, "
            "Director de Finanzas, presentaron los resultados.")
    scrubbed, stats = ar.scrub_text(text, CFG, "acme", "ACME", "2025-2T")
    assert stats["person_hits"] == 2
    assert "Beckmann" not in scrubbed
    assert "Maza" not in scrubbed, "compound surnames must redact whole"
    assert "Director General" in scrubbed and "Director de Finanzas" in scrubbed


def test_person_redaction_leaves_ordinary_prose_alone():
    """Anchoring on a title is what keeps this narrow — an unanchored
    capitalized-name matcher would eat product and place names."""
    text = ("Las ventas de Mercancias Generales crecieron en Nuevo Leon y "
            "Baja California durante el trimestre.")
    scrubbed, stats = ar.scrub_text(text, CFG, "acme", "ACME", "2025-2T")
    assert stats["person_hits"] == 0
    assert "Mercancias Generales" in scrubbed
    assert "Nuevo Leon" in scrubbed


def test_scrubber_keeps_generic_words_that_are_also_tickers():
    """HOTEL is a ticker and 'hotel' is the word a hotel issuer needs to
    describe its own business — the ALL-CAPS ticker goes, the noun stays."""
    scrubbed, _ = ar.scrub_text(
        "El hotel de la ciudad; la emisora HOTEL reportó.",
        CFG, "grupo_hotelero", "HOTEL", "2025-2T")
    assert "hotel de la ciudad" in scrubbed
    assert "HOTEL reportó" not in scrubbed


def test_scrubber_makes_years_relative_without_touching_amounts():
    text = "Ingresos de 2025 vs 2024 fueron 2,025 y 1,980 millones."
    scrubbed, _ = ar.scrub_text(text, CFG, "acme", "ACME", "2025-2T")
    assert "YEAR_T" in scrubbed and "YEAR_T-1" in scrubbed
    assert "2,025" in scrubbed, "a formatted amount was mangled as a year"
    assert "1,980" in scrubbed


def test_scrubber_relabels_quarters_relative_to_the_report():
    scrubbed, _ = ar.scrub_text("Comparado con 2T24 y 2T25.", CFG, "acme",
                                "ACME", "2025-2T")
    assert "QUARTER_T" in scrubbed
    assert "QUARTER_T-4" in scrubbed
    assert "2T25" not in scrubbed and "2T24" not in scrubbed


def test_named_arm_is_not_scrubbed():
    hist = pd.DataFrame()
    prompt = ar.build_prompt(CFG, arm="N", slug="walmex", ticker="WALMEX",
                             period="2025-2T", sector="Retail",
                             text="Walmex creció.", source="mdna",
                             history=hist)
    assert "Walmex" in prompt.user
    assert prompt.scrub_stats == {}


def test_unknown_arm_is_rejected():
    with pytest.raises(ValueError):
        ar.build_prompt(CFG, arm="C", slug="acme", ticker="ACME",
                        period="2025-2T", sector="", text="x", source="mdna",
                        history=pd.DataFrame())


# --------------------------------------------------------------------------
# cache determinism
# --------------------------------------------------------------------------

def _prompt(text: str = "hello") -> ar.Prompt:
    return ar.Prompt(system="sys", user=text, arm="B", source="mdna",
                     truncated=False, scrub_stats={})


def test_cache_key_is_stable_for_identical_inputs():
    a = ar.cache_key(CFG, "claude-opus-5", 0.0, _prompt())
    b = ar.cache_key(CFG, "claude-opus-5", 0.0, _prompt())
    assert a == b


@pytest.mark.parametrize("mutate", [
    lambda kw: {**kw, "model": "claude-sonnet-5"},
    lambda kw: {**kw, "temperature": 0.7},
    lambda kw: {**kw, "prompt": ar.Prompt(system="sys", user="different",
                                          arm="B", source="mdna",
                                          truncated=False, scrub_stats={})},
    lambda kw: {**kw, "prompt": ar.Prompt(system="sys", user="hello", arm="N",
                                          source="mdna", truncated=False,
                                          scrub_stats={})},
])
def test_cache_key_changes_when_anything_material_changes(mutate):
    base = {"model": "claude-opus-5", "temperature": 0.0, "prompt": _prompt()}
    ref = ar.cache_key(CFG, **base)
    other = mutate(base)
    assert ar.cache_key(CFG, **other) != ref


def test_cache_key_changes_when_the_prompt_config_changes(tmp_path):
    """A prompt edit is a new pre-registration, so it must miss the cache
    rather than silently reuse reads made under the old prompt."""
    path = tmp_path / "ai_reader.yaml"
    path.write_text(Path(ar.READER_CFG_PATH).read_text() + "\n# edited\n")
    edited = ar.load_reader_cfg(path)
    assert edited.sha256 != CFG.sha256
    assert (ar.cache_key(edited, "claude-opus-5", 0.0, _prompt())
            != ar.cache_key(CFG, "claude-opus-5", 0.0, _prompt()))


def test_api_and_subagent_backends_never_share_a_cache_entry():
    """Subagent reads are not temperature-pinned and not reproducible; if they
    collided with API reads in the cache, an unreproducible answer could be
    served silently in place of the specified one."""
    block = {"model": "claude-opus-5", "temperature": 0.0}
    api = ar.backend_identity(block, "api")
    sub = ar.backend_identity(block, "subagent")
    assert api == ("claude-opus-5", 0.0)
    assert sub[0].startswith(ar.SUBAGENT_PREFIX) and sub[1] != api[1]
    assert (ar.cache_key(CFG, *api, _prompt())
            != ar.cache_key(CFG, *sub, _prompt()))


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError):
        ar.backend_identity({"model": "m", "temperature": 0.0}, "guess")


def test_offline_mode_refuses_to_call_and_names_the_miss(tmp_path):
    """The prospective instrument relies on this: a read that is not already
    cached cannot be conjured at decision time."""
    with pytest.raises(LookupError):
        ar.call_reader(_prompt(), CFG, model="claude-opus-5", temperature=0.0,
                       max_tokens=100, cache_dir=tmp_path, allow_call=False)


def test_cached_record_is_returned_without_a_call(tmp_path):
    prompt = _prompt()
    key = ar.cache_key(CFG, "claude-opus-5", 0.0, prompt)
    record = {"key": key, "arm": "B", "response": {"direction": "up"}}
    (tmp_path / f"{key}.json").write_text(json.dumps(record))
    got = ar.call_reader(prompt, CFG, model="claude-opus-5", temperature=0.0,
                         max_tokens=100, cache_dir=tmp_path, allow_call=False)
    assert got == record


# --------------------------------------------------------------------------
# response validation
# --------------------------------------------------------------------------

def test_valid_response_is_normalized():
    fields, problems = ar.validate({
        "direction": "Down", "conviction": "HIGH", "reason_code": "guia_outlook",
        "guidance_direction": "withdrawn", "beat_quality": "low",
        "rationale": "FY guidance pulled.",
        "one_off_items": [{"label": "asset sale", "material": True},
                          {"label": "fx", "material": False}],
        "organic_constant_fx_growth_pct": "3.5",
    }, CFG)
    assert not problems
    assert fields["direction"] == "down" and fields["ai_dir"] == -1
    assert fields["conviction"] == "high"
    assert fields["n_one_offs"] == 2 and fields["n_material_one_offs"] == 1
    assert fields["organic_constant_fx_growth_pct"] == pytest.approx(3.5)


def test_malformed_response_is_neutralized_not_guessed():
    """An unusable read must abstain. Silently coercing it to a direction would
    put an unfounded trade into the cascade."""
    fields, problems = ar.validate({"direction": "sideways", "conviction": "?"},
                                   CFG)
    assert fields["direction"] == "flat" and fields["ai_dir"] == 0
    assert fields["reason_code"] == "otro"
    assert any("direction" in p for p in problems)


def test_probe_identification_is_scored_generously():
    """The gate is more conservative the more events count as identified, so
    a loose match here is the safe direction to err in."""
    assert ar.validate_probe({"ticker": "WALMEX"}, "WALMEX")["identified"]
    assert ar.validate_probe({"ticker": "unknown",
                              "company": "Walmex"}, "WALMEX")["identified"]
    assert not ar.validate_probe({"ticker": "unknown",
                                  "company": "unknown"}, "WALMEX")["identified"]


def test_probe_move_is_confined_to_known_values():
    assert ar.validate_probe({"next_day_move": "maybe up"},
                             "X")["probe_move"] == "unknown"
    assert ar.validate_probe({"next_day_move": "UP"}, "X")["probe_move"] == "up"
