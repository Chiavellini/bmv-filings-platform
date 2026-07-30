#!/usr/bin/env python3
"""Download Regional's canonical quarterly reports from its public official JSON feed."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from estate_bridge import load_estate_bridge  # noqa: E402
from scripts.parse_reports_for_search import parse_for_search  # noqa: E402
from src.download.downloader import _download_pdf, _make_session  # noqa: E402

ESTATE_BRIDGE = load_estate_bridge(project_root=ROOT)
FEED_URL = "https://www.regional.mx/consulta.php?action=archivo"
BASE_URL = "https://www.regional.mx/"
DEFAULT_OUT = ROOT / "data" / "reports" / "regional"
DEFAULT_REPORT = (
    ESTATE_BRIDGE.estate_root / "expansion" / "regional_ir.json"
)
_QUARTER_RE = re.compile(
    r"\b(1st|2nd|3rd|4th|first|second|third|fourth)\s+quarter\b.*?\b(20\d{2})\b",
    re.IGNORECASE,
)
_QUARTERS = {
    "1st": 1, "first": 1, "2nd": 2, "second": 2,
    "3rd": 3, "third": 3, "4th": 4, "fourth": 4,
}


def period_from_row(row: dict) -> str | None:
    text = f"{row.get('titulo', '')} {row.get('subtitulo', '')}"
    match = _QUARTER_RE.search(text)
    if not match:
        return None
    return f"{match.group(2)}-{_QUARTERS[match.group(1).lower()]}T"


def select_quarterlies(rows: list[dict], floor_year: int) -> list[dict]:
    """Select one latest primary Regional quarterly report per period."""
    selected: dict[str, dict] = {}
    for row in rows:
        if str(row.get("id_empresa")) != "2" or str(row.get("id_clase_documento")) != "16":
            continue
        period = period_from_row(row)
        if not period or int(period[:4]) < floor_year:
            continue
        raw_url = row.get("esp") or row.get("ing")
        if not raw_url:
            continue
        enriched = {**row, "period": period, "url": urljoin(BASE_URL, raw_url)}
        current = selected.get(period)
        if current is None or int(row.get("id_archivo") or 0) > int(
            current.get("id_archivo") or 0
        ):
            selected[period] = enriched
    return [selected[period] for period in sorted(selected)]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--floor-year", type=int, default=2016)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--parse", action="store_true")
    args = parser.parse_args(argv)

    session = _make_session()
    response = session.get(FEED_URL, timeout=120)
    response.raise_for_status()
    selected = select_quarterlies(response.json(), args.floor_year)
    print(
        f"Regional official quarterly feed: {len(selected)} periods "
        f"({selected[0]['period'] if selected else '—'}.."
        f"{selected[-1]['period'] if selected else '—'})"
    )
    downloaded = parsed = 0
    failures: list[dict] = []
    if args.apply:
        args.out.mkdir(parents=True, exist_ok=True)
        for number, row in enumerate(selected, 1):
            pdf = args.out / f"{row['period']}.pdf"
            try:
                if not pdf.exists():
                    _download_pdf(session, row["url"], pdf)
                    downloaded += 1
                if args.parse and not pdf.with_suffix(".md").exists():
                    _atomic_write(pdf.with_suffix(".md"), parse_for_search(pdf))
                    parsed += 1
                print(f"[{number}/{len(selected)}] {row['period']}", flush=True)
            except Exception as exc:
                failures.append({
                    "period": row["period"],
                    "url": row["url"],
                    "error": f"{type(exc).__name__}: {exc}",
                })

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feed_url": FEED_URL,
        "applied": args.apply,
        "selected_periods": len(selected),
        "downloaded": downloaded,
        "parsed": parsed,
        "failures": failures,
        "documents": selected,
    }
    _atomic_write(args.report, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(
        f"downloaded={downloaded} parsed={parsed} failures={len(failures)} "
        f"report={args.report}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
