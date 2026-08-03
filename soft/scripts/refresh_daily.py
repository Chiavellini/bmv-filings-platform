#!/usr/bin/env python3
"""Daily market-data refresh — re-price every built workbook without re-doing the quarterly work.

The engine already separates the two data clocks:
  * FUNDAMENTALS (revenue, book value, shares, …) come from cached BMV-XBRL and change only when a
    new filing lands (~4×/year). Re-extracting them daily is wasted work.
  * MARKET DATA (price, and the market-cap / EV / multiples that are live Excel formulas over it)
    changes every trading day.

So a daily refresh reuses the cached XBRL fundamentals and only re-pulls live Yahoo prices/macro,
then re-emits each workbook through the same math gate as a full build. This is the ``--cached-funds``
path of :mod:`build_all`, honouring the engine's ``SOFT_MARKET_TTL_HOURS`` (18h) so a 24h-apart daily
run pulls a fresh quote while a throttled fetch reuses the last-good cached price, plus a per-run
summary of which prices actually moved.

    python3 scripts/refresh_daily.py                 # refresh every company with cached filings
    python3 scripts/refresh_daily.py --only walmex,gentera
    python3 scripts/refresh_daily.py --limit 20
    python3 scripts/refresh_daily.py --ttl-hours 6   # reuse quotes younger than 6h (gentler on Yahoo)

Writes ``outputs/_scorecard/refresh_<date>.md`` (companies refreshed, prices moved, failures).
Idempotent: safe to re-run; a second run within the TTL re-uses quotes and moves nothing.
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read_price(slug: str, name: str) -> float | None:
    """The 'Price' cell from a company's last-emitted coverage CSV (None if absent)."""
    fs = glob.glob(str(ROOT / "outputs" / name / "csv" / f"{slug}_coverage.csv"))
    if not fs:
        fs = glob.glob(str(ROOT / "outputs" / name / "csv" / "*_coverage.csv"))
    if not fs:
        return None
    try:
        for row in csv.reader(open(fs[0], encoding="utf-8")):
            if len(row) >= 3 and row[1].strip() == "Price":
                try:
                    return float(row[2])
                except ValueError:
                    return None
    except OSError:
        return None
    return None


def _refresh_one(build_one, slug: str, spec_path: str):
    """Re-price one company through the full math gate; return its summary row
    ``(name, slug, status, old_px, new_px, moved, err)``. Shared by the main sweep and the straggler
    re-fetch pass. A single company failing must never sink the run — exceptions become an ERROR row."""
    from src.coverage.spec import parse_spec
    try:
        name = parse_spec(spec_path).name
    except Exception:
        name = slug
    old_px = _read_price(slug, name)
    try:
        res = build_one(spec_path, no_network=False, offline_fundamentals=True,
                        subject_facts_only=False, gate=True)
        err = res.error
        status = res.coverage_status or ("emitted" if res.emitted else "blocked")
    except Exception as e:  # a single company must never sink the whole refresh
        err, status = f"{type(e).__name__}: {e}", "ERROR"
    new_px = _read_price(slug, name)
    moved = None
    if old_px and new_px and old_px != 0:
        moved = (new_px - old_px) / old_px * 100.0
    return (name, slug, status, old_px, new_px, moved, err)


def _print_row(row, prefix: str = "  ") -> None:
    name, slug, status, old_px, new_px, moved, err = row
    tag = "✓" if not err else "✗"
    mv = f"{moved:+.2f}%" if moved is not None else "—"
    print(f"{prefix}{tag} {slug:24} {status:11} px {old_px} → {new_px}  ({mv})")


def _straggler_indices(rows) -> list[int]:
    """Indices of rows whose live price never resolved (``new_px is None``) — the candidates for a
    second-chance re-fetch. A full sweep can 429-throttle a batch of otherwise-valid symbols; Yahoo's
    rate-limit window resets after a short pause, so these are worth one more attempt after a cooldown."""
    return [i for i, r in enumerate(rows) if r[4] is None]


def _regenerate_dense_matrix() -> tuple[int, int]:
    """Rebuild the required dense deliverable or propagate its failure.

    ``build_dense.emit`` invalidates stale success artifacts and writes a FAILED
    marker before raising. The scheduler must therefore fail as well, instead of
    claiming a successful refresh with no trustworthy dense matrix.
    """
    from scripts import build_dense

    out = ROOT / "outputs" / "_master"
    rows, metrics = build_dense.emit(list(build_dense.DENSE_METRICS), out)
    return len(rows), len(metrics)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None, help="comma-separated slugs")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--ttl-hours", type=float, default=None,
                    help="reuse cached quotes younger than this many hours. Default: unset → the "
                         "engine's SOFT_MARKET_TTL_HOURS (18h). The daily scheduled run (24h apart) "
                         "still pulls a FRESH quote, but a same-day re-run reuses the cache and — "
                         "critically — a throttled/failed fetch reinstates the last-good cached price "
                         "instead of blanking the cell. Pass 0 to force a re-fetch of every quote.")
    ap.add_argument("--date", default=None,
                    help="stamp for the summary filename (YYYY-MM-DD); defaults to today. Passed "
                         "explicitly by the scheduler so the run is reproducible.")
    ap.add_argument("--no-straggler", action="store_true",
                    help="skip the second-chance re-fetch of names whose price didn't resolve.")
    ap.add_argument("--straggler-cooldown", type=float, default=45.0,
                    help="seconds to pause before the straggler re-fetch, letting Yahoo's rate-limit "
                         "window reset (default 45).")
    args = ap.parse_args()

    # Only override the engine's cache TTL when the operator asks. Forcing TTL=0 re-fetched all ~130
    # quotes every run, which triggered Yahoo 429s and (before the last-good fallback) blanked cells;
    # leaving the 18h default lets a 24h-apart daily run pull fresh while sparing same-day re-runs.
    if args.ttl_hours is not None:
        os.environ["SOFT_MARKET_TTL_HOURS"] = str(args.ttl_hours)

    # Refresh CNBV's authoritative bank-capital table (CET1/ICAP) BEFORE the company builds read it,
    # so bank rows carry current ratios. Best-effort + non-fatal (CNBV publishes monthly, small PDF).
    try:
        from src.download import cnbv
        payload = cnbv.fetch_and_cache()
        if payload:
            print(f"[refresh] CNBV bank-capital refreshed (period {payload['period']}, "
                  f"{len(payload['banks'])} banks)")
    except Exception as e:  # non-fatal
        print(f"[refresh] CNBV bank-capital refresh skipped (non-fatal): {type(e).__name__}: {e}")

    from scripts.build_coverage import build_one  # noqa: E402  (after sys.path + env set)
    from scripts.gen_universe import slugify  # noqa: E402

    only = {s.strip() for s in args.only.split(",")} if args.only else None

    # Refresh every company that has cached filings and an input spec (the built universe).
    slugs: list[tuple[str, str]] = []  # (slug, spec_path)
    for spec_path in sorted(glob.glob(str(ROOT / "inputs" / "*.md"))):
        slug = Path(spec_path).stem
        if slug.startswith("_"):
            continue
        if only and slug not in only:
            continue
        if not (ROOT / "data" / "reports" / slug).is_dir():
            continue
        slugs.append((slug, spec_path))
    if args.limit:
        slugs = slugs[: args.limit]

    rows = []  # (name, slug, status, old_px, new_px, moved_pct, error)
    for slug, spec_path in slugs:
        row = _refresh_one(build_one, slug, spec_path)
        rows.append(row)
        _print_row(row)

    # --- straggler re-fetch pass -------------------------------------------
    # A full sweep can trip Yahoo's rate limiter, so a batch of otherwise-valid symbols 429s and their
    # live price never resolves (new_px is None) — the dominant `throttled-price` gap in the master
    # matrix. The rate-limit window resets after a short pause, so re-attempt ONLY the price-less names
    # once, after a cooldown. Cheap (one pass over the stragglers), and converts most throttle gaps
    # into fills without re-pricing the whole universe. Backoff on each fetch (market_data) handles the
    # transient blips; this handles a sustained sweep-wide throttle.
    stragglers = _straggler_indices(rows)
    if stragglers and not args.no_straggler:
        import time
        print(f"\n[refresh] {len(stragglers)} name(s) with no resolved price after the sweep — "
              f"straggler re-fetch after a {args.straggler_cooldown:.0f}s cooldown "
              f"(Yahoo rate-limit reset)")
        time.sleep(args.straggler_cooldown)
        recovered = 0
        for i in stragglers:
            slug, spec_path = slugs[i]
            row = _refresh_one(build_one, slug, spec_path)
            if row[4] is not None:
                recovered += 1
            rows[i] = row
            _print_row(row, prefix="  ↻ ")
        print(f"[refresh] straggler pass recovered {recovered}/{len(stragglers)} price(s)")

    # --- summary ------------------------------------------------------------
    n = len(rows)
    n_err = sum(1 for r in rows if r[6])
    moved_rows = [r for r in rows if r[5] is not None and abs(r[5]) > 1e-9]
    date = args.date or _today()
    out = ROOT / "outputs" / "_scorecard" / f"refresh_{date}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Daily market-data refresh — {date}", "",
             f"- Companies refreshed: **{n - n_err}/{n}**  ·  errors: **{n_err}**",
             f"- Prices moved: **{len(moved_rows)}**  (cached XBRL fundamentals reused; only market "
             f"data re-pulled)", "",
             "| Company | Status | Prev px | New px | Move | Note |",
             "|---|---|---|---|---|---|"]
    for name, slug, status, old_px, new_px, moved, err in sorted(rows, key=lambda r: r[0]):
        mv = f"{moved:+.2f}%" if moved is not None else "—"
        note = err or ("price unchanged" if moved == 0 else "")
        lines.append(f"| {name} | {status} | {old_px if old_px is not None else '—'} | "
                     f"{new_px if new_px is not None else '—'} | {mv} | {note} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[refresh] {n - n_err}/{n} refreshed, {len(moved_rows)} prices moved, {n_err} errors")
    print(f"[refresh] summary -> {out}")

    # Regenerate the master matrix (xlsx / csv / self-generating HTML page) from the freshly-refreshed
    # coverage CSVs, so the self-updating deliverable — including the published artifact page — carries
    # today's prices. skip_existing reuses the just-written CSVs (no rebuild); no_network keeps it fast.
    # Guarded: a master-build hiccup must never fail the price refresh that already completed.
    try:
        from scripts.build_master import build_master as _build_master
        _build_master(None, no_network=True, skip_existing=True, render=False, limit=None, core=True)
        print("[refresh] core master matrix regenerated (xlsx/csv/html)")
    except Exception as e:  # non-fatal
        print(f"[refresh] master rebuild failed (non-fatal): {type(e).__name__}: {e}")

    # Dense block — the genuine-100% deliverable (every cell a real value). This
    # is a required scheduled output, so failure propagates to the scheduler.
    try:
        n_dense_companies, n_dense_metrics = _regenerate_dense_matrix()
    except Exception as exc:
        print(
            f"[refresh] dense rebuild FAILED: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise RuntimeError("required dense matrix regeneration failed") from exc
    print(
        f"[refresh] dense block regenerated "
        f"({n_dense_companies} companies × {n_dense_metrics} metrics, 100% real)"
    )

    # Publish the freshest dense + core matrices to GitHub Pages — the autonomous, self-updating URL
    # (a headless job can't push a claude.ai Artifact). GUARDED on SOFT_PUBLISH=1 (set by the scheduled
    # launchd run) so ad-hoc local refreshes stay local. Non-fatal: a publish hiccup must never fail the
    # price refresh that already completed.
    if os.environ.get("SOFT_PUBLISH") == "1":
        try:
            from scripts import publish_pages
            publish_pages.run(push=True, date=date)
            print(f"[refresh] published → {publish_pages.PAGES_URL}")
        except Exception as e:  # non-fatal
            print(f"[refresh] publish failed (non-fatal): {type(e).__name__}: {e}")

    # Advisory certification: report applicable-free coverage + any remaining GAPs (with cause) so a
    # throttled-price straggler is visible for a targeted retry. Never fails the refresh.
    try:
        from scripts.certify_master import certify as _certify, _write_report as _cert_report
        per_group, gaps, totals = _certify()
        _cert_report(per_group, gaps, totals)
        n_filled, n_applicable = totals[0], totals[1]
        cov = (100.0 * n_filled / n_applicable) if n_applicable else 0.0
        thr = sum(1 for _, _, c in gaps if c == "throttled-price")
        print(f"[refresh] certify: {n_filled}/{n_applicable} applicable-free filled ({cov:.1f}%) · "
              f"GAP {len(gaps)} (throttled-price {thr}) → outputs/_reconcile/master_certification.md")
    except Exception as e:  # non-fatal
        print(f"[refresh] certify failed (non-fatal): {type(e).__name__}: {e}")


def _today() -> str:
    """Today's date (YYYY-MM-DD) from the wall clock — only used for the summary filename."""
    import datetime
    return datetime.date.today().isoformat()


if __name__ == "__main__":
    main()
