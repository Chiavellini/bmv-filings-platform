"""Launchpad Segments wrapper: sync-then-build, never loosening publication."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_segments, console_segments


FRESHNESS_BLOCKER = "canonical estate freshness is unproven (no_succeeded_sync_receipt)"


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, fresh: list[bool],
                 sync_ok: bool = True, blockers: tuple[str, ...] = (), published: bool = True):
        self.fresh = list(fresh)
        self.sync_calls: list[str] = []
        self.run_kwargs: dict = {}
        self.candidate = tmp_path / "outputs" / "Soriana" / "excel" / "Soriana_segments.xlsx"
        self.latest = tmp_path / "outputs" / "latest" / "Soriana.xlsx"
        self.result_path = tmp_path / "state" / "result.json"

        def freshness(slug):
            return SimpleNamespace(fresh=self.fresh.pop(0) if len(self.fresh) > 1 else self.fresh[0])

        def sync(slug):
            self.sync_calls.append(slug)
            return sync_ok

        def run(md_path, **kwargs):
            self.run_kwargs = kwargs
            return build_segments.BuildResult(
                candidate_path=self.candidate,
                latest_path=self.latest if published else None,
                manifest_path=tmp_path / "manifest.json",
                publish_blockers=blockers,
            )

        monkeypatch.setattr(console_segments, "_freshness", freshness)
        monkeypatch.setattr(console_segments, "_sync_issuer", sync)
        monkeypatch.setattr(console_segments, "_estate_connected", lambda: True)
        monkeypatch.setattr(build_segments, "run", run)

    def main(self, *extra: str) -> int:
        return console_segments.main([
            "input.md", "--slug", "soriana", "--analyst-metrics", "metrics.csv",
            "--analyst-company", "SORIANA", "--result", str(self.result_path), *extra,
        ])

    @property
    def result(self) -> dict:
        return json.loads(self.result_path.read_text(encoding="utf-8"))


def test_already_fresh_issuer_skips_sync_and_publishes(monkeypatch, tmp_path) -> None:
    h = _Harness(monkeypatch, tmp_path, fresh=[True])
    assert h.main() == 0
    # A strict sync that then failed would supersede the earlier success.
    assert h.sync_calls == []
    assert h.run_kwargs["require_fresh_estate"] is True
    assert h.run_kwargs["estate_only"] is True
    assert h.result["status"] == "published"
    assert h.result["workbook"] == str(h.latest)


def test_stale_issuer_is_synced_and_then_built_strictly(monkeypatch, tmp_path) -> None:
    h = _Harness(monkeypatch, tmp_path, fresh=[False, True])
    assert h.main() == 0
    assert h.sync_calls == ["soriana"]
    assert h.run_kwargs["require_fresh_estate"] is True
    assert (h.result["status"], h.result["fresh"], h.result["synced"]) == ("published", True, True)


def test_failed_sync_yields_labelled_candidate_not_publication(monkeypatch, tmp_path, capsys) -> None:
    h = _Harness(
        monkeypatch, tmp_path, fresh=[False], sync_ok=False,
        blockers=(FRESHNESS_BLOCKER,), published=False,
    )
    assert h.main() == 0
    assert h.sync_calls == ["soriana"]
    assert h.run_kwargs["require_fresh_estate"] is False
    assert h.result["status"] == "candidate_unverified"
    assert h.result["workbook"] == str(h.candidate)
    assert h.result["fresh"] is False and h.result["synced"] is False
    assert "frescura del Estate no verificada" in capsys.readouterr().out


def test_sync_success_without_receipt_still_counts_as_unverified(monkeypatch, tmp_path) -> None:
    h = _Harness(
        monkeypatch, tmp_path, fresh=[False, False],
        blockers=(FRESHNESS_BLOCKER,), published=False,
    )
    assert h.main() == 0
    assert h.run_kwargs["require_fresh_estate"] is False
    assert h.result["status"] == "candidate_unverified"


def test_other_publication_blockers_still_fail_the_job(monkeypatch, tmp_path, capsys) -> None:
    h = _Harness(
        monkeypatch, tmp_path, fresh=[False], sync_ok=False,
        blockers=(FRESHNESS_BLOCKER, "verification gate is not STRONG"), published=False,
    )
    assert h.main() == 3
    assert h.result["status"] == "blocked"
    assert h.result["workbook"] is None
    assert "ERROR: La hoja no pasó las verificaciones de publicación" in capsys.readouterr().out


def test_no_sync_flag_never_touches_the_estate(monkeypatch, tmp_path) -> None:
    h = _Harness(monkeypatch, tmp_path, fresh=[False], blockers=(FRESHNESS_BLOCKER,), published=False)
    assert h.main("--no-sync") == 0
    assert h.sync_calls == []


def test_input_errors_surface_as_spanish_error_line(monkeypatch, tmp_path, capsys) -> None:
    h = _Harness(monkeypatch, tmp_path, fresh=[True])

    def boom(md_path, **kwargs):
        raise build_segments.InputError("analyst metric contract failed before extraction:\n  - x")

    monkeypatch.setattr(build_segments, "run", boom)
    assert h.main() == 2
    out = capsys.readouterr().out
    assert "ERROR: La solicitud no pasó la validación: analyst metric contract failed" in out
    assert h.result["status"] == "failed"


def test_disconnected_estate_is_never_synced(monkeypatch, tmp_path, capsys) -> None:
    h = _Harness(monkeypatch, tmp_path, fresh=[False], blockers=(FRESHNESS_BLOCKER,), published=False)
    monkeypatch.setattr(console_segments, "_estate_connected", lambda: False)
    assert h.main() == 0
    assert h.sync_calls == []
    assert h.result["synced"] is None
    assert "no está conectado" in capsys.readouterr().out
