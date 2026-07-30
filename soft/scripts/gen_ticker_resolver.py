#!/usr/bin/env python3
"""Build a Bloomberg ticker-resolver sheet.

The tickers in the specs lack the exact BMV series suffix Bloomberg needs (AC → AC*, CEMEX →
CEMEXCPO, FUNO → FUNO11, …) and the suffix varies per issuer. Rather than guess 145 of them, emit
a sheet that lists candidate tickers (bare clave + each common BMV suffix) each wrapped in a NAME
lookup. Paste it into a Bloomberg Excel: for each company exactly one candidate returns the real
company name — that's the correct ticker. Bloomberg resolves; no guessing.

Output: outputs/_bloomberg/ticker_resolver.csv  (columns: orig, candidate, name_check_formula)
"""
from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# common BMV series suffixes (Bloomberg appends these to the clave); "" = bare, plus the original.
_SUFFIXES = ["", "*", "A", "B", "O", "L", "CPO", "UBL", "UBD", "UBC", "UB", "11", "B-1", "C-1", "A-1"]


def _candidates(orig: str) -> list[str]:
    """orig like 'AC MM' or 'COST US' or 'CHDRAUI* MM' → candidate ticker strings."""
    parts = orig.rsplit(" ", 1)
    if len(parts) != 2:
        return [orig]
    sym, exch = parts
    root = sym.rstrip("*")
    out, seen = [], set()
    for cand_sym in [sym] + [root + s for s in _SUFFIXES]:
        tk = f"{cand_sym} {exch}"
        if tk not in seen:
            seen.add(tk)
            out.append(tk)
    return out


def main() -> None:
    src = ROOT / "outputs" / "_bloomberg" / "necessary_pull.csv"
    tickers = []
    with src.open(encoding="utf-8") as fh:
        r = csv.reader(fh)
        next(r, None)
        for row in r:
            if row and row[0]:
                tickers.append(row[0])

    out = ROOT / "outputs" / "_bloomberg" / "ticker_resolver.csv"
    n = 0
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["orig_ticker", "candidate", "name_check"])
        for orig in tickers:
            for cand in _candidates(orig):
                w.writerow([orig, cand, f'=BDP("{cand} Equity","NAME")'])
                n += 1
    print(f"[resolver] {len(tickers)} companies → {n} candidate rows -> {out}")


if __name__ == "__main__":
    main()
