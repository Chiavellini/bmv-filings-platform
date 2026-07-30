#!/usr/bin/env python3
"""Build the CLEAN final Bloomberg pull sheet — only tickers Bloomberg already confirmed.

Reads the returned `ticker_resolved.csv` (candidate → NAME) and emits ONLY the candidates that
returned a real company name. No invalid candidates, no FIBRA year brute-force, no phantom "/OLD"
series. For each company:
  - the validated tickers (dropping "/OLD" names, de-duping identical names)
  - plus a curated fix for the few that returned nothing (known FIBRA/ADR tickers)
Each row pulls NAME, PX_LAST, CUR_MKT_CAP, BEST_EPS, NAV_PER_SHARE. Multi-series companies keep
their 1-2 real series; CUR_MKT_CAP picks the traded one. One paste, essentially zero #N/A.

Output: outputs/_bloomberg/final_pull.csv
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BB = ROOT / "outputs" / "_bloomberg"

REIT = set()
with (BB / "necessary_map.csv").open(encoding="utf-8") as fh:
    r = csv.reader(fh); next(r, None)
    for row in r:
        if len(row) >= 2 and row[1] == "nav_ps":
            REIT.add(row[0])

# curated tickers for the companies that returned NO valid candidate (known, not guessed)
CURATED: dict[str, list[str]] = {
    "DANHOS MM": ["DANHOS13 MM"], "FIHO MM": ["FIHO12 MM"], "FINN MM": ["FINN13 MM"],
    "FMTY MM": ["FMTY14 MM"], "FNOVA MM": ["FNOVA17 MM"], "FSHOP MM": ["FSHOP13 MM"],
    "LIVERPOL MM": ["LIVEPOLC-1 MM"], "GCARSO MM": ["GCARSOA1 MM"], "FMX MM": ["FMX US"],
    "LACOMER MM": ["LACOMUBC MM"], "SORIANA B MM": ["SORIANAB MM"], "GPH MM": ["GPH1 MM"],
}
# companies I can't ticker confidently (brand-new 2024 FIBRAs / delisted small-caps) — do NOT guess;
# reported separately. Their forward P/E just stays blank (everything else is native).
UNCERTAIN = {"FCFE MM", "FEXI MM", "FORION MM", "FVIA MM", "NEXT MM", "SOMA MM", "XFRA MM",
             "FPLUS MM", "FSITES MM", "FIDEAL MM", "FIBRAHD MM", "AGRO MM", "KAMOSA MM",
             "ISTA MM", "SPORT MM"}


def valid(name: str) -> bool:
    n = (name or "").strip()
    return bool(n) and not n.startswith("#") and "Invalid" not in n and "N/A" not in n


def main() -> None:
    resolved = defaultdict(list)
    with (BB / "ticker_resolved.csv").open(encoding="utf-8", errors="ignore") as fh:
        r = csv.reader(fh); next(r, None)
        for row in r:
            if len(row) >= 3:
                resolved[row[0]].append((row[1], row[2].strip()))

    rows: list[tuple[str, str]] = []   # (company, ticker)
    uncertain: list[str] = []
    for orig, cands in resolved.items():
        hits = [(c, n) for c, n in cands if valid(n)]
        # drop clearly-dead "/OLD" listings; de-dupe by the returned name
        hits = [(c, n) for c, n in hits if "/OLD" not in n.upper()]
        seen_names, keep = set(), []
        for c, n in hits:
            key = n.upper()
            if key not in seen_names:
                seen_names.add(key)
                keep.append(c)
        if keep:
            for c in keep:
                rows.append((orig, c))
        elif orig in CURATED:
            for c in CURATED[orig]:
                rows.append((orig, c))
        else:
            uncertain.append(orig)

    def bdp(tk, field):
        return f'=BDP("{tk} Equity","{field}")'

    # NAV_PER_SHARE confirmed an invalid field for these equities → dropped (P/NAV is optional).
    out = BB / "final_pull.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["company_clave", "ticker", "is_reit", "NAME", "PX_LAST", "CUR_MKT_CAP",
                    "BEST_EPS"])
        for orig, tk in sorted(rows):
            reit = orig in REIT
            w.writerow([orig, tk, "Y" if reit else "",
                        bdp(tk, "NAME"), bdp(tk, "PX_LAST"), bdp(tk, "CUR_MKT_CAP"),
                        bdp(tk, "BEST_EPS")])

    n_multi = sum(1 for o in resolved if len([1 for _, tk in rows if _ == o]) > 1)
    print(f"[final] {len(rows)} rows across {len({o for o,_ in rows})} companies "
          f"({n_multi} still multi-series → picked by market cap) -> {out}")
    if uncertain:
        print(f"[final] {len(uncertain)} not confidently on Bloomberg (skip — forward P/E blank, "
              f"rest native): {', '.join(sorted(uncertain))}")


if __name__ == "__main__":
    main()
