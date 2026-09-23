#!/usr/bin/env python3
"""Run the frozen 63-event forced-choice reader through an external API.

Supports OpenAI-compatible chat-completions providers (including DeepSeek and
Moonshot/Kimi) and Anthropic. Responses are resumable and isolated by model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.shared.env import load_env

from earnings.scripts.run_codex_reader import (
    ANALYST_LABEL_DIRECTION,
    analyst_forced_prompt,
    select_jobs,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_json_object(text: str) -> dict:
    text = (text or "").strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        pass
    left, right = text.find("{"), text.rfind("}")
    if left >= 0 and right > left:
        try:
            value = json.loads(text[left:right + 1])
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def valid(payload: dict, expected_keys: set[str]) -> bool:
    return (
        set(payload) == expected_keys
        and payload.get("analyst_label") in ANALYST_LABEL_DIRECTION
        and payload.get("direction") in {"up", "down"}
        and ANALYST_LABEL_DIRECTION[payload["analyst_label"]] == payload["direction"]
    )


def split_prompt(prompt: str) -> tuple[str, str]:
    marker = "## Issuer"
    position = prompt.find(marker)
    if position < 0:
        return "Return one JSON object and nothing else.", prompt
    return prompt[:position].strip(), prompt[position:].strip()


def call_provider(args: argparse.Namespace, system: str, user: str) -> tuple[dict, dict]:
    if args.provider == "anthropic":
        url = (args.base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        body = {
            "model": args.model, "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": args.max_tokens, "temperature": 0,
        }
        headers = {
            "content-type": "application/json", "x-api-key": args.api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        url = args.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": args.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": args.max_tokens, "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {"content-type": "application/json", "authorization": f"Bearer {args.api_key}"}
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=args.timeout or 600) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    if args.provider == "anthropic":
        output = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage = data.get("usage", {})
    else:
        output = data["choices"][0]["message"].get("content", "")
        usage = data.get("usage", {})
    return parse_json_object(output), usage


def run_one(job: dict, args: argparse.Namespace) -> dict:
    key = f"{job['slug']}__{job['period']}"
    response_path = args.output_dir / "responses" / f"{key}.json"
    log_path = args.output_dir / "logs" / f"{key}.json"
    if response_path.exists():
        try:
            cached = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        if valid(cached, args.expected_keys):
            return {"slug": job["slug"], "ticker": job["ticker"], "period": job["period"], "status": "cached", "response_path": str(response_path)}

    prompt = analyst_forced_prompt(Path(job["prompt_path"]).read_text(encoding="utf-8"))
    system, user = split_prompt(prompt)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    started = time.monotonic()
    last_error = ""
    for attempt in range(1, args.retries + 1):
        try:
            payload, usage = call_provider(args, system, user)
            log_path.write_text(json.dumps({"payload": payload, "usage": usage}, ensure_ascii=False, indent=2), encoding="utf-8")
            if valid(payload, args.expected_keys):
                response_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                return {
                    "slug": job["slug"], "ticker": job["ticker"], "period": job["period"],
                    "status": "ok", "attempts": attempt,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "response_path": str(response_path), "prompt_sha256": prompt_sha256,
                    "usage": usage,
                }
            last_error = f"invalid response keys or forced label: {sorted(payload)}"
        except Exception as exc:  # provider SDK/network errors are recorded per event
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < args.retries:
            time.sleep(min(20, 3 * attempt))
    return {
        "slug": job["slug"], "ticker": job["ticker"], "period": job["period"],
        "status": "error", "attempts": args.retries,
        "elapsed_seconds": round(time.monotonic() - started, 2), "error": last_error,
        "response_path": str(response_path), "prompt_sha256": prompt_sha256,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--provider", choices=("openai-compatible", "anthropic"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key-env", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=1400)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    load_env()
    args.api_key = os.environ.get(args.api_key_env, "")
    if not args.api_key:
        raise SystemExit(f"Missing required credential: {args.api_key_env}")
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    args.expected_keys = set(schema["properties"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "responses").mkdir(exist_ok=True)
    (args.output_dir / "logs").mkdir(exist_ok=True)
    jobs = select_jobs(args.index.resolve(), args.target.resolve())
    if args.limit:
        jobs = jobs[:args.limit]

    manifest_path = args.output_dir / "run_manifest.json"
    manifest = {
        "started_at": now(), "provider": args.provider, "model": args.model,
        "base_url": args.base_url, "api_key_env": args.api_key_env,
        "prompt_arm": "B", "prompt_mode": "analyst-forced",
        "selected_jobs": len(jobs), "results": [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_one, job, args) for job in jobs]
        for number, future in enumerate(as_completed(futures), 1):
            result = future.result()
            manifest["results"].append(result)
            manifest["results"].sort(key=lambda item: (item["slug"], item["period"]))
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            manifest["counts"] = counts
            manifest["updated_at"] = now()
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            print(f"[{number:02d}/{len(jobs):02d}] {result['slug']} {result['period']}: {result['status']}", flush=True)
    manifest["finished_at"] = now()
    manifest["counts"] = counts
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if counts.get("error"):
        raise SystemExit(f"run finished with errors: {counts}")
    print(f"complete: {counts}")


if __name__ == "__main__":
    main()
