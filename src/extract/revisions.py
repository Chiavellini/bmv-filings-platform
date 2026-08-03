"""Typed financial observations and conservative revision selection.

The legacy extractor returns one :class:`MetricRow` per report period.  That is
enough for a table's current/prior columns, but it cannot describe a later
filing which revises an arbitrary earlier fiscal period (for example, a Q4
release revising Q3).  This module adds that missing intermediate contract while
keeping the legacy dictionary API intact.

An observation's *series identity* includes period kind, accounting basis,
currency, unit and dimensions.  Consequently a consolidated quarterly MXN fact
can never be silently replaced by a YTD, USD, or segment fact.  Conflicting
candidates are retained in :class:`RevisionDecision` instead of being guessed
away.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import Enum
import math
import re
from typing import Iterable, Mapping, Sequence


DEFAULT_TRUSTED_TIERS = frozenset(
    {"xbrl", "bmv", "statement", "note", "verified", "restated"}
)


class PeriodKind(str, Enum):
    QUARTER = "quarter"
    YTD = "ytd"
    FY = "fy"
    INSTANT = "instant"

    @classmethod
    def coerce(cls, value: "PeriodKind | str | None", period: str = "") -> "PeriodKind":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.FY if period.endswith("-FY") else cls.QUARTER
        text = str(value).strip().lower()
        aliases = {"q": cls.QUARTER, "quarterly": cls.QUARTER,
                   "annual": cls.FY, "year": cls.FY, "point": cls.INSTANT}
        if text in aliases:
            return aliases[text]
        return cls(text)


class ObservationRole(str, Enum):
    CURRENT = "current"
    COMPARATIVE = "comparative"
    RESTATED = "restated"

    @classmethod
    def coerce(cls, value: "ObservationRole | str | None") -> "ObservationRole":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.CURRENT
        text = str(value).strip().lower()
        aliases = {"prior": cls.COMPARATIVE, "comparison": cls.COMPARATIVE,
                   "revision": cls.RESTATED}
        if text in aliases:
            return aliases[text]
        return cls(text)


class RevisionMode(str, Enum):
    OFF = "off"
    ALLOW = "allow"
    FORCE = "force"

    @classmethod
    def coerce(cls, value: "RevisionMode | str | bool | None", *, default: str = "off") -> "RevisionMode":
        if isinstance(value, cls):
            return value
        if value is True:
            return cls.ALLOW
        if value is False:
            return cls.OFF
        if value is None:
            value = default
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"revision mode must be off, allow, or force; got {value!r}") from exc


_CANONICAL_Q_RE = re.compile(r"^(?P<year>(?:19|20)\d{2})-(?P<quarter>[1-4])T$", re.I)
_EVAL_Q_RE = re.compile(
    r"^(?P<quarter>[1-4])[QT](?P<year>\d{2}|(?:19|20)\d{2})(?:A)?$", re.I
)
_YEAR_Q_RE = re.compile(
    r"^(?P<year>(?:19|20)\d{2})[-_/ ]?[QT](?P<quarter>[1-4])$", re.I
)


def normalize_period_label(value: str | None) -> str:
    """Return the canonical ``YYYY-NT``/``YYYY-FY`` form when recognizable.

    Both certification labels (``2Q21A``) and production labels (``2021-2T``)
    normalize to the same value.  Unknown labels are preserved, which lets the
    typed reducer support non-quarter fiscal identifiers without inventing a
    calendar mapping.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"(?:19|20)\d{2}-FY", text, re.I):
        return text.upper()
    match = _CANONICAL_Q_RE.fullmatch(text) or _EVAL_Q_RE.fullmatch(text) or _YEAR_Q_RE.fullmatch(text)
    if match:
        year = int(match.group("year"))
        if year < 100:
            year += 1900 if year >= 70 else 2000
        return f"{year:04d}-{int(match.group('quarter'))}T"
    # Shared filename inference understands additional issuer naming variants.
    try:
        from src.shared.report_index import infer_period_label
        inferred = infer_period_label(text)
        if inferred:
            return inferred
    except Exception:
        pass
    return text


def shift_canonical_period(value: str, years: int = 1) -> str | None:
    """Shift a canonicalizable quarter by whole years."""
    period = normalize_period_label(value)
    match = _CANONICAL_Q_RE.fullmatch(period)
    if not match:
        return None
    return f"{int(match.group('year')) + years:04d}-{match.group('quarter')}T"


def _normalize_dimensions(value) -> tuple[tuple[str, str], ...]:
    if not value:
        return ()
    items = value.items() if isinstance(value, Mapping) else value
    return tuple(sorted((str(key).strip(), str(val).strip()) for key, val in items))


def _parse_datetime(value: datetime | date | str | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class SeriesKey:
    company: str
    metric: str
    observed_period: str
    period_kind: PeriodKind
    basis: str
    currency: str
    unit: str
    dimensions: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class FactObservation:
    """One value asserted by one report about one fiscal period."""

    metric: str
    observed_period: str
    value: float
    report_period: str | None = None
    company: str = ""
    period_kind: PeriodKind | str | None = None
    basis: str = ""
    currency: str = ""
    unit: str = ""
    dimensions: tuple[tuple[str, str], ...] | Mapping[str, object] = ()
    source_tier: str = "other"
    source_document_id: str = ""
    document_family_id: str = ""
    issued_at: datetime | date | str | None = None
    report_issue_order: int | float | str | None = None
    document_version: int | float | str | None = None
    role: ObservationRole | str = ObservationRole.CURRENT
    trusted: bool | None = None
    explicit_restatement: bool = False
    evidence: str = ""

    def __post_init__(self) -> None:
        observed = normalize_period_label(self.observed_period)
        report = normalize_period_label(self.report_period or observed)
        object.__setattr__(self, "metric", str(self.metric).strip())
        object.__setattr__(self, "observed_period", observed)
        object.__setattr__(self, "report_period", report)
        object.__setattr__(self, "value", float(self.value))
        object.__setattr__(self, "company", str(self.company or "").strip().lower())
        object.__setattr__(self, "period_kind", PeriodKind.coerce(self.period_kind, observed))
        object.__setattr__(self, "basis", str(self.basis or "").strip().lower())
        object.__setattr__(self, "currency", str(self.currency or "").strip().upper())
        object.__setattr__(self, "unit", str(self.unit or "").strip().lower())
        object.__setattr__(self, "dimensions", _normalize_dimensions(self.dimensions))
        tier = str(self.source_tier or "other").strip().lower().strip("[]")
        object.__setattr__(self, "source_tier", tier or "other")
        object.__setattr__(self, "role", ObservationRole.coerce(self.role))
        object.__setattr__(self, "issued_at", _parse_datetime(self.issued_at))

    @property
    def series_key(self) -> SeriesKey:
        return SeriesKey(
            company=self.company,
            metric=self.metric,
            observed_period=self.observed_period,
            period_kind=self.period_kind,
            basis=self.basis,
            currency=self.currency,
            unit=self.unit,
            dimensions=self.dimensions,
        )


# Short alias for callers which prefer the domain term alone.
Observation = FactObservation


@dataclass(frozen=True)
class RevisionDecision:
    series_key: SeriesKey
    selected: FactObservation
    candidates: tuple[FactObservation, ...]
    superseded: tuple[FactObservation, ...] = ()
    conflicts: tuple[FactObservation, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict:
        def observation_ref(observation: FactObservation) -> dict:
            return {
                "value": observation.value,
                "report_period": observation.report_period,
                "source_tier": observation.source_tier,
                "document_id": observation.source_document_id,
            }

        return {
            "metric": self.series_key.metric,
            "observed_period": self.series_key.observed_period,
            "period_kind": self.series_key.period_kind.value,
            "basis": self.series_key.basis,
            "currency": self.series_key.currency,
            "unit": self.series_key.unit,
            "dimensions": dict(self.series_key.dimensions),
            "selected_value": self.selected.value,
            "selected_report_period": self.selected.report_period,
            "selected_source_tier": self.selected.source_tier,
            "selected_document_id": self.selected.source_document_id,
            "candidate_count": len(self.candidates),
            "conflict_count": len(self.conflicts),
            "superseded": [observation_ref(item) for item in self.superseded],
            "conflicts": [observation_ref(item) for item in self.conflicts],
            "reason": self.reason,
        }


@dataclass
class ExtractionBatch:
    """Dictionary rows plus zero or more arbitrary-period observations."""

    rows: Mapping[str, object] = field(default_factory=dict)
    observations: Sequence[FactObservation] = field(default_factory=tuple)


# Compatibility-friendly name for per-company extractor authors.
ExtractorBatch = ExtractionBatch
ObservationBatch = ExtractionBatch


class ExtractedMetrics(dict):
    """Legacy dict result with an attached typed-observation side channel."""

    def __init__(self, *args, observations: Iterable[FactObservation] = (), **kwargs):
        super().__init__(*args, **kwargs)
        self.observations = list(observations)


def unpack_extraction_result(result) -> tuple[dict, list[FactObservation]]:
    """Coerce a legacy mapping or :class:`ExtractionBatch` without API drift."""
    if isinstance(result, ExtractionBatch):
        return dict(result.rows), list(result.observations)
    if isinstance(result, Mapping):
        return dict(result), list(getattr(result, "observations", ()) or ())
    raise TypeError(
        "extractor must return a metric-row mapping or ExtractionBatch; "
        f"got {type(result).__name__}"
    )


def _period_order(period: str) -> tuple[int, int, str]:
    normalized = normalize_period_label(period)
    quarter = _CANONICAL_Q_RE.fullmatch(normalized)
    if quarter:
        return (int(quarter.group("year")), int(quarter.group("quarter")), normalized)
    annual = re.fullmatch(r"((?:19|20)\d{2})-FY", normalized)
    if annual:
        return (int(annual.group(1)), 5, normalized)
    return (9999, 99, normalized)


def _natural_order(value) -> tuple:
    if value is None or value == "":
        return ()
    if isinstance(value, (int, float)):
        return ((0, float(value)),)
    return tuple((0, int(part)) if part.isdigit() else (1, part.lower())
                 for part in re.split(r"(\d+)", str(value)) if part != "")


def _observation_order(observation: FactObservation, input_index: int) -> tuple:
    issued = observation.issued_at
    return (
        _period_order(observation.report_period or observation.observed_period),
        _natural_order(observation.report_issue_order),
        issued or datetime.min.replace(tzinfo=timezone.utc),
        _natural_order(observation.document_version),
        input_index,
    )


def _same_value(left: float, right: float) -> bool:
    if math.isnan(left) and math.isnan(right):
        return True
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-9)


def _is_trusted(observation: FactObservation, trusted_tiers: set[str]) -> bool:
    if observation.trusted is not None:
        return observation.trusted
    return observation.source_tier in trusted_tiers


def _same_document_family_upgrade(candidate: FactObservation, selected: FactObservation) -> bool:
    if not candidate.document_family_id or candidate.document_family_id != selected.document_family_id:
        return False
    return _natural_order(candidate.document_version) > _natural_order(selected.document_version)


def reduce_observations(
    observations: Iterable[FactObservation],
    *,
    mode: RevisionMode | str = RevisionMode.ALLOW,
    trusted_tiers: Iterable[str] = DEFAULT_TRUSTED_TIERS,
) -> list[RevisionDecision]:
    """Select one observation per exact series, retaining unsafe conflicts.

    ``allow`` accepts a later, trusted comparative/restatement (or a trusted
    higher version of the same filing family).  ``force`` accepts every later
    candidate.  ``off`` freezes the earliest assertion.  Values with a different
    basis/currency/unit/dimension never meet because those fields are part of the
    grouping key.
    """
    selected_mode = RevisionMode.coerce(mode, default="allow")
    trusted = {str(tier).strip().lower().strip("[]") for tier in trusted_tiers}
    groups: dict[SeriesKey, list[tuple[int, FactObservation]]] = {}
    for index, observation in enumerate(observations):
        if not isinstance(observation, FactObservation):
            raise TypeError(f"expected FactObservation, got {type(observation).__name__}")
        groups.setdefault(observation.series_key, []).append((index, observation))

    decisions: list[RevisionDecision] = []
    for key in sorted(groups, key=lambda item: (
            item.company, item.metric, _period_order(item.observed_period),
            item.period_kind.value, item.basis, item.currency, item.unit, item.dimensions)):
        ordered_pairs = sorted(groups[key], key=lambda pair: _observation_order(pair[1], pair[0]))
        candidates = tuple(observation for _, observation in ordered_pairs)
        selected = candidates[0]
        superseded: list[FactObservation] = []
        conflicts: list[FactObservation] = []
        accepted_revision = False

        for candidate in candidates[1:]:
            same = _same_value(candidate.value, selected.value)
            if selected_mode is RevisionMode.OFF:
                if not same:
                    conflicts.append(candidate)
                continue

            allowed = selected_mode is RevisionMode.FORCE
            if selected_mode is RevisionMode.ALLOW:
                revision_signal = (
                    candidate.role in {ObservationRole.COMPARATIVE, ObservationRole.RESTATED}
                    or candidate.explicit_restatement
                    or _same_document_family_upgrade(candidate, selected)
                )
                allowed = _is_trusted(candidate, trusted) and (same or revision_signal)

            if allowed:
                superseded.append(selected)
                accepted_revision = accepted_revision or not same
                selected = candidate
            elif not same:
                conflicts.append(candidate)

        if conflicts:
            reason = "unsafe conflicting observations retained"
        elif accepted_revision:
            reason = "later observation accepted by revision policy"
        elif len(candidates) > 1:
            reason = "later observation confirms selected value"
        else:
            reason = "single observation"
        decisions.append(RevisionDecision(
            series_key=key,
            selected=selected,
            candidates=candidates,
            superseded=tuple(superseded),
            conflicts=tuple(conflicts),
            reason=reason,
        ))
    return decisions


# Descriptive alias used by integration code and external extractors.
reduce_revisions = reduce_observations


def policy_config(cfg: dict | None, name: str, *, default: str) -> tuple[RevisionMode, dict]:
    """Read a revision policy in either concise or expanded YAML form."""
    cfg = cfg or {}
    raw = cfg.get(name)
    if raw is None:
        for container_name in ("revision_policy", "revisions", "restatement_policy"):
            container = cfg.get(container_name) or {}
            if isinstance(container, Mapping) and name in container:
                raw = container[name]
                break
    if raw is None:
        return RevisionMode.coerce(default, default=default), {}
    if isinstance(raw, Mapping):
        details = dict(raw)
        return RevisionMode.coerce(details.get("mode"), default=default), details
    return RevisionMode.coerce(raw, default=default), {}


def _metric_defaults(metric_defs: Sequence, cfg: dict | None) -> dict[str, tuple[str, str]]:
    company_cfg = ((cfg or {}).get("company") or {})
    reporting_unit = str(company_cfg.get("unit") or "").strip().lower()
    defaults: dict[str, tuple[str, str]] = {}
    for metric_def in metric_defs:
        measure_unit = str(getattr(metric_def, "unit", "") or "").lower()
        unit = reporting_unit if measure_unit in {"currency", "miles_mxn"} and reporting_unit else measure_unit
        defaults[getattr(metric_def, "key")] = (measure_unit, unit)
    return defaults


def _fill_observation_defaults(
    observation: FactObservation,
    *,
    company: str,
    basis: str,
    currency: str,
    unit: str,
) -> FactObservation:
    return replace(
        observation,
        company=observation.company or company,
        basis=observation.basis or basis,
        currency=observation.currency or currency,
        unit=observation.unit or unit,
    )


def _source_tier_from_line(source_line: str) -> str:
    match = re.match(r"\[([^\]]+)\]", source_line or "")
    return (match.group(1).lower() if match else "other").replace("calculated", "calc")


def apply_observation_revisions(
    extracted_by_period: dict[str, dict],
    observations: Iterable[FactObservation],
    metric_defs: Sequence,
    cfg: dict | None,
) -> list[dict]:
    """Apply typed observation batches to legacy rows and return audit events.

    Only the default, dimensionless wide-table series (quarterly, or FY for an
    annual period label) is projected. Other kinds/dimensions/bases/units are
    still reduced and appear in the audit events, but cannot overwrite an
    unrelated cell.
    """
    supplied = list(observations)
    if not supplied:
        return []

    mode, details = policy_config(cfg, "observation_revisions", default="allow")
    trusted_tiers = details.get("trusted_tiers") or DEFAULT_TRUSTED_TIERS
    company_cfg = ((cfg or {}).get("company") or {})
    company = str(company_cfg.get("ticker") or company_cfg.get("name") or "").strip().lower()
    currency = str(company_cfg.get("currency") or "").strip().upper()
    basis = str(company_cfg.get("basis") or (cfg or {}).get("accounting_basis") or "reported").lower()
    metric_defaults = _metric_defaults(metric_defs, cfg)
    reporting_unit = str(company_cfg.get("unit") or "").strip().lower()
    # Dynamic segment keys may be emitted at extraction time rather than living
    # in the static registry. Their existing MetricRow supplies the same safe
    # unit default used for registered metrics.
    for rows in extracted_by_period.values():
        for metric, row in rows.items():
            if metric in metric_defaults:
                continue
            measure_unit = str(getattr(row, "unit", "") or "").lower()
            unit = (reporting_unit if measure_unit in {"currency", "miles_mxn"}
                    and reporting_unit else measure_unit)
            metric_defaults[metric] = (measure_unit, unit)

    normalized_supplied: list[FactObservation] = []
    for observation in supplied:
        if not isinstance(observation, FactObservation):
            raise TypeError(f"expected FactObservation, got {type(observation).__name__}")
        _, default_unit = metric_defaults.get(observation.metric, (observation.unit, observation.unit))
        normalized_supplied.append(_fill_observation_defaults(
            observation, company=company, basis=basis, currency=currency, unit=default_unit,
        ))

    # Existing rows are the as-reported baseline against which later assertions
    # are evaluated.  Their synthetic document id lets us distinguish a selected
    # supplied observation from an unchanged legacy cell.
    baseline_ids: set[str] = set()
    baseline_observations: list[FactObservation] = []
    default_keys: dict[tuple[str, str], SeriesKey] = {}
    for raw_period, rows in extracted_by_period.items():
        period = normalize_period_label(raw_period)
        for metric, row in rows.items():
            value = getattr(row, "current", None)
            if value is None:
                continue
            row_measure_unit = str(getattr(row, "unit", "") or "").lower()
            _, default_unit = metric_defaults.get(metric, (row_measure_unit, row_measure_unit))
            source_line = getattr(row, "source_line", "") or ""
            document_id = f"legacy:{period}:{metric}"
            baseline_ids.add(document_id)
            baseline = FactObservation(
                company=company,
                metric=metric,
                observed_period=period,
                value=value,
                report_period=period,
                period_kind=None,  # inferred from the canonical period label
                basis=basis,
                currency=currency,
                unit=default_unit,
                source_tier=_source_tier_from_line(source_line),
                source_document_id=document_id,
                role=ObservationRole.CURRENT,
                evidence=source_line,
            )
            baseline_observations.append(baseline)
            default_keys[(period, metric)] = baseline.series_key

    # Define safe projection keys for observations whose target cell is missing.
    for observation in normalized_supplied:
        cell = (observation.observed_period, observation.metric)
        if cell in default_keys:
            continue
        _, default_unit = metric_defaults.get(
            observation.metric, (observation.unit or "", observation.unit or ""))
        default_keys[cell] = SeriesKey(
            company=company,
            metric=observation.metric,
            observed_period=observation.observed_period,
            period_kind=(PeriodKind.FY if observation.observed_period.endswith("-FY")
                         else PeriodKind.QUARTER),
            basis=basis,
            currency=currency,
            unit=default_unit,
            dimensions=(),
        )

    decisions = reduce_observations(
        [*baseline_observations, *normalized_supplied], mode=mode,
        trusted_tiers=trusted_tiers,
    )
    defs_by_key = {getattr(metric_def, "key"): metric_def for metric_def in metric_defs}
    actual_period_keys = {normalize_period_label(period): period for period in extracted_by_period}
    supplied_object_ids = {id(observation) for observation in normalized_supplied}
    events: list[dict] = []

    for decision in decisions:
        event = decision.as_dict()
        selected = decision.selected
        cell = (decision.series_key.observed_period, decision.series_key.metric)
        projectable = (
            decision.series_key == default_keys.get(cell)
            and selected.source_document_id not in baseline_ids
        )
        event["applied"] = False
        if projectable:
            actual_period = actual_period_keys.get(selected.observed_period, selected.observed_period)
            rows = extracted_by_period.setdefault(actual_period, {})
            old = rows.get(selected.metric)
            old_value = getattr(old, "current", None) if old is not None else None
            if old_value is None or not _same_value(float(old_value), selected.value):
                provenance = (
                    f"[restated] [{selected.source_tier}] observation "
                    f"{selected.report_period} revises "
                    f"{selected.observed_period}; tier={selected.source_tier}"
                )
                if selected.source_document_id:
                    provenance += f"; doc={selected.source_document_id}"
                if selected.evidence:
                    provenance += f" — {selected.evidence}"
                if old is not None:
                    rows[selected.metric] = replace(
                        # The row's prior-year comparative is a separate observed
                        # fiscal period. A revision to ``current`` must not erase
                        # it; apply_restated_priors may legitimately consume it.
                        old, current=selected.value, var_pct=None,
                        source_line=provenance[:160],
                    )
                else:
                    from src.extract.extract_metrics import MetricRow
                    metric_def = defs_by_key.get(selected.metric)
                    rows[selected.metric] = MetricRow(
                        metric=selected.metric,
                        label_es=getattr(metric_def, "label_es", selected.metric),
                        current=selected.value,
                        prior=None,
                        var_pct=None,
                        unit=getattr(metric_def, "unit", selected.unit or "currency"),
                        source_line=provenance[:160],
                    )
                event["applied"] = True
                event["previous_value"] = old_value
        has_supplied_candidate = any(
            id(candidate) in supplied_object_ids for candidate in decision.candidates
        )
        if has_supplied_candidate or decision.conflicts or event["applied"]:
            events.append(event)

    # Recompute calculated dependents for periods changed by a typed revision.
    touched = {event["observed_period"] for event in events if event.get("applied")}
    if touched:
        from src.model.financial_model import compute_derived_metrics
        for canonical_period in touched:
            actual_period = actual_period_keys.get(canonical_period, canonical_period)
            rows = extracted_by_period.get(actual_period, {})
            for metric, derived in compute_derived_metrics(
                    rows, list(metric_defs), include_existing=True).items():
                old = rows.get(metric)
                if old is None or "[calc" in (getattr(old, "source_line", "") or ""):
                    rows[metric] = derived
    return events
