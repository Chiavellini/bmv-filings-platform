#!/usr/bin/env python3
"""Run frozen earnings-reader prompts through Codex, cheaply and resumably.

This runner deliberately does not write into the original Claude cache. It
selects the final tradable analyst calls (strength > 0), submits the prepared
scrubbed B-arm prompt verbatim, and stores one response and execution log per
event in a separate experiment directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


ANALYST_LABEL_DIRECTION = {
    "Positive": "up",
    "Negative": "down",
    "N to P": "up",
    "N to N": "down",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_response(path: Path, args: argparse.Namespace) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if set(payload) != args.expected_keys:
        return False
    if payload.get("direction") not in args.allowed_directions:
        return False
    if args.prompt_mode == "analyst-forced":
        label = payload.get("analyst_label")
        return label in ANALYST_LABEL_DIRECTION and (
            ANALYST_LABEL_DIRECTION[label] == payload.get("direction")
        )
    return True


def analyst_forced_prompt(prompt: str) -> str:
    replacements = [
        (
            'You have no price data, no consensus estimates, and no knowledge of what\n'
            'happened. Do not pretend otherwise. If the report does not support a call,\n'
            'say so — "flat" and "low" are real answers and are preferred over a guess.',
            'You have no price data, no consensus estimates, and no knowledge of what\n'
            'happened. Do not pretend otherwise. This is a forced-choice benchmark using\n'
            'the analyst\'s exact directional labels. You MUST choose Positive, Negative,\n'
            'N to P, or N to N; flat, neutral, and abstention are not available.'
        ),
        (
            'A call that depends on knowing the outcome is worse\n'
            'than no call: answer "flat" with "low" conviction instead.',
            'Do not use later knowledge. When evidence is weak, choose the soft label\n'
            'N to P or N to N according to the direction of your best report-only lean.'
        ),
        (
            '  "direction": "up" | "down" | "flat",',
            '  "analyst_label": "Positive" | "Negative" | "N to P" | "N to N",\n'
            '  "direction": "up" | "down",'
        ),
        (
            '"direction" is the stock\'s move on the session after publication, relative to\n'
            'the market. "conviction" is high only when you would put the trade on.',
            '"analyst_label" is the forced trading choice. Positive/Negative are full-\n'
            'strength up/down calls; N to P/N to N are soft up/down calls. There is no\n'
            'flat or neutral option. "direction" must agree with that label. "conviction"\n'
            'is high only when you would put the trade on.'
        ),
    ]
    for old, new in replacements:
        if old not in prompt:
            raise ValueError(f"forced prompt source text not found: {old[:80]!r}")
        prompt = prompt.replace(old, new, 1)
    forbidden = [
        '"direction": "up" | "down" | "flat"',
        'answer "flat" with "low" conviction',
        '"flat" and "low" are real answers',
    ]
    if any(text in prompt for text in forbidden):
        raise ValueError("forced prompt still contains an abstention instruction")
    return prompt


def select_jobs(index_path: Path, target_path: Path) -> list[dict]:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    target = pd.read_parquet(target_path)
    target = target[target["strength"] > 0]
    wanted = set(zip(target["slug"].astype(str), target["period"].astype(str)))
    jobs = [
        job for job in index
        if job.get("arm") == "B" and (job["slug"], job["period"]) in wanted
    ]
    found = {(job["slug"], job["period"]) for job in jobs}
    missing = sorted(wanted - found)
    duplicates = len(jobs) - len(found)
    if len(wanted) != 63 or len(jobs) != 63 or missing or duplicates:
        raise SystemExit(
            "expected exactly 63 unique tradable prompts; "
            f"targets={len(wanted)} jobs={len(jobs)} missing={missing} "
            f"duplicates={duplicates}"
        )
    return sorted(jobs, key=lambda j: (j["slug"], j["period"]))


def usage_from_log(log_path: Path) -> dict:
    usage: dict = {}
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return usage
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        candidate = event.get("usage")
        if isinstance(candidate, dict):
            usage.update(candidate)
        item = event.get("item")
        if isinstance(item, dict) and isinstance(item.get("usage"), dict):
            usage.update(item["usage"])
    return usage


def run_one(job: dict, args: argparse.Namespace) -> dict:
    key = f"{job['slug']}__{job['period']}"
    response_path = args.output_dir / "responses" / f"{key}.json"
    log_path = args.output_dir / "logs" / f"{key}.jsonl"
    stderr_path = args.output_dir / "logs" / f"{key}.stderr.txt"
    prompt = Path(job["prompt_path"]).read_text(encoding="utf-8")
    if args.prompt_mode == "analyst-forced":
        prompt = analyst_forced_prompt(prompt)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if valid_response(response_path, args):
        return {
            "slug": job["slug"], "ticker": job["ticker"],
            "period": job["period"], "status": "cached",
            "response_path": str(response_path), "log_path": str(log_path),
            "prompt_sha256": prompt_sha256,
            "usage": usage_from_log(log_path),
        }
    env = dict(os.environ)
    # Force use of the existing ChatGPT/Codex login, never an accidental API key.
    env.pop("OPENAI_API_KEY", None)
    env.pop("CODEX_API_KEY", None)
    started = time.monotonic()
    last_error = ""
    for attempt in range(1, args.retries + 1):
        with tempfile.TemporaryDirectory(prefix="codex-reader-") as empty_cwd:
            command = [
                args.codex, "exec", "-m", args.model,
                "-c", f'model_reasoning_effort="{args.reasoning}"',
                "-c", 'model_verbosity="low"',
                "--sandbox", "read-only", "--ephemeral",
                "--ignore-user-config", "--ignore-rules",
                "--skip-git-repo-check", "--json",
                "--output-schema", str(args.schema),
                "-o", str(response_path), "-C", empty_cwd, "-",
            ]
            try:
                completed = subprocess.run(
                    command, input=prompt, text=True, capture_output=True,
                    env=env, timeout=args.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {args.timeout}s"
                log_path.write_text(exc.stdout or "", encoding="utf-8")
                stderr_path.write_text(exc.stderr or "", encoding="utf-8")
            else:
                log_path.write_text(completed.stdout, encoding="utf-8")
                stderr_path.write_text(completed.stderr, encoding="utf-8")
                if completed.returncode == 0 and valid_response(response_path, args):
                    return {
                        "slug": job["slug"], "ticker": job["ticker"],
                        "period": job["period"], "status": "ok",
                        "attempts": attempt,
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                        "response_path": str(response_path),
                        "log_path": str(log_path),
                        "prompt_sha256": prompt_sha256,
                        "usage": usage_from_log(log_path),
                    }
                last_error = (
                    f"exit={completed.returncode}; "
                    f"stderr={completed.stderr[-500:].strip()}"
                )
        if attempt < args.retries:
            time.sleep(min(30, 5 * attempt))
    return {
        "slug": job["slug"], "ticker": job["ticker"],
        "period": job["period"], "status": "error",
        "attempts": args.retries,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "error": last_error, "response_path": str(response_path),
        "log_path": str(log_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--reasoning", default="low")
    parser.add_argument(
        "--prompt-mode", choices=("original", "analyst-forced"),
        default="original",
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    args.expected_keys = set(schema["properties"])
    args.allowed_directions = set(schema["properties"]["direction"]["enum"])
    if args.prompt_mode == "analyst-forced" and (
        set(schema["properties"]["analyst_label"]["enum"])
        != set(ANALYST_LABEL_DIRECTION)
        or args.allowed_directions != {"up", "down"}
    ):
        raise SystemExit("analyst-forced mode requires the exact four-label schema")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "responses").mkdir(exist_ok=True)
    (args.output_dir / "logs").mkdir(exist_ok=True)
    jobs = select_jobs(args.index.resolve(), args.target.resolve())
    if args.limit:
        jobs = jobs[:args.limit]

    manifest_path = args.output_dir / "run_manifest.json"
    manifest = {
        "started_at": utc_now(), "model": args.model,
        "reasoning": args.reasoning, "prompt_arm": "B",
        "prompt_mode": args.prompt_mode,
        "schema": str(args.schema.resolve()),
        "selected_jobs": len(jobs), "results": [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, job, args): job for job in jobs}
        for number, future in enumerate(as_completed(futures), 1):
            result = future.result()
            manifest["results"].append(result)
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            manifest["results"].sort(key=lambda r: (r["slug"], r["period"]))
            manifest["updated_at"] = utc_now()
            manifest["counts"] = counts
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            print(
                f"[{number:02d}/{len(jobs):02d}] {result['slug']} "
                f"{result['period']}: {result['status']}",
                flush=True,
            )
    manifest["finished_at"] = utc_now()
    manifest["counts"] = counts
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if counts.get("error"):
        raise SystemExit(f"run finished with errors: {counts}")
    print(f"complete: {counts}")


if __name__ == "__main__":
    main()
