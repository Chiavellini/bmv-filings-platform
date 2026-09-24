"""An empty coverage matrix must fail, not present itself as a result.

Running "Actualizar matriz de cobertura" from the Analyst Console on a checkout
without `soft/data/reports/` produced a matrix with 157 companies and **0 of
3,925 metric cells populated**, then exited 0. The console showed a green
"listo para abrir", marked the matrix available, and opened the blank file.
The build log already said "0 with data · filled 0 (0.0%)" -- the information
was there, it just never reached the analyst.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.build_master import build_master, coverage_counts  # noqa: E402


def _blank_row(slug: str) -> dict:
    """A company row that produced no values at all -- what a missing corpus yields."""
    return {
        "slug": slug,
        "name": slug.title(),
        "values": {},   # every metric unresolved
        "applic": {},   # nothing marked N/A, so the cells count as applicable gaps
    }


def test_coverage_counts_reports_zero_for_rows_with_no_values() -> None:
    """The guard's trigger condition: N/A and blank both count as unfilled."""
    filled, applicable, _gap = coverage_counts([_blank_row("alterna"), _blank_row("bbva")])
    assert filled == 0
    assert applicable > 0, "N/A-only rows must still contribute applicable cells"


@pytest.mark.skipif(
    (ROOT / "data" / "reports").is_dir(),
    reason="a real corpus is attached, so the matrix will not be empty",
)
def test_build_master_refuses_to_write_an_empty_matrix(monkeypatch) -> None:
    master_dir = ROOT / "outputs" / "_master"
    if master_dir.exists():
        shutil.rmtree(master_dir)

    with pytest.raises(SystemExit) as failure:
        build_master(
            None, no_network=True, skip_existing=False, render=False,
            limit=3, fetch_history=False, core=True,
        )

    message = str(failure.value)
    assert "vacía" in message, message
    assert "soft/data/reports" in message, "must name what is missing"
    assert not master_dir.exists(), (
        "a blank matrix was written; the console would advertise and open it"
    )
