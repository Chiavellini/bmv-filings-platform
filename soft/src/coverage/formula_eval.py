"""A tiny, strict evaluator for the Excel formulas :mod:`src.sheets.valuation_sheet` emits.

``openpyxl`` stores formulas as strings and never computes them (``data_only=True`` only reads
values cached by a prior *Excel* save, which we never have). To math-audit the workbook we must
evaluate the formulas ourselves. The sheet only ever emits four shapes:

    =B12*B13                      market cap  (price × shares)
    =B14+B15+B16                  enterprise value  (mc + net debt + minority)
    =B14/B18                      a multiple  (numerator / denominator)
    =MEDIAN(C20,D20,E20)          peer-group median

So this evaluator supports exactly: number literals, A1 cell references (resolved recursively),
the binary operators ``+ - * /``, parentheses, and the single function ``MEDIAN``. **Anything
else raises** — an unrecognised token or function fails loud so a formula the auditor does not
understand can never silently pass the gate.
"""
from __future__ import annotations

import re
import statistics

from openpyxl.utils import column_index_from_string, get_column_letter


class FormulaError(Exception):
    """Raised for an unparseable/unsupported formula or a bad cell reference."""


class DivisionByZero(FormulaError):
    """A formula divided by zero — in Excel this renders as ``#DIV/0!``."""


_CELL_RE = re.compile(r"^\$?([A-Z]{1,3})\$?([0-9]+)$")
_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?) |
        (?P<func>[A-Za-z]+)\s*\(               |
        (?P<cell>\$?[A-Z]{1,3}\$?[0-9]+)       |
        (?P<op>[+\-*/])                        |
        (?P<lp>\()                             |
        (?P<rp>\))                             |
        (?P<comma>,)
    )
    """,
    re.VERBOSE,
)


def _norm(coord: str) -> str:
    """Normalise ``$B$12``/``b12`` → ``B12``."""
    m = _CELL_RE.match(coord.upper())
    if not m:
        raise FormulaError(f"bad cell reference {coord!r}")
    col, rownum = m.group(1), m.group(2)
    return f"{get_column_letter(column_index_from_string(col))}{int(rownum)}"


def _tokenize(expr: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    i = 0
    n = len(expr)
    while i < n:
        if expr[i].isspace():
            i += 1
            continue
        m = _TOKEN_RE.match(expr, i)
        if not m or m.end() == i:
            raise FormulaError(f"unexpected token near {expr[i:i + 12]!r} in {expr!r}")
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "func":
            if val.upper() != "MEDIAN":
                raise FormulaError(f"unsupported function {val!r}")
            toks.append(("func", val.upper()))
            toks.append(("lp", "("))
        else:
            toks.append((kind, val))
        i = m.end()
    return toks


class _Evaluator:
    """Reads a worksheet once, then resolves any cell to a numeric value (recursing through
    formulas, with cycle detection and memoisation)."""

    def __init__(self, ws):
        self._raw: dict[str, object] = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None:
                    self._raw[cell.coordinate] = cell.value
        self._cache: dict[str, float | None] = {}
        self._stack: set[str] = set()

    # -- public -------------------------------------------------------------
    def formula_cells(self) -> dict[str, str]:
        return {c: v for c, v in self._raw.items()
                if isinstance(v, str) and v.startswith("=")}

    def cell(self, coord: str) -> float | None:
        coord = _norm(coord)
        if coord in self._cache:
            return self._cache[coord]
        if coord in self._stack:
            raise FormulaError(f"circular reference at {coord}")
        raw = self._raw.get(coord)
        self._stack.add(coord)
        try:
            if raw is None:
                out = None
            elif isinstance(raw, str) and raw.startswith("="):
                out = self._eval(raw[1:])
            elif isinstance(raw, (int, float)):
                out = float(raw)
            else:
                # a text label / tag cell — not numeric
                out = None
        finally:
            self._stack.discard(coord)
        self._cache[coord] = out
        return out

    # -- expression evaluation (shunting-yard → RPN → eval) -----------------
    _PREC = {"+": 1, "-": 1, "*": 2, "/": 2}

    def _eval(self, expr: str) -> float | None:
        toks = _tokenize(expr)
        output: list[tuple[str, str]] = []
        ops: list[tuple[str, str]] = []
        for kind, val in toks:
            if kind in ("num", "cell"):
                output.append((kind, val))
            elif kind == "func":
                ops.append((kind, val))
            elif kind == "comma":
                while ops and ops[-1][0] != "lp":
                    output.append(ops.pop())
            elif kind == "op":
                while (ops and ops[-1][0] == "op"
                       and self._PREC[ops[-1][1]] >= self._PREC[val]):
                    output.append(ops.pop())
                ops.append((kind, val))
            elif kind == "lp":
                ops.append((kind, val))
            elif kind == "rp":
                while ops and ops[-1][0] != "lp":
                    output.append(ops.pop())
                if not ops:
                    raise FormulaError(f"mismatched parens in {expr!r}")
                ops.pop()  # discard the '('
                if ops and ops[-1][0] == "func":
                    output.append(ops.pop())
        while ops:
            top = ops.pop()
            if top[0] == "lp":
                raise FormulaError(f"mismatched parens in {expr!r}")
            output.append(top)
        return self._eval_rpn(output, expr)

    def _eval_rpn(self, rpn: list[tuple[str, str]], expr: str) -> float | None:
        stack: list = []
        for kind, val in rpn:
            if kind == "num":
                stack.append(float(val))
            elif kind == "cell":
                stack.append(self.cell(val))
            elif kind == "func":  # only MEDIAN
                vals = [v for v in stack if v is not None]
                stack.clear()
                stack.append(statistics.median(vals) if vals else None)
            elif kind == "op":
                if len(stack) < 2:
                    raise FormulaError(f"malformed expression {expr!r}")
                b, a = stack.pop(), stack.pop()
                stack.append(self._apply(val, a, b))
            else:
                raise FormulaError(f"unexpected RPN token {val!r}")
        if len(stack) != 1:
            raise FormulaError(f"malformed expression {expr!r}")
        return stack[0]

    @staticmethod
    def _apply(op: str, a, b):
        if a is None or b is None:
            return None
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                raise DivisionByZero("division by zero")
            return a / b
        raise FormulaError(f"unsupported operator {op!r}")


def evaluate_worksheet(ws) -> tuple[dict[str, float | None], dict[str, str]]:
    """Return ``(values, formulas)`` where ``values`` maps every formula-cell coordinate to its
    evaluated number (or raises), and ``formulas`` maps the same coordinates to their raw string.
    """
    ev = _Evaluator(ws)
    formulas = ev.formula_cells()
    values = {coord: ev.cell(coord) for coord in formulas}
    return values, formulas


def make_evaluator(ws) -> _Evaluator:
    return _Evaluator(ws)
