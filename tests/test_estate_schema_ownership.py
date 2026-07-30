"""
test_estate_schema_ownership.py — one table, one owning module.

``data/document_estate/catalog.db`` is written by several modules:

    src/shared/document_estate.py  documents, artifacts, project_records,
                                  memberships, hash_cache, content_objects,
                                  consolidation_*
    src/acquisition/ledger.py      acquisition_*, source_record*,
                                   document_projects, outbox
    src/consumers/outbox.py        outbox_subscriptions, outbox_deliveries,
                                   outbox_delivery_attempts, and its own
                                   migration bookkeeping
    src/consumers/derivatives.py   document_derivations

That is workable *only* while each table has exactly one declaring module.  It
did not hold: ``outbox`` was CREATE TABLE'd in both ``acquisition/ledger.py``
and ``consumers/derivatives.py``, so the two definitions could drift apart while
both wrote to the same database.  ``src/deployment/preflight.py`` documents
routing around this collision rather than resolving it.

Repeated declarations *within* one module are fine — that is the standard SQLite
rename-and-rebuild migration (see ``consumers/outbox.py``, which rebuilds
``outbox_delivery_attempts`` to add a column).  What must never happen is two
different modules declaring the same table.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).parent.parent
SRC = ROOT / "src"

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(\w+)",
    re.IGNORECASE,
)


def _declarations() -> dict[str, set[str]]:
    """Map table name -> set of modules that CREATE TABLE it."""
    owners: dict[str, set[str]] = defaultdict(set)
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for table in _CREATE_TABLE.findall(source):
            owners[table].add(str(path.relative_to(ROOT)))
    return owners


def test_no_table_is_declared_by_two_modules():
    conflicts = {
        table: sorted(modules)
        for table, modules in _declarations().items()
        if len(modules) > 1
    }
    assert not conflicts, (
        "tables declared by more than one module — the definitions can drift while "
        "both write the same database. Give each table a single owning module and "
        "have the others execute the owner's DDL:\n"
        + "\n".join(f"  {table}: {', '.join(mods)}" for table, mods in conflicts.items())
    )


def test_outbox_is_owned_by_the_acquisition_ledger():
    """Regression pin for the specific collision this suite was written for."""
    owners = _declarations().get("outbox", set())
    assert owners == {"src/acquisition/ledger.py"}, (
        f"outbox must be declared only by the acquisition ledger; found: {sorted(owners)}"
    )


def test_consumers_reuse_the_canonical_outbox_ddl():
    """The derivative consumer must still ensure the table exists standalone —
    by executing the owner's DDL, not by re-declaring it."""
    from src.acquisition.ledger import OUTBOX_DDL

    assert "CREATE TABLE IF NOT EXISTS outbox" in OUTBOX_DDL
    derivatives = (SRC / "consumers" / "derivatives.py").read_text(encoding="utf-8")
    assert "OUTBOX_DDL" in derivatives, (
        "consumers/derivatives.py no longer references the canonical outbox DDL"
    )


@pytest.mark.parametrize(
    "table, owner",
    [
        ("documents", "src/shared/document_estate.py"),
        ("artifacts", "src/shared/document_estate.py"),
        ("content_objects", "src/shared/document_estate.py"),
        ("source_records", "src/acquisition/ledger.py"),
        ("document_projects", "src/acquisition/ledger.py"),
        ("document_derivations", "src/consumers/derivatives.py"),
        ("outbox_deliveries", "src/consumers/outbox.py"),
    ],
)
def test_documented_table_ownership_holds(table: str, owner: str):
    """Pins the ownership map published in docs/DOCUMENT_ESTATE.md."""
    owners = _declarations().get(table, set())
    assert owner in owners, f"{table} is no longer declared by {owner} (found {sorted(owners)})"
