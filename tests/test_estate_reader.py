"""EstateReader — read-side estate API used by subprojects (earnings/ first)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.shared.document_estate import (  # noqa: E402
    DocumentEstate,
    EstateDocument,
    EstateReader,
)
from src.shared.paths import DOCUMENT_ESTATE_DB  # noqa: E402


@pytest.fixture()
def synthetic_catalog(tmp_path: Path) -> Path:
    db = tmp_path / "catalog.db"
    soft_facts = tmp_path / "soft" / "WALMEX_2023-1T_facts.json"
    root_facts = tmp_path / "root" / "WALMEX_2023-1T_facts.json"
    for f in (soft_facts, root_facts):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}")
    with DocumentEstate(db) as estate:
        estate.upsert_document(
            EstateDocument(
                document_id="doc-1",
                company="walmex",
                period="2023-1T",
                doc_type="regulatory_filing",
                title="WALMEX 2023-1T XBRL",
            ),
            memberships=[{"company": "walmex", "industry": None},
                         {"company": "wal-mart de mexico", "industry": None}],
        )
        estate.add_artifact("doc-1", soft_facts, project="soft", role="derived")
        estate.add_artifact("doc-1", root_facts, project="root", role="derived")
        # canonical slug present in the catalog so the YAML alias fallback
        # (banorte -> gfnorte in configs/company_aliases.yaml) can resolve
        estate.upsert_document(
            EstateDocument(
                document_id="doc-2",
                company="gfnorte",
                period="2023-1T",
                doc_type="quarterly_release",
                title="GFNorte 2023-1T",
            )
        )
        estate.commit()
    return db


def test_reader_is_physically_read_only(synthetic_catalog: Path) -> None:
    reader = EstateReader(synthetic_catalog)
    with pytest.raises(sqlite3.OperationalError):
        reader.conn.execute("DELETE FROM artifacts")
    reader.close()


def test_project_filter_separates_soft_and_root_facts(synthetic_catalog: Path) -> None:
    with EstateReader(synthetic_catalog) as reader:
        soft_map = reader.xbrl_facts_map("walmex", project="soft")
        root_map = reader.xbrl_facts_map("walmex", project="root")
    assert set(soft_map) == {"2023-1T"} and set(root_map) == {"2023-1T"}
    assert "/soft/" in str(soft_map["2023-1T"])
    assert "/root/" in str(root_map["2023-1T"])


def test_membership_alias_resolves(synthetic_catalog: Path) -> None:
    with EstateReader(synthetic_catalog) as reader:
        assert reader.resolve_company("wal-mart de mexico") == "walmex"
        assert reader.resolve_company("walmex") == "walmex"


def test_unknown_company_returns_empty_not_raises(synthetic_catalog: Path) -> None:
    with EstateReader(synthetic_catalog) as reader:
        assert reader.artifacts("no_such_company") == []
        assert reader.xbrl_facts_map("no_such_company") == {}


def test_missing_catalog_raises() -> None:
    with pytest.raises(FileNotFoundError):
        EstateReader(Path("/nonexistent/catalog.db"))


@pytest.mark.skipif(not DOCUMENT_ESTATE_DB.exists(), reason="live estate absent")
def test_live_catalog_walmex_soft_facts() -> None:
    with EstateReader() as reader:
        facts = reader.xbrl_facts_map("walmex", project="soft")
        assert len(facts) >= 1
        sample = next(iter(facts.values()))
        assert sample.name.endswith("_facts.json")
        assert not sample.name.endswith("_facts__root.json")
        assert reader.view_dir("walmex") is not None


def test_yaml_alias_fallback(synthetic_catalog: Path) -> None:
    with EstateReader(synthetic_catalog) as reader:
        # 'banorte' is not a company in the synthetic catalog, so resolution
        # falls through configs/company_aliases.yaml to canonical 'gfnorte'.
        assert reader.resolve_company("banorte") == "gfnorte"
