#!/usr/bin/env python3
"""Route the pulled forward consensus EPS into per-company Bloomberg packs.

`forward_eps.csv` is keyed by the Bloomberg ticker STRING as it appears in the specs (e.g. "AC MM").
For every `inputs/<slug>.md`, write/merge `data/bloomberg/<slug>.csv` with a subject `eps_ntm` row
(from `spec.ticker`) and a peer `eps_ntm` row per peer (from `peer.name`, keyed by `peer.slug`).
Existing pack rows are preserved; only `eps_ntm` rows are added/updated. Companies with no EPS for
subject or any peer get no pack (they build fully native — no forward P/E).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.coverage.spec import parse_spec  # noqa: E402

COLS = ["entity", "entity_kind", "field", "period", "label", "unit", "bbg_hint", "value"]


def _eps_by_ticker() -> dict[str, float]:
    out: dict[str, float] = {}
    with (ROOT / "outputs" / "_bloomberg" / "forward_eps.csv").open(encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        for row in r:
            try:
                out[row["company"].strip()] = float(row["forward_eps"])
            except (ValueError, KeyError):
                pass
    return out


def _load_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [row for row in csv.DictReader(fh)]


def main() -> None:
    eps = _eps_by_ticker()
    bbg_dir = ROOT / "data" / "bloomberg"
    bbg_dir.mkdir(parents=True, exist_ok=True)
    n_written = n_cells = 0

    for spec_path in sorted((ROOT / "inputs").glob("*.md")):
        try:
            spec = parse_spec(str(spec_path))
        except Exception:
            continue

        # (entity, entity_kind) -> eps value for this company
        new_eps: dict[tuple[str, str], tuple[str, float]] = {}
        if spec.ticker and spec.ticker.strip() in eps:
            new_eps[(spec.slug, "subject")] = (spec.ticker.strip(), eps[spec.ticker.strip()])
        for p in spec.peers:
            tk = (p.name or "").strip()
            if tk in eps:
                new_eps[(p.slug, "peer")] = (tk, eps[tk])
        if not new_eps:
            continue  # nothing to add for this company → stays native

        path = bbg_dir / f"{spec.slug}.csv"
        existing = _load_existing(path)
        # drop any prior eps_ntm rows we are replacing; keep everything else
        keep = [row for row in existing
                if not (row.get("field") == "eps_ntm"
                        and (row.get("entity"), row.get("entity_kind")) in new_eps)]
        for (entity, kind), (tk, val) in new_eps.items():
            label = ("Consensus EPS, next 12m" if kind == "subject"
                     else f"{tk} — Consensus EPS, next 12m")
            keep.append({"entity": entity, "entity_kind": kind, "field": "eps_ntm",
                         "period": "", "label": label, "unit": "price",
                         "bbg_hint": "BEST_EPS (NTM)", "value": val})
            n_cells += 1

        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            for row in keep:
                w.writerow({c: row.get(c, "") for c in COLS})
        n_written += 1

    print(f"[eps] wrote {n_cells} eps_ntm cells across {n_written} company packs "
          f"(from {len(eps)} tickers with consensus)")


if __name__ == "__main__":
    main()
