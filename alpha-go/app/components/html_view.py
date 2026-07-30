"""Natural-state HTML reader — render an HTML-sourced filing (FEMSA MD&A) inline.

The PDF-less corpus docs (all FEMSA) were ingested from BMV XBRL MD&A **HTML**; the parsed
markdown is garbled, so instead we show the ORIGINAL filing HTML in its natural rendered state
(inline styles, base64-image tables) inside an iframe, with the searched terms highlighted and
scrolled to the first hit. Mirrors `pdf_view`: `html_path_for()` resolves the source and
`render()` returns ``False`` when there is none, so the caller can fall through.

The originals live OUTSIDE alpha-go, in the sibling `soft/` tree, and the manifest stores no path
to them — so the path is resolved by rule (see `html_path_for`). Only ~20 of the 29 FEMSA periods
have an HTML on disk; the rest resolve to ``None`` and the caller shows a plain note.
"""
from __future__ import annotations

import functools
import os
import re
import json
from pathlib import Path

_HIGHLIGHT = "#f5c518"          # AlphaSense-yellow, matches the PDF viewer


@functools.lru_cache(maxsize=1)
def _reports_root() -> Path:
    # this file: alpha-go/app/components/html_view.py → parents[2] == alpha-go; the reports tree
    # lives in the sibling `soft/` project one level up from alpha-go.
    return Path(__file__).resolve().parents[2].parent / "soft" / "data" / "reports"


def html_path_for(doc_id: str) -> "str | None":
    """Original MD&A HTML for a doc, or ``None`` (no source on disk).

    Rule: ``<reports_root>/<company>/xbrl/<TICKER>_<period>_mdna.html`` with ``TICKER =
    company.upper()`` (holds for the FEMSA docs — the only HTML-sourced set). Guarded by
    ``os.path.exists``.
    """
    root = Path(__file__).resolve().parents[2]
    try:
        docs = json.loads((root / "data" / "corpus" / "manifest.json").read_text(encoding="utf-8"))["documents"]
        row = next((d for d in docs if d.get("doc_id") == doc_id), None)
        raw = None
        if row and row.get("original_format") in {"html", "htm"}:
            raw = row.get("original_path")
        if not raw and row and row.get("source_format") in {"html", "htm"}:
            raw = row.get("source_path")
        if raw:
            p = Path(raw)
            if not p.is_absolute():
                p = next((candidate for candidate in (p, root / p,
                                                       root / "data" / "corpus" / p)
                          if candidate.exists()), root / p)
            if p.exists():
                return str(p)
    except (OSError, ValueError, KeyError):
        pass
    if "/" not in doc_id:
        return None
    company, period = doc_id.split("/", 1)
    p = _reports_root() / company / "xbrl" / f"{company.upper()}_{period}_mdna.html"
    return str(p) if p.exists() else None


@functools.lru_cache(maxsize=32)
def _highlighted_html(path: str, mtime: float, terms: "tuple[str, ...]") -> "tuple[str, int]":
    """Read the original HTML (latin-1) and wrap literal term matches in ``<mark>``; cached.

    Highlighting is done at the **text-node** level with BeautifulSoup so tags/attributes are never
    corrupted (skips ``<script>``/``<style>``). Numeric tables are rasterized base64 images, so only
    prose hits highlight — that's expected. Returns ``(html, n_highlights)``.
    """
    from bs4 import BeautifulSoup, NavigableString

    from src.qa.sentiment import sentiment_color

    # The files declare iso-8859-1 in a meta tag but are in fact UTF-8 encoded; read UTF-8 first
    # (correct accents) and only fall back to latin-1 for any genuinely 8859-1 file.
    try:
        raw = open(path, encoding="utf-8").read()
    except UnicodeDecodeError:
        raw = open(path, encoding="latin-1").read()
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup.find_all(["script", "style", "iframe", "object", "embed", "form"]):
        node.decompose()
    for node in soup.find_all(True):
        for attr in list(node.attrs):
            if attr.lower().startswith("on"):
                del node.attrs[attr]

    pats = [t for t in terms if t and t.strip()]
    n = 0
    if pats:
        # Longest-first so a phrase wins over its constituent words; literal, case-insensitive.
        rx = re.compile("|".join(re.escape(t) for t in sorted(pats, key=len, reverse=True)),
                        re.IGNORECASE)
        for node in list(soup.find_all(string=True)):
            if node.parent.name in ("script", "style", "mark"):
                continue
            s = str(node)
            if not rx.search(s):
                continue
            color = sentiment_color(s)             # tone of this passage → green/red/yellow box
            span = soup.new_tag("span")            # inline wrapper — safe on every bs4 version
            last = 0
            for m in rx.finditer(s):
                if m.start() > last:
                    span.append(NavigableString(s[last:m.start()]))
                mark = soup.new_tag("mark")
                mark["style"] = f"background:{color};color:#000"
                mark.string = m.group(0)
                span.append(mark)
                last = m.end()
                n += 1
            if last < len(s):
                span.append(NavigableString(s[last:]))
            node.replace_with(span)

    # Readable white canvas (the filing assumes a white page) + jump to the first hit on load.
    jump = ("<script>window.addEventListener('load',function(){"
            "var m=document.querySelector('mark');"
            "if(m){m.scrollIntoView({block:'center'});}});</script>")
    doc = f"<div style='background:#fff;color:#111;padding:12px 16px'>{soup}</div>{jump}"
    return doc, n


def render(st, *, doc_id: str, terms: "list[str]", key: str) -> bool:
    """Render the original filing HTML inline (iframe), hits highlighted. ``False`` if no source."""
    path = html_path_for(doc_id)
    if not path:
        return False

    doc, n = _highlighted_html(path, os.path.getmtime(path), tuple(terms))
    # ``components.v1.html`` was removed from Streamlit's supported surface after 2026-06-01.
    # ``st.iframe`` accepts sanitized inline HTML directly and preserves the isolated scrolling
    # reader plus our first-hit jump script.
    st.iframe(doc, height=850)
    period = doc_id.split("/", 1)[1] if "/" in doc_id else doc_id
    hl = f"{n} highlight(s) · " if n else ""
    st.caption(f"{hl}original filing HTML · {period} · {Path(path).name}")
    return True
