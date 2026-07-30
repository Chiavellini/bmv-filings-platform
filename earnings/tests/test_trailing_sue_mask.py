"""Issue A — availability mask. trailing_sue itself honors filed=; the bug is
call sites (margin_sue, seasonal_qoq, walk-forward) that omit it."""
import importlib.util
import inspect
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _calls_with_filed(source: str) -> list[bool]:
    """For each trailing_sue( call in source, whether the balanced-paren call
    text contains a filed= argument."""
    out = []
    for m in re.finditer(r"trailing_sue\(", source):
        i, depth = m.end(), 1
        while depth and i < len(source):
            depth += {"(": 1, ")": -1}.get(source[i], 0)
            i += 1
        out.append("filed" in source[m.start():i])
    return out


def test_filed_mask_excludes_late_filed_quarter():
    """A quarter filed AFTER the event quarter must not enter sigma."""
    from earnlib.surprises import trailing_sue

    dx = np.array([1.0, 2.0, 1.5, 30.0, 2.5], dtype=float)
    filed = np.array([datetime(2020, 4, 1), datetime(2020, 7, 1),
                      datetime(2020, 10, 1),
                      datetime(2021, 6, 1),   # Q4 filed late, after Q1 event
                      datetime(2021, 4, 30)], dtype=object)
    masked = trailing_sue(dx, min_hist=2, target_hist=8, filed=filed)
    unmasked = trailing_sue(dx, min_hist=2, target_hist=8)
    sigma_masked = np.std(np.array([1.0, 2.0, 1.5]), ddof=1)
    assert np.isclose(masked[4], 2.5 / sigma_masked)
    sigma_unmasked = np.std(np.array([1.0, 2.0, 1.5, 30.0]), ddof=1)
    assert np.isclose(unmasked[4], 2.5 / sigma_unmasked)
    assert not np.isclose(masked[4], unmasked[4])


def test_margin_sue_call_site_masked():
    mod = _load_script("run_refinements")
    flags = _calls_with_filed(inspect.getsource(mod.add_margin_sue))
    assert flags and all(flags)


def test_seasonal_qoq_call_site_masked():
    mod = _load_script("run_refinements")
    flags = _calls_with_filed(inspect.getsource(mod.add_composites))
    assert flags and all(flags)


def test_walkforward_call_sites_masked():
    source = (SCRIPTS / "run_walkforward.py").read_text()
    flags = _calls_with_filed(source)
    assert flags and all(flags)
