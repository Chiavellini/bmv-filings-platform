"""
test_patterns.py — Unit tests for individual regex patterns.

Each test is isolated: it applies a single PatternSpec to a known text
fragment and asserts the captured values. This tests the pattern logic
independently of the full extraction pipeline.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.extract.extract_metrics import parse_number
from src.model.financial_model import METRIC_BY_KEY, _N, _NL, _FN, PatternSpec


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def try_pattern(text: str, pspec: PatternSpec):
    """Apply a pattern to text; return (groups_as_floats, match) or None."""
    m = re.search(pspec.regex, text, re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    vals = [parse_number(g) * pspec.multiplier
            if parse_number(g) is not None else None
            for g in m.groups()]
    return vals, m


# ---------------------------------------------------------------------------
# Revenue patterns
# ---------------------------------------------------------------------------

class TestRevenuePatterns:

    def _revenue_patterns(self):
        return METRIC_BY_KEY["revenue"].patterns

    def test_revenue_table_es_four_col(self):
        """2022+ Spanish table: 4-column format with exact values."""
        text = "   Ingresos Totales    249,793    77,303   172,490  223.1%"
        for p in self._revenue_patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(249793.0)
                assert vals[1] == pytest.approx(77303.0)
                return
        pytest.fail("No revenue pattern matched the ES 4-col line")

    def test_revenue_table_es_two_col(self):
        """2016-2018 Spanish table: 2-column format."""
        text = "         Total de Ingresos Netos         314,925 268,169 17.4%"
        for p in self._revenue_patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(314925.0)
                return
        pytest.fail("No revenue pattern matched the ES 2-col line")

    def test_revenue_table_en(self):
        """English table format."""
        text = "   Total Revenue          517,708   413,220   104,489   25.3%"
        for p in self._revenue_patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(517708.0)
                return
        pytest.fail("No revenue pattern matched EN table line")

    def test_revenue_rejects_year_number(self):
        """Column header '2019' must NOT be captured as revenue."""
        text = "   Ingresos Totales                 2019              14,189"
        # _NL-based patterns require a comma — "2019" has none → no match
        table_patterns = [p for p in self._revenue_patterns()
                          if p.source == "table"]
        for p in table_patterns:
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] != pytest.approx(2019.0), \
                    f"Pattern incorrectly captured year '2019' as revenue"

    def test_revenue_rejects_split_artifact(self):
        """'5 16,237' is a split of '516,237' — first value '5' must be rejected."""
        text = "   Total de ingresos      5  16,237  517,708  ( 1,471)"
        table_patterns = [p for p in self._revenue_patterns()
                          if p.source == "table"]
        for p in table_patterns:
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                # Must not capture 5.0 as current revenue
                assert vals[0] != pytest.approx(5.0), \
                    "Pattern captured split artifact '5' as revenue"

    def test_revenue_prose_es(self):
        """Spanish prose pattern with millones multiplier."""
        text = ("Los Ingresos Totales del 1T26 alcanzaron los $589.0 millones de pesos, "
                "un crecimiento de doble dígito, de 14.1% vs el 1T25")
        prose_patterns = [p for p in self._revenue_patterns()
                          if p.source == "prose"]
        for p in prose_patterns:
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(589_000, rel=0.001)
                return
        pytest.fail("No prose revenue pattern matched")


# ---------------------------------------------------------------------------
# EBITDA patterns
# ---------------------------------------------------------------------------

class TestEbitdaPatterns:

    def _ebitda_patterns(self):
        return METRIC_BY_KEY["ebitda"].patterns

    def test_ebitda_simple_table(self):
        """Basic EBITDA table line."""
        text = "   UAFIDA                          45,179  34,006  32.9%"
        for p in self._ebitda_patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(45179.0)
                assert vals[1] == pytest.approx(34006.0)
                return
        pytest.fail("No EBITDA pattern matched UAFIDA line")

    def test_ebitda_with_footnote_artifact(self):
        """pdfplumber superscript artifact: '192,119 1 39,062'."""
        text = "          EBITDA                    192,119 1  39,062   53,057   38.2%"
        for p in self._ebitda_patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(192119.0), \
                    f"Expected 192119, got {vals[0]}"
                assert vals[1] == pytest.approx(39062.0), \
                    f"Expected 39062, got {vals[1]}"
                return
        pytest.fail("No EBITDA pattern handled footnote artifact")

    def test_ebitda_negative_parenthetical(self):
        """COVID period: EBITDA was negative with parenthetical format."""
        text = "              EBITDA               41,209  ( 94,052)  135,261  (143.8%)"
        for p in self._ebitda_patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(41209.0)
                assert vals[1] == pytest.approx(-94052.0), \
                    f"Expected -94052 (parenthetical negative), got {vals[1]}"
                return
        pytest.fail("No EBITDA pattern handled parenthetical negative")

    def test_ebitda_sin_ifrs_negative_prose(self):
        """EBITDA sin IFRS 16 prose with negative dollar sign."""
        text = ("el EBITDA (sin considerar IFRS 16) fue -$66.0 millones de pesos "
                "vs -$116.2 millones")
        for p in METRIC_BY_KEY["ebitda_sin_ifrs"].patterns:
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                # -$66.0 × 1000 = -66000
                assert vals[0] == pytest.approx(-66_000, rel=0.001)
                return
        pytest.fail("No ebitda_sin_ifrs pattern handled negative prose")

    def test_ebitda_sin_ifrs_variant_verbs(self):
        """Hardening: capture totalizó / terminó / llegó variants in prose."""
        samples = [
            ("El EBITDA (sin considerar IFRS 16) totalizó en $51.9 millones de pesos", 51_900),
            ("En tanto, el EBITDA (sin considerar IFRS 16) terminó en -$138.5 millones de pesos", -138_500),
            ("El EBITDA sin IFRS 16 llegó a $97.3 millones de pesos", 97_300),
        ]
        for text, expected in samples:
            for p in METRIC_BY_KEY["ebitda_sin_ifrs"].patterns:
                result = try_pattern(text, p)
                if result:
                    vals, _ = result
                    assert vals[0] == pytest.approx(expected, rel=0.001)
                    break
            else:
                pytest.fail(f"No ebitda_sin_ifrs pattern handled: {text}")


# ---------------------------------------------------------------------------
# EBITDA Margin patterns
# ---------------------------------------------------------------------------

class TestEbitdaMarginPatterns:

    def _patterns(self):
        return METRIC_BY_KEY["ebitda_margin"].patterns

    def test_margin_con_ifrs_from_prose_2026(self):
        """2026 prose: 'sin IFRS 16 ... 16.5% y con IFRS 16 de 36.3%'
        Must capture CON IFRS value (36.3%), NOT sin IFRS (16.5%)."""
        text = ("margen EBITDA sin IFRS 16 para el trimestre fue del 16.5% "
                "y con IFRS 16 de 36.3%.")
        for p in self._patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(36.3, abs=0.05), \
                    f"Expected 36.3 (con IFRS), got {vals[0]} — wrong IFRS variant captured"
                return
        pytest.fail("No ebitda_margin pattern matched 2026 prose")

    def test_margin_from_prose_2024(self):
        """2024 prose: 'margen EBITDA para el trimestre fue del 37.1%'."""
        text = "El margen EBITDA para el trimestre fue del 37.1% y sin IFRS 16 fue del 15.1%."
        for p in self._patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(37.1, abs=0.05)
                return
        pytest.fail("No ebitda_margin pattern matched 2024 prose")

    def test_margin_from_table_2022(self):
        """2022 table: 'Margen EBITDA  16.5% -121.7%'."""
        text = "              Margen EBITDA         16.5% -121.7% 138.2 pp -26.4% -150.4% 123.9 pp"
        for p in self._patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                # In 2022 table, first column is CON IFRS = 16.5
                assert vals[0] == pytest.approx(16.5, abs=0.05)
                return
        pytest.fail("No ebitda_margin pattern matched 2022 table")

    def test_margin_sin_ifrs_rejects_net_margin_false_positive(self):
        """Do not match 'Margen Neto sin IFRS 16' as EBITDA margin."""
        text = "El Margen Neto sin IFRS 16 fue de 1.2%."
        assert not any(try_pattern(text, p) for p in self._patterns())


# ---------------------------------------------------------------------------
# Cash patterns
# ---------------------------------------------------------------------------

class TestCashPatterns:

    def _patterns(self):
        return METRIC_BY_KEY["cash"].patterns

    def test_cash_from_balance_table(self):
        """Balance General table: 'Efectivo y Equivalentes  131,568  74,260  57,308  77.2%'."""
        text = "          Efectivo y Equivalentes            131,568 74,260 57,308 77.2%"
        for p in self._patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(131568.0)
                assert vals[1] == pytest.approx(74260.0)
                return
        pytest.fail("No cash pattern matched balance table")

    def test_cash_from_prose(self):
        """Prose: 'fue de $369.2 millones de pesos'."""
        text = ("El rubro de Efectivo y Equivalentes al cierre del 1T26 "
                "fue de $369.2 millones de pesos")
        prose_patterns = [p for p in self._patterns() if p.source == "prose"]
        for p in prose_patterns:
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(369_200, rel=0.001)
                return
        pytest.fail("No cash prose pattern matched")

    def test_cash_registrado_prose(self):
        """Prose variant: 'registró $134.3 millones'."""
        text = "El rubro de Efectivo y Equivalentes al cierre del año registró $134.3 millones de pesos"
        prose_patterns = [p for p in self._patterns() if p.source == "prose"]
        for p in prose_patterns:
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(134_300, rel=0.001)
                return
        pytest.fail("No cash prose pattern matched registró variant")


# ---------------------------------------------------------------------------
# Operating income patterns
# ---------------------------------------------------------------------------

class TestOperatingIncomePatterns:

    def _patterns(self):
        return METRIC_BY_KEY["operating_income"].patterns

    def test_operating_income_trimestre_fue(self):
        text = "La Utilidad de Operación en el trimestre fue $236.0 millones de pesos vs $122.9 millones de pesos"
        for p in self._patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(236_000.0, rel=0.001)
                return
        pytest.fail("No operating_income pattern matched trimestre fue")

    def test_operating_income_considerando_ifrs16(self):
        text = ("La Utilidad de Operación en el trimestre sin considerar el efecto IFRS 16 fue de $55.3 millones de "
                "pesos en el 1T26. Considerando el IFRS 16, se tuvo un aumento de 11.7% llegando a $107.9 millones "
                "de pesos vs $96.6 millones del mismo trimestre de 2025.")
        for p in self._patterns():
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(107_900.0, rel=0.001)
                return
        pytest.fail("No operating_income pattern matched considerando IFRS 16")

    def test_net_debt_con_ifrs_prose(self):
        samples = [
            ("Incluyendo el IFRS 16, la Deuda Financiera Neta ascendió a $1,241.0 millones de pesos", 1_241_000.0),
            ("Al cierre del 1T20, la Deuda Financiera Neta ascendió a $3,135.8 millones de pesos", 3_135_800.0),
        ]
        patterns = METRIC_BY_KEY["net_debt"].patterns
        for text, expected in samples:
            for p in patterns:
                if p.source != "prose":
                    continue
                result = try_pattern(text, p)
                if result:
                    vals, _ = result
                    assert vals[0] == pytest.approx(expected, rel=0.001)
                    break
            else:
                pytest.fail(f"No net_debt prose pattern matched: {text}")


# ---------------------------------------------------------------------------
# Operational KPI patterns (SPORT-specific)
# ---------------------------------------------------------------------------

class TestOperationalKpiPatterns:
    """Tests for company-specific KPIs loaded from configs/sport.yaml."""

    @pytest.fixture
    def sport_defs(self):
        from src.model.financial_model import METRICS, apply_config, load_config
        cfg = load_config(ROOT / "configs" / "sport.yaml")
        defs = apply_config(METRICS, cfg)
        return {m.key: m for m in defs}

    def test_clientes_activos_table(self, sport_defs):
        text = "                      Clientes activos totales 96,616 99,830  -3.2%"
        mdef = sport_defs["clientes_activos"]
        for p in mdef.patterns:
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(96616.0)
                assert vals[1] == pytest.approx(99830.0)
                return
        pytest.fail("No clientes_activos table pattern matched")

    def test_clientes_activos_maximo_historico(self, sport_defs):
        text = ("Al cierre del 2T16, el número de Clientes Activos alcanzó un máximo "
                "histórico de 69,230, lo cual impulsó el crecimiento.")
        mdef = sport_defs["clientes_activos"]
        for p in mdef.patterns:
            if p.source != "prose":
                continue
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(69230.0)
                return
        pytest.fail("No clientes_activos prose pattern matched máximo histórico")

    def test_clubs_count_rejects_footnote_491(self, sport_defs):
        """'49¹' pdfplumber renders as '491' → must extract 49, not 491."""
        text = "Grupo Sports World cerró el primer trimestre con 491 clubes en operación."
        mdef = sport_defs["clubs_count"]
        for p in mdef.patterns:
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(49.0), \
                    f"Expected 49 (2-digit capture strips footnote), got {vals[0]}"
                return
        # Acceptable if no pattern matches — better to return None than wrong value
        # (prose fallback will skip this)

    def test_clubs_count_split_prose(self, sport_defs):
        """Split prose should still capture the club count before the line wraps."""
        text = ("Grupo Sports World cerró el trimestre con 48 Los Ingresos Totales ascendieron a "
                "$1,046.7 millones de pesos, con un incremento del 1.6% vs 2024, "
                "clubes en operación.")
        mdef = sport_defs["clubs_count"]
        for p in mdef.patterns:
            if p.source != "prose":
                continue
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(48.0)
                return
        pytest.fail("No clubs_count prose pattern matched split sentence")

    def test_operating_profit_con_ifrs_prose(self, sport_defs):
        from src.model.sport_metrics import get_sport_metrics
        defs = {m.key: m for m in get_sport_metrics()}
        text = ("La Utilidad de Operación en el trimestre sin considerar el efecto IFRS 16 fue de $55.3 millones "
                "de pesos en el 1T26. Considerando el IFRS 16, se tuvo un aumento de 11.7% llegando a $107.9 "
                "millones de pesos vs $96.6 millones del mismo trimestre de 2025.")
        mdef = defs["operating_profit_con_ifrs"]
        for p in mdef.patterns:
            if p.source != "prose":
                continue
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(107900.0, rel=0.001)
                return
        pytest.fail("No operating_profit_con_ifrs prose pattern matched")

    def test_operating_profit_sin_ifrs_prose(self, sport_defs):
        from src.model.sport_metrics import get_sport_metrics
        defs = {m.key: m for m in get_sport_metrics()}
        text = ("La Utilidad de Operación en el trimestre sin considerar el efecto IFRS 16 fue de $55.3 millones "
                "de pesos en el 1T26. Considerando el IFRS 16, se tuvo un aumento de 11.7% llegando a $107.9 "
                "millones de pesos vs $96.6 millones del mismo trimestre de 2025.")
        mdef = defs["operating_profit_sin_ifrs"]
        for p in mdef.patterns:
            if p.source != "prose":
                continue
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(55300.0, rel=0.001)
                return
        pytest.fail("No operating_profit_sin_ifrs prose pattern matched")

    def test_aforo_table_exact(self, sport_defs):
        """Operational table: exact visit counts."""
        text = "                      Aforo promedio mensual 877,678 836,103  5.0%"
        mdef = sport_defs["monthly_visits"]
        for p in mdef.patterns:
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(877678.0)
                return
        pytest.fail("No monthly_visits pattern matched")

    def test_desercion_table(self, sport_defs):
        """Net churn from operational table."""
        text = "                      Deserción neta promedio   5.6%   5.6%  0.0 pp"
        mdef = sport_defs["net_churn"]
        for p in mdef.patterns:
            result = try_pattern(text, p)
            if result:
                vals, _ = result
                assert vals[0] == pytest.approx(5.6, abs=0.05)
                return
        pytest.fail("No net_churn pattern matched")
