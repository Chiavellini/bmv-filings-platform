#!/usr/bin/env python3
"""Generate configs/<slug>.yaml + inputs/<slug>.md + emitted Bloomberg templates for the full
coverage universe, from a single classification table (sector → template → members).

Peers are auto-assigned as the other members of the same sector group (single-currency MXN, so
absolute comparisons stay valid). Slug = slugified company name. Idempotent — safe to re-run.

Usage (from soft/):  python3 scripts/gen_universe.py [--no-emit]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MACRO = [
    ("Mexico GDP growth, %", "gdp_growth"),
    ("Banxico policy rate, %", "policy_rate"),
    ("USDMXN", "usdmxn"),
    ("Mexico CPI inflation, %", "inflation"),
]

# Claves absent from the BMV XBRL archive → Bloomberg-only fundamentals (fetch skips them).
# NOTE: XBRL-availability of the newer additions is UNVERIFIED — this set is best-effort;
# fetch_xbrl_batch just skips claves it can't find, so a false-negative here only annotates
# the doc, it doesn't break anything.
ABSENT = {"FEMSA", "BAFAR", "GLOBC", "FHIP", "KAMOSA", "SOMA", "AGRO", "XFRA", "COXA",
          # Bucket B (2026-07 roster reconciliation): BMV-listed but NOT in the free XBRL archive →
          # Yahoo-only fallback (6 price metrics; rest honest N/A). Some are suspended/delisted.
          "ELEKTRA", "AZTECA", "BACHOCO", "BBVA", "GSANBOR", "ALFA", "TS", "FRES", "MONEX",
          "CERAMIC", "ALEATIC", "ICA", "TEKCHEM", "JAVER", "SAVIA", "URBI", "SARE", "CREAL",
          "AGRIEXP", "ANB", "EDOARDO", "GOMO", "IASASA", "INGEAL", "QBINDUS", "QUMMA", "SAN", "SRE"}


# The declared ABSENT set above is a best-effort snapshot ("XBRL-availability … UNVERIFIED"). A
# company that actually has a filing corpus on disk is not source-absent regardless of the static
# set — so membership is DATA-DERIVED at read time: is_absent() subtracts any slug with cached raw
# XBRL *or* local quarterly-report PDFs/markdown. This auto-corrects stale entries (FEMSA, 20 cached
# XBRL quarters) AND names onboarded from IR-page PDFs (grupo_bafar — no BMV XBRL, but a downloaded
# PDF corpus feeds the same fundamentals path). Filesystem-derived, mirroring
# applicability._annual_periods.
_RAW_XBRL_SUFFIXES = (".json.gz", ".json")


def has_cached_xbrl(slug: str) -> bool:
    """True when ``data/reports/<slug>/xbrl/`` holds at least one raw BMV XBRL filing (excluding the
    ``_facts.json`` / ``_mdna`` extraction artifacts)."""
    xd = ROOT / "data" / "reports" / slug / "xbrl"
    if not xd.is_dir():
        return False
    for p in xd.iterdir():
        n = p.name
        if "_facts" in n or "_mdna" in n:
            continue
        if n.endswith(_RAW_XBRL_SUFFIXES):
            return True
    return False


def has_local_reports(slug: str) -> bool:
    """True when ``data/reports/<slug>/`` holds top-level quarterly-report PDFs/markdown — the
    IR-page-download corpus that feeds the tiered PDF cascade (the ``run()`` branch in
    load_fundamentals). Mirrors fundamentals._has_local_reports without importing across the
    coverage boundary."""
    rd = ROOT / "data" / "reports" / slug
    if not rd.is_dir():
        return False
    return any(rd.glob("*.md")) or any(rd.glob("*.pdf"))


def has_filing_corpus(slug: str) -> bool:
    """True when the company has any on-disk filing source — cached XBRL or a local PDF/markdown
    corpus. Either feeds the fundamentals extraction, so either takes it out of Yahoo-only land."""
    return has_cached_xbrl(slug) or has_local_reports(slug)


def is_absent(clave: str, slug: str) -> bool:
    """Effective source-absence: declared ABSENT AND no on-disk filing corpus for the slug (neither
    cached XBRL nor downloaded IR PDFs). Use this instead of ``clave in ABSENT`` at every runtime
    site so a company with a real filing source is never Yahoo-only-gated."""
    return clave in ABSENT and not has_filing_corpus(slug)

# sector group -> (template, [(clave, display_name)])
# Full 136-company coverage universe (5 bursatilidad tiers). Sub-sector groups are the peer
# sets (single-currency MXN, so absolute comparisons stay valid). Display names for companies
# that already have a hand-tuned config (walmex/chedraui/lacomer/liverpool/gruma/bimbo/ac/kof/
# kimber/lab/becle) are chosen so slugify(name) == the existing rich-config slug — the
# non-clobbering guard in main() then PRESERVES those rich configs instead of overwriting them.
UNIVERSE = {
    # ================= FINANCIALS (template=financials) =================
    "banks": ("financials", [
        ("GFNORTE", "GFNorte"), ("BBAJIO", "Banco del Bajio"), ("R", "Regional"),
        ("GFINBUR", "GFInbursa"), ("GENTERA", "Gentera"), ("GFMULTI", "GF Multiva"),
        ("FINDEP", "Financiera Independencia"), ("INVEX", "Invex"), ("ALTERNA", "Alterna"),
        ("BSMX", "Banco Santander Mexico"), ("UNIFIN", "Unifin"),
        ("BBVA", "BBVA Mexico"), ("MONEX", "Monex"), ("CREAL", "Credito Real")]),
    "brokers_exchange": ("financials", [
        ("GBM", "GBM"), ("ACTINVR", "Actinver"), ("VALUEGF", "Value GF"),
        ("FINAMEX", "Finamex"), ("BOLSA", "Bolsa Mexicana"), ("PROCORP", "Procorp")]),
    "insurers_afores": ("financials", [
        ("Q", "Qualitas"), ("GNP", "GNP"), ("PV", "Pena Verde"), ("GPROFUT", "Profuturo"),
        ("LASEG", "Latinoamericana Seguros")]),

    # ================= REITs / real estate (template=reit) =================
    "fibras": ("reit", [
        ("FUNO", "Fibra Uno"), ("FIBRAPL", "Fibra Prologis"), ("FIBRAMQ", "Fibra Macquarie"),
        ("FMTY", "Fibra Mty"), ("DANHOS", "Fibra Danhos"), ("FSHOP", "Fibra Shop"),
        ("FIHO", "Fibra Hotel"), ("FINN", "Fibra Inn"), ("FNOVA", "Fibra Nova"),
        ("FEXI", "Fibra Exi"), ("FPLUS", "Fibra Plus"), ("FCFE", "Fibra CFE"),
        ("STORAGE", "Fibra Storage"), ("FIBRAUP", "Fibra Uptown"), ("FIBRAHD", "Fibra HD"),
        ("FSITES", "Fibra Sites"), ("FHIP", "Fibra Hipotecaria"), ("FVIA", "Fibra Via"),
        ("FORION", "Fibra Orion"), ("FIDEAL", "Fibra Ideal"), ("NEXT", "Fibra Next"),
        ("FMX", "Fibra Mx"), ("GAV", "Grupo GAV")]),
    "real_estate": ("reit", [
        ("VESTA", "Vesta"), ("GICSA", "GICSA"), ("DINE", "Dine"),
        ("ARISTOS", "Grupo Aristos"), ("SOMA", "Soma"), ("XFRA", "Xfra"),
        ("PLANI", "Planigrupo"), ("INCARSO", "Inmuebles Carso")]),

    # ================= INDUSTRIALS (template=industrial; sub-sector = peer group) =========
    "metals_mining": ("industrial", [
        ("GMEXICO", "Grupo Mexico"), ("PE&OLES", "Penoles"), ("AUTLAN", "Autlan"),
        ("MFRISCO", "Minera Frisco"), ("FRES", "Fresnillo")]),
    "steel": ("industrial", [
        ("ICH", "Industrias CH"), ("SIMEC", "Simec"), ("COLLADO", "Grupo Collado"),
        ("TS", "Tenaris")]),
    "cement_materials": ("industrial", [
        ("CEMEX", "Cemex"), ("GCC", "GCC"), ("CMOCTEZ", "Cementos Moctezuma"),
        ("VITRO", "Vitro"), ("LAMOSA", "Grupo Lamosa"), ("ELEMAT", "Elementia Materiales"),
        ("FORTALE", "Fortaleza Materiales"), ("CERAMIC", "Interceramic")]),
    "chemicals": ("industrial", [
        ("ORBIA", "Orbia"), ("ALPEK", "Alpek"), ("CYDSASA", "Cydsa"), ("POCHTEC", "Pochteca"),
        ("TEKCHEM", "Tekchem")]),
    "telecom_towers": ("industrial", [
        ("AMX", "America Movil"), ("SITES1", "Telesites"), ("LASITE", "LaSite"),
        ("TELMEX", "Telmex")]),
    "cable_media": ("industrial", [
        ("MEGA", "Megacable"), ("TLEVISA", "Televisa"), ("AXTEL", "Axtel"),
        ("CTAXTEL", "CT Axtel"), ("CABLE", "Grupo Cable"), ("CIDMEGA", "Cidmega"),
        ("RCENTRO", "Grupo Radio Centro"), ("AZTECA", "TV Azteca")]),
    "beverages": ("industrial", [
        ("FEMSA", "FEMSA"), ("KOF", "KOF"), ("AC", "AC"), ("CUERVO", "Becle")]),
    "food_staples": ("industrial", [
        ("GRUMA", "Gruma"), ("BIMBO", "Bimbo"), ("SIGMA", "Sigma"), ("HERDEZ", "Herdez"),
        ("BAFAR", "Grupo Bafar"), ("CULTIBA", "Cultiba"), ("MINSA", "Minsa"),
        ("KUO", "Grupo Kuo"), ("LALA", "Grupo Lala"), ("BACHOCO", "Industrias Bachoco"),
        ("INGEAL", "Ingeal"), ("QUMMA", "Grupo Qumma")]),
    "household_personal": ("industrial", [
        ("KIMBER", "Kimber")]),
    "retail_selfservice": ("industrial", [
        ("WALMEX", "Walmex"), ("CHDRAUI", "Chedraui"), ("LACOMER", "Lacomer"),
        ("SORIANA", "Soriana"), ("GIGANTE", "Grupo Gigante")]),
    "retail_pharma": ("industrial", [
        ("FRAGUA", "Corporativo Fragua"), ("BEVIDES", "Farmacias Benavides"),
        ("NUTRISA", "Nutrisa")]),
    "dept_specialty": ("industrial", [
        ("LIVERPOL", "Liverpool"), ("GPH", "Palacio de Hierro"), ("ALSEA", "Alsea"),
        ("GFAMSA", "Grupo Famsa"), ("ELEKTRA", "Grupo Elektra"), ("GSANBOR", "Grupo Sanborns"),
        ("EDOARDO", "Edoardos Martin")]),
    "airports": ("industrial", [
        ("GAP", "GAP"), ("ASUR", "ASUR"), ("OMA", "OMA")]),
    "transport": ("industrial", [
        ("VOLAR", "Volaris"), ("AERO", "Aeromexico"), ("TRAXION", "Traxion"), ("TMM", "TMM"),
        ("GMXT", "GMexico Transportes")]),
    "infra": ("industrial", [
        ("PINFRA", "Pinfra"), ("IDEAL", "Ideal"), ("ESENTIA", "Esentia"), ("GMD", "GMD"),
        ("PASA", "PASA"), ("AGUA", "Rotoplas"), ("ALEATIC", "Aleatica"), ("ICA", "Empresas ICA")]),
    "energy": ("industrial", [
        ("VISTA", "Vista")]),
    "auto_capital_goods": ("industrial", [
        ("NEMAK", "Nemak"), ("GCARSO", "Grupo Carso"), ("GISSA", "Grupo GIS"),
        ("CONVER", "Convertidora"), ("ACCELSA", "Accel"), ("VASCONI", "Vasconia")]),
    "homebuilders": ("industrial", [
        ("ARA", "Consorcio ARA"), ("VINTE", "Vinte"), ("CADU", "Cadu"),
        ("HOMEX", "Homex"), ("JAVER", "Javer"), ("URBI", "Urbi"), ("SARE", "Sare")]),
    "hotels_leisure": ("industrial", [
        ("HOTEL", "Grupo Hotelero"), ("HCITY", "Hoteles City"), ("POSADAS", "Grupo Posadas"),
        ("RLH", "RLH Properties"), ("SPORT", "Sports World"), ("CMR", "CMR"), ("CIE", "CIE")]),
    "sports_ent": ("industrial", [
        ("AGUILAS", "Club America"), ("DIABLOS", "Diablos Rojos")]),
    "healthcare": ("industrial", [
        ("LAB", "Lab"), ("MEDICA", "Medica Sur")]),
    "agro_forestry": ("industrial", [
        ("TEAK", "Proteak"), ("AGRO", "Grupo Agro"), ("SAVIA", "Savia"),
        ("AGRIEXP", "Agroexportadora")]),
    "misc_industrial": ("industrial", [
        ("GLOBC", "Globc"), ("EDUCA", "Aleatica Educa"), ("COXA", "Coxa"),
        ("KAMOSA", "Kamosa"), ("ALFA", "Alfa"), ("ANB", "ANB"), ("GOMO", "Grupo GOMO"),
        ("IASASA", "IASA"), ("QBINDUS", "QB Industrial"), ("SAN", "SAN"), ("SRE", "SRE")]),
}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


# Markers that identify a hand-tuned artifact we must NOT overwrite with a minimal stub.
_CONFIG_HANDTUNED = ("metric_overrides", "custom_metrics", "sections:", "segments_file", "unavailable:")
_INPUT_HANDTUNED = ("## Blocks", "## Segments")


def _handtuned(path: Path, markers) -> bool:
    """True if the file exists and carries a hand-tuned marker (rich extractor / block spec)."""
    return path.exists() and any(m in path.read_text("utf-8") for m in markers)


def gen_config(name, ticker, clave, template):
    return (
        f"# {name} — minimal coverage config (XBRL-sourced fundamentals; no custom extractor).\n"
        f"company:\n  name: {name}\n  ticker: \"{ticker}\"\n  currency: MXN\n"
        f"  exchange: BMV\n  unit: millions\n\n"
        f"tier_precedence:\n  default: [xbrl, bmv, prose, search, table, calc]\n\n"
        f"ir_website:\n  xbrl_ticker: \"{clave}\"\n"
    )


def gen_input(name, ticker, template, peers):
    lines = [f"# {name}", f"Ticker: {ticker}", "",
             "## Settings", "- currency: MXN", "- units: millions",
             "- history_years: 5", f"- template: {template}", ""]
    lines += ["## Peers"] + [f"- {pc} MM {{{pslug}}}" for pc, pslug in peers] + [""]
    lines += ["## Macro"] + [f"- {lab} {{{k}}}" for lab, k in MACRO] + [""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-emit", action="store_true", help="skip emitting Bloomberg templates")
    args = ap.parse_args()

    entries = []  # (slug, name, clave, template, sector)
    for sector, (template, members) in UNIVERSE.items():
        for clave, name in members:
            entries.append((slugify(name), name, clave, template, sector))

    # detect duplicate slugs
    seen = {}
    for slug, name, *_ in entries:
        if slug in seen and seen[slug] != name:
            print(f"[gen] WARNING duplicate slug {slug!r}: {seen[slug]} vs {name}")
        seen[slug] = name

    preserved = []  # slugs whose hand-tuned config/input we left untouched
    for slug, name, clave, template, sector in entries:
        ticker = f"{clave} MM"
        # peers = other sector-mates (cap 5)
        mates = [(c, slugify(n)) for c, n in UNIVERSE[sector][1] if n != name][:5]
        cfg = ROOT / "configs" / f"{slug}.yaml"
        inp = ROOT / "inputs" / f"{slug}.md"
        cfg_kept = _handtuned(cfg, _CONFIG_HANDTUNED)
        inp_kept = _handtuned(inp, _INPUT_HANDTUNED)
        if not cfg_kept:
            cfg.write_text(gen_config(name, ticker, clave, template), "utf-8")
        if not inp_kept:
            inp.write_text(gen_input(name, ticker, template, mates), "utf-8")
        if cfg_kept or inp_kept:
            preserved.append(slug)

    print(f"[gen] wrote {len(entries)} entries "
          f"({len(entries) - len(preserved)} generated, {len(preserved)} preserved hand-tuned)")
    if preserved:
        print(f"[gen] preserved (hand-tuned, not overwritten): {', '.join(sorted(preserved))}")
    print(f"[gen] templates: "
          + ", ".join(f"{t}={sum(1 for e in entries if e[3]==t)}"
                      for t in ("industrial", "financials", "reit")))
    print(f"[gen] XBRL-absent (Bloomberg-only): {sorted(ABSENT)}")

    if not args.no_emit:
        for slug, *_ in entries:
            subprocess.run([sys.executable, str(ROOT / "scripts" / "emit_bloomberg_template.py"),
                            str(ROOT / "inputs" / f"{slug}.md")],
                           check=False, capture_output=True)
        print(f"[gen] emitted {len(entries)} Bloomberg templates")

    # write the source-of-truth doc
    doc = ["# Coverage Universe", "",
           "Source of truth for the soft-coverage roster. slug = slugified name.",
           "Peers = sector-mates (single-currency MXN).", "",
           "| slug | clave | template | sector | XBRL |", "|---|---|---|---|---|"]
    for slug, name, clave, template, sector in sorted(entries):
        xbrl = "none" if is_absent(clave, slug) else "yes"
        doc.append(f"| {slug} | {clave} | {template} | {sector} | {xbrl} |")
    (ROOT / "docs" / "UNIVERSE.md").write_text("\n".join(doc) + "\n", "utf-8")
    print(f"[gen] wrote docs/UNIVERSE.md")


if __name__ == "__main__":
    main()
