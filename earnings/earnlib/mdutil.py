"""Minimal markdown helpers (no tabulate dependency in the venv)."""
from __future__ import annotations

import pandas as pd


def md_table(df: pd.DataFrame, float_fmt: str = "{:.4g}") -> str:
    def cell(v) -> str:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        if isinstance(v, float):
            return float_fmt.format(v)
        return str(v)

    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |",
             "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(cell(r[c]) for c in cols) + " |")
    return "\n".join(lines)
