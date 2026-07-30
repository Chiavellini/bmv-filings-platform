"""Table reconstruction in parse_pdf: pure helpers + char-offset round-trip on a real PDF."""
from __future__ import annotations

import pytest

from src.parse import parse_pdf as P
from src.shared.paths import PROJECT_ROOT


# --- pure helpers -------------------------------------------------------------------------

def test_clean_grid_drops_empty_rows_and_cols():
    grid = [["Net Sales", "", "2Q22", None],
            [None, "", "", ""],                       # all-empty row → dropped
            ["Mexico", "", "31,768", ""]]             # col 1 all-empty → dropped
    rows = P._clean_grid(grid)
    assert rows == [["Net Sales", "2Q22"], ["Mexico", "31,768"]]


def test_is_reconstructable_accepts_financial_table():
    rows = [["Net Sales", "2Q22", "2Q21"],
            ["Mexico", "31,768", "26,119"],
            ["EAA", "8,906", "8,289"]]
    assert P._is_reconstructable(rows) is True


def test_is_reconstructable_rejects_prose_block():
    # A CEO quote misdetected as a table: multi-word cells, almost no numeric cells.
    rows = [["Our Net Sales and", "reached historic", "levels for"],
            ["a second quarter", "despite higher", "commodity prices"]]
    assert P._is_reconstructable(rows) is False


def test_is_reconstructable_rejects_header_fragment():
    rows = [["2022", "", ""], ["", "Millions", "%"]]   # no numeric DATA rows
    assert P._is_reconstructable(rows) is False


def test_render_pipe_table_alignment_and_no_trailing_ws():
    rows = [["Net Sales", "2Q22", "2Q21"],
            ["Mexico", "31,768", "26,119"]]
    out = P._render_pipe_table(rows)
    lines = out.splitlines()
    assert lines[0].startswith("| ") and all(ln.endswith("|") for ln in lines)
    # every line ends in '|' with NO trailing whitespace → tidy()'s rstrip is a no-op
    assert all(ln == ln.rstrip() for ln in lines)
    assert set(lines[1]) <= {"|", "-"}               # separator row
    assert len({len(ln) for ln in lines}) == 1        # columns aligned (equal width)


def test_render_pipe_table_escapes_pipe_in_cell():
    out = P._render_pipe_table([["a|b", "1"], ["c", "2"]])
    # a literal '|' inside a cell must not create a phantom column
    assert "a/b" in out and "a|b" not in out


def test_page_render_interleaves_prose_and_table(monkeypatch):
    class _FakePage:
        bbox = (0.0, 0.0, 100.0, 100.0)

        def crop(self, box):
            x0, top, x1, bottom = box
            band = _FakeBand("PROSE-ABOVE" if top < 10 else "PROSE-BELOW")
            return band

    class _FakeBand:
        def __init__(self, text):
            self._text = text

        def extract_text(self, layout=True):
            return self._text

    rows = [["Net Sales", "2Q22"], ["Mexico", "31,768"], ["EAA", "8,906"]]
    monkeypatch.setattr(P, "_filtered_page", lambda page: page)
    monkeypatch.setattr(P, "_page_tables", lambda page: [((0.0, 40.0, 100.0, 60.0), rows)])
    out = P._page_render(_FakePage())
    # prose above, then the pipe table, then prose below — in order, no duplication
    assert out.index("PROSE-ABOVE") < out.index("| Net Sales") < out.index("PROSE-BELOW")
    assert out.count("| EAA") == 1


# --- round-trip on a real PDF (char-offset integrity) -------------------------------------

# The corpus lives in the shared parent estate, one level above alpha-go — not under
# ``paths.REPORTS_DIR``, which resolves to alpha-go's own ``data/reports``. Same idiom as
# scripts/reparse_local.py.
_BIMBO_PDF = PROJECT_ROOT.parent / "data" / "reports" / "bimbo" / "2022-2T.pdf"


@pytest.mark.skipif(not _BIMBO_PDF.exists(), reason="source PDF not present")
def test_real_pdf_reconstructs_table_and_preserves_offsets():
    from src.index.chunker import chunk_document

    md, _blocks = P.parse_pdf(str(_BIMBO_PDF))
    # a real reconstructed financial table is present
    assert "| Net Sales" in md and "| EAA" in md
    # the EAA→Europe footnote bridge survives parsing
    assert "includes operations in Europe" in md
    # every chunk slices back to identical text (the viewer/FTS char-offset invariant)
    chunks = chunk_document("bimbo/2022-2T", md)
    assert chunks and all(md[c.char_start:c.char_end] == c.text for c in chunks)
