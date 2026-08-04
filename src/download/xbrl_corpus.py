"""Make downloaded BMV XBRL filings visible to the extractor.

``bmv_xbrl.download_ticker`` already stores, for every filing, an instance JSON
plus two derived artifacts: ``<stem>_facts.json`` (tagged IFRS concepts) and
``<stem>_mdna.html`` (the management-discussion narrative — the PDF-text
analogue). 1,233 of those MD&A files sit under ``data/reports/`` today and
nothing reads them, because:

* the extractor globs ``<report_dir>/*.md`` and ``<report_dir>/*_facts.json``
  at the top level, while ``download_ticker`` writes ticker-prefixed names into
  a ``xbrl/`` subdirectory (``ALSEA_2021-2T_mdna.html``); and
* the estate's document typer classifies by file extension, so every ``.html``
  becomes a ``regulatory_filing`` regardless of what it contains.

This module bridges that gap without moving or re-downloading anything: it
hardlinks each filing's facts to the canonical ``<period>_facts.json`` and
renders its MD&A to ``<period>.md``, then records where every period's text
actually came from in ``provenance.json``.

Provenance is not decoration. An MD&A text block is *not* the issuer's earnings
release — it is thinner, and its numbers are the tagged ones. A workbook built
from it must be able to say so, which is why every period carries a source
label that the coverage report and the accuracy report both read.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import os
import re
import shutil
import sys
from pathlib import Path

from src.shared.report_index import infer_period_label, period_sort_key


__all__ = [
    "PeriodProvenance",
    "MaterializeResult",
    "PROVENANCE_FILENAME",
    "load_provenance",
    "materialize_xbrl_corpus",
    "render_mdna_html",
]

PROVENANCE_FILENAME = "provenance.json"

#: Ranked best → worst. A period keeps the strongest source it has.
SOURCE_PDF = "pdf"          # parsed from the issuer's own earnings-release PDF
SOURCE_MDNA = "mdna"        # rendered from the XBRL filing's MD&A narrative
SOURCE_FACTS = "facts"      # tagged concepts only, no narrative at all
_SOURCE_RANK = {SOURCE_PDF: 0, SOURCE_MDNA: 1, SOURCE_FACTS: 2}


@dataclass(frozen=True)
class PeriodProvenance:
    """Where one period's extractable text and numbers came from."""

    period: str
    source: str
    markdown: str | None = None
    facts: str | None = None
    instance: str | None = None


@dataclass
class MaterializeResult:
    report_dir: Path
    markdown_written: list[str]
    facts_linked: list[str]
    provenance: dict[str, PeriodProvenance]

    @property
    def periods(self) -> list[str]:
        return sorted(self.provenance, key=period_sort_key)


#: Tags that end a line of output. ``td``/``th``/``tr`` are handled separately
#: so table rows stay on one line with their cells delimited.
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "blockquote", "td", "th", "tr", "table",
    }
)
_NESTING_BLOCKS = _BLOCK_TAGS - {"td", "th", "tr"}


def render_mdna_html(html: str) -> str:
    """Render an XBRL MD&A text block to line-structured plain text.

    The embedded HTML is Aspose.Words output: every styled run is its own
    ``<span>``, and BeautifulSoup's ``get_text`` splits *inside* words at those
    boundaries. Joining with a newline or a space yields ``202\\n1`` and
    ``5.4 v eces`` — numbers and words broken mid-token, which defeats every
    downstream regex. Joining with ``""`` repairs the words but welds adjacent
    paragraphs together (``RAZONES FINANCIERASAl 30de…``).

    So: concatenate inline runs with no separator (words survive intact) and
    emit a line break only at real block boundaries. Table rows become
    ``cell | cell | cell``, which keeps a metric and its columns on one line.
    """
    from bs4 import BeautifulSoup, Tag

    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")

    lines: list[str] = []

    def text_of(node) -> str:
        # separator="" so runs re-join into whole words; collapse the runs of
        # spaces that inline styling leaves behind.
        return re.sub(r"[ \t]+", " ", node.get_text("", strip=False)).strip()

    def walk(node) -> None:
        for child in node.children:
            if not isinstance(child, Tag):
                continue
            if child.name == "tr":
                cells = [text_of(c) for c in child.find_all(["td", "th"], recursive=True)]
                cells = [cell for cell in cells if cell]
                if cells:
                    lines.append(" | ".join(cells))
                continue
            if child.name == "table":
                walk(child)
                continue
            if child.name in _BLOCK_TAGS:
                nested = (
                    child.find(_NESTING_BLOCKS)
                    or child.find("table")
                    or child.find("tr")
                )
                if nested:
                    walk(child)
                else:
                    text = text_of(child)
                    if text:
                        lines.append(text)
                continue
            walk(child)

    walk(soup)
    return "\n".join(lines)


def _mdna_text(instance: Path) -> str:
    """Structured plain text for a filing's MD&A, regenerating it if needed."""
    from src.download.bmv_xbrl import extract_artifacts

    mdna_path = instance.with_name(_logical_stem(instance) + "_mdna.html")
    if not mdna_path.exists():
        extract_artifacts(instance)
    if not mdna_path.exists():
        return ""
    return render_mdna_html(mdna_path.read_text(encoding="utf-8"))


def _logical_stem(path: Path) -> str:
    """``ALSEA_2021-2T.json.gz`` → ``ALSEA_2021-2T``."""
    name = path.name
    for suffix in (".json.gz", ".json", ".html"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _is_derived(stem: str) -> bool:
    return stem.endswith("_facts") or stem.endswith("_mdna")


def _xbrl_search_dirs(report_dir: Path) -> list[Path]:
    """Both on-disk layouts: canonical top level, and the ``xbrl/`` subdir."""
    dirs = [report_dir]
    nested = report_dir / "xbrl"
    if nested.is_dir():
        dirs.append(nested)
    return dirs


def _discover_instances(report_dir: Path) -> dict[str, Path]:
    """Period → XBRL instance JSON, newest layout winning on collision."""
    found: dict[str, Path] = {}
    for directory in _xbrl_search_dirs(report_dir):
        for path in sorted(directory.iterdir() if directory.is_dir() else []):
            if not path.is_file():
                continue
            if not path.name.lower().endswith((".json", ".json.gz")):
                continue
            stem = _logical_stem(path)
            if _is_derived(stem):
                continue
            period = infer_period_label(stem)
            if period:
                found.setdefault(period, path)
    return found


def _link_or_copy(src: Path, dest: Path) -> None:
    """Hardlink ``src`` to ``dest``, falling back to a copy across devices.

    Facts artifacts average ~1 MB across ~1,500 files; hardlinking keeps the
    canonical view free instead of duplicating 1.5 GB.
    """
    try:
        os.link(src, dest)
    except (OSError, NotImplementedError):
        shutil.copy2(src, dest)


def _existing_facts(instance: Path) -> Path | None:
    candidate = instance.with_name(_logical_stem(instance) + "_facts.json")
    return candidate if candidate.exists() else None


def has_existing_corpus(report_dir: str | Path) -> bool:
    """True when the directory already holds extractable reports."""
    report_dir = Path(report_dir)
    if not report_dir.is_dir():
        return False
    return any(report_dir.glob("*.pdf")) or any(report_dir.glob("*.md"))


def materialize_xbrl_corpus(
    report_dir: str | Path,
    *,
    overwrite: bool = False,
    fill_gaps: bool | None = None,
    write_provenance: bool = True,
) -> MaterializeResult:
    """Surface XBRL facts and MD&A text at the paths the extractor reads.

    ``fill_gaps`` decides whether this may add artifacts to a corpus that
    already has content. Default (``None``) means **bootstrap only**: fill a
    directory that holds no PDFs and no Markdown, and otherwise report
    provenance without writing anything.

    That default is not timidity — it is the corpus phase-gate. Making
    previously-invisible facts and MD&A readable *adds observations*, and new
    observations have not been certified: dropping them into `lab`, `chedraui`
    and `femsa` raised each company's FAIL count against its pinned regression
    baseline. Extending an existing corpus is therefore a deliberate act
    (``fill_gaps=True``) that the operator follows with re-certification.

    Never touches a period that already has a PDF either way: that period's
    Markdown is owned by the PDF parse step and is the better source. Existing
    Markdown is left alone unless ``overwrite`` is set.
    """
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    if fill_gaps is None:
        fill_gaps = not has_existing_corpus(report_dir)

    instances = _discover_instances(report_dir)
    provenance: dict[str, PeriodProvenance] = {}
    markdown_written: list[str] = []
    facts_linked: list[str] = []

    # Periods already backed by a PDF — the parse path owns their Markdown.
    pdf_periods = {
        period
        for period in (infer_period_label(p.stem) for p in report_dir.glob("*.pdf"))
        if period
    }

    for period in sorted(instances, key=period_sort_key):
        instance = instances[period]
        md_path = report_dir / f"{period}.md"
        facts_dest = report_dir / f"{period}_facts.json"

        source_facts = _existing_facts(instance)
        if fill_gaps and source_facts is not None and not facts_dest.exists():
            _link_or_copy(source_facts, facts_dest)
            facts_linked.append(period)

        has_pdf = period in pdf_periods
        wrote_markdown = False
        if fill_gaps and not has_pdf and (overwrite or not md_path.exists()):
            try:
                text = _mdna_text(instance)
            except Exception as exc:  # noqa: BLE001 — one bad filing must not stop the rest
                print(
                    f"WARN mdna {instance.name}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                text = ""
            if text.strip():
                md_path.write_text(text, encoding="utf-8")
                markdown_written.append(period)
                wrote_markdown = True

        if has_pdf:
            source = SOURCE_PDF
        elif wrote_markdown or md_path.exists():
            source = SOURCE_MDNA
        else:
            source = SOURCE_FACTS

        provenance[period] = PeriodProvenance(
            period=period,
            source=source,
            markdown=md_path.name if md_path.exists() else None,
            facts=facts_dest.name if facts_dest.exists() else None,
            instance=str(instance.relative_to(report_dir))
            if instance.is_relative_to(report_dir)
            else str(instance),
        )

    # Periods with a PDF but no XBRL filing still belong in the record.
    for period in sorted(pdf_periods, key=period_sort_key):
        if period in provenance:
            continue
        md_path = report_dir / f"{period}.md"
        provenance[period] = PeriodProvenance(
            period=period,
            source=SOURCE_PDF,
            markdown=md_path.name if md_path.exists() else None,
            facts=None,
            instance=None,
        )

    if write_provenance and provenance:
        _write_provenance(report_dir, provenance)

    return MaterializeResult(
        report_dir=report_dir,
        markdown_written=markdown_written,
        facts_linked=facts_linked,
        provenance=provenance,
    )


def _write_provenance(
    report_dir: Path, provenance: dict[str, PeriodProvenance]
) -> None:
    """Merge into any existing record so unrelated periods survive a rerun."""
    existing = load_provenance(report_dir)
    merged = dict(existing)
    for period, entry in provenance.items():
        prior = merged.get(period)
        # Never let a rerun downgrade a period's recorded source.
        if prior is not None and _SOURCE_RANK.get(
            prior.source, 99
        ) < _SOURCE_RANK.get(entry.source, 99):
            continue
        merged[period] = entry

    payload = {
        "version": 1,
        "periods": {
            period: asdict(merged[period])
            for period in sorted(merged, key=period_sort_key)
        },
    }
    (report_dir / PROVENANCE_FILENAME).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def load_provenance(report_dir: str | Path) -> dict[str, PeriodProvenance]:
    """Read ``provenance.json``; empty dict when absent or unreadable."""
    path = Path(report_dir) / PROVENANCE_FILENAME
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    periods = payload.get("periods") if isinstance(payload, dict) else None
    if not isinstance(periods, dict):
        return {}
    out: dict[str, PeriodProvenance] = {}
    for period, raw in periods.items():
        if not isinstance(raw, dict):
            continue
        out[period] = PeriodProvenance(
            period=str(raw.get("period") or period),
            source=str(raw.get("source") or SOURCE_FACTS),
            markdown=raw.get("markdown"),
            facts=raw.get("facts"),
            instance=raw.get("instance"),
        )
    return out
