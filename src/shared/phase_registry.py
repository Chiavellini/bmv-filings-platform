"""Load and validate the pipeline phase source of truth.

The manifest in ``docs/architecture/pipeline_phases.yaml`` is the canonical map
of product phases, stable entrypoints, artifacts, and quality gates. This module
keeps tests and future tools from hand-parsing that YAML.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.shared.paths import PROJECT_ROOT


DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "docs" / "architecture" / "pipeline_phases.yaml"
REQUIRED_PRODUCT_PHASES = ("download", "parse", "extract", "generate_excel", "cli")


class PhaseRegistryError(ValueError):
    """Raised when the phase manifest is malformed."""


@dataclass(frozen=True)
class EntryPoint:
    kind: str
    symbol: str | None = None
    command: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, owner_id: str) -> "EntryPoint":
        if not isinstance(raw, dict):
            raise PhaseRegistryError(f"{owner_id}: entrypoint must be a mapping")
        kind = raw.get("kind")
        if kind not in {"python", "command"}:
            raise PhaseRegistryError(f"{owner_id}: unsupported entrypoint kind {kind!r}")
        symbol = raw.get("symbol")
        command = raw.get("command")
        if kind == "python" and not isinstance(symbol, str):
            raise PhaseRegistryError(f"{owner_id}: python entrypoint requires symbol")
        if kind == "command" and not isinstance(command, str):
            raise PhaseRegistryError(f"{owner_id}: command entrypoint requires command")
        return cls(kind=kind, symbol=symbol, command=command)


@dataclass(frozen=True)
class ContractItem:
    name: str
    contract: str
    artifacts: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, owner_id: str) -> "ContractItem":
        if not isinstance(raw, dict):
            raise PhaseRegistryError(f"{owner_id}: contract item must be a mapping")
        name = raw.get("name")
        contract = raw.get("contract")
        if not isinstance(name, str) or not name:
            raise PhaseRegistryError(f"{owner_id}: contract item requires name")
        if not isinstance(contract, str) or not contract:
            raise PhaseRegistryError(f"{owner_id}: contract item requires contract")
        return cls(
            name=name,
            contract=contract,
            artifacts=tuple(_string_list(raw.get("artifacts", []), owner_id, "artifacts")),
        )


@dataclass(frozen=True)
class Phase:
    id: str
    label: str
    package: str
    purpose: str
    entrypoints: tuple[EntryPoint, ...]
    tests: tuple[str, ...]
    commands: tuple[str, ...] = ()
    ordinal: int | None = None
    inputs: tuple[ContractItem, ...] = ()
    outputs: tuple[ContractItem, ...] = ()
    upstream: tuple[str, ...] = ()
    downstream: tuple[str, ...] = ()
    quality_gates: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, require_ordinal: bool) -> "Phase":
        if not isinstance(raw, dict):
            raise PhaseRegistryError("phase must be a mapping")
        phase_id = raw.get("id")
        if not isinstance(phase_id, str) or not phase_id:
            raise PhaseRegistryError("phase requires id")
        ordinal = raw.get("ordinal")
        if require_ordinal and not isinstance(ordinal, int):
            raise PhaseRegistryError(f"{phase_id}: product phase requires integer ordinal")
        return cls(
            id=phase_id,
            ordinal=ordinal if isinstance(ordinal, int) else None,
            label=_required_str(raw, "label", phase_id),
            package=_required_str(raw, "package", phase_id),
            purpose=_required_str(raw, "purpose", phase_id),
            entrypoints=tuple(
                EntryPoint.from_raw(item, owner_id=phase_id)
                for item in raw.get("entrypoints", [])
            ),
            inputs=tuple(
                ContractItem.from_raw(item, owner_id=phase_id)
                for item in raw.get("inputs", [])
            ),
            outputs=tuple(
                ContractItem.from_raw(item, owner_id=phase_id)
                for item in raw.get("outputs", [])
            ),
            upstream=tuple(_string_list(raw.get("upstream", []), phase_id, "upstream")),
            downstream=tuple(_string_list(raw.get("downstream", []), phase_id, "downstream")),
            commands=tuple(_string_list(raw.get("commands", []), phase_id, "commands")),
            tests=tuple(_string_list(raw.get("tests", []), phase_id, "tests")),
            quality_gates=tuple(
                _string_list(raw.get("quality_gates", []), phase_id, "quality_gates")
            ),
        )


@dataclass(frozen=True)
class PhaseRegistry:
    version: int
    name: str
    summary: str
    product_phases: tuple[Phase, ...]
    quality_gates: tuple[Phase, ...]
    agent_rules: tuple[str, ...]

    @property
    def phases_by_id(self) -> dict[str, Phase]:
        return {phase.id: phase for phase in self.product_phases}

    def phase(self, phase_id: str) -> Phase:
        try:
            return self.phases_by_id[phase_id]
        except KeyError as exc:
            raise PhaseRegistryError(f"unknown product phase: {phase_id}") from exc


def load_phase_registry(path: str | Path = DEFAULT_MANIFEST_PATH) -> PhaseRegistry:
    """Load and validate the phase manifest."""
    manifest_path = Path(path)
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PhaseRegistryError(f"{manifest_path}: manifest must be a mapping")

    registry = PhaseRegistry(
        version=_required_int(data, "version", "manifest"),
        name=_required_str(data, "name", "manifest"),
        summary=_required_str(data, "summary", "manifest"),
        product_phases=tuple(
            Phase.from_raw(item, require_ordinal=True)
            for item in data.get("product_phases", [])
        ),
        quality_gates=tuple(
            Phase.from_raw(item, require_ordinal=False)
            for item in data.get("quality_gates", [])
        ),
        agent_rules=tuple(_string_list(data.get("agent_rules", []), "manifest", "agent_rules")),
    )
    validate_phase_registry(registry)
    return registry


def validate_phase_registry(registry: PhaseRegistry) -> None:
    """Validate ids, graph links, and minimum contract metadata."""
    phase_ids = [phase.id for phase in registry.product_phases]
    if len(set(phase_ids)) != len(phase_ids):
        raise PhaseRegistryError("duplicate product phase id")
    if tuple(phase_ids) != REQUIRED_PRODUCT_PHASES:
        raise PhaseRegistryError(
            f"product phase ids must be ordered as {REQUIRED_PRODUCT_PHASES!r}"
        )

    ordinals = [phase.ordinal for phase in registry.product_phases]
    if ordinals != list(range(1, len(REQUIRED_PRODUCT_PHASES) + 1)):
        raise PhaseRegistryError("product phase ordinals must be 1..5")

    known = set(phase_ids)
    for phase in registry.product_phases:
        if not phase.entrypoints:
            raise PhaseRegistryError(f"{phase.id}: at least one entrypoint required")
        if not phase.tests:
            raise PhaseRegistryError(f"{phase.id}: at least one test reference required")
        for linked in (*phase.upstream, *phase.downstream):
            if linked not in known:
                raise PhaseRegistryError(f"{phase.id}: unknown linked phase {linked!r}")

    _assert_acyclic(registry.product_phases)


def _assert_acyclic(phases: tuple[Phase, ...]) -> None:
    graph = {phase.id: set(phase.downstream) for phase in phases}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise PhaseRegistryError(f"cycle detected at phase {node!r}")
        visiting.add(node)
        for child in graph[node]:
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for phase in phases:
        visit(phase.id)


def _required_str(raw: dict[str, Any], key: str, owner_id: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PhaseRegistryError(f"{owner_id}: required string field {key!r}")
    return value


def _required_int(raw: dict[str, Any], key: str, owner_id: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int):
        raise PhaseRegistryError(f"{owner_id}: required integer field {key!r}")
    return value


def _string_list(raw: Any, owner_id: str, field: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise PhaseRegistryError(f"{owner_id}: {field} must be a list of strings")
    return raw
