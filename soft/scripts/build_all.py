#!/usr/bin/env python3
"""Batch-build the whole coverage universe through the hard math gate, then write a scorecard.

    python3 scripts/build_all.py                 # build every company that has an input spec
    python3 scripts/build_all.py --only walmex,gfnorte
    python3 scripts/build_all.py --template reit --no-network

For each company it runs the same pipeline as ``build_coverage.py`` (via ``build_one``) — so every
emitted workbook has passed the math gate — and records the outcome. A company that lacks filings,
lacks a Bloomberg pack, or trips the gate is reported with the reason; the batch never aborts.

Scorecard → ``outputs/_scorecard/soft_coverage_<date>.{md,csv}``: the live "how far from done" view.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_coverage import BuildResult, build_one  # noqa: E402
from scripts.gen_universe import UNIVERSE, slugify  # noqa: E402
from src.shared.paths import REPORTS_DIR  # noqa: E402

# XBRL reported in USD while the equity lists in MXN — multiples mix currencies until the
# Bloomberg pack supplies MXN market data. Flagged, not blocked.
USD_XBRL = {"grupo_mexico", "orbia", "america_movil"}


def roster():
    for _sector, (template, members) in UNIVERSE.items():
        for clave, name in members:
            yield slugify(name), name, clave, template


def _has_filings(slug: str) -> bool:
    d = REPORTS_DIR / slug
    return d.is_dir() and any(d.rglob("*"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default=None, help="comma-separated slugs")
    ap.add_argument("--template", default=None, help="restrict to industrial|financials|reit")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-network", action="store_true",
                    help="fully offline: cached filings, no prices/macro (market cells blank)")
    ap.add_argument("--cached-funds", action="store_true",
                    help="fast path: cached XBRL filings + LIVE Yahoo prices/macro (skips the "
                         "~85s/company archive fetch). Ideal for the ~47 already-cached companies.")
    ap.add_argument("--no-gate", action="store_true", help="DEBUG: emit even on math failure")
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip companies whose workbook already exists")
    ap.add_argument("--fast", action="store_true",
                    help="skip subject MD&A + peer cross-section (XBRL fundamentals + subject price "
                         "only) — big speedup for reviewing subject completeness")
    args = ap.parse_args()
    offline_funds = True if args.cached_funds else None  # None → follows no_network

    only = set(args.only.split(",")) if args.only else None
    rows = [r for r in roster() if (ROOT / "inputs" / f"{r[0]}.md").exists()]
    if only:
        rows = [r for r in rows if r[0] in only]
    if args.template:
        rows = [r for r in rows if r[3] == args.template]
    if args.limit:
        rows = rows[: args.limit]

    results = []
    print(f"[build-all] {len(rows)} companies "
          f"({'offline' if args.no_network else 'with network'}, "
          f"gate {'OFF' if args.no_gate else 'ON'})")
    for slug, name, clave, template in rows:
        xlsx = ROOT / "outputs" / name / "excel" / f"{name}.xlsx"
        if args.skip_existing and xlsx.exists():
            print(f"  skip {slug} (exists)")
            continue
        spec_path = ROOT / "inputs" / f"{slug}.md"
        # Bulletproof: a transient workspace I/O stall (TimeoutError/OSError) or any per-company
        # crash must NEVER kill the whole batch. Retry once for transient IO, then record + move on.
        res = None
        for attempt in (1, 2):
            try:
                res = build_one(spec_path, no_network=args.no_network, gate=not args.no_gate,
                                offline_fundamentals=offline_funds, subject_facts_only=args.fast)
                break
            except (OSError, TimeoutError) as e:
                if attempt == 2:
                    res = BuildResult(slug, name, template, emitted=False, audit_passed=False,
                                      n_errors=0, n_advisories=0, n_blank=0, has_bbg_pack=False,
                                      n_periods=0, error=f"IO/{type(e).__name__}: {e}")
                else:
                    time.sleep(1.0)
            except Exception as e:  # any other per-company failure → record, keep going
                res = BuildResult(slug, name, template, emitted=False, audit_passed=False,
                                  n_errors=0, n_advisories=0, n_blank=0, has_bbg_pack=False,
                                  n_periods=0, error=f"{type(e).__name__}: {e}")
                break
        note = "USD-XBRL vs MXN listing" if slug in USD_XBRL else ""
        if res.error:
            verdict = "ERROR"
        elif not res.emitted:
            verdict = "BLOCKED"
        elif res.audit_passed:
            verdict = "PASS"
        else:
            verdict = "EMITTED*"  # gate off
        # Completeness status is the deliverable-truth verdict (independent of the math gate): a
        # math-PASS workbook can still be BROKEN (engine gap / wrong value) or INCOMPLETE (needs data).
        status = res.coverage_status or ("—" if res.error else "?")
        results.append({
            "slug": slug, "name": name, "clave": clave, "template": template,
            "has_filings": _has_filings(slug), "has_bbg_pack": res.has_bbg_pack,
            "periods": res.n_periods, "emitted": res.emitted, "verdict": verdict,
            "status": status, "miss_engine": res.n_missing_engine,
            "miss_data": res.n_missing_data, "sentinels": res.n_sentinels,
            "errors": res.n_errors, "advisories": res.n_advisories, "blank": res.n_blank,
            "note": (res.error or note),
        })
        mark = {"PASS": "✅", "INCOMPLETE": "⚠️", "BROKEN": "🛑"}.get(status,
               {"ERROR": "💥", "BLOCKED": "❌"}.get(verdict, "?"))
        print(f"  {mark} {slug:24s} {status:10s} gate={verdict:8s} "
              f"eng_gap={res.n_missing_engine} bad={res.n_sentinels} "
              f"need_data={res.n_missing_data} {res.error[:50]}", flush=True)
        _write_scorecard(results, quiet=True)  # incremental — partial scorecard visible mid-run

    _write_scorecard(results)


def _write_scorecard(results: list[dict], quiet: bool = False) -> None:
    out_dir = ROOT / "outputs" / "_scorecard"
    out_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.date.today().isoformat()
    csv_path = out_dir / f"soft_coverage_{day}.csv"
    md_path = out_dir / f"soft_coverage_{day}.md"

    cols = ["slug", "name", "clave", "template", "has_filings", "has_bbg_pack", "periods",
            "emitted", "verdict", "status", "miss_engine", "sentinels", "miss_data",
            "errors", "advisories", "blank", "note"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    n = len(results)
    n_pass = sum(1 for r in results if r.get("status") == "PASS")
    n_incomplete = sum(1 for r in results if r.get("status") == "INCOMPLETE")
    n_broken = sum(1 for r in results if r.get("status") == "BROKEN")
    n_error = sum(1 for r in results if r["verdict"] == "ERROR")
    n_gateblock = sum(1 for r in results if r["verdict"] == "BLOCKED")
    n_bbg = sum(1 for r in results if r["has_bbg_pack"])
    n_filings = sum(1 for r in results if r["has_filings"])

    lines = [f"# Soft Coverage audit — {day}", ""]
    lines.append(f"- Companies attempted: **{n}**")
    lines.append(f"- Completeness: ✅ PASS **{n_pass}**  ·  ⚠️ INCOMPLETE (needs data) **{n_incomplete}**"
                 f"  ·  🛑 BROKEN (engine gap/wrong value) **{n_broken}**")
    lines.append(f"- Pipeline: 💥 ERROR **{n_error}**  ·  ❌ math-gate BLOCKED **{n_gateblock}**")
    lines.append(f"- Have cached filings: **{n_filings}/{n}**  ·  have Bloomberg pack: "
                 f"**{n_bbg}/{n}**")
    lines.append("")
    lines.append("> **Read this column, not the math gate.** 🛑 **BROKEN** = a required cell is blank "
                 "though it should derive from the filing, or a value is self-evidently wrong "
                 "(these are OUR bugs — fix before shipping). ⚠️ **INCOMPLETE** = correct as far as it "
                 "goes but a required market/estimate cell is still awaiting its Bloomberg value. "
                 "✅ **PASS** = complete and sane. The math gate only proves arithmetic, not coverage.")
    lines.append("")
    lines.append("| Company | Clave | Template | Filings | BBG | Periods | Status | EngGap | Bad | NeedData | Gate | Blank | Note |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    order = {"BROKEN": 0, "INCOMPLETE": 1, "PASS": 2}
    # Worst first: pipeline errors/blocks, then BROKEN, then INCOMPLETE (most engine gaps first), then PASS.
    def sort_key(x):
        if x["verdict"] in ("ERROR", "BLOCKED"):
            return (-1, 0, 0)
        return (order.get(x.get("status"), 9), -x.get("miss_engine", 0), -x.get("miss_data", 0))
    for r in sorted(results, key=sort_key):
        st = "💥 ERROR" if r["verdict"] == "ERROR" else (
            "❌ BLOCKED" if r["verdict"] == "BLOCKED" else r.get("status", "?"))
        lines.append(
            f"| {r['name']} | {r['clave']} | {r['template']} | "
            f"{'✓' if r['has_filings'] else '—'} | {'✓' if r['has_bbg_pack'] else '—'} | "
            f"{r['periods']} | {st} | {r.get('miss_engine', 0)} | {r.get('sentinels', 0)} | "
            f"{r.get('miss_data', 0)} | {r['verdict']} | {r['blank']} | {r['note']} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if quiet:
        return
    print(f"\n[build-all] audit -> {md_path}")
    print(f"[build-all] PASS={n_pass}  INCOMPLETE={n_incomplete}  BROKEN={n_broken}  "
          f"(math-BLOCKED={n_gateblock}, ERROR={n_error}, filings={n_filings}, bbg={n_bbg}, total={n})")


if __name__ == "__main__":
    main()
