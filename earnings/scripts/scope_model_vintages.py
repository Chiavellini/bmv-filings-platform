#!/usr/bin/env python3
"""scope_model_vintages.py — read-only inventory of analyst-model snapshots.

Finds every desk Excel model / flattened model CSV on the machine whose name
carries a vintage tag (pre/post + quarter), so the point-in-time estimates
series (estimates_pit.parquet) is built from a documented, deduplicated
inventory rather than an ad-hoc file pick. Output committed to
earnings/audit/model_vintage_inventory.csv.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import pandas as pd

DEFAULT_SEARCH_ROOTS = [
    Path.home() / "Code",
    Path.home() / "Downloads",
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Library" / "Containers" / "com.microsoft.Excel" / "Data",
]
# <TICKER>_Model_<pre|post><Q><YY|YYYY>… .xlsx/.csv (case-insensitive)
VINTAGE = re.compile(
    r"(?P<ticker>[A-Za-z]+)[_ ]Model[_ ](?P<tag>pre|post)\s*(?P<q>[1-4])Q(?P<y>\d{2,4})",
    re.IGNORECASE)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def configured_search_roots(explicit: list[str] | None = None) -> list[Path]:
    if explicit:
        return [Path(item).expanduser().resolve() for item in explicit]
    configured = os.environ.get("EARNINGS_MODEL_SEARCH_ROOTS", "")
    if configured:
        return [
            Path(item).expanduser().resolve()
            for item in configured.split(os.pathsep)
            if item.strip()
        ]
    return DEFAULT_SEARCH_ROOTS


def find_candidates(search_roots: list[Path] | None = None) -> list[Path]:
    seen = set()
    out = []
    for root in search_roots or configured_search_roots():
        if not root.exists():
            continue
        try:
            proc = subprocess.run(
                ["find", str(root), "-iname", "*model*", "(",
                 "-iname", "*.xlsx", "-o", "-iname", "*.xlsm",
                 "-o", "-iname", "*.csv", ")", "-not", "-path", "*/.git/*"],
                capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            print(f"WARN: find timed out under {root}")
            continue
        for line in proc.stdout.splitlines():
            p = Path(line)
            if p in seen or not VINTAGE.search(p.name):
                continue
            seen.add(p)
            out.append(p)
    return sorted(out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--search-root",
        action="append",
        default=[],
        help=(
            "directory to inspect (repeatable); otherwise use "
            "EARNINGS_MODEL_SEARCH_ROOTS or portable home-directory defaults"
        ),
    )
    arguments = parser.parse_args(argv)
    rows = []
    for p in find_candidates(configured_search_roots(arguments.search_root)):
        m = VINTAGE.search(p.name)
        year = m.group("y")
        year = f"20{year}" if len(year) == 2 else year
        try:
            stat = p.stat()
            digest = sha256_file(p)
        except OSError:
            continue
        rows.append({
            "path": str(p),
            "ticker": m.group("ticker").upper(),
            "vintage": f"{m.group('tag').lower()}{m.group('q')}Q{year[2:]}",
            "vintage_period": f"{year}-{m.group('q')}T",
            "tag": m.group("tag").lower(),
            "ext": p.suffix.lower(),
            "mtime": datetime.fromtimestamp(stat.st_mtime)
                     .isoformat(timespec="seconds"),
            "size": stat.st_size,
            "sha256": digest,
        })
    df = pd.DataFrame(rows)
    if len(df):
        df["dup_group"] = df.groupby("sha256").ngroup()
        df = df.sort_values(["ticker", "vintage_period", "tag", "path"])
    out_dir = bs.EARNINGS_ROOT / "audit"
    out_dir.mkdir(exist_ok=True)
    df.to_csv(out_dir / "model_vintage_inventory.csv", index=False)

    print(f"inventory: {len(df)} files, "
          f"{df['sha256'].nunique() if len(df) else 0} unique contents")
    if len(df):
        uniq = df.drop_duplicates("sha256")
        print("\nunique snapshots per (vintage, tag):")
        print(uniq.groupby(["vintage_period", "tag"])["ticker"]
              .agg(["count", lambda s: ",".join(sorted(set(s)))])
              .rename(columns={"<lambda_0>": "tickers"}).to_string())


if __name__ == "__main__":
    main()
