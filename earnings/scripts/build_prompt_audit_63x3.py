#!/usr/bin/env python3
"""Build a self-contained audit bundle for the 63-event, three-model run."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from earnings.scripts.run_codex_reader import analyst_forced_prompt, select_jobs


INDEX = REPO_ROOT / "earnings/outputs/codex_luna_63/prompts/index.json"
TARGET = REPO_ROOT / "earnings/outputs/multi_ai_forced_63/frozen_63_targets.parquet"
EVENTS = REPO_ROOT / "earnings/outputs/codex_luna_forced_63/handoff/Analyst_Luna_63_Events.csv"
OUTPUT_ROOT = REPO_ROOT / "earnings/outputs/multi_ai_forced_63/prompt_audit"
ZIP_PATH = REPO_ROOT / "earnings/outputs/multi_ai_forced_63/PROMPT_AUDIT_63x3.zip"

MODELS = [
    {
        "short_name": "luna",
        "display_name": "Luna",
        "model_id": "gpt-5.6-luna",
        "run_dir": REPO_ROOT / "earnings/outputs/codex_luna_forced_63/run",
        "transport": "The complete prompt text was passed to Codex CLI on standard input.",
    },
    {
        "short_name": "deepseek_chat",
        "display_name": "DeepSeek Chat",
        "model_id": "deepseek-chat",
        "run_dir": REPO_ROOT / "earnings/outputs/multi_ai_forced_63/deepseek_chat",
        "transport": (
            "The identical prompt text was split immediately before the '## Issuer' heading: "
            "the preamble was the system message and the issuer/history/report body was the user message."
        ),
    },
    {
        "short_name": "codex_mini",
        "display_name": "Codex Mini",
        "model_id": "gpt-5.4-mini",
        "run_dir": REPO_ROOT / "earnings/outputs/multi_ai_forced_63/codex_mini",
        "transport": "The complete prompt text was passed to Codex CLI on standard input.",
    },
]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_event_metadata() -> dict[tuple[str, str], dict[str, str]]:
    with EVENTS.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    return {(row["slug"], row["period"]): row for row in rows}


def main() -> None:
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    (OUTPUT_ROOT / "prompts").mkdir(parents=True)
    (OUTPUT_ROOT / "responses").mkdir()

    jobs = select_jobs(INDEX, TARGET)
    event_meta = load_event_metadata()
    model_manifests = {
        model["short_name"]: json.loads((model["run_dir"] / "run_manifest.json").read_text(encoding="utf-8"))
        for model in MODELS
    }
    result_indexes = {
        name: {(row["slug"], row["period"]): row for row in manifest["results"]}
        for name, manifest in model_manifests.items()
    }

    executions: list[dict] = []
    prompt_index: list[dict] = []
    recorded_hashes_checked = 0
    prompt_sizes: list[int] = []

    for job in jobs:
        key = (job["slug"], job["period"])
        source_text = Path(job["prompt_path"]).read_text(encoding="utf-8")
        prompt = analyst_forced_prompt(source_text)
        prompt_hash = sha256_text(prompt)
        prompt_name = f"{job['slug']}__{job['period']}.md"
        prompt_rel = Path("prompts") / prompt_name
        (OUTPUT_ROOT / prompt_rel).write_text(prompt, encoding="utf-8")

        if "## The report's management discussion" not in prompt:
            raise RuntimeError(f"report section missing from {prompt_name}")
        if "## Its own reported history" not in prompt:
            raise RuntimeError(f"history section missing from {prompt_name}")
        if "flat, neutral, and abstention are not available" not in prompt:
            raise RuntimeError(f"forced-choice instruction missing from {prompt_name}")

        meta = event_meta[key]
        prompt_sizes.append(len(prompt.encode("utf-8")))
        prompt_index.append(
            {
                "slug": job["slug"],
                "ticker": job["ticker"],
                "period": job["period"],
                "report_date": meta["report_date"],
                "report_session": meta["report_session"],
                "trading_day": meta["t0"],
                "source_kind": job.get("source"),
                "source_was_truncated": bool(job.get("truncated")),
                "prompt_file": prompt_rel.as_posix(),
                "prompt_sha256": prompt_hash,
                "prompt_bytes": len(prompt.encode("utf-8")),
            }
        )

        for model in MODELS:
            short_name = model["short_name"]
            result = result_indexes[short_name].get(key)
            if not result or result.get("status") not in {"ok", "cached"}:
                raise RuntimeError(f"missing complete result for {short_name} {key}")
            recorded_hash = result.get("prompt_sha256")
            if recorded_hash is not None:
                recorded_hashes_checked += 1
                if recorded_hash != prompt_hash:
                    raise RuntimeError(f"prompt hash mismatch for {short_name} {key}")

            source_response = Path(result["response_path"])
            if not source_response.is_absolute():
                source_response = REPO_ROOT / source_response
            response_dir = OUTPUT_ROOT / "responses" / short_name
            response_dir.mkdir(exist_ok=True)
            response_rel = Path("responses") / short_name / f"{job['slug']}__{job['period']}.json"
            shutil.copy2(source_response, OUTPUT_ROOT / response_rel)
            response_payload = json.loads((OUTPUT_ROOT / response_rel).read_text(encoding="utf-8"))

            executions.append(
                {
                    "execution_id": f"{short_name}__{job['slug']}__{job['period']}",
                    "model_name": model["display_name"],
                    "model_id": model["model_id"],
                    "slug": job["slug"],
                    "ticker": job["ticker"],
                    "period": job["period"],
                    "report_date": meta["report_date"],
                    "report_session": meta["report_session"],
                    "trading_day": meta["t0"],
                    "prompt_file": prompt_rel.as_posix(),
                    "prompt_sha256": prompt_hash,
                    "recorded_run_prompt_sha256": recorded_hash,
                    "transport": model["transport"],
                    "response_file": response_rel.as_posix(),
                    "response_sha256": hashlib.sha256((OUTPUT_ROOT / response_rel).read_bytes()).hexdigest(),
                    "response_analyst_label": response_payload["analyst_label"],
                    "response_direction": response_payload["direction"],
                    "run_status": result["status"],
                }
            )

    if len(prompt_index) != 63 or len(executions) != 189:
        raise RuntimeError(f"unexpected audit size: prompts={len(prompt_index)} executions={len(executions)}")

    (OUTPUT_ROOT / "prompt_index.json").write_text(
        json.dumps(prompt_index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (OUTPUT_ROOT / "executions_189.jsonl").open("w", encoding="utf-8") as handle:
        for execution in executions:
            handle.write(json.dumps(execution, ensure_ascii=False) + "\n")

    readme = f"""# Exact prompt and response audit — 63 events × 3 AIs

This folder makes the three-model experiment independently inspectable.

## What each AI received

For each event, the file in `prompts/` is the exact forced-choice prompt text used in the experiment. It contains:

1. the trading task and anti-look-ahead rules;
2. the forced label set: `Positive`, `Negative`, `N to P`, or `N to N` (no abstention);
3. the issuer's prior-quarter KPI history; and
4. the report's entity-scrubbed management discussion under `## The report's management discussion`.

The prompt does **not** embed the later share-price result or the analyst's choice. Issuer names and dates inside the report text were scrubbed during the original prompt preparation to reduce recognition and look-ahead leakage. This is report-derived text, not the original PDF binary or its page layout.

The original prompt index flags two unusually long MD&A extracts as length-truncated: BIMBO 2024-4T and KOF 2024-4T. The archive preserves exactly the truncated text the models actually received; the other 61 source extracts are not flagged as truncated.

The same event prompt was used for all three models. Luna and Codex Mini received the full text through Codex CLI standard input. DeepSeek received the same text through a chat API, divided into system and user messages immediately before `## Issuer`; concatenating those two parts reproduces the prompt file's content apart from boundary whitespace.

## Models

- Luna: `gpt-5.6-luna`
- DeepSeek Chat: `deepseek-chat`
- Codex Mini: `gpt-5.4-mini`

## Files

- `prompts/`: 63 exact prompt files, one per event.
- `responses/`: all 189 JSON responses, separated by model.
- `prompt_index.json`: event metadata, prompt hashes, and prompt sizes.
- `executions_189.jsonl`: one trace row per model/event execution, mapping the model, prompt, hash, and response.

## Integrity checks performed

- Prompt files: 63
- Execution records: 189
- Response files: 189
- Recorded run hashes compared successfully: {recorded_hashes_checked}
- Prompt size range: {min(prompt_sizes):,}–{max(prompt_sizes):,} bytes
- Every prompt contains the report MD&A section, prior-quarter history, and the no-abstention forced-choice instruction.

One cached DeepSeek result did not preserve `prompt_sha256` in its original run-manifest row; its audit hash is reconstructed from the same frozen source and transformation used by the runner. All other recorded hashes matched exactly.
"""
    (OUTPUT_ROOT / "README.md").write_text(readme, encoding="utf-8")

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(OUTPUT_ROOT.rglob("*")):
            if path.is_file():
                archive.write(path, Path("PROMPT_AUDIT_63x3") / path.relative_to(OUTPUT_ROOT))

    print(
        json.dumps(
            {
                "prompt_files": len(prompt_index),
                "execution_records": len(executions),
                "response_files": sum(1 for _ in (OUTPUT_ROOT / "responses").rglob("*.json")),
                "recorded_hashes_checked": recorded_hashes_checked,
                "audit_dir": str(OUTPUT_ROOT),
                "zip_path": str(ZIP_PATH),
                "zip_bytes": ZIP_PATH.stat().st_size,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
