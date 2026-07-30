#!/usr/bin/env python3
"""run_ai_reads.py — execute the pre-registered AI reads over the scope frame.

Spec: study.yaml `hybrid.reader` + `hybrid.contamination`, frozen and committed
before this script first ran. Four calls per event:

  arm N     the named report                      (sensitivity)
  arm B     the entity-scrubbed report            (PRIMARY)
  probe     "which issuer is this, and what did the stock do?"  (contamination)
  placebo   arm-B read of a DIFFERENT issuer's report from the same quarter,
            later scored against this event's outcome (expectation ~50%)

Every call is content-addressed in data/ai_cache/, so a re-run costs nothing and
returns byte-identical records. Nothing here scores anything against a return —
that happens in simulate_hybrid.py, after the reads exist.

Output: outputs/ai_reads.parquet  (+ per-run log lines on stderr)
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import airead as ar
from earnlib import bootstrap as bs
from earnlib import study

import pandas as pd

sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
from eval_analyst_edge import build_join, sector_map  # noqa: E402

MAX_RETRIES = 4
RETRY_BASE_SECONDS = 5.0


def scope_frame(cfg: dict) -> pd.DataFrame:
    """The pre-registered scope: analyst-covered dev events, one row per event.

    Reuses eval_analyst_edge.build_join so this is the *same* canonical join the
    edge decomposition reported (n=73 matched / 67 directional), not a re-derived
    lookalike. Holdout quarters are already excluded inside dev_sample.
    """
    join, _ = build_join(cfg)
    cols = ["slug", "ticker", "period", "direction", "strength", "s_ts",
            "margin_sue_z", "sigma_pre", "ar0_cc", "median_peso_volume"]
    scope = join[[c for c in cols if c in join.columns]].copy()
    return scope.drop_duplicates(subset=["slug", "period"]).reset_index(drop=True)


def donor_map(scope: pd.DataFrame, seed: int) -> dict[tuple[str, str], str]:
    """(slug, period) -> a DIFFERENT slug reporting the same quarter.

    A seeded derangement per quarter; quarters with a single issuer get no donor
    (their placebo is simply absent, and the count is reported).
    """
    out: dict[tuple[str, str], str] = {}
    for period, group in scope.groupby("period"):
        slugs = sorted(group["slug"])
        if len(slugs) < 2:
            continue
        rng = random.Random(f"{seed}:{period}")
        rotation = rng.randrange(1, len(slugs))
        for i, slug in enumerate(slugs):
            out[(slug, period)] = slugs[(i + rotation) % len(slugs)]
    return out


def _is_retryable(exc: Exception) -> bool:
    """Rate limits, overloads and transport failures are worth retrying; a 400
    is not. Backing off four times on 'credit balance is too low' just burns
    minutes per call and buries the real message."""
    status = getattr(exc, "status_code", None)
    if status is None:
        return True                                  # transport / unknown
    return status == 429 or status >= 500


def with_retry(fn, label: str):
    """Retry transient API failures; a persistent one is recorded, not fatal."""
    for attempt in range(MAX_RETRIES):
        try:
            return fn(), None
        except LookupError:
            # offline and not cached. Not an error here: a partially completed
            # batch must still assemble, with unread events abstaining rather
            # than taking the whole run down. call_reader still raises, so the
            # prospective instrument keeps its "no read, no row" guarantee.
            return None, "not cached"
        except Exception as exc:                     # noqa: BLE001 - reported
            fatal = not _is_retryable(exc) or attempt == MAX_RETRIES - 1
            if fatal:
                print(f"  ! {label}: {type(exc).__name__}: {exc}", file=sys.stderr)
                return None, f"{type(exc).__name__}: {exc}"
            time.sleep(RETRY_BASE_SECONDS * (2 ** attempt))
    return None, "unreachable"


class Reader:
    def __init__(self, cfg: dict, reader_cfg: ar.ReaderConfig, *,
                 allow_call: bool = True, backend: str = "api"):
        block = cfg["hybrid"]["reader"]
        self.rcfg = reader_cfg
        self.model, self.temperature = ar.backend_identity(block, backend)
        self.max_tokens = int(block["max_output_tokens"])
        # the subagent backend never calls the API — its reads are ingested
        # into the cache out of band by scripts/subagent_reads.py
        self.allow_call = allow_call and backend == "api"

    def __call__(self, prompt: ar.Prompt, label: str):
        return with_retry(
            lambda: ar.call_reader(prompt, self.rcfg, model=self.model,
                                   temperature=self.temperature,
                                   max_tokens=self.max_tokens,
                                   allow_call=self.allow_call),
            label)


def read_one(row: pd.Series, *, reader: Reader, rcfg: ar.ReaderConfig,
             kpi: pd.DataFrame, avail: dict, sectors: dict,
             donors: dict, docs: dict, arms: set[str]) -> dict:
    slug, ticker, period = row["slug"], row["ticker"], row["period"]
    text, source = docs[(slug, period)]
    out: dict = {"slug": slug, "ticker": ticker, "period": period,
                 "doc_source": source, "doc_chars": len(text)}
    if not text:
        out["error"] = "no document"
        return out

    history = ar.kpi_history(kpi, slug, period, avail)
    out["history_quarters"] = len(history)
    sector = sectors.get(ticker, "")

    def prompt_for(arm: str, s: str, t: str, p: str, txt: str, src: str,
                   hist: pd.DataFrame) -> ar.Prompt:
        return ar.build_prompt(rcfg, arm=arm, slug=s, ticker=t, period=p,
                               sector=sectors.get(t, ""), text=txt, source=src,
                               history=hist)

    prompts = {}
    if "N" in arms:
        prompts["N"] = prompt_for("N", slug, ticker, period, text, source, history)
    if "B" in arms or "probe" in arms:
        prompts["B"] = prompt_for("B", slug, ticker, period, text, source, history)

    for arm in ("N", "B"):
        if arm not in arms or arm not in prompts:
            continue
        record, err = reader(prompts[arm], f"{slug} {period} arm{arm}")
        if err or record is None:
            out[f"error_{arm}"] = err
            continue
        fields, problems = ar.validate(record["response"], rcfg)
        out[f"truncated_{arm}"] = record["truncated"]
        for key, value in fields.items():
            out[f"{arm.lower()}_{key}"] = value
        if problems:
            out[f"schema_problems_{arm}"] = "; ".join(problems)

    if "probe" in arms and "B" in prompts:
        pp = ar.probe_prompt(rcfg, prompts["B"].user)
        record, err = reader(pp, f"{slug} {period} probe")
        if err or record is None:
            out["error_probe"] = err
        else:
            out.update(ar.validate_probe(record["response"], ticker))

    if "placebo" in arms:
        donor = donors.get((slug, period))
        donor_row = None if donor is None else DONOR_INDEX.get((donor, period))
        if donor_row is not None:
            dtext, dsource = docs[(donor, period)]
            if dtext:
                dhist = ar.kpi_history(kpi, donor, period, avail)
                dp = prompt_for("B", donor, donor_row["ticker"], period,
                                dtext, dsource, dhist)
                record, err = reader(dp, f"{slug} {period} placebo<-{donor}")
                if err or record is None:
                    out["error_placebo"] = err
                else:
                    fields, _ = ar.validate(record["response"], rcfg)
                    out["placebo_donor"] = donor
                    out["placebo_dir"] = fields["ai_dir"]
                    out["placebo_conviction"] = fields["conviction"]
    return out


DONOR_INDEX: dict[tuple[str, str], pd.Series] = {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="N,B,probe,placebo",
                    help="comma list of N,B,probe,placebo")
    ap.add_argument("--only", default="",
                    help="comma list of slug:period to restrict to (smoke test)")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--offline", action="store_true",
                    help="cache only; fail on anything not already read")
    ap.add_argument("--backend", default="api", choices=("api", "subagent"),
                    help="which reader produced the cached reads to assemble")
    ap.add_argument("--out", default="ai_reads")
    args = ap.parse_args()
    arms = {a.strip() for a in args.arms.split(",") if a.strip()}

    cfg = bs.load_config()
    rcfg = ar.load_reader_cfg()
    seed = int(cfg["validation"]["seed"]) + int(cfg["hybrid"]["seed_offset"])

    scope = scope_frame(cfg)
    if args.only:
        wanted = {tuple(x.split(":")) for x in args.only.split(",")}
        scope = scope[[(s, p) in wanted for s, p in
                       zip(scope["slug"], scope["period"])]].reset_index(drop=True)
    print(f"scope: {len(scope)} events, seed {seed}, arms {sorted(arms)}",
          file=sys.stderr)

    DONOR_INDEX.clear()
    full_scope = scope_frame(cfg)
    for _, r in full_scope.iterrows():
        DONOR_INDEX[(r["slug"], r["period"])] = r
    donors = donor_map(full_scope, seed)

    kpi = pd.read_parquet(bs.OUTPUTS_DIR / "kpi_panel.parquet")
    avail = ar.availability(study.availability_map())
    sectors = sector_map()

    # documents are read once and shared: the placebo re-reads a donor's text
    needed = {(r["slug"], r["period"]) for _, r in scope.iterrows()}
    needed |= {(donors[k], k[1]) for k in needed if k in donors}
    docs = {}
    for slug, period in sorted(needed):
        ticker = DONOR_INDEX[(slug, period)]["ticker"]
        docs[(slug, period)] = ar.load_document(slug, ticker, period)
    missing = [k for k, (t, _) in docs.items() if not t]
    fallbacks = {k: s for k, (t, s) in docs.items() if t and s != "mdna"}
    print(f"documents: {len(docs)} loaded, {len(missing)} missing, "
          f"{len(fallbacks)} via estate fallback", file=sys.stderr)
    for k, s in sorted(fallbacks.items()):
        print(f"  fallback {k[0]} {k[1]} -> {s}", file=sys.stderr)

    reader = Reader(cfg, rcfg, allow_call=not args.offline,
                    backend=args.backend)
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(read_one, row, reader=reader, rcfg=rcfg, kpi=kpi,
                               avail=avail, sectors=sectors, donors=donors,
                               docs=docs, arms=arms)
                   for _, row in scope.iterrows()]
        for i, fut in enumerate(futures, 1):
            rows.append(fut.result())
            if i % 10 == 0 or i == len(futures):
                print(f"  {i}/{len(futures)} events", file=sys.stderr)

    reads = pd.DataFrame(rows)
    out_path = bs.OUTPUTS_DIR / f"{args.out}.parquet"
    reads.to_parquet(out_path, index=False)
    print(f"wrote {out_path} ({len(reads)} rows)", file=sys.stderr)

    for arm in ("n", "b"):
        col = f"{arm}_direction"
        if col in reads.columns:
            print(f"arm {arm.upper()} directions: "
                  f"{reads[col].value_counts().to_dict()}", file=sys.stderr)
    if "identified" in reads.columns:
        print(f"probe identified: {int(reads['identified'].sum())}/"
              f"{int(reads['identified'].notna().sum())}", file=sys.stderr)


if __name__ == "__main__":
    main()
