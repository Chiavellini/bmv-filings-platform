"""materialize.py — force iCloud "dataless" files back to local, with a bounded wait.

The repo lives on an iCloud-synced Desktop, so ``fileproviderd`` evicts files to a
``dataless`` placeholder under load; a plain ``read_text()`` on one then blocks for
minutes on the network fetch (and long runs time out). These helpers request
materialization via ``brctl download`` (idempotent — a no-op on an already-local file),
poll the ``dataless`` flag until it clears, and force a read as a fallback. Every wait
is bounded so materialization can never hang forever.

See docs/DEV_SETUP.md; the permanent fix is to move the repo off the synced Desktop.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Iterable

_BRCTL = "/usr/bin/brctl"


def is_dataless(path: Path) -> bool:
    """True if ``path`` is an evicted iCloud placeholder (not materialized locally)."""
    try:
        out = subprocess.run(
            ["stat", "-f", "%Sf", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "dataless" in out.stdout


def _brctl_download(path: Path) -> None:
    try:
        subprocess.run([_BRCTL, "download", str(path)],
                       capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def _materialize_one(path: Path, timeout: float) -> None:
    """Force ``path`` local by reading it through. ``brctl download`` is only a hint —
    the synchronous read is what reliably triggers the fetch. Bounded by ``timeout`` so a
    single pathologically-slow file (iCloud can throttle to ~90s/file) can't block forever;
    a timed-out file is simply left dataless for the next pass."""
    _brctl_download(path)
    try:
        subprocess.run(["cat", str(path)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        pass


def materialize(
    paths: Iterable[Path | str], *, per_file_timeout: float = 300.0,
    on_progress: Callable[[int, int, Path], None] | None = None,
) -> int:
    """Force each evicted path local; return how many were dataless to begin with."""
    dataless = [Path(p) for p in paths if Path(p).exists() and is_dataless(Path(p))]
    for i, f in enumerate(dataless):
        _materialize_one(f, per_file_timeout)
        if on_progress:
            on_progress(i + 1, len(dataless), f)
    return len(dataless)


def materialize_dir(
    directory: Path | str,
    patterns: Iterable[str] = ("*.md", "*_facts.json", "*.pdf"),
    *, per_file_timeout: float = 300.0,
    on_progress: Callable[[int, int, Path], None] | None = None,
) -> int:
    """Force every matching evicted file under ``directory`` (recursively) local."""
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    files = [f for pat in patterns for f in directory.rglob(pat)]
    dataless = [f for f in files if is_dataless(f)]
    if not dataless:
        return 0
    _brctl_download(directory)     # one recursive hint up front
    for i, f in enumerate(dataless):
        _materialize_one(f, per_file_timeout)
        if on_progress:
            on_progress(i + 1, len(dataless), f)
    return len(dataless)


def request_download(path: Path | str) -> None:
    """Fire-and-forget: request a (recursive) download without waiting. Cheap warm-up."""
    _brctl_download(Path(path))


def dataless_count(directory: Path | str,
                   patterns: Iterable[str] = ("*.md", "*_facts.json", "*.pdf")) -> int:
    """How many matching files under ``directory`` are currently evicted (for reporting)."""
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    return sum(1 for pat in patterns for f in directory.rglob(pat) if is_dataless(f))
