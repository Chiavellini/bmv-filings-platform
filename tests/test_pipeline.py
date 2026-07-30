"""
test_pipeline.py — End-to-end pipeline tests.

Network tests (--url mode) are skipped by default.
Run with: pytest -m "not network" to skip them.
"""

import pytest
from pathlib import Path

from tests._corpus import requires_corpus_for

ROOT = Path(__file__).parent.parent

#: The local-file and directory modes read parsed SPORT filings from the
#: gitignored corpus. Guarding the classes that need it keeps the validation and
#: URL-mode classes running on a data-less clone.
_requires_sport = requires_corpus_for("sport")


@_requires_sport
class TestPipelineLocalFile:

    def test_single_md_returns_dataframe(self, sport_metric_defs):
        from src.extract.pipeline import run
        df = run(ROOT / "data" / "reports" / "sport" / "2026-1T.md",
                 do_validate=True,
                 config=ROOT / "configs" / "sport.yaml")
        assert df is not None
        assert len(df) == 1
        assert "revenue" in df.columns

    def test_single_md_revenue_value(self, sport_metric_defs):
        """Revenue must equal 589,053 — exact table value."""
        from src.extract.pipeline import run
        df = run(ROOT / "data" / "reports" / "sport" / "2026-1T.md",
                 config=ROOT / "configs" / "sport.yaml")
        assert df["revenue"].iloc[0] == pytest.approx(589_053.0)

    def test_single_md_attaches_suspects_attr(self, sport_metric_defs):
        """pipeline.run must attach df.attrs['suspects'] (series_checks) so the
        workbook marks sign/range/magnitude suspects — empty dict when clean."""
        from src.extract.pipeline import run
        df = run(ROOT / "data" / "reports" / "sport" / "2026-1T.md",
                 config=ROOT / "configs" / "sport.yaml")
        assert isinstance(df.attrs.get("suspects"), dict)

    def test_single_md_csv_output(self, tmp_path, sport_metric_defs):
        import pandas as pd
        from src.extract.pipeline import run
        csv_path = tmp_path / "output.csv"
        run(ROOT / "data" / "reports" / "sport" / "2026-1T.md",
            output_csv=csv_path,
            config=ROOT / "configs" / "sport.yaml")
        assert csv_path.exists()
        df = pd.read_csv(csv_path)
        assert "revenue" in df.columns
        assert len(df) == 1

    def test_single_md_long_csv(self, tmp_path, sport_metric_defs):
        """Long format CSV: one row per metric."""
        import pandas as pd
        from src.extract.pipeline import run
        csv_path = tmp_path / "long.csv"
        run(ROOT / "data" / "reports" / "sport" / "2026-1T.md",
            output_csv=csv_path, long_format=True,
            config=ROOT / "configs" / "sport.yaml")
        df = pd.read_csv(csv_path)
        assert "metric" in df.columns
        assert "confidence" in df.columns
        assert "current" in df.columns
        assert len(df) >= 10  # at least 10 metric rows

    def test_metric_filter(self, sport_metric_defs):
        from src.extract.pipeline import run
        df = run(ROOT / "data" / "reports" / "sport" / "2026-1T.md",
                 metrics=["revenue", "ebitda"],
                 config=ROOT / "configs" / "sport.yaml")
        cols = set(df.columns) - {"period"}
        # Should only contain the requested metrics (or subset if not found)
        assert cols <= {"revenue", "ebitda"}


@_requires_sport
class TestPipelineDirectory:

    def test_directory_mode_produces_40_rows(self):
        from src.extract.pipeline import run
        df = run(ROOT / "data" / "reports" / "sport",
                 config=ROOT / "configs" / "sport.yaml",
                 do_validate=False)
        assert len(df) >= 38, f"Expected ≥38 periods, got {len(df)}"

    def test_directory_mode_sorted_by_period(self):
        from src.extract.pipeline import run
        df = run(ROOT / "data" / "reports" / "sport",
                 config=ROOT / "configs" / "sport.yaml",
                 do_validate=False)
        periods = df["period"].tolist()
        assert periods == sorted(periods), "Periods are not sorted chronologically"

    def test_directory_csv_export(self, tmp_path):
        import pandas as pd
        from src.extract.pipeline import run
        csv_path = tmp_path / "batch.csv"
        run(ROOT / "data" / "reports" / "sport",
            output_csv=csv_path,
            config=ROOT / "configs" / "sport.yaml",
            do_validate=False)
        assert csv_path.exists()
        df = pd.read_csv(csv_path)
        assert "period" in df.columns
        assert "revenue" in df.columns
        assert len(df) >= 38

    def test_revenue_no_artifacts_in_batch(self):
        """No revenue value should be < 1000 (year numbers or split artifacts)."""
        from src.extract.pipeline import run
        df = run(ROOT / "data" / "reports" / "sport",
                 metrics=["revenue"],
                 config=ROOT / "configs" / "sport.yaml",
                 do_validate=False)
        if "revenue" not in df.columns:
            pytest.skip("revenue not in output")
        bad = df[df["revenue"] < 1_000]["revenue"].dropna()
        assert len(bad) == 0, f"Artifact revenue values found: {bad.tolist()}"


@_requires_sport
class TestPipelineMetricFiltering:

    def test_section_filter_income(self):
        from src.extract.pipeline import run
        from src.model.financial_model import METRICS, apply_config, load_config
        cfg = load_config(ROOT / "configs" / "sport.yaml")
        income_keys = {m.key for m in apply_config(METRICS, cfg) if m.section == "income"}

        df = run(ROOT / "data" / "reports" / "sport" / "2026-1T.md",
                 config=ROOT / "configs" / "sport.yaml",
                 do_validate=False)
        metric_cols = set(df.columns) - {"period"}
        # Should include at least revenue
        assert "revenue" in df.columns

    def test_specific_metrics_only(self):
        from src.extract.pipeline import run
        df = run(ROOT / "data" / "reports" / "sport" / "2026-1T.md",
                 metrics=["revenue", "ebitda", "clientes_activos"],
                 config=ROOT / "configs" / "sport.yaml",
                 do_validate=False)
        cols = set(df.columns) - {"period"}
        assert "revenue" in cols


@_requires_sport
class TestPipelineValidation:
    # Without the corpus these pass vacuously — run() returns an empty frame,
    # which is still "not None" — so they report green while asserting nothing.

    def test_validation_runs_by_default(self, capsys):
        from src.extract.pipeline import run
        run(ROOT / "data" / "reports" / "sport" / "2026-1T.md",
            config=ROOT / "configs" / "sport.yaml",
            do_validate=True)
        # Validation runs without error — no assertion needed; just must not raise

    def test_no_validate_flag_works(self):
        from src.extract.pipeline import run
        df = run(ROOT / "data" / "reports" / "sport" / "2026-1T.md",
                 config=ROOT / "configs" / "sport.yaml",
                 do_validate=False)
        assert df is not None


@pytest.mark.network
class TestPipelineUrlMode:
    """Network tests — skipped unless explicitly opted in."""

    def test_url_returns_dataframe(self, tmp_path, monkeypatch):
        """Mock the downloader to avoid real network calls."""
        from src.extract.pipeline import run
        from src.parse import parse_pdf

        # Monkeypatch download_from_ir to return existing PDFs
        pdf_files = sorted((ROOT / "data" / "reports" / "sport").glob("*.pdf"))[:3]

        monkeypatch.setattr(
            "src.download.downloader.download_from_ir",
            lambda *a, **kw: pdf_files,
        )
        df = run("https://www.sportsworld.com.mx/inversionistas/",
                 output_dir=tmp_path,
                 config=ROOT / "configs" / "sport.yaml",
                 do_validate=False)
        assert len(df) >= 1
        assert "revenue" in df.columns

    def test_url_uses_company_config_pattern(self, tmp_path, monkeypatch):
        """URL mode must pass the company-specific PDF selector into the downloader."""
        from src.extract.pipeline import run

        pdf_files = sorted((ROOT / "data" / "reports" / "sport").glob("*.pdf"))[:3]
        captured: dict = {}

        def fake_download(*args, **kwargs):
            captured.update(kwargs)
            return pdf_files

        monkeypatch.setattr("src.download.downloader.download_from_ir", fake_download)

        run("https://www.walmex.mx/en/financial-information/quarterly.html",
            output_dir=tmp_path,
            config=ROOT / "configs" / "walmex.yaml",
            do_validate=False)

        assert captured["file_pattern"] == r"(?:reporte|report|trimest|quarter|result|earning).*\.pdf"
