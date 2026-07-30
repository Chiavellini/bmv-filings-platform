#!/usr/bin/env python3
"""subagent_reads.py — run the frozen reader prompts through blinded subagents.

The API backend is the specified path (pinned model, temperature 0, byte-identical
re-runs). This is the alternative when no API credit is available: the *same*
prompts, the *same* schema and the *same* content-addressed cache, but answered
by outcome-blinded subagents on a Claude subscription instead of by the Messages
API. It is the method the original eight case reads used.

Two steps, because the agent dispatch happens outside this process:

    prepare   write one self-contained prompt file per (event, arm) into a
              directory outside the repository, plus an index. Each file holds
              everything the reader may see and nothing else.
    ingest    read the agents' JSON answers back and write them into the cache
              in the API backend's record format, so everything downstream —
              run_ai_reads.py --offline, simulate_hybrid.py, decide.py — is
              unchanged.

Blinding rests on three things, in descending order of how much they can be
trusted: the arm-B scrubber (there is often nothing left to recognize), the
identification probe (the frozen gate scores only events the reader could not
identify), and the prompt's instruction to ignore anything it knows. The
instruction is the weakest of the three — a model cannot reliably introspect
what it already knows — which is why it is not the control we rely on.

Prompt files are written OUTSIDE the repository on purpose: a reader that only
ever opens its own prompt file cannot wander into outputs/ and read the answer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import airead as ar
from earnlib import bootstrap as bs
from earnlib import study

import pandas as pd

sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
from eval_analyst_edge import sector_map            # noqa: E402
from run_ai_reads import scope_frame                # noqa: E402

BACKEND = "subagent"


def prepare(args) -> None:
    cfg = bs.load_config()
    rcfg = ar.load_reader_cfg()
    block = cfg["hybrid"]["reader"]
    model, temperature = ar.backend_identity(block, BACKEND)

    scope = scope_frame(cfg)
    if args.only:
        wanted = {tuple(x.split(":")) for x in args.only.split(",")}
        scope = scope[[(s, p) in wanted for s, p in
                       zip(scope["slug"], scope["period"])]].reset_index(drop=True)
    if scope.empty:
        raise SystemExit("no events selected")

    kpi = pd.read_parquet(bs.OUTPUTS_DIR / "kpi_panel.parquet")
    avail = ar.availability(study.availability_map())
    sectors = sector_map()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for _, r in scope.iterrows():
        slug, ticker, period = r["slug"], r["ticker"], r["period"]
        text, source = ar.load_document(slug, ticker, period)
        if not text:
            print(f"  skip {slug} {period}: no document", file=sys.stderr)
            continue
        history = ar.kpi_history(kpi, slug, period, avail)
        built = {}
        for arm in ("N", "B"):
            if arm in arms or (arm == "B" and "probe" in arms):
                built[arm] = ar.build_prompt(
                    rcfg, arm=arm, slug=slug, ticker=ticker, period=period,
                    sector=sectors.get(ticker, ""), text=text, source=source,
                    history=history)
        jobs = [(a, built[a]) for a in arms if a in built]
        if "probe" in arms and "B" in built:
            jobs.append(("probe", ar.probe_prompt(rcfg, built["B"].user)))

        for arm, prompt in jobs:
            key = ar.cache_key(rcfg, model, temperature, prompt)
            if ar.cache_path(key).exists() and not args.force:
                continue
            path = out_dir / f"{arm}_{slug}_{period}_{key[:10]}.md"
            path.write_text(
                f"{prompt.system}\n\n---\n\n{prompt.user}\n", encoding="utf-8")
            index.append({"key": key, "arm": arm, "slug": slug,
                          "ticker": ticker, "period": period,
                          "source": prompt.source,
                          "truncated": bool(prompt.truncated),
                          "prompt_path": str(path),
                          "response_path": str(out_dir / f"resp_{key[:10]}.json")})

    (out_dir / "index.json").write_text(json.dumps(index, indent=1))
    print(f"prepared {len(index)} prompt(s) in {out_dir}", file=sys.stderr)
    print(f"index: {out_dir / 'index.json'}", file=sys.stderr)
    for job in index:
        print(f"  {job['arm']:5s} {job['slug']:16s} {job['period']}  "
              f"{job['prompt_path']}", file=sys.stderr)


def _run_one(job: dict, model: str) -> tuple[str, str]:
    """Answer one prepared prompt with a headless `claude -p` reader."""
    import subprocess

    resp_path = Path(job["response_path"])
    if resp_path.exists():
        return job["key"], "cached"
    prompt = Path(job["prompt_path"]).read_text(encoding="utf-8")

    # ANTHROPIC_API_KEY is unset deliberately: a key in the environment takes
    # precedence over the claude.ai login, and a dead key makes every read fail
    # with "credit balance is too low". Tools are disallowed because the reader
    # must answer from the prompt alone — it has no business touching a file.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        out = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "json",
             "--disallowedTools",
             "Bash,Write,Edit,Read,Glob,Grep,WebFetch,WebSearch,Task"],
            input=prompt, capture_output=True, text=True, env=env, timeout=900)
    except subprocess.TimeoutExpired:
        return job["key"], "timeout"
    if out.returncode != 0:
        return job["key"], f"exit {out.returncode}"
    try:
        payload = json.loads(out.stdout)
    except ValueError:
        return job["key"], "unparseable envelope"
    if payload.get("is_error"):
        return job["key"], f"error: {str(payload.get('result'))[:60]}"

    text = str(payload.get("result", ""))
    match = re.search(r"\{.*\}", text, re.DOTALL)   # strip ```json fences
    if not match:
        return job["key"], "no JSON in answer"
    try:
        answer = json.loads(match.group(0))
    except ValueError:
        return job["key"], "unparseable answer"
    resp_path.write_text(json.dumps(answer, ensure_ascii=False, indent=1))
    return job["key"], "ok"


def run(args) -> None:
    """Answer every outstanding prompt. Resumable: existing answers are kept."""
    from concurrent.futures import ThreadPoolExecutor

    cfg = bs.load_config()
    model = str(cfg["hybrid"]["reader"]["model"])
    out_dir = Path(args.dir)
    index = json.loads((out_dir / "index.json").read_text())
    todo = [j for j in index if not Path(j["response_path"]).exists()]
    print(f"{len(index)} prompts, {len(todo)} outstanding, "
          f"{args.workers} workers, model {model}", file=sys.stderr)

    done = {"ok": 0, "cached": 0}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (key, status) in enumerate(
                pool.map(lambda j: _run_one(j, model), todo), 1):
            done[status] = done.get(status, 0) + 1
            if status not in ("ok", "cached"):
                print(f"  ! {key[:10]}: {status}", file=sys.stderr)
            if i % 10 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}  {done}", file=sys.stderr)
    print(f"done: {done}", file=sys.stderr)


def ingest(args) -> None:
    cfg = bs.load_config()
    rcfg = ar.load_reader_cfg()
    model, temperature = ar.backend_identity(cfg["hybrid"]["reader"], BACKEND)

    out_dir = Path(args.dir)
    index = json.loads((out_dir / "index.json").read_text())
    written, missing = 0, []
    for job in index:
        resp_path = Path(job["response_path"])
        if not resp_path.exists():
            missing.append(f"{job['arm']} {job['slug']} {job['period']}")
            continue
        try:
            response = json.loads(resp_path.read_text())
        except ValueError as exc:
            missing.append(f"{job['arm']} {job['slug']} {job['period']} "
                           f"(unparseable: {exc})")
            continue
        record = {
            "key": job["key"], "arm": job["arm"], "model": model,
            "temperature": temperature, "reader_cfg_sha256": rcfg.sha256,
            "source": job["source"], "truncated": job["truncated"],
            "prompt_chars": None, "backend": BACKEND, "response": response,
        }
        path = ar.cache_path(job["key"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1))
        written += 1

    print(f"ingested {written} read(s) into {ar.CACHE_DIR}", file=sys.stderr)
    if missing:
        print(f"still missing {len(missing)}:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    default_dir = "/private/tmp/earnings_subagent_reads"

    p = sub.add_parser("prepare")
    p.add_argument("--only", default="", help="slug:period,slug:period")
    p.add_argument("--arms", default="N,B,probe")
    p.add_argument("--dir", default=default_dir)
    p.add_argument("--force", action="store_true",
                   help="re-prepare prompts that already have a cached read")
    p.set_defaults(func=prepare)

    r = sub.add_parser("run")
    r.add_argument("--dir", default=default_dir)
    r.add_argument("--workers", type=int, default=6)
    r.set_defaults(func=run)

    g = sub.add_parser("ingest")
    g.add_argument("--dir", default=default_dir)
    g.set_defaults(func=ingest)

    args = ap.parse_args()
    assert bs.V3, "set EARNINGS_V3=1"
    args.func(args)


if __name__ == "__main__":
    main()
