#!/usr/bin/env python3
"""
build_segments.py — one command, one markdown input, one Segments workbook.

Replaces the old Streamlit dashboard. You write a single, regularized markdown
file describing the company, its Investor-Relations page, and the metrics you
want, then run::

    python3 scripts/build_segments.py inputs/company.md

Clean input/output scheme:
  * INPUT  — the markdown spec lives in ``inputs/<company>.md``.
  * OPEN   — the newest analyst-ready workbook is always the only file in
             ``outputs/latest/``::

        outputs/latest/
            <Company>.xlsx       # open this

  * REVIEW — supporting build artifacts stay in ``outputs/<Company>/``::

        outputs/<Company>/
            csv/                # <slug>_metrics.csv — extracted metric values
            excel/              # retained company-specific workbook copy
            validation/         # <slug>_validation.md — confidence + identity +
                                #   ground-truth report

Source documents remain in the cataloged document estate. Extraction reads the
effective zero-copy estate views; ``data/reports/<slug>/`` is transitional and
cannot prove publication freshness on its own.

─────────────────────────────────────────────────────────────────────────────
Markdown input format — the metrics list is a lightweight outline
─────────────────────────────────────────────────────────────────────────────
    # Walmex
    IR: https://www.walmex.mx/en/financial-information/quarterly.html

    ## Total Income
    - Total Income {revenue}
    - YoY

    ## Profitability
    - Gross Income {gross_profit}
    - Margin
    - bps change
    - EBITDA {ebitda}
    - Margin
    - Net Income {net_income}
    - Margin

The fields are distinguished by structure:
  * Company name = first level-1 heading (``# ...``).
  * IR link      = first ``http(s)://`` URL found anywhere in the file.
  * ``## Heading`` = a section header in the workbook.
  * ``- Label``    = drafting shorthand only. Strict onboarding requires every
                     reported row to carry an explicit canonical key pin.
  * ``- Label {metric_key}`` = a data row with a PINNED key — authoritative, this
                     is how you fix any mis-mapping (e.g. ``Total Income {revenue}``).
  * ``- YoY`` / ``- Margin`` / ``- As % of Total`` / ``- bps change`` / ``- Check``
    / ``- 2-year comp`` = derived rows, rendered as live Excel formulas.

The original analyst CSV/XLSX is an input contract: label order, row role, and
any analyst-declared canonical keys must match the Markdown. The whole list runs
through the existing outline machinery (``parse_outline`` → ``pipeline.run`` →
``build_outline_workbook``). Missing inputs remain visible for review and block
publication; they are never silently pruned from the analyst request.

Extraction is generic/config-driven by default. A company-specific extractor is
reserved for stable structural exceptions. Shipping requires a fresh strict
canonical acquisition receipt bound to the exact selected catalog artifacts;
legacy direct download and stale-cache flags can only produce review candidates.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

# Make the project root importable when run as ``python3 scripts/build_segments.py``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.paths import (  # noqa: E402
    PROJECT_ROOT, CONFIGS_DIR, REPORTS_DIR, SHARED_REPORTS_DIR,
    SHARED_PARSED_REPORTS_DIR, OUTPUTS_DIR, LATEST_OUTPUT_DIR,
    LATEST_OUTPUT_RECEIPT, DELIVERABLE_ARCHIVE_DIR, DOCUMENT_ESTATE_DB,
    DOCUMENT_ESTATE_DIR,
)
from src.shared.report_index import (  # noqa: E402
    available_report_periods,
    controlled_report_directories,
    index_report_files,
    infer_period_label,
    period_sort_key,
)

_URL_RE = re.compile(r"https?://\S+")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")
_ANALYST_SHEET_RE = re.compile(
    r"^\s*(?:Analyst[- ]Metrics|Metrics[- ]Sheet)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_ANALYST_COMPANY_RE = re.compile(
    r"^\s*(?:Analyst[- ]Company|Metrics[- ]Company)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------

class InputError(Exception):
    """The markdown input was missing a required field."""


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Machine-readable disposition of an onboarding build."""

    candidate_path: Path
    latest_path: Path | None
    manifest_path: Path
    publish_blockers: tuple[str, ...]

    @property
    def published(self) -> bool:
        return self.latest_path is not None


def _display_path(path: Path) -> str:
    """Readable project-relative path, while supporting configured external estate roots."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def parse_input(md_text: str) -> tuple[str, str, list[str]]:
    """Pull ``(company_name, ir_url, metrics)`` out of the regularized markdown.

    Raises ``InputError`` if the company name, IR link, or metric list is absent.
    """
    name: str | None = None
    ir_url: str | None = None
    metrics: list[str] = []

    for raw in md_text.splitlines():
        line = raw.rstrip()
        if name is None:
            m = _H1_RE.match(line)
            if m:
                name = m.group(1).strip()
                continue
        if ir_url is None:
            m = _URL_RE.search(line)
            if m:
                ir_url = m.group(0).rstrip(").,;>")
                # A URL that is itself a list item is the link, not a metric.
                continue
        m = _LIST_RE.match(line)
        if m:
            item = m.group(1).strip()
            if _URL_RE.search(item):
                continue
            if item:
                metrics.append(item)

    if not name:
        raise InputError("no company name found — add a '# Company Name' heading.")
    if not ir_url:
        raise InputError("no IR link found — add a line with an http(s):// URL.")
    if not metrics:
        raise InputError("no metrics found — add a markdown list of metric names.")
    return name, ir_url, metrics


# ── Outline compiler ─────────────────────────────────────────────────────────
# The metrics list is a lightweight outline that maps onto segments_sheet.parse_outline:
#   `## Heading`          → section
#   `- Label`             → data row (key auto-resolved if unambiguous)
#   `- Label {metric_key}`→ data row with a pinned key (authoritative)
#   `- YoY` / `- Margin` / `- As % of Total` / `- bps change` / `- Check` / `- 2-year comp`
#                         → derived rows (rendered as Excel formulas / placeholders)

_HEADING_RE = re.compile(r"^#{2,}\s+(.+?)\s*$")
_PIN_RE = re.compile(r"\{([A-Za-z0-9_]+)\}\s*$")
_AUTO_CONFIDENCE = 0.9  # only auto-map exact/alias hits; ambiguous labels stay blank


def _split_pin(item: str) -> tuple[str, str | None]:
    """Split a trailing ``{metric_key}`` pin off a bullet: 'X {k}' → ('X', 'k')."""
    m = _PIN_RE.search(item)
    if m:
        return item[: m.start()].strip(), m.group(1)
    return item.strip(), None


def _derived_token(label: str) -> str | None:
    """Return the canonical derived-row token parse_outline recognizes, or None.

    Only clean, standalone derived phrasings convert; anything with extra words
    (e.g. 'SSS YoY') stays a data row so it isn't mistaken for a derived formula.
    """
    norm = re.sub(r"\s+", " ", label.strip().lower())
    if norm in ("yoy", "yoy %", "yoy%", "% yoy", "yoy growth", "yoy change"):
        return "YoY"
    if norm in ("2-year comp", "2 year comp", "2yr comp", "2-yr comp", "two-year comp"):
        return "2-year comp"
    if norm == "bps change" or norm in ("bps", "bps delta", "bps δ", "bps chg") \
            or norm.startswith("bps "):
        return "bps change"
    if norm == "check" or norm.startswith("check:") or norm.startswith("check "):
        return "Check"
    if norm.startswith(("as % of total", "% of total", "as percent of total", "percent of total")):
        return "As % of Total"
    if norm.startswith(("as % of", "% of")):
        return "As % of"
    if norm in ("margin", "+ margin", "margin %"):
        return "Margin"
    if norm.endswith(" margin"):
        return label.strip()          # e.g. 'EBITDA Margin' — parse_outline handles it
    if norm == "fx effect":
        return "fx effect"
    # Productivity / ratio calculations rendered as live cross-row formulas.
    if norm in ("effective tax rate", "avg store size", "average store size",
                "sales per m²", "sales per m2", "sales per store",
                "capex per store", "capex per m²", "capex per m2"):
        return label.strip()
    if norm.startswith("yoy "):     # "YoY Sales per m²" etc. (bare "yoy" handled above)
        return label.strip()
    return None


def compile_outline(
    md_text: str,
    resolver,
    valid_keys: set[str],
) -> tuple[str, list[str], dict[object, str], list[tuple]]:
    """Turn the markdown body into (outline_text, sections, mapping, report).

    Skips the H1 title and the IR-link line. ``report`` rows are
    ``(label, kind, detail)`` where kind ∈ {section, derived, pinned, auto, unmapped, bad-pin}.
    """
    out_lines: list[str] = []
    sections: list[str] = []
    mapping: dict[object, str] = {}
    report: list[tuple] = []
    occurrences: dict[tuple[str, str], int] = {}

    from src.excel.segments_sheet import _outline_position_key

    name_seen = False
    url_seen = False
    cur_section = ""
    for raw in md_text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not name_seen and _H1_RE.match(line):
            name_seen = True
            continue
        if not url_seen and _URL_RE.search(line) and not _LIST_RE.match(line):
            url_seen = True
            continue
        if not stripped:
            out_lines.append("")
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            sec = m.group(1).strip()
            sections.append(sec)
            cur_section = sec
            out_lines.append("")
            out_lines.append(sec)
            report.append((sec, "section", ""))
            continue
        m = _LIST_RE.match(line)
        if not m:
            continue  # prose / unrecognized line — ignored
        label, pin = _split_pin(m.group(1).strip())
        if not label:
            continue
        # An explicit analyst pin is authoritative even when its display label
        # resembles a derived row (for example ``Margin {gross_margin}``).
        if pin is None:
            dt = _derived_token(label)
            if dt is not None:
                # Classification is normalized, but the workbook display label
                # remains exactly what the analyst requested.
                out_lines.append(label)
                report.append((label, "derived", dt))
                continue
        # Section-qualify the mapping key so a section heading that shares a label
        # with a data row (e.g. "Total Income") is not swallowed as a data row.
        mkey = f"{cur_section}/{label}" if cur_section else label
        identity = (cur_section, label)
        occurrence = occurrences.get(identity, 0) + 1
        occurrences[identity] = occurrence
        if pin:
            if pin in valid_keys:
                # Keep the first legacy string binding for compatibility, while
                # positional bindings preserve distinct duplicate-row keys.
                mapping.setdefault(mkey, pin)
                mapping[_outline_position_key(cur_section, label, occurrence)] = pin
                report.append((label, "pinned", pin))
            else:
                report.append((label, "bad-pin", pin))   # unknown key → blank row
        else:
            r = resolver.resolve(label)
            if r and r.keys and r.confidence >= _AUTO_CONFIDENCE and r.keys[0] in valid_keys:
                mapping.setdefault(mkey, r.keys[0])
                mapping[_outline_position_key(cur_section, label, occurrence)] = r.keys[0]
                report.append((label, "auto", f"{r.keys[0]} ({r.confidence:.2f})"))
            else:
                guess = r.keys[0] if (r and r.keys) else None
                report.append((label, "unmapped", guess))   # blank row; pin to fill
        out_lines.append(label)

    return "\n".join(out_lines), sections, mapping, report


def metric_contract_issues(report: list[tuple], *, require_pins: bool = True) -> list[str]:
    """Return analyst-row mapping defects that make onboarding unsafe.

    A pinned key is an explicit analyst-to-model decision.  Exact resolver hits
    remain useful while drafting, but strict onboarding rejects them so a future
    alias change cannot silently retarget a shipped row.
    """
    issues: list[str] = []
    for label, kind, detail in report:
        if kind == "bad-pin":
            issues.append(f"{label!r} pins unknown metric key {detail!r}")
        elif kind == "unmapped":
            issues.append(f"{label!r} has no pinned metric key")
        elif require_pins and kind == "auto":
            issues.append(
                f"{label!r} was auto-mapped to {str(detail).split(' ', 1)[0]!r}; "
                "pin it explicitly with {metric_key}"
            )
    return issues


def requested_label_sequence(report: list[tuple]) -> tuple[str, ...]:
    """Ordered visible section/row labels from the analyst Markdown."""
    return tuple(label for label, _kind, _detail in report if label.strip())


def requested_key_sequence(report: list[tuple]) -> tuple[str | None, ...]:
    """Canonical key aligned with each visible row in ``report``."""
    keys: list[str | None] = []
    for label, kind, detail in report:
        if not label.strip() or kind in {"section", "derived", "unmapped"}:
            keys.append(None)
        elif kind == "auto":
            keys.append(str(detail).split(" ", 1)[0] if detail else None)
        else:
            keys.append(str(detail).strip() if detail else None)
    return tuple(keys)


def requested_kind_sequence(report: list[tuple]) -> tuple[str, ...]:
    """Analyst-visible role aligned with each row: section or requested row."""
    return tuple(
        "section" if kind == "section" else "row"
        for label, kind, _detail in report
        if label.strip()
    )


def _analyst_sheet_metadata(md_text: str) -> tuple[str | None, str | None, str | None]:
    """Return ``(path, company marker, worksheet)`` declared in Markdown."""
    raw_path = company = sheet = None
    for line in md_text.splitlines():
        match = _ANALYST_SHEET_RE.match(line)
        if match:
            raw_path = match.group(1).strip()
            if "#" in raw_path:
                raw_path, sheet = raw_path.rsplit("#", 1)
                raw_path, sheet = raw_path.strip(), sheet.strip() or None
            continue
        match = _ANALYST_COMPANY_RE.match(line)
        if match:
            company = match.group(1).strip()
    return raw_path, company, sheet


def _resolve_analyst_sheet_path(raw: str | Path, md_path: Path) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    beside_input = (md_path.parent / path).resolve()
    if beside_input.exists():
        return beside_input
    return (PROJECT_ROOT / path).resolve()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Durably replace one small metadata file without exposing partial JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _atomic_copy_file(source: Path, target: Path) -> None:
    _atomic_write_bytes(Path(target), Path(source).read_bytes())


def _write_metrics_manifest(
    path: Path,
    *,
    company: str,
    slug: str,
    md_path: Path,
    input_sha256: str | None = None,
    report: list[tuple],
    mapping_issues: list[str],
    analyst_spec=None,
    fidelity=None,
    key_fidelity=None,
    kind_fidelity=None,
    input_lineage=None,
    rendered_labels: list[str] | None = None,
    excel_issues: list[dict] | None = None,
    freshness=None,
    gate_strong: bool | None = None,
    publish_blockers: list[str] | None = None,
    build_id: str | None = None,
    generated_at: str | None = None,
    workbook_path: Path | None = None,
    workbook_sha256: str | None = None,
    published: bool = False,
) -> None:
    """Persist the exact analyst-row contract and its build disposition."""
    if input_sha256 is None:
        input_sha256 = hashlib.sha256(md_path.read_bytes()).hexdigest()
    payload = {
        "version": 3,
        "build_id": build_id,
        "generated_at": generated_at,
        "company": company,
        "slug": slug,
        "input": {
            "path": _display_path(md_path.resolve()),
            "sha256": input_sha256,
        },
        "requested_rows": [
            {"order": index, "label": label, "kind": kind, "detail": detail}
            for index, (label, kind, detail) in enumerate(report, 1)
        ],
        "mapping_issues": mapping_issues,
        "analyst_sheet": (
            {
                "path": _display_path(analyst_spec.path),
                "sheet": analyst_spec.sheet_name,
                "company": analyst_spec.company,
                "sha256": analyst_spec.sha256,
                "labels": list(analyst_spec.labels),
                "keys": list(analyst_spec.keys),
                "kinds": list(analyst_spec.kinds),
            }
            if analyst_spec is not None
            else None
        ),
        "analyst_sheet_matches_outline": fidelity.matches if fidelity is not None else None,
        "analyst_sheet_first_difference": (
            fidelity.first_difference if fidelity is not None else None
        ),
        "analyst_sheet_matches_keys": (
            key_fidelity.matches if key_fidelity is not None else None
        ),
        "analyst_sheet_first_key_difference": (
            key_fidelity.first_difference if key_fidelity is not None else None
        ),
        "analyst_sheet_matches_row_kinds": (
            kind_fidelity.matches if kind_fidelity is not None else None
        ),
        "analyst_sheet_first_row_kind_difference": (
            kind_fidelity.first_difference if kind_fidelity is not None else None
        ),
        "rendered_labels": rendered_labels,
        "extraction_inputs": list(input_lineage or ()),
        "excel_issues": excel_issues or [],
        "estate_freshness": (
            {
                "fresh": freshness.fresh,
                "reason": freshness.reason,
                "run_id": freshness.run_id,
                "completed_at": freshness.completed_at,
                "age_seconds": freshness.age_seconds,
                "max_age_seconds": freshness.max_age_seconds,
                "detail": freshness.detail,
                "input_document_ids": list(
                    getattr(freshness, "input_document_ids", ()) or ()
                ),
                "input_artifact_ids": list(
                    getattr(freshness, "input_artifact_ids", ()) or ()
                ),
                "run_document_ids": list(
                    getattr(freshness, "run_document_ids", ()) or ()
                ),
                "catalog_document_ids": list(
                    getattr(freshness, "catalog_document_ids", ()) or ()
                ),
                "input_watermark": getattr(freshness, "input_watermark", None),
            }
            if freshness is not None
            else None
        ),
        "gate_strong": gate_strong,
        "workbook": (
            {
                "path": _display_path(Path(workbook_path).resolve()),
                "sha256": workbook_sha256,
            }
            if workbook_path is not None
            else None
        ),
        "publish_blockers": publish_blockers or [],
        "published": published,
    }
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_bytes(path, encoded)


def _print_resolution_report(report: list[tuple]) -> None:
    """Show what each metric label mapped to, so mis-maps and blanks are visible."""
    data = [r for r in report if r[1] != "section"]
    filled = [r for r in data if r[1] in ("pinned", "auto")]
    print(f"\n  Metric mapping — {len(filled)}/{len(data)} data row(s) mapped to a key")
    print("  " + "─" * 64)
    for label, kind, detail in data:
        if kind == "pinned":
            mark, note = "✓", f"{detail}  (pinned)"
        elif kind == "auto":
            mark, note = "✓", f"{detail}"
        elif kind == "derived":
            mark, note = "·", f"derived → {detail}"
        elif kind == "bad-pin":
            mark, note = "✗", f"unknown key '{detail}' → blank row"
        else:
            hint = f" (fuzzy guess: {detail}; pin with {{key}} to fill)" if detail else ""
            mark, note = "○", f"unmapped → blank row{hint}"
        print(f"   {mark} {label:<42} {note}")
    print()


def slugify(name: str) -> str:
    """Lowercase config slug: 'Grupo Bimbo' → 'grupo_bimbo'."""
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "company"


def dirname_for(name: str) -> str:
    """Filesystem-safe, human-readable directory name from the company name."""
    s = re.sub(r"[^\w.-]+", "_", name.strip()).strip("_")
    return s or "Company"


def _next_archive_target(archive_dir: Path, previous_dir: Path) -> Path:
    """Return a collision-safe directory for an exact prior handoff."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    workbooks = sorted(previous_dir.glob("*.xlsx"))
    label = dirname_for(workbooks[0].stem if workbooks else "previous")
    base = f"{stamp}_{label}"
    candidate = archive_dir / base
    suffix = 2
    while candidate.exists():
        candidate = archive_dir / f"{base}_{suffix}"
        suffix += 1
    return candidate


def _attach_build_metadata(workbook, fields: dict[str, object]) -> None:
    """Bind provenance to the XLSX itself in a very-hidden worksheet."""
    if "_Build" in workbook.sheetnames:
        del workbook["_Build"]
    worksheet = workbook.create_sheet("_Build")
    worksheet.sheet_state = "veryHidden"
    worksheet.append(["field", "value"])
    for key, value in fields.items():
        worksheet.append([key, "" if value is None else str(value)])


@contextmanager
def _publication_lock(latest_dir: Path):
    """Serialize publishers; POSIX locks are released automatically on crash."""
    lock_path = latest_dir.parent / f".{latest_dir.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another publication is already updating {latest_dir}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _receipt_workbook_sha_bytes(data: bytes) -> str | None:
    try:
        payload = json.loads(data.decode("utf-8"))
        value = ((payload.get("workbook") or {}).get("sha256") or "").strip()
        return value or None
    except (UnicodeError, json.JSONDecodeError, AttributeError):
        return None


def _receipt_workbook_sha(path: Path | None) -> str | None:
    if path is None or not Path(path).is_file():
        return None
    try:
        return _receipt_workbook_sha_bytes(Path(path).read_bytes())
    except OSError:
        return None


def _directory_workbook_sha(directory: Path) -> str | None:
    workbooks = [path for path in directory.glob("*.xlsx") if path.is_file()]
    if len(workbooks) != 1:
        return None
    return hashlib.sha256(workbooks[0].read_bytes()).hexdigest()


def _recover_interrupted_publication(
    latest_dir: Path,
    archive_dir: Path,
    *,
    receipt_path: Path | None = None,
    pending_receipt: Path | None = None,
) -> None:
    """Recover the only crash windows in the directory/receipt swap.

    A pending receipt is written before the first rename. If the new workbook is
    already current, recovery completes that commit. If latest vanished after it
    was renamed aside, the newest hidden backup is restored. Orphaned prior
    directories are archived, never deleted.
    """
    parent = latest_dir.parent
    backups = sorted(
        (path for path in parent.glob(f".{latest_dir.name}-previous-*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime_ns,
    )
    for staging in parent.glob(f".{latest_dir.name}-staging-*"):
        if staging.is_dir():
            shutil.rmtree(staging, ignore_errors=True)

    if not latest_dir.exists() and backups:
        backups.pop().replace(latest_dir)

    if latest_dir.exists() and pending_receipt and pending_receipt.is_file():
        pending_sha = _receipt_workbook_sha(pending_receipt)
        if pending_sha and pending_sha == _directory_workbook_sha(latest_dir):
            if receipt_path is not None:
                _atomic_copy_file(pending_receipt, receipt_path)
            pending_receipt.unlink()

    current_sha = _directory_workbook_sha(latest_dir) if latest_dir.is_dir() else None
    receipt_sha = _receipt_workbook_sha(receipt_path)
    if current_sha and receipt_sha and current_sha != receipt_sha and backups:
        matching = next(
            (path for path in reversed(backups)
             if _directory_workbook_sha(path) == receipt_sha),
            None,
        )
        if matching is not None:
            uncommitted = _next_archive_target(archive_dir, latest_dir)
            latest_dir.replace(uncommitted)
            matching.replace(latest_dir)
            backups.remove(matching)
            current_sha = receipt_sha
    if current_sha and receipt_sha and current_sha != receipt_sha:
        raise RuntimeError(
            "latest workbook does not match its cryptographic receipt and no "
            "recoverable prior handoff exists"
        )

    for backup in backups:
        if backup.exists():
            backup.replace(_next_archive_target(archive_dir, backup))


def publish_latest_workbook(
    source: Path,
    latest_dir: Path = LATEST_OUTPUT_DIR,
    archive_dir: Path | None = None,
    *,
    receipt_source: Path | None = None,
    receipt_path: Path | None = None,
) -> Path:
    """Replace the analyst handoff with exactly one freshly built workbook.

    The new directory is staged beside ``latest_dir`` and swapped in only after
    the workbook copy succeeds. A failed copy therefore leaves the prior analyst
    handoff intact.  After a successful swap, the exact prior directory is moved
    to the deliverable archive instead of being deleted.
    """
    source = Path(source)
    latest_dir = Path(latest_dir)
    if archive_dir is None:
        archive_dir = (
            DELIVERABLE_ARCHIVE_DIR
            if latest_dir.resolve() == LATEST_OUTPUT_DIR.resolve()
            else latest_dir.parent / "archive" / "deliverables"
        )
    archive_dir = Path(archive_dir)
    if not source.is_file():
        raise FileNotFoundError(f"workbook not found: {source}")
    if source.suffix.lower() != ".xlsx":
        raise ValueError(f"latest deliverable must be an .xlsx workbook: {source}")

    receipt_bytes = None
    receipt_workbook_sha = None
    if receipt_source is not None:
        receipt_source = Path(receipt_source)
        if not receipt_source.is_file():
            raise FileNotFoundError(f"publication receipt not found: {receipt_source}")
        receipt_path = Path(receipt_path or LATEST_OUTPUT_RECEIPT)
        receipt_bytes = receipt_source.read_bytes()
        receipt_workbook_sha = _receipt_workbook_sha_bytes(receipt_bytes)
        if receipt_workbook_sha != hashlib.sha256(source.read_bytes()).hexdigest():
            raise ValueError("publication receipt does not match the candidate workbook")
    elif receipt_path is not None:
        raise ValueError("receipt_path requires receipt_source")

    parent = latest_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    pending_receipt = (
        parent / f".{latest_dir.name}-pending-receipt.json"
        if receipt_source is not None else None
    )

    with _publication_lock(latest_dir):
        _recover_interrupted_publication(
            latest_dir,
            archive_dir,
            receipt_path=receipt_path,
            pending_receipt=pending_receipt,
        )
        staging_dir = Path(tempfile.mkdtemp(
            prefix=f".{latest_dir.name}-staging-", dir=parent,
        ))
        staged_workbook = staging_dir / source.name
        backup_dir: Path | None = None
        latest_swapped = False
        previous_receipt = (
            receipt_path.read_bytes()
            if receipt_path is not None and receipt_path.is_file()
            else None
        )

        try:
            shutil.copy2(source, staged_workbook)
            # The company-specific candidate path can be overwritten by another
            # build while this publisher waits for the latest-output lock. Bind
            # the bytes that will actually be swapped, not only the earlier
            # source-path snapshot used for receipt validation.
            if (
                receipt_workbook_sha is not None
                and hashlib.sha256(staged_workbook.read_bytes()).hexdigest()
                != receipt_workbook_sha
            ):
                raise ValueError(
                    "staged workbook changed after publication receipt validation"
                )
            if receipt_bytes is not None and pending_receipt is not None:
                _atomic_write_bytes(pending_receipt, receipt_bytes)
            if latest_dir.exists():
                if not latest_dir.is_dir():
                    raise NotADirectoryError(
                        f"latest deliverable path is not a directory: {latest_dir}"
                    )
                backup_dir = Path(tempfile.mkdtemp(
                    prefix=f".{latest_dir.name}-previous-", dir=parent,
                ))
                backup_dir.rmdir()
                latest_dir.replace(backup_dir)
            staging_dir.replace(latest_dir)
            latest_swapped = True
            if receipt_bytes is not None and receipt_path is not None:
                _atomic_write_bytes(receipt_path, receipt_bytes)
                if pending_receipt is not None and pending_receipt.exists():
                    pending_receipt.unlink()
            if backup_dir is not None:
                backup_dir.replace(_next_archive_target(archive_dir, backup_dir))
        except Exception:
            failed_new = None
            if latest_swapped and latest_dir.exists() and (
                backup_dir is None or backup_dir.exists()
            ):
                failed_new = Path(tempfile.mkdtemp(
                    prefix=f".{latest_dir.name}-failed-new-", dir=parent,
                ))
                failed_new.rmdir()
                latest_dir.replace(failed_new)
            if backup_dir is not None and backup_dir.exists() and not latest_dir.exists():
                backup_dir.replace(latest_dir)
            if receipt_path is not None:
                if previous_receipt is None:
                    if receipt_path.exists():
                        receipt_path.unlink()
                else:
                    _atomic_write_bytes(receipt_path, previous_receipt)
            if failed_new is not None and failed_new.exists():
                shutil.rmtree(failed_new, ignore_errors=True)
            if pending_receipt is not None and pending_receipt.exists():
                pending_receipt.unlink()
            raise
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

    return latest_dir / source.name


# ---------------------------------------------------------------------------
# Phase orchestration
# ---------------------------------------------------------------------------

def _ir_options(config_path: Path | None) -> dict:
    """Read IR download tuning from a tuned config's ``ir_website`` block, if any."""
    opts: dict = {}
    if not config_path or not config_path.exists():
        return opts
    import yaml

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    ir = cfg.get("ir_website", {}) or {}
    if ir.get("url"):
        opts["url"] = ir["url"]
    if ir.get("pdf_link_pattern"):
        opts["file_pattern"] = ir["pdf_link_pattern"]
    if ir.get("year_api_urls"):
        opts["year_api_urls"] = ir["year_api_urls"]
    if ir.get("use_playwright") is not None:
        opts["use_playwright"] = bool(ir["use_playwright"])
    if ir.get("impersonate"):
        opts["impersonate"] = ir["impersonate"]
    if ir.get("delay_ms") is not None:
        opts["delay_ms"] = int(ir["delay_ms"])
    if ir.get("max_reports") is not None:
        opts["max_reports"] = int(ir["max_reports"])
    return opts


def _company_ticker(config_path: Path | None) -> str:
    if not config_path or not config_path.exists():
        return ""
    import yaml

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return str((cfg.get("company") or {}).get("ticker") or "")


def _units_label(config_path: Path | None) -> str:
    """Header units string, e.g. 'MXN mn', derived from the config; default 'P$mn'."""
    if not config_path or not config_path.exists():
        return "P$mn"
    import yaml

    co = (yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}).get("company") or {}
    currency = str(co.get("currency") or "").strip()
    abbr = {"millions": "mn", "thousands": "k", "miles_mxn": "k"}.get(
        str(co.get("unit") or "").strip(), "mn")
    return f"{currency} {abbr}".strip() if currency else "P$mn"


def download_reports(ir_url: str, downloads_dir: Path, ir_opts: dict,
                     max_reports: int) -> list[str]:
    """Download PDFs from the IR page and canonicalize them to ``<period>.pdf``.

    Returns the sorted list of canonical period labels saved into ``downloads_dir``.
    """
    from src.download.downloader import download_from_ir

    staging = downloads_dir / ".staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    url = ir_opts.get("url") or ir_url
    download_from_ir(
        url=url,
        output_dir=staging,
        max_reports=ir_opts.get("max_reports", max_reports),
        file_pattern=ir_opts.get("file_pattern"),
        delay_ms=ir_opts.get("delay_ms", 500),
        year_api_urls=ir_opts.get("year_api_urls"),
        use_playwright=ir_opts.get("use_playwright"),
        impersonate=ir_opts.get("impersonate"),
    )

    saved = _canonicalize(list(staging.glob("**/*.pdf")), downloads_dir)
    shutil.rmtree(staging, ignore_errors=True)
    return saved


def _canonicalize(staged: list[Path], out_dir: Path) -> list[str]:
    """Copy staged native-name PDFs into ``out_dir`` as ``<period>.pdf``.

    When several files map to the same period, ``index_report_files`` picks the
    preferred one via its report-type precedence. Idempotent.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    groups = index_report_files(staged)
    for period in sorted(groups, key=period_sort_key):
        target = out_dir / f"{period}.pdf"
        src = groups[period].selected_path
        if src is None or src.suffix.lower() != ".pdf":
            continue
        if not target.exists():
            target.write_bytes(src.read_bytes())
        saved.append(period)
    return saved


def parse_reports(downloads_dir: Path, parses_dir: Path) -> list[str]:
    """Parse each canonical PDF in ``downloads_dir`` into ``parses_dir/<period>.md``."""
    from src.parse.parse_pdf import parse_pdf

    parses_dir.mkdir(parents=True, exist_ok=True)
    pdfs = [p for p in downloads_dir.glob("*.pdf")
            if infer_period_label(p.stem) is not None]
    parsed: list[str] = []
    for pdf in sorted(pdfs, key=lambda p: period_sort_key(infer_period_label(p.stem) or p.stem)):
        md_dest = parses_dir / f"{pdf.stem}.md"
        if md_dest.exists():
            parsed.append(pdf.stem)
            continue
        try:
            md, _blocks = parse_pdf(pdf)
        except Exception as exc:  # noqa: BLE001 — keep parsing the rest
            print(f"  WARN parse {pdf.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        md_dest.write_text(md, encoding="utf-8")
        parsed.append(pdf.stem)
    return parsed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _report_cache(slug: str, *, force_download: bool = False) -> Path:
    """Prefer the monorepo-wide zero-copy estate view when it has parsed reports.

    The shared view is STRICTLY read-only from this pipeline's perspective — it is
    regenerated from the estate catalog by a separate process, and writing into it
    would race that builder. Fresh downloads and parse products always land in the
    root project's writable cache (``data/reports/<slug>``); the estate picks them
    up on its next rebuild.
    """
    shared = SHARED_REPORTS_DIR / slug
    if not force_download and shared.is_dir() and any(shared.glob("*.md")):
        return shared
    return REPORTS_DIR / slug


def _report_sources(slug: str, *, force_download: bool = False) -> tuple[Path, ...]:
    """Ordered zero-copy report inputs for one company.

    The compatibility view can hold PDFs, legacy Markdown and XBRL facts; the
    acquisition derivative consumer writes versioned Markdown into
    ``views/parsed``; and transitional/manual reports can still live in the
    writable root cache. Pipeline union handling collapses them by period.
    """
    writable = REPORTS_DIR / slug
    if force_download:
        return (writable,)

    candidates = (
        SHARED_REPORTS_DIR / slug,
        writable,
        SHARED_PARSED_REPORTS_DIR / slug,
    )
    populated = controlled_report_directories(candidates)
    return populated or (writable,)


def _available_periods(directories: tuple[Path, ...]) -> list[str]:
    """Canonical periods visible across all report/facts directories."""
    return available_report_periods(directories)


def run(
    md_path: Path,
    *,
    max_reports: int = 100,
    force_download: bool = False,
    estate_only: bool = False,
    strict_metrics: bool = True,
    require_analyst_sheet: bool = True,
    require_fresh_estate: bool = True,
    freshness_hours: float = 24.0,
    analyst_metrics: str | Path | None = None,
    analyst_company: str | None = None,
    analyst_sheet: str | None = None,
) -> BuildResult:
    """Run the full pipeline and return its publication disposition.

    Canonical estate views and any transitional local report cache are read as
    one zero-copy union.  Publication additionally requires pinned mappings, an
    exact analyst-sheet match, a fresh strict acquisition receipt, and a clean
    verification/workbook audit.  Escape hatches may build a review candidate,
    but can never weaken those publication conditions.
    """
    input_bytes = md_path.read_bytes()
    md_text = input_bytes.decode("utf-8")
    input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    name, ir_url, _flat = parse_input(md_text)        # validates name + IR link present
    slug = slugify(name)
    build_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()

    # Deliverables live under outputs/<Company>/; raw PDFs + parsed markdown stay
    # in the data/reports/<slug> cache (single source of truth, never duplicated).
    out_root = OUTPUTS_DIR / dirname_for(name)
    csv_dir = out_root / "csv"
    excel_dir = out_root / "excel"
    validation_dir = out_root / "validation"
    for d in (csv_dir, excel_dir, validation_dir):
        d.mkdir(parents=True, exist_ok=True)

    report_sources = _report_sources(slug, force_download=force_download)
    writable_cache = REPORTS_DIR / slug
    if report_sources == (writable_cache,):
        writable_cache.mkdir(parents=True, exist_ok=True)
    # Estate views in report_sources are read-only; never mkdir/write there.

    config_path = CONFIGS_DIR / f"{slug}.yaml"
    config_arg = str(config_path) if config_path.exists() else None
    manifest_path = validation_dir / f"{slug}_metrics_manifest.json"

    # Compile the metrics outline and resolve each data label to a metric key.
    from src.excel.segments_sheet import load_metric_defs
    from src.extract.interface import MetricResolver

    defs = load_metric_defs(config_arg)
    resolver = MetricResolver(defs)
    valid_keys = {m.key for m in defs}
    outline_text, sections, mapping, report = compile_outline(md_text, resolver, valid_keys)

    # The analyst's labels and their canonical keys are a hard input contract,
    # not a fuzzy suggestion.  Strict onboarding requires every data row to be
    # explicitly pinned before any download or extraction work starts.
    mapping_issues = metric_contract_issues(report, require_pins=strict_metrics)

    # When the original analyst CSV/XLSX is available, compare it directly with
    # the executable Markdown translation.  The optional Markdown metadata keeps
    # this check reproducible on later refreshes:
    #   Analyst-Metrics: requests/Metrics.xlsx#Worksheet
    #   Analyst-Company: FEMSA
    declared_sheet, declared_company, declared_worksheet = _analyst_sheet_metadata(md_text)
    analyst_metrics = analyst_metrics or declared_sheet
    analyst_company = analyst_company or declared_company or name
    analyst_sheet = analyst_sheet or declared_worksheet
    analyst_spec = fidelity = key_fidelity = kind_fidelity = freshness = None
    if not analyst_metrics and require_analyst_sheet:
        mapping_issues.append(
            "original analyst metric sheet is required; pass --analyst-metrics "
            "PATH or declare Analyst-Metrics in the Markdown"
        )
    if analyst_metrics:
        from src.excel.analyst_spec import (
            AnalystMetricSheetError,
            compare_metric_kinds,
            compare_metric_keys,
            compare_metric_sequences,
            load_analyst_metric_sheet,
        )

        analyst_path = _resolve_analyst_sheet_path(analyst_metrics, md_path)
        ticker = _company_ticker(config_path if config_arg else None)
        try:
            analyst_spec = load_analyst_metric_sheet(
                analyst_path,
                analyst_company,
                aliases=tuple(value for value in (name, slug, ticker) if value),
                sheet_name=analyst_sheet,
            )
        except AnalystMetricSheetError as exc:
            mapping_issues.append(f"analyst metrics sheet is unusable: {exc}")
        if analyst_spec is not None:
            fidelity = compare_metric_sequences(
                analyst_spec.labels,
                requested_label_sequence(report),
            )
            key_fidelity = compare_metric_keys(
                analyst_spec.labels,
                analyst_spec.keys,
                requested_key_sequence(report),
            )
            kind_fidelity = compare_metric_kinds(
                analyst_spec.labels,
                analyst_spec.kinds,
                requested_kind_sequence(report),
            )
        if fidelity is not None and not fidelity.matches:
            mapping_issues.append(
                "analyst metrics sheet does not match the onboarding outline: "
                + str(fidelity.first_difference)
            )
        if key_fidelity is not None and not key_fidelity.matches:
            mapping_issues.append(
                "analyst metrics sheet canonical keys do not match the onboarding pins: "
                + str(key_fidelity.first_difference)
            )
        if kind_fidelity is not None and not kind_fidelity.matches:
            mapping_issues.append(
                "analyst metrics sheet row roles do not match the onboarding outline: "
                + str(kind_fidelity.first_difference)
            )

    # A local file's existence is not evidence that the recurring updater
    # actually observed the latest IR state.  Strict onboarding therefore
    # requires a recent, fully-successful canonical acquisition receipt for
    # this exact issuer.  The explicit escape hatch is for offline development,
    # and is recorded in the manifest as an unverified/stale receipt.
    if freshness_hours <= 0:
        raise InputError("freshness_hours must be positive")
    from src.acquisition.service import check_quarterly_sync_freshness
    freshness = check_quarterly_sync_freshness(
        DOCUMENT_ESTATE_DB,
        slug,
        freshness_hours * 60 * 60,
    )
    if require_fresh_estate and not freshness.fresh:
        mapping_issues.append(
            "estate freshness is unproven "
            f"({freshness.reason}); run `refresh-quarterly-estate sync --only "
            f"{slug} --apply` successfully before onboarding"
        )

    print(f"Company : {name}  (slug: {slug})")
    print(f"Config  : {'configs/' + config_path.name + ' (reused)' if config_arg else 'generic (no tuned config)'}")
    print(f"Output  : {out_root}")
    print(f"Outline : {len(sections)} section(s), {sum(1 for r in report if r[1] != 'section')} metric row(s)")
    _print_resolution_report(report)

    if mapping_issues:
        _write_metrics_manifest(
            manifest_path,
            company=name,
            slug=slug,
            md_path=md_path,
            input_sha256=input_sha256,
            report=report,
            mapping_issues=mapping_issues,
            analyst_spec=analyst_spec,
            fidelity=fidelity,
            key_fidelity=key_fidelity,
            kind_fidelity=kind_fidelity,
            freshness=freshness,
            build_id=build_id,
            generated_at=generated_at,
        )
        detail = "\n  - ".join(mapping_issues[:20])
        raise InputError(
            "analyst metric contract failed before extraction:\n  - " + detail
        )

    parsed = _available_periods(report_sources)
    reuse = not force_download and bool(parsed)

    if reuse:
        # ── Phases 1–2 · Reuse already-downloaded + parsed reports ───────────
        locations = ", ".join(_display_path(path) for path in report_sources)
        print(f"\n[1-2/5] Reusing zero-copy report inputs from {locations} "
              "— skipping download/parse.")
        print(f"        {len(parsed)} report period(s) available")
    elif estate_only:
        raise InputError(
            f"No hay reportes trimestrales procesados para {name} en el Data Estate."
        )
    else:
        # ── Phase 1 · Download (always into the WRITABLE cache, never the
        # estate view — the view is read-only and owned by the estate builder) ─
        writable_cache.mkdir(parents=True, exist_ok=True)
        print("\n[1/5] Downloading reports from IR page…")
        ir_opts = _ir_options(config_path if config_arg else None)
        periods = download_reports(ir_url, writable_cache, ir_opts, max_reports)
        print(f"      {len(periods)} report(s) in {_display_path(writable_cache)}")
        if not periods:
            print(f"      No PDFs downloaded. The IR page may render links via JavaScript; "
                  f"drop PDFs into {_display_path(writable_cache)} manually and re-run.",
                  file=sys.stderr)

        # ── Phase 2 · Parse (siblings in the cache) ──────────────────────────
        print("[2/5] Parsing PDFs → markdown…")
        parsed = parse_reports(writable_cache, writable_cache)
        print(f"      {len(parsed)} report(s) parsed")
        report_sources = _report_sources(slug)

    if not parsed:
        print("      Nothing to extract — aborting.", file=sys.stderr)
        raise SystemExit(1)

    # ── Phase 3 · Extract (only the mapped keys, through the full cascade) ─────
    print("[3/5] Extracting metrics…")
    from src.excel.segments_sheet import (
        build_outline_workbook,
        parse_outline,
        segments_title,
        _unit_map,
    )
    from src.extract import pipeline

    rows = parse_outline(outline_text, sections, mapping)
    keys = sorted({r.key for r in rows if r.key})
    csv_path = csv_dir / f"{slug}_metrics.csv"
    df = pipeline.run(
        list(report_sources),
        metrics=keys,
        config=config_arg,
        output_csv=csv_path,
        verbose=False,
    )
    # Replace the discovery-only receipt with publication proof bound to the
    # exact effective files chosen by directory/version/facts precedence.
    from src.acquisition.service import check_quarterly_publication_freshness
    freshness = check_quarterly_publication_freshness(
        DOCUMENT_ESTATE_DB,
        slug,
        freshness_hours * 60 * 60,
        input_paths=tuple((getattr(df, "attrs", {}) or {}).get("input_paths") or ()),
        estate_root=DOCUMENT_ESTATE_DIR,
    )
    filled = [k for k in keys if k in getattr(df, "columns", [])]
    print(f"      CSV → {_display_path(csv_path)}  "
          f"({len(df)} period(s); {len(filled)}/{len(keys)} mapped key(s) extracted)")
    if not freshness.fresh:
        print(
            f"      ⚠ publication freshness blocked: {freshness.reason}"
            + (f" — {freshness.detail}" if freshness.detail else "")
        )

    # ── Phase 4 · Excel (outline mode: sections + derived rows as formulas) ────
    print("[4/5] Building Segments workbook…")
    from src.excel.segments_sheet import augment_outline_checks
    from src.model.financial_model import load_config as _load_cfg
    title = segments_title(name, _company_ticker(config_path if config_arg else None))
    units = _units_label(config_path if config_arg else None)
    auto_checks = bool((_load_cfg(config_arg) if config_arg else {}).get("auto_checks", True))
    layout = _unit_map(config_arg)
    if auto_checks:
        _, n_auto = augment_outline_checks(rows, df, skip_rules=layout.skip_rules)
        if n_auto:
            print(f"      {n_auto} auto check row(s) inserted (segment-sum / identity)")
    wb = build_outline_workbook(
        title,
        rows,
        df,
        units=units,
        unit_map=layout,
        auto_checks=auto_checks,
        preserve_requested_rows=True,
    )
    xlsx_path = excel_dir / f"{dirname_for(name)}.xlsx"
    _attach_build_metadata(
        wb,
        {
            "build_id": build_id,
            "generated_at": generated_at,
            "company": name,
            "slug": slug,
            "input_sha256": input_sha256,
            "analyst_sheet_sha256": analyst_spec.sha256 if analyst_spec else None,
            "acquisition_run_id": freshness.run_id if freshness else None,
            "acquisition_completed_at": freshness.completed_at if freshness else None,
            "acquisition_input_watermark": freshness.input_watermark if freshness else None,
            "acquisition_input_document_ids": (
                ",".join(freshness.input_document_ids) if freshness else ""
            ),
        },
    )
    wb.save(xlsx_path)
    workbook_sha256 = hashlib.sha256(xlsx_path.read_bytes()).hexdigest()

    # ── Phase 5 · Validation report (confidence + identities + ground truth) ──
    print("[5/5] Writing validation report…")
    from src.eval.validation_report import write_validation_report
    report_path = validation_dir / f"{slug}_validation.md"
    write_validation_report(
        df,
        keys,
        name=name,
        slug=slug,
        out_path=report_path,
        skip_rules=layout.skip_rules,
    )
    print(f"      Report → {_display_path(report_path)}")

    # ── Phase 6 · Verification gate (strength scorecard + worklist + Excel audit) ─
    from src.eval.verification_gate import score_metrics, scorecard_markdown
    from src.excel.segments_sheet import audit_outline
    from src.model.financial_model import load_config
    cfg_dict = load_config(config_arg) if config_arg else {}
    expectations = (cfg_dict or {}).get("metric_expectations") or {}
    scores, is_strong, worklist = score_metrics(df, keys, expectations=expectations)
    # Excel audit: the workbook must == the deliverable — no blank rows, every
    # derived row a live formula. Any unreachable row the outline still lists is an
    # audit failure (remove it from inputs/<slug>.md) and blocks STRONG.
    excel_issues = audit_outline(
        rows,
        df,
        unit_map=layout,
        preserve_requested_rows=True,
    )
    if excel_issues:
        is_strong = False
        for it in excel_issues:
            worklist.append({"key": "(excel)", "period": it["kind"], "value": None,
                             "reason": f"EXCEL AUDIT — drop from outline: {it['label']} "
                                       f"({it['reason']})", "priority": 0})
    # Round-trip the rendered label sequence.  The workbook may add trusted
    # auto-check rows, so compare against the same augmented plan the renderer
    # uses; analyst-declared rows are never pruned on this onboarding path.
    # Row 5 is the period-header row; analyst-visible outline rows begin at 6.
    rendered_labels = [
        str(wb["Segments"].cell(row=row, column=2).value).strip()
        for row in range(6, wb["Segments"].max_row + 1)
        if wb["Segments"].cell(row=row, column=2).value not in (None, "")
    ]
    expected_rows = (
        augment_outline_checks(rows, df, skip_rules=layout.skip_rules)[0]
        if auto_checks else rows
    )
    expected_labels = [spec.label for spec in expected_rows if spec.kind != "spacer"]
    render_matches = rendered_labels == expected_labels
    if not render_matches:
        is_strong = False
        issue = {
            "label": "(workbook sequence)",
            "kind": "fidelity",
            "reason": "rendered section/row order differs from the analyst metric plan",
        }
        excel_issues.append(issue)
        worklist.append({
            "key": "(excel)",
            "period": "fidelity",
            "value": None,
            "reason": "EXCEL AUDIT — rendered order differs from analyst metric plan",
            "priority": 0,
        })

    worklist_path = validation_dir / f"{slug}_worklist.json"
    worklist_path.write_text(json.dumps(worklist, indent=2), encoding="utf-8")
    card = scorecard_markdown(scores, is_strong)
    print("[6/6] Verification gate:")
    print("      " + card.splitlines()[0])
    if excel_issues:
        print(f"      ⚠ EXCEL AUDIT: {len(excel_issues)} issue(s) block publication:")
        for it in excel_issues[:20]:
            print(f"          • {it['label']}  ({it['reason']})")
    print(f"      Worklist ({len(worklist)} item(s)) → {_display_path(worklist_path)}")
    # Append the final scorecard + audit after the rendered-sequence check so
    # the worklist and report cannot disagree about publication readiness.
    with report_path.open("a", encoding="utf-8") as fh:
        fh.write("\n## 5. Verification-gate scorecard\n\n" + card + "\n")
        if excel_issues:
            fh.write("\n## 6. Excel audit — publication blockers\n\n")
            for it in excel_issues:
                fh.write(f"- **{it['label']}** ({it['kind']}) — {it['reason']}\n")

    # Shipping is stricter than successful candidate generation. Exploratory
    # escape hatches can never replace the analyst handoff unless the resulting
    # build independently satisfies every production contract.
    publish_blockers = list(metric_contract_issues(report, require_pins=True))
    try:
        current_input_sha256 = hashlib.sha256(md_path.read_bytes()).hexdigest()
    except OSError:
        current_input_sha256 = None
    if current_input_sha256 != input_sha256:
        publish_blockers.append("onboarding Markdown changed during the build")
    if analyst_spec is None:
        publish_blockers.append("original analyst metric sheet was not verified")
    elif fidelity is None or not fidelity.matches:
        publish_blockers.append("analyst metric label/order contract did not pass")
    elif key_fidelity is None or not key_fidelity.matches:
        publish_blockers.append("analyst-declared canonical key contract did not pass")
    elif kind_fidelity is None or not kind_fidelity.matches:
        publish_blockers.append("analyst row-role contract did not pass")
    if freshness is None or not freshness.fresh:
        reason = freshness.reason if freshness is not None else "not checked"
        publish_blockers.append(f"canonical estate freshness is unproven ({reason})")
    if not is_strong:
        publish_blockers.append("verification gate is not STRONG")
    if excel_issues or not render_matches:
        publish_blockers.append("workbook audit/fidelity gate did not pass")
    publish_blockers = list(dict.fromkeys(publish_blockers))

    # Persist a non-published manifest first. If provenance cannot be recorded,
    # publication never starts. A successful swap is then reflected atomically
    # in the same manifest.
    _write_metrics_manifest(
        manifest_path,
        company=name,
        slug=slug,
        md_path=md_path,
        input_sha256=input_sha256,
        report=report,
        mapping_issues=mapping_issues,
        analyst_spec=analyst_spec,
        fidelity=fidelity,
        key_fidelity=key_fidelity,
        kind_fidelity=kind_fidelity,
        input_lineage=tuple((getattr(df, "attrs", {}) or {}).get("input_lineage") or ()),
        rendered_labels=rendered_labels,
        excel_issues=excel_issues,
        freshness=freshness,
        gate_strong=is_strong,
        publish_blockers=publish_blockers,
        build_id=build_id,
        generated_at=generated_at,
        workbook_path=xlsx_path,
        workbook_sha256=workbook_sha256,
        published=False,
    )
    latest_path = None
    if not publish_blockers:
        publication_receipt = validation_dir / f".{slug}_{build_id}_publication.json"
        _write_metrics_manifest(
            publication_receipt,
            company=name,
            slug=slug,
            md_path=md_path,
            input_sha256=input_sha256,
            report=report,
            mapping_issues=mapping_issues,
            analyst_spec=analyst_spec,
            fidelity=fidelity,
            key_fidelity=key_fidelity,
            kind_fidelity=kind_fidelity,
            input_lineage=tuple(
                (getattr(df, "attrs", {}) or {}).get("input_lineage") or ()
            ),
            rendered_labels=rendered_labels,
            excel_issues=excel_issues,
            freshness=freshness,
            gate_strong=is_strong,
            publish_blockers=[],
            build_id=build_id,
            generated_at=generated_at,
            workbook_path=xlsx_path,
            workbook_sha256=workbook_sha256,
            published=True,
        )
        try:
            latest_path = publish_latest_workbook(
                xlsx_path,
                receipt_source=publication_receipt,
                receipt_path=LATEST_OUTPUT_RECEIPT,
            )
        finally:
            if publication_receipt.exists():
                publication_receipt.unlink()
        _write_metrics_manifest(
            manifest_path,
            company=name,
            slug=slug,
            md_path=md_path,
            input_sha256=input_sha256,
            report=report,
            mapping_issues=mapping_issues,
            analyst_spec=analyst_spec,
            fidelity=fidelity,
            key_fidelity=key_fidelity,
            kind_fidelity=kind_fidelity,
            input_lineage=tuple(
                (getattr(df, "attrs", {}) or {}).get("input_lineage") or ()
            ),
            rendered_labels=rendered_labels,
            excel_issues=excel_issues,
            freshness=freshness,
            gate_strong=is_strong,
            publish_blockers=[],
            build_id=build_id,
            generated_at=generated_at,
            workbook_path=xlsx_path,
            workbook_sha256=workbook_sha256,
            published=True,
        )

    mark = "✓ PUBLISHED" if latest_path is not None else "⚠ REVIEW CANDIDATE"
    print(f"\n{mark} — {len(keys)} mapped metric(s) over {len(df)} period(s)  "
          f"[{'STRONG' if is_strong else 'NEEDS VERIFICATION'}]")
    if latest_path is not None:
        print(f"  OPEN THIS → {_display_path(latest_path)}")
    else:
        print(f"  CANDIDATE (not published) → {_display_path(xlsx_path)}")
        print("  The current analyst handoff was left unchanged until the metric "
              "contract and verification gate pass.")
    print(f"  Metric contract → {_display_path(manifest_path)}")
    print(f"  Review files → {_display_path(out_root)}/  (excel/ · csv/ · validation/)")
    return BuildResult(
        candidate_path=xlsx_path,
        latest_path=latest_path,
        manifest_path=manifest_path,
        publish_blockers=tuple(publish_blockers),
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="build_segments.py",
        description="Build a company's Segments workbook from a single markdown file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("input", help="Path to the regularized company markdown file.")
    ap.add_argument("--max-reports", type=int, default=100,
                    help="Cap on PDFs to download from the IR page (default: 100).")
    ap.add_argument("--force-download", action="store_true",
                    help="Download fresh even if data/reports/<slug> already has parsed reports.")
    ap.add_argument(
        "--analyst-metrics",
        metavar="PATH",
        help=(
            "Original analyst CSV/XLSX metric request. Its ordered labels must match "
            "the Markdown outline exactly. May also be declared as Analyst-Metrics: "
            "PATH[#Worksheet] inside the Markdown."
        ),
    )
    ap.add_argument(
        "--analyst-company",
        help=(
            "Company/ticker block marker in a legacy analyst metric sheet "
            "(defaults to the Markdown company name)."
        ),
    )
    ap.add_argument(
        "--analyst-sheet",
        help="Worksheet name when --analyst-metrics points to an XLSX workbook.",
    )
    ap.add_argument(
        "--estate-only",
        action="store_true",
        help="Use only reports already present in the Data Estate; never download files.",
    )
    ap.add_argument(
        "--allow-auto-map",
        action="store_true",
        help=(
            "Exploratory only: allow exact resolver matches without explicit {metric_key} "
            "pins. Strict pinned mappings are required by default."
        ),
    )
    ap.add_argument(
        "--allow-missing-analyst-sheet",
        action="store_true",
        help=(
            "Exploratory only: build a candidate without the original analyst "
            "CSV/XLSX. Such a candidate cannot replace outputs/latest/."
        ),
    )
    ap.add_argument(
        "--allow-stale-estate",
        action="store_true",
        help=(
            "Offline/exploratory only: build without a recent successful canonical "
            "acquisition receipt. The metrics manifest records the missing/stale receipt."
        ),
    )
    ap.add_argument(
        "--freshness-hours",
        type=float,
        default=24.0,
        help="Maximum age of the required successful company sync receipt (default: 24).",
    )
    args = ap.parse_args()

    md_path = Path(args.input).expanduser()
    if not md_path.exists():
        ap.error(f"input file not found: {md_path}")

    try:
        result = run(
            md_path,
            max_reports=args.max_reports,
            force_download=args.force_download,
            estate_only=args.estate_only,
            strict_metrics=not args.allow_auto_map,
            require_analyst_sheet=not args.allow_missing_analyst_sheet,
            require_fresh_estate=not args.allow_stale_estate,
            freshness_hours=args.freshness_hours,
            analyst_metrics=args.analyst_metrics,
            analyst_company=args.analyst_company,
            analyst_sheet=args.analyst_sheet,
        )
    except InputError as exc:
        ap.error(f"bad markdown input: {exc}")
    return 0 if result.published else 3


if __name__ == "__main__":
    raise SystemExit(main())
