"""Focused contract tests for the shared company-alias configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.shared.company_aliases import (
    CompanyAliasConfigError,
    expand_company_aliases,
    load_company_aliases,
)


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "company_aliases.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_default_config_loads_and_expands_canonical_slug() -> None:
    aliases = load_company_aliases()

    assert aliases["gfnorte"] == ("banorte",)
    assert expand_company_aliases("gfnorte", aliases) == ("gfnorte", "banorte")


def test_alias_input_resolves_to_the_same_canonical_group() -> None:
    aliases = load_company_aliases()

    assert expand_company_aliases("banorte", aliases) == ("gfnorte", "banorte")
    assert expand_company_aliases("unconfigured_issuer", aliases) == (
        "unconfigured_issuer",
    )


def test_loader_sorts_canonical_groups_and_aliases(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
canonical:
  zeta: [zulu, alpha]
  beta: [bravo]
""",
    )

    aliases = load_company_aliases(path)

    assert list(aliases) == ["beta", "zeta"]
    assert aliases["zeta"] == ("alpha", "zulu")
    assert expand_company_aliases("zeta", aliases) == ("zeta", "alpha", "zulu")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("- not-a-mapping\n", "root must be a mapping"),
        ("canonical: []\n", "'canonical' field must be a mapping"),
        ("canonical:\n  gfnorte: banorte\n", "must be a list"),
        ("canonical:\n  GFNORTE: [banorte]\n", "must be normalized lowercase"),
        (
            "canonical:\n  gfnorte: [banorte, banorte]\n",
            "duplicate alias 'banorte'",
        ),
        (
            "canonical:\n  gfnorte: [shared]\n  other: [shared]\n",
            "belongs to both",
        ),
        (
            "canonical:\n  gfnorte: [other]\n  other: []\n",
            "is also a canonical slug",
        ),
    ],
)
def test_loader_rejects_malformed_config(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    path = _write(tmp_path, content)

    with pytest.raises(CompanyAliasConfigError, match=message):
        load_company_aliases(path)


def test_loader_wraps_missing_file_with_config_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"

    with pytest.raises(CompanyAliasConfigError, match="cannot read company aliases"):
        load_company_aliases(missing)
