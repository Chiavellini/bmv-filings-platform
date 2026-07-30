#!/usr/bin/env python3
"""Parser de PDFs (digitales) a texto plano alineado, por página, en Markdown.

Convierte reportes trimestrales a un .md legible preservando:
- el numero de pagina (para citar donde esta cada cifra),
- la ALINEACION espacial de los estados financieros (columnas de cifras intactas),
- los numeros exactos: comas, puntos, parentesis de negativos, $, %, tal cual.

Estrategia: pdfplumber `extract_text(layout=True)`, que reproduce la posicion del texto
en la pagina usando espacios. En estos reportes (sin bordes de tabla reales) resulta mas
fiable que reconstruir tablas Markdown: las columnas numericas quedan alineadas y las
cifras se leen sin ambiguedad.

Uso:
    python3 parse_pdf.py reporte_q1.pdf            # genera reporte_q1.md
    python3 parse_pdf.py *.pdf                      # procesa varios
    python3 parse_pdf.py carpeta/ -o salida/        # carpeta entera a un directorio
    python3 parse_pdf.py reporte.pdf --jsonl        # ademas genera bloques .jsonl por pagina
"""

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("Falta pdfplumber. Instala con: python3 -m pip install pdfplumber")


# ---------------------------------------------------------------------------
# Structured parse metadata (Tier-2 scale + section scoping). Computed at parse
# time and carried on PeriodSource.doc; the flat markdown stays the primary
# contract, so callers that don't ask for meta are unaffected.
# ---------------------------------------------------------------------------

@dataclass
class DocMeta:
    scale: float = 1.0                  # monetary multiplier → metric's expected unit
    scale_source: str = "default"       # "header" | "default"
    sections: list = field(default_factory=list)   # [(name, header_regex), ...]


# ---- Stage 1: superscript / footnote stripping --------------------------------

def _superscript_keys_to_drop(page) -> set:
    """Identify inline raised/small digit glyphs (footnote markers) to remove.

    pdfplumber's layout text flattens a superscript (``49¹`` or the lone ``¹`` in
    ``192,119 ¹ 39,062``) into an inline digit that corrupts numeric extraction.
    A glyph is dropped only when it is a digit, notably SMALLER than its line's
    body text, AND raised above that line's baseline — so legitimate small
    sub-headers (not raised) and digits inside a number (not isolated/small)
    survive. Returns a set of (x0, top, text) keys.
    """
    drop: set = set()
    try:
        lines = page.extract_text_lines(strip=False, return_chars=True)
    except Exception:
        return drop
    for line in lines:
        chars = [c for c in line.get("chars", []) if c.get("text", "").strip()]
        if len(chars) < 2:
            continue
        sizes = sorted(c["size"] for c in chars)
        body = sizes[len(sizes) // 2]          # median size = body text
        if body <= 0:
            continue
        body_bottom = max((c["bottom"] for c in chars if c["size"] >= body * 0.9),
                          default=None)
        if body_bottom is None:
            continue
        for c in chars:
            if (c["text"].isdigit()
                    and c["size"] <= body * 0.72
                    and c["bottom"] <= body_bottom - 0.10 * body):
                drop.add((round(c["x0"], 1), round(c["top"], 1), c["text"]))
    return drop


def _page_text(page) -> str:
    """Layout text for a page, with inline superscript footnote glyphs removed."""
    try:
        drop = _superscript_keys_to_drop(page)
        if drop:
            def keep(obj):
                if obj.get("text") is None:        # non-char objects: keep
                    return True
                return (round(obj.get("x0", 0), 1),
                        round(obj.get("top", 0), 1),
                        obj.get("text")) not in drop
            page = page.filter(keep)
        return page.extract_text(layout=True) or ""
    except Exception:
        try:
            return page.extract_text(layout=True) or ""
        except Exception:
            return ""


# ---- Stage 1.5: conservative financial-table reconstruction ------------------

_NUMERIC_CELL = re.compile(
    r"^[\s$€£¥₹()\-+%]*\d[\d,.'\s]*(?:%|[A-Za-z]{0,4})?[\s$€£¥₹()\-+]*$"
)


def _clean_grid(grid):
    """Normalize an extracted table and remove wholly empty rows and columns."""
    rows = [["" if cell is None else str(cell).strip() for cell in (row or [])]
            for row in (grid or [])]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    keep = [i for i in range(width) if any(row[i] for row in rows)]
    return [[row[i] for i in keep] for row in rows]


def _is_numeric_cell(value: str) -> bool:
    value = (value or "").strip()
    return bool(value and _NUMERIC_CELL.match(value) and any(ch.isdigit() for ch in value))


def _is_reconstructable(rows) -> bool:
    """Return True only for a table with actual numeric data, not a prose block/header."""
    rows = _clean_grid(rows)
    if len(rows) < 3 or max((len(row) for row in rows), default=0) < 2:
        return False
    data = rows[1:]
    numeric = sum(_is_numeric_cell(cell) for row in data for cell in row)
    data_rows = sum(any(_is_numeric_cell(cell) for cell in row) for row in data)
    labels = sum(bool(row[0]) and not _is_numeric_cell(row[0]) for row in data)
    return data_rows >= 2 and labels >= 1 and numeric >= 2


def _render_pipe_table(rows) -> str:
    """Render aligned Markdown table rows with safe, deterministic cell widths."""
    rows = _clean_grid(rows)
    if not rows:
        return ""
    rows = [[cell.replace("|", "/") for cell in row] for row in rows]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]

    def line(row):
        return "| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |"

    separator = "|" + "|".join("-" * (width + 2) for width in widths) + "|"
    return "\n".join([line(rows[0]), separator] + [line(row) for row in rows[1:]])


def _filtered_page(page):
    """Hook for page-level filtering; kept separate for parser tests and future OCR filters."""
    return page


def _page_tables(page):
    """Return ``[(bbox, rows)]`` for tables pdfplumber can locate on a page."""
    try:
        return [(table.bbox, table.extract()) for table in page.find_tables()]
    except Exception:
        return []


def _crop_text(page, bbox) -> str:
    try:
        return tidy(page.crop(bbox).extract_text(layout=True) or "")
    except Exception:
        return ""


def _page_render(page) -> str:
    """Interleave prose and conservative pipe-table renderings without duplication."""
    page = _filtered_page(page)
    tables = []
    for bbox, rows in sorted(_page_tables(page), key=lambda item: item[0][1]):
        cleaned = _clean_grid(rows)
        if cleaned and _is_reconstructable(cleaned):
            tables.append((bbox, cleaned))
    if not tables:
        return tidy(_page_text(page))

    x0, top0, x1, bottom0 = page.bbox
    cursor = top0
    parts = []
    for (tx0, ttop, tx1, tbottom), rows in tables:
        if ttop > cursor:
            prose = _crop_text(page, (x0, cursor, x1, ttop))
            if prose:
                parts.append(prose)
        parts.append(_render_pipe_table(rows))
        cursor = max(cursor, tbottom)
    if cursor < bottom0:
        prose = _crop_text(page, (x0, cursor, x1, bottom0))
        if prose:
            parts.append(prose)
    return tidy("\n\n".join(part for part in parts if part))


# ---- Stage 2: monetary scale detection ----------------------------------------

def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()

# CAPTION-only unit detection (relative to a millions-stored registry). We match
# ONLY explicit table captions — "(million pesos)", "cifras en millones/miles" —
# NOT loose prose like "...ascendieron a $6,184 millones de pesos", which states
# the figure's unit, not the table's. (lacomer is the trap: prose says millones,
# but its income table prints full pesos.) This stays a DIAGNOSTIC: the printed→
# stored mapping is company-specific, so it does not override the config scale.
_SCALE_RULES = [
    (re.compile(r"cifras en miles|\(\s*(?:miles|thousand)s?\s+(?:de\s+)?pesos\s*\)|\(000\)"), 0.001),
    (re.compile(r"cifras en millones|\(\s*(?:millones|million)s?\s+(?:de\s+)?(?:pesos|\$?mxn)\s*\)"), 1.0),
]


def detect_scale(text: str) -> tuple:
    """(scale, source) detected from an explicit table caption (millions-relative).

    Diagnostic only — ``scale_source='default'`` when no caption is found, and
    callers keep their per-company config scale either way.
    """
    folded = _fold(text)
    for rx, scale in _SCALE_RULES:
        if rx.search(folded):
            return scale, "header"
    return 1.0, "default"


# ---- Stage 3: generic section anchors -----------------------------------------
# Best-effort segment-region detection for companies without an explicit
# `sections:` config. Company configs override these (config wins). Names match
# the roles the extractor scopes against; "consolidated" is the implicit region
# before the first anchor (handled by extract_metrics._split_sections).
_GENERIC_SECTION_ANCHORS = [
    ("mexico", r"M[eé]xico\s+main\s+figures\s+are|Principales\s+cifras\s+de\s+M[eé]xico"),
    ("cam", r"Central\s+America\s+main\s+figures\s+are|Principales\s+cifras\s+de\s+Centroam[eé]rica"),
    ("appendix", r"Appendix\s+\d+\s*:|Ap[eé]ndice\s+\d+\s*:"),
]


def detect_sections(text: str) -> list:
    """Return [(name, header_regex), ...] for generic segment regions present."""
    return [(name, pat) for name, pat in _GENERIC_SECTION_ANCHORS
            if re.search(pat, text, re.IGNORECASE)]


def tidy(text):
    """Quita espacios al final de cada linea, recorta lineas en blanco al inicio/fin
    y colapsa corridas de 3+ lineas en blanco a una sola. No toca el contenido."""
    if not text:
        return ""
    lines = [ln.rstrip() for ln in text.splitlines()]
    out = []
    blanks = 0
    for ln in lines:
        if ln == "":
            blanks += 1
            if blanks <= 1:
                out.append("")
        else:
            blanks = 0
            out.append(ln)
    # recorta blancos al inicio y al final
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def parse_pdf(pdf_path, with_meta=False):
    """Devuelve (markdown_str, blocks) para un PDF.

    Con ``with_meta=True`` devuelve ``(markdown_str, blocks, DocMeta)``: la
    metadata estructurada (escala monetaria, secciones) que la capa de búsqueda
    puede aprovechar. El texto plano sigue siendo idéntico al contrato previo.
    """
    md_parts = [f"# {Path(pdf_path).name}\n"]
    blocks = []
    page_texts = []

    with pdfplumber.open(pdf_path) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            md_parts.append(f"\n\n===== Página {pageno} =====\n\n")
            text = _page_render(page)          # prose + reconstructable tables

            if text:
                md_parts.append(text + "\n")
                blocks.append({"page": pageno, "type": "text", "content": text})
                page_texts.append(text)
            else:
                # KNOWN GAP (follow-up, out of scope for the corpus-wide literal-mention scan):
                # scanned/image pages yield no extractable text, so this placeholder — and NOT
                # the page's real words — is what lands in the corpus. Such pages are therefore
                # invisible to keyword search and to `doc_matches.find_matches`; they are left as
                # a visible marker (an OCR pass would fill them in) rather than silently dropped.
                md_parts.append(
                    f"<!-- página {pageno} sin texto: posible escaneo/imagen (requiere OCR) -->\n"
                )
                blocks.append({"page": pageno, "type": "empty", "content": ""})

    markdown = "".join(md_parts)
    if not with_meta:
        return markdown, blocks

    scale, scale_source = detect_scale("\n".join(page_texts))
    doc = DocMeta(scale=scale, scale_source=scale_source,
                  sections=detect_sections("\n".join(page_texts)))
    return markdown, blocks, doc


def resolve_inputs(paths):
    """Expande carpetas a sus PDFs; mantiene archivos PDF tal cual."""
    pdfs = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            pdfs.extend(sorted(path.glob("*.pdf")))
        elif path.suffix.lower() == ".pdf":
            pdfs.append(path)
        else:
            print(f"  (omitido, no es PDF): {p}", file=sys.stderr)
    return pdfs


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("inputs", nargs="+", help="PDF(s) o carpeta(s) con PDFs")
    ap.add_argument("-o", "--out", help="Directorio de salida (def: junto al PDF)")
    ap.add_argument("--jsonl", action="store_true",
                    help="Genera además un .jsonl con un bloque por página")
    args = ap.parse_args()

    pdfs = resolve_inputs(args.inputs)
    if not pdfs:
        sys.exit("No se encontraron PDFs en las rutas dadas.")

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    for pdf in pdfs:
        print(f"Procesando {pdf} ...", file=sys.stderr)
        try:
            md, blocks = parse_pdf(pdf)
        except Exception as e:
            print(f"  ERROR en {pdf}: {e}", file=sys.stderr)
            continue

        dest = (out_dir or pdf.parent) / (pdf.stem + ".md")
        dest.write_text(md, encoding="utf-8")
        print(f"  -> {dest}", file=sys.stderr)

        if args.jsonl:
            jdest = (out_dir or pdf.parent) / (pdf.stem + ".jsonl")
            with jdest.open("w", encoding="utf-8") as f:
                for b in blocks:
                    f.write(json.dumps(b, ensure_ascii=False) + "\n")
            print(f"  -> {jdest}", file=sys.stderr)


if __name__ == "__main__":
    main()
