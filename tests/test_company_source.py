"""Config resolution across the three surfaces that describe a company's sources."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.shared.company_source import (
    UnknownCompanyError,
    resolve_company_source,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def registry_file(tmp_path: Path) -> Path:
    payload = {
        "version": 1,
        "defaults": {"exchange": "BMV", "currency": "MXN"},
        "groups": {
            "retail": {
                "template": "industrial",
                "issuers": [
                    {"slug": "bound_co", "ticker": "BOUND", "name": "Bound Co"},
                    {"slug": "roster_only", "ticker": "ROSTER", "name": "Roster Only"},
                ],
            }
        },
        "overlays": {
            "bound_co": {
                "name": "Bound Company SA",
                "ir": {
                    "url": "https://ir.example.com/reports",
                    "pdf_link_pattern": r"(?i)\.pdf",
                    "use_playwright": True,
                    "max_reports": 40,
                    "delay_ms": 250,
                    "coverage_from_period": "2024-1T",
                },
            }
        },
    }
    path = tmp_path / "issuers.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_roster_only_issuer_still_resolves_an_xbrl_ticker(registry_file, tmp_path):
    """The whole point: no per-company file, no IR binding, still usable.

    A ticker is all the BMV XBRL archive needs, so an unconfigured issuer must
    resolve rather than raise — that is what unblocks the other 155.
    """
    source = resolve_company_source(
        "roster_only", configs_dir=tmp_path / "configs", registry_path=registry_file
    )
    assert source.ticker == "ROSTER"
    assert source.xbrl_ticker == "ROSTER"
    assert source.has_xbrl is True
    assert source.has_ir_binding is False
    assert source.ir_url is None
    assert source.provenance == ("roster",)


def test_registry_overlay_supplies_the_ir_binding(registry_file, tmp_path):
    source = resolve_company_source(
        "bound_co", configs_dir=tmp_path / "configs", registry_path=registry_file
    )
    assert source.ir_url == "https://ir.example.com/reports"
    assert source.use_playwright is True
    assert source.max_reports == 40
    assert source.delay_ms == 250
    assert source.has_ir_binding is True


def test_source_coverage_window_never_becomes_the_listing_floor(registry_file, tmp_path):
    """``coverage_from_period`` describes the binding, not the issuer.

    Reading it as a listing date would declare a company whose source was
    verified for one quarter to be at 100% coverage.
    """
    source = resolve_company_source(
        "bound_co", configs_dir=tmp_path / "configs", registry_path=registry_file
    )
    assert source.ir_coverage_from_period == "2024-1T"
    assert source.coverage_from_period is None


def test_per_company_config_outranks_the_registry(registry_file, tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "bound_co.yaml").write_text(
        yaml.safe_dump(
            {
                "company": {"name": "Bound Co"},
                "ir_website": {
                    "url": "https://hand-tuned.example.com/",
                    "delay_ms": 900,
                },
            }
        ),
        encoding="utf-8",
    )
    source = resolve_company_source(
        "bound_co", configs_dir=configs, registry_path=registry_file
    )
    assert source.ir_url == "https://hand-tuned.example.com/"
    assert source.delay_ms == 900
    # Unset in the per-company file, so the registry overlay still supplies it.
    assert source.max_reports == 40
    assert source.provenance[-1] == "per_company_config"


def test_config_without_a_url_falls_back_instead_of_raising(registry_file, tmp_path):
    """configs/orbia.yaml and configs/grupo_mexico.yaml ship an ``ir_website:``
    block with no ``url:``; the old fetcher raised KeyError on both."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "bound_co.yaml").write_text(
        yaml.safe_dump({"ir_website": {"delay_ms": 700}}), encoding="utf-8"
    )
    source = resolve_company_source(
        "bound_co", configs_dir=configs, registry_path=registry_file
    )
    assert source.ir_url == "https://ir.example.com/reports"
    assert source.delay_ms == 700


def test_unknown_slug_raises(registry_file, tmp_path):
    with pytest.raises(UnknownCompanyError):
        resolve_company_source(
            "no_such_issuer",
            configs_dir=tmp_path / "configs",
            registry_path=registry_file,
        )


def test_ir_kwargs_only_carries_resolved_values(registry_file, tmp_path):
    source = resolve_company_source(
        "bound_co", configs_dir=tmp_path / "configs", registry_path=registry_file
    )
    kwargs = source.ir_kwargs()
    assert kwargs["use_playwright"] is True
    assert kwargs["delay_ms"] == 250
    assert kwargs["file_pattern"] == r"(?i)\.pdf"
    # Never invent keys the downloader would treat as an override.
    assert "browser_first" not in kwargs
    assert "impersonate" not in kwargs


def test_every_real_roster_issuer_resolves():
    """The live registry must leave no issuer unreachable."""
    from src.acquisition.registry import load_issuer_registry

    registry = load_issuer_registry()
    unresolved = []
    for issuer in registry.issuers:
        source = resolve_company_source(issuer.slug)
        if not source.has_xbrl:
            unresolved.append(issuer.slug)
    assert unresolved == []
    assert len(registry.issuers) == 179
