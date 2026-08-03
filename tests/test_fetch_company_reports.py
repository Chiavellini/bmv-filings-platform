"""Tests for scripts/fetch_company_reports.py — canonical rename, 2016 floor,
collision precedence, and the period_from_url shim."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.fetch_company_reports as fcr  # noqa: E402
from src.shared.report_index import infer_period_label  # noqa: E402


def _pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.4\n%canonical-rename-test\n")
    return path


def test_period_from_url_delegates_to_infer_period_label():
    urls = [
        "https://x.com/reports/Reporte-BMV-1T-2025.pdf",
        "https://x.com/2018/4to-trimestre.pdf",
        "https://x.com/files/4Q16-results.pdf",
    ]
    for u in urls:
        stem = u.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        assert fcr.period_from_url(u) == infer_period_label(stem)


def test_canonical_rename_from_native_names(tmp_path):
    staging = tmp_path / "_raw"
    staging.mkdir()
    out = tmp_path / "out"
    _pdf(staging / "Reporte-BMV-1T-2025.pdf")
    _pdf(staging / "4Q16-results.pdf")
    _pdf(staging / "La-comer-3er-Trimestre-2018.pdf")

    saved = fcr._canonicalize_into(list(staging.glob("*.pdf")), out, floor_year=2016)

    assert set(saved) == {"2025-1T", "2016-4T", "2018-3T"}
    assert (out / "2025-1T.pdf").exists()
    assert (out / "2016-4T.pdf").exists()
    assert (out / "2018-3T.pdf").exists()


def test_2016_floor_drops_pre_2016(tmp_path):
    staging = tmp_path / "_raw"
    staging.mkdir()
    out = tmp_path / "out"
    _pdf(staging / "Reporte-BMV-4T-2015.pdf")
    _pdf(staging / "Reporte-BMV-1T-2016.pdf")

    saved = fcr._canonicalize_into(list(staging.glob("*.pdf")), out, floor_year=2016)

    assert saved == ["2016-1T"]
    assert not (out / "2015-4T.pdf").exists()
    assert (out / "2016-1T.pdf").exists()


def test_collision_prefers_release_over_dictaminado(tmp_path):
    staging = tmp_path / "_raw"
    staging.mkdir()
    out = tmp_path / "out"
    # Two files for the same period: an audited ("Dictaminado") and a release.
    _pdf(staging / "Reporte-BMV-4T-2023-Dictaminada.pdf")
    release = _pdf(staging / "Reporte-BMV-4T-2023-release.pdf")

    saved = fcr._canonicalize_into(list(staging.glob("*.pdf")), out, floor_year=2016)

    assert saved == ["2023-4T"]
    # The canonical file's bytes must come from the release (precedence in
    # report_index._candidate_key ranks "release" ahead of others).
    assert (out / "2023-4T.pdf").read_bytes() == release.read_bytes()


def test_canonicalize_is_idempotent(tmp_path):
    staging = tmp_path / "_raw"
    staging.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    (out / "2024-1T.pdf").write_bytes(b"%PDF-1.4\nexisting\n")
    _pdf(staging / "Reporte-BMV-1T-2024.pdf")

    saved = fcr._canonicalize_into(list(staging.glob("*.pdf")), out, floor_year=2016)

    # Existing canonical target is left untouched and not re-reported.
    assert saved == []
    assert (out / "2024-1T.pdf").read_bytes() == b"%PDF-1.4\nexisting\n"


def test_force_refresh_cleanup_preserves_non_parser_artifacts(tmp_path):
    out = tmp_path / "reports"
    out.mkdir()
    for name in ("2024-1T.pdf", "2024-1T.md", "2024-1T.jsonl"):
        (out / name).write_text("generated", encoding="utf-8")
    (out / "2024-1T_facts.json").write_text("facts", encoding="utf-8")
    (out / "notes.txt").write_text("keep", encoding="utf-8")

    removed = fcr._clear_generated_report_artifacts(out, floor_year=2016)

    assert removed == 3
    assert not (out / "2024-1T.pdf").exists()
    assert not (out / "2024-1T.md").exists()
    assert not (out / "2024-1T.jsonl").exists()
    assert (out / "2024-1T_facts.json").read_text(encoding="utf-8") == "facts"
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_force_refresh_cleanup_preserves_pre_floor_pdfs(tmp_path):
    """Files below the refill floor must survive --force-refresh.

    The refill enforces floor_year, so deleting older files (e.g. chedraui's
    2010–2015 Wayback-recovered PDFs) is permanent data loss.
    """
    out = tmp_path / "reports"
    out.mkdir()
    for name in ("2012-3T.pdf", "2012-3T.md", "2015-4T.pdf"):
        (out / name).write_text("historic", encoding="utf-8")
    for name in ("2024-1T.pdf", "2024-1T.md"):
        (out / name).write_text("refreshable", encoding="utf-8")
    (out / "unparseable-name.md").write_text("keep", encoding="utf-8")

    removed = fcr._clear_generated_report_artifacts(out, floor_year=2016)

    assert removed == 2
    assert (out / "2012-3T.pdf").exists()
    assert (out / "2012-3T.md").exists()
    assert (out / "2015-4T.pdf").exists()
    assert (out / "unparseable-name.md").exists()
    assert not (out / "2024-1T.pdf").exists()
    assert not (out / "2024-1T.md").exists()


def test_copy_generated_artifacts_respects_overwrite(tmp_path):
    staged = tmp_path / "staged"
    out = tmp_path / "out"
    staged.mkdir()
    out.mkdir()
    (staged / "2024-1T.pdf").write_text("new", encoding="utf-8")
    (staged / "2024-1T.md").write_text("new-md", encoding="utf-8")
    (staged / "2024-1T_facts.json").write_text("skip", encoding="utf-8")
    (out / "2024-1T.pdf").write_text("old", encoding="utf-8")

    copied = fcr._copy_generated_artifacts(staged, out, overwrite=False)

    assert copied == 1
    assert (out / "2024-1T.pdf").read_text(encoding="utf-8") == "old"
    assert (out / "2024-1T.md").read_text(encoding="utf-8") == "new-md"
    assert not (out / "2024-1T_facts.json").exists()

    copied = fcr._copy_generated_artifacts(staged, out, overwrite=True)
    assert copied == 2
    assert (out / "2024-1T.pdf").read_text(encoding="utf-8") == "new"


def test_expected_periods_respects_floor():
    periods = fcr._expected_periods(2016)
    assert "2016-1T" in periods
    assert "2015-4T" not in periods
    # Every period is on/after the floor.
    assert all(int(p[:4]) >= 2016 for p in periods)


def test_fetch_propagates_browser_tls_profile_to_url_templates(
    monkeypatch,
    tmp_path,
):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "soriana.yaml").write_text(
        """
ir_website:
  url: https://issuer.example/quarterlies
  impersonate: safari
  direct_url_templates:
    - https://issuer.example/{year}/{quarter}Q{year2}.pdf
""".lstrip(),
        encoding="utf-8",
    )

    observed: dict[str, object] = {}

    def fake_template_download(templates, out_dir, periods, **kwargs):
        observed.update(
            templates=templates,
            out_dir=out_dir,
            periods=periods,
            kwargs=kwargs,
        )
        return []

    from src.download import downloader

    monkeypatch.setattr(fcr, "ROOT", tmp_path)
    monkeypatch.setattr(fcr, "download_from_ir", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(fcr, "_expected_periods", lambda _floor: {"2026-2T"})
    monkeypatch.setattr(
        downloader,
        "download_from_url_templates",
        fake_template_download,
    )

    fcr.fetch(
        "soriana",
        floor_year=2026,
        use_xbrl=False,
        use_wayback=False,
    )

    assert observed["periods"] == {"2026-2T"}
    assert observed["kwargs"] == {"delay_ms": 300, "impersonate": "safari"}


def test_parse_period_spanish_ordinals():
    """parse_period handles LACOMER's Spanish quarter-ordinal filename conventions."""
    from src.eval.compare_extractions import parse_period

    cases = [
        ("La-comer-1er-Trimestre-2018", "1Q18A"),
        ("La-comer-2do-Trimestre-2018", "2Q18A"),
        ("La-comer-3er-Trimestre-2018-82", "3Q18A"),
        ("La-comer-4to-Trimestre-2018", "4Q18A"),
        ("La-comer-1er-Trimestre-2019", "1Q19A"),
        ("La-comer-2do-Trimestre-2019", "2Q19A"),
        ("La-Comer-2do-Trimestre-2022", "2Q22A"),
        ("4_Trimestre_2023", "4Q23A"),
        # Regression: existing patterns still work.
        ("2016-1T", "1Q16A"),
        ("1T20bmv", "1Q20A"),
        ("1Q16-Results", "1Q16A"),
    ]
    for stem, expected in cases:
        assert parse_period(stem) == expected, f"parse_period({stem!r}) = {parse_period(stem)!r}, expected {expected!r}"
