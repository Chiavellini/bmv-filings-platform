#!/usr/bin/env python3
"""Measure quarterly-document coverage for every company mentioned by Soft."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from estate_bridge import load_estate_bridge  # noqa: E402

ESTATE_BRIDGE = load_estate_bridge(project_root=ROOT)
SOFT = ROOT / "soft"
DEFAULT_ESTATE = ESTATE_BRIDGE.catalog_path
DEFAULT_JSON = (
    ESTATE_BRIDGE.estate_root / "coverage" / "soft_quarterly_coverage.json"
)


def _soft_universe() -> dict[str, dict]:
    spec = importlib.util.spec_from_file_location(
        "soft_gen_universe", SOFT / "scripts" / "gen_universe.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    universe = {}
    for sector, (template, members) in module.UNIVERSE.items():
        for ticker, name in members:
            universe[module.slugify(name)] = {
                "name": name, "ticker": ticker, "industry": sector, "template": template,
            }
    # The input directory is the operational contract; include orphan inputs such as ISTA.
    for path in sorted((SOFT / "inputs").glob("*.md")):
        if path.stem in universe:
            continue
        config = yaml.safe_load(
            (SOFT / "configs" / f"{path.stem}.yaml").read_text(encoding="utf-8")
        ) or {}
        company = config.get("company") or {}
        ticker = (config.get("ir_website") or {}).get("xbrl_ticker")
        universe[path.stem] = {
            "name": company.get("name") or path.stem,
            "ticker": ticker,
            "industry": None,
            "template": None,
        }
    return universe


def _ticker_aliases() -> dict[str, set[str]]:
    path = ROOT / "alpha-go" / "configs" / "bmv_corpus.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result: dict[str, set[str]] = {}
    for row in config.get("companies", []) or []:
        ticker = str(row.get("ticker") or "").strip().upper()
        slug = str(row.get("slug") or "").strip()
        if ticker and slug:
            result.setdefault(ticker, set()).add(slug)
    return result


def _period_key(period: str) -> tuple[int, int]:
    return int(period[:4]), int(period[5])


def audit(estate_path: Path, floor: int = 20) -> dict:
    universe = _soft_universe()
    aliases = _ticker_aliases()
    conn = sqlite3.connect(f"file:{estate_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT DISTINCT m.company,d.period,d.doc_type,a.format
        FROM memberships m
        JOIN documents d ON d.document_id=m.document_id
        JOIN artifacts a ON a.document_id=d.document_id
        WHERE d.period GLOB '[0-9][0-9][0-9][0-9]-[1-4]T'
    """).fetchall()
    conn.close()

    observed: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        bucket = observed.setdefault(
            row["company"], {"all": set(), "release": set(), "pdf": set()}
        )
        bucket["all"].add(row["period"])
        if row["doc_type"] == "quarterly_release":
            bucket["release"].add(row["period"])
        if row["format"] == "pdf":
            bucket["pdf"].add(row["period"])

    all_periods = sorted(
        {period for values in observed.values() for period in values["all"]},
        key=_period_key,
    )
    recent = all_periods[-floor:]
    companies = []
    for slug, metadata in sorted(universe.items()):
        company_aliases = {slug}
        if metadata.get("ticker"):
            company_aliases |= aliases.get(str(metadata["ticker"]).upper(), set())
        merged = {"all": set(), "release": set(), "pdf": set()}
        for alias in company_aliases:
            for kind in merged:
                merged[kind] |= observed.get(alias, {}).get(kind, set())
        quarter_count = len(merged["all"])
        companies.append({
            "slug": slug,
            **metadata,
            "aliases": sorted(company_aliases - {slug}),
            "quarter_count": quarter_count,
            "release_quarter_count": len(merged["release"]),
            "pdf_quarter_count": len(merged["pdf"]),
            "floor_gap": max(0, floor - quarter_count),
            "recent_floor_complete": set(recent).issubset(merged["all"]),
            "first_period": min(merged["all"], key=_period_key) if merged["all"] else None,
            "last_period": max(merged["all"], key=_period_key) if merged["all"] else None,
            "periods": sorted(merged["all"], key=_period_key),
        })

    summary = {
        "universe_companies": len(companies),
        "floor": floor,
        "companies_above_floor": sum(row["quarter_count"] > floor for row in companies),
        "companies_at_or_above_floor": sum(row["quarter_count"] >= floor for row in companies),
        "companies_below_floor": sum(0 < row["quarter_count"] < floor for row in companies),
        "companies_with_zero": sum(row["quarter_count"] == 0 for row in companies),
        "companies_with_recent_floor_complete": sum(
            row["recent_floor_complete"] for row in companies
        ),
        "missing_quarters_to_floor": sum(row["floor_gap"] for row in companies),
        "latest_period": all_periods[-1] if all_periods else None,
        "recent_floor_window": recent,
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "estate": str(estate_path.resolve()),
        "summary": summary,
        "companies": companies,
    }


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _markdown(payload: dict) -> str:
    summary = payload["summary"]
    below = [
        row for row in payload["companies"] if row["quarter_count"] < summary["floor"]
    ]
    lines = [
        "# Soft quarterly-document coverage",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        f"- Operational Soft universe: **{summary['universe_companies']} companies**",
        f"- At least {summary['floor']} quarters: "
        f"**{summary['companies_at_or_above_floor']}**",
        f"- More than {summary['floor']} quarters: **{summary['companies_above_floor']}**",
        f"- Partial (1–{summary['floor'] - 1}): **{summary['companies_below_floor']}**",
        f"- Zero: **{summary['companies_with_zero']}**",
        f"- Missing slots to the {summary['floor']}-quarter floor: "
        f"**{summary['missing_quarters_to_floor']}**",
        f"- Complete latest-{summary['floor']} window through "
        f"{summary['latest_period']}: **{summary['companies_with_recent_floor_complete']}**",
        "",
        "## Companies below the floor",
        "",
        "| Company | Quarters | Gap | First | Latest |",
        "|---|---:|---:|---|---|",
    ]
    for row in sorted(below, key=lambda value: (value["quarter_count"], value["slug"])):
        lines.append(
            f"| {row['slug']} | {row['quarter_count']} | {row['floor_gap']} | "
            f"{row['first_period'] or '—'} | {row['last_period'] or '—'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--estate", type=Path, default=DEFAULT_ESTATE)
    parser.add_argument("--floor", type=int, default=20)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    markdown = args.markdown or args.json.with_suffix(".md")
    payload = audit(args.estate, args.floor)
    _write(args.json, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _write(markdown, _markdown(payload))
    print(json.dumps(payload["summary"], ensure_ascii=False, sort_keys=True))
    print(f"JSON: {args.json}")
    print(f"Markdown: {markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
