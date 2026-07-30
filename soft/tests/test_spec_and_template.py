"""Phase-1 tests — coverage-spec parsing, template emit, and pack ingest."""
from __future__ import annotations

from pathlib import Path

from src.bloomberg.ingest import load_pack
from src.bloomberg.schema import history_years, required_rows
from src.bloomberg.template import write_template
from src.coverage.spec import parse_spec

SOFT_ROOT = Path(__file__).resolve().parents[1]
WALMEX_MD = SOFT_ROOT / "inputs" / "walmex.md"


def test_parse_walmex_spec():
    spec = parse_spec(WALMEX_MD)
    assert spec.slug == "walmex"
    assert spec.name == "Walmex"
    assert spec.ticker == "WALMEX* MM"
    assert spec.ir_url and spec.ir_url.startswith("https://")
    assert spec.currency == "MXN"
    assert spec.units == "millions"
    assert spec.history_years == 5
    assert spec.template == "industrial"
    # walmex.md lists an explicit ## Blocks section; the industrial template also auto-appends the
    # four deeper-analysis blocks (profitability/fcf_liquidity/temporal_ebit/growth).
    assert set(spec.blocks) == {
        "snapshot_multiples", "historical_multiples", "sum_of_the_parts",
        "replacement_value", "financial_analysis", "macro_sector",
        "profitability", "fcf_liquidity", "temporal_ebit", "growth",
    }
    assert [p.slug for p in spec.peers] == ["chedraui", "soriana", "lacomer", "costco"]
    assert [s.key for s in spec.segments] == ["mexico", "cam"]
    assert [m.key for m in spec.macro][:2] == ["gdp_growth", "policy_rate"]


def test_required_rows_scope_expansion():
    spec = parse_spec(WALMEX_MD)
    rows = required_rows(spec, base_year=2026)

    # Subject gets market + estimate fields but NOT peer-only fundamentals.
    subj = {r.field for r in rows if r.entity_kind == "subject"}
    assert {"px_last", "shares_out", "net_debt", "dvd_yield", "replacement_cost_per_sqm"} <= subj
    assert "eps_ntm" not in subj
    assert "sales_ltm" not in subj  # peer-only

    # Each peer gets market + peer fundamentals.
    peer_rows = [r for r in rows if r.entity_kind == "peer"]
    peers = {r.entity for r in peer_rows}
    assert peers == {"chedraui", "soriana", "lacomer", "costco"}
    chedraui_fields = {r.field for r in peer_rows if r.entity == "chedraui"}
    assert {"px_last", "ebitda_ltm", "total_equity", "fcf_ltm"} <= chedraui_fields

    # Segments get one seg_ev_ebitda each.
    seg_rows = [r for r in rows if r.entity_kind == "segment"]
    assert {r.entity for r in seg_rows} == {"mexico", "cam"}

    # History: 5 FY rows for the subject price.
    hist = [r for r in rows if r.entity_kind == "history"]
    assert len(hist) == 5
    assert {int(r.period) for r in hist} == set(history_years(spec, base_year=2026))

    # Macro: one row per macro line.
    macro = [r for r in rows if r.entity_kind == "macro"]
    assert len(macro) == len(spec.macro)


def test_blocks_subset_prunes_rows():
    spec = parse_spec(WALMEX_MD)
    spec.blocks = ["snapshot_multiples"]  # only the snapshot block
    rows = required_rows(spec, base_year=2026)
    kinds = {r.entity_kind for r in rows}
    assert "history" not in kinds       # historical_multiples pruned
    assert "segment" not in kinds       # sum_of_the_parts pruned
    assert "macro" not in kinds         # macro_sector pruned
    assert "subject" in kinds and "peer" in kinds


def test_template_roundtrip(tmp_path):
    spec = parse_spec(WALMEX_MD)
    csv_path = tmp_path / "walmex.bloomberg.csv"
    write_template(spec, csv_path, base_year=2026)

    text = csv_path.read_text(encoding="utf-8")
    header = text.splitlines()[0]
    assert header == "entity,entity_kind,field,period,label,unit,bbg_hint,value"

    # A freshly-emitted (all-blank) template ingests to an empty pack with every cell "missing".
    res = load_pack(csv_path, slug="walmex")
    assert res.pack.slug == "walmex"
    assert not res.pack.subject and not res.pack.peers
    assert len(res.missing) == len(text.splitlines()) - 1


def test_ingest_filled_values(tmp_path):
    """Fill a couple of cells and confirm they route to the right pack buckets."""
    csv_path = tmp_path / "walmex.bloomberg.csv"
    csv_path.write_text(
        "entity,entity_kind,field,period,label,unit,bbg_hint,value\n"
        "walmex,subject,px_last,,Last price,price,PX_LAST,58.4\n"
        "walmex,subject,shares_out,,Shares,shares,EQY_SH_OUT,17400\n"
        "chedraui,peer,ebitda_ltm,,CHDRAUI EBITDA,currency,EBITDA,15200\n"
        "mexico,segment,seg_ev_ebitda,,Mexico EV/EBITDA,x,comp,9.5\n"
        "walmex,history,px_fy_close,2024,Price FY2024,price,PX_LAST,62.1\n"
        "gdp_growth,macro,value,,Mexico GDP growth,pct,GDP,1.4\n",
        encoding="utf-8",
    )
    res = load_pack(csv_path, slug="walmex")
    assert res.pack.subject["px_last"] == 58.4
    assert res.pack.subject["shares_out"] == 17400
    assert res.pack.peers["chedraui"]["ebitda_ltm"] == 15200
    assert res.pack.segment_multiples["mexico"] == 9.5
    assert res.pack.history[2024] == 62.1
    assert res.pack.macro["gdp_growth"] == 1.4
    assert res.missing == []
