#!/usr/bin/env python3
"""build_universe.py — resolve the full expandable universe offline.

For every soft slug with quarterly XBRL facts, resolve slug -> BMV ticker
(facts filenames) -> Yahoo symbol, using in order: the phase-A explicit map,
soft's symbol_probe.md table, soft configs' company.ticker, and BMV-ticker +
series-suffix candidates — accepting only symbols whose frozen snapshot file
has usable bars. Writes configs/universe_full.yaml (frozen; commit before the
first expanded run).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import yaml

SUFFIXES = ["", "B", "A", "O", "CPO", "UBC", "UB", "UBL", "UBD", "1", "L", "C",
            "B-1", "C-1", "A-1", "11", "13", "14", "15", "16", "17", "18"]

# facts-filename ticker -> BMV archive ticker (soft strips the ampersand)
TICKER_ALIASES = {"PEOLES": "PE&OLES"}


def main() -> None:
    cfg = bs.load_config()
    phase_a = {slug: v["symbol"].replace(".MX", "")
               for slug, v in cfg["universe"].items()}

    # slugs with quarterly facts (+ their BMV ticker from filenames)
    slugs: dict[str, dict] = {}
    for d in sorted((bs.SOFT_ROOT / "data" / "reports").glob("*/xbrl")):
        q = [f for f in d.glob("*_facts.json")
             if re.search(r"_20\d{2}-[1-4]T_facts", f.name)]
        if q:
            slugs[d.parent.name] = {
                "ticker": q[0].name.split("_", 1)[0], "n_quarters": len(q)}

    # snapshot symbols with usable bars
    snap_syms = set()
    for f in bs.SNAPSHOT_DIR.glob("yf_*.json"):
        if f.name == "yf_MXX_INDEX.json":
            continue
        try:
            if json.loads(f.read_text()).get("timestamp"):
                snap_syms.add(f.name[3:-8])
        except Exception:
            pass

    probe = {}
    pm = (bs.SOFT_ROOT / "outputs" / "_reconcile" / "symbol_probe.md").read_text()
    for m in re.finditer(r"- (\w+)\s+→\s+`([A-Z0-9.&-]+)\.MX`", pm):
        probe[m.group(1)] = m.group(2)

    cfg_ticker = {}
    for f in (bs.SOFT_ROOT / "configs").glob("*.yaml"):
        try:
            c = yaml.safe_load(f.read_text()) or {}
            t = ((c.get("company") or {}).get("ticker") or "")
            if t:
                cfg_ticker[f.stem] = t
        except Exception:
            pass

    resolved: dict[str, dict] = {}
    unresolved: list[dict] = []
    for slug, info in sorted(slugs.items()):
        sym = None
        source = None
        if slug in phase_a and phase_a[slug] in snap_syms:
            sym, source = phase_a[slug], "phase_a"
        elif slug in probe and probe[slug] in snap_syms:
            sym, source = probe[slug], "symbol_probe"
        else:
            ct = (cfg_ticker.get(slug, "")
                  .replace(" MM", "").replace("*", "").replace("/", ""))
            for base in [ct, info["ticker"], TICKER_ALIASES.get(info["ticker"], "")]:
                if not base:
                    continue
                for suf in SUFFIXES:
                    cand = (base + suf).upper().replace(" ", "")
                    if cand in snap_syms:
                        sym, source = cand, f"candidate({base})"
                        break
                if sym:
                    break
        if sym:
            resolved[slug] = {"ticker": info["ticker"], "symbol": f"{sym}.MX",
                              "source": source, "n_quarters": info["n_quarters"]}
        else:
            unresolved.append({"slug": slug, "ticker": info["ticker"],
                               "n_quarters": info["n_quarters"],
                               "reason": "no snapshot price series resolvable"})

    out = {
        "universe": {s: {"ticker": v["ticker"], "symbol": v["symbol"]}
                     for s, v in resolved.items()},
        "resolution_sources": {s: v["source"] for s, v in resolved.items()},
        "excluded": unresolved,
    }
    path = bs.EARNINGS_ROOT / "configs" / "universe_full.yaml"
    path.write_text(yaml.safe_dump(out, sort_keys=True, allow_unicode=True))

    # ---- verify block ----
    print(f"slugs with quarterly facts : {len(slugs)}")
    print(f"resolved                   : {len(resolved)} "
          f"(~{sum(v['n_quarters'] for v in resolved.values())} company-quarters)")
    print(f"excluded                   : {len(unresolved)} -> "
          f"{[u['slug'] for u in unresolved]}")
    for s in ("walmex", "femsa", "liverpool", "gruma", "becle"):
        print(f"  spot-check {s}: {resolved.get(s, {}).get('symbol')} "
              f"[{resolved.get(s, {}).get('source')}]")
    n_new = len([s for s in resolved if s not in cfg["universe"]])
    print(f"NEW companies vs phase A   : {n_new}")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
