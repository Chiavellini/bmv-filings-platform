# soft — Soft Coverage Engine

A self-contained equity-research **coverage-summary generator**. You give it a company; it
processes the company's filings plus a Bloomberg data pack and emits a themed Excel workbook.
Sheet 1 is **Fundamental Valuation Metrics** (P/E, P/BV, EV/EBITDA, historical multiples,
sum-of-the-parts, replacement value) alongside a financial-analysis block (revenue vs
peers/industry, profitability, FCF/liquidity/inventory, ROIC/ROA/ROE) and a macro/sector block.
More sheets get added as themes over time.

`soft/` is a sibling of `alpha-go/`: it **vendors** (copies) the reusable parent infra under its
own `src/` and never imports from the parent. See [docs/REUSE_MAP.md](docs/REUSE_MAP.md).

## Environment

The certified interpreter is Python **3.11.13**, recorded in `.python-version`.
Build a repository-local environment rather than using a system or personal
shared interpreter:

```bash
cd soft
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

## Why "hybrid"

Valuation needs **market data + consensus estimates** that only a Bloomberg terminal has, while
fundamentals (EBITDA, book value, FCF, ROIC inputs) come from filings. Every number carries a
source tag:

| tag | origin |
|-----|--------|
| `[bbg]`    | filled Bloomberg template (`data/bloomberg/<slug>.csv`) |
| `[filing]` | vendored extraction cascade over cached filings |
| `[calc]`   | live Excel formula referencing the input cells |
| `[macro]`  | macro/sector series (part of the BBG pack) |

`[bbg]` and `[filing]` land as **hard input cells**; multiples/ratios are **live Excel formulas**,
so editing a price re-prices the whole sheet.

## Quick start (WALMEX pilot)

```bash
cd soft
.venv/bin/python scripts/vendor_sync.py --check                 # provenance vs parent (no writes)
.venv/bin/python -m pytest -m "not network and not model"       # smoke + unit tests

# 1) emit the blank Bloomberg template for the metrics the spec needs
.venv/bin/python scripts/emit_bloomberg_template.py inputs/walmex.md
#    -> inputs/walmex.bloomberg.csv  (fill from the terminal, save to data/bloomberg/walmex.csv)

# 2) build the coverage workbook
.venv/bin/python scripts/build_coverage.py inputs/walmex.md
#    -> outputs/Walmex/{excel/Walmex.xlsx, csv/walmex_coverage.csv, validation/walmex_validation.md}
```

The filing corpus is data, not source code, and is not stored in Git. Attach an
exported corpus at `data/reports/` or set `PDFS_REPORTS_DIR` to its location.
The unit suite runs without it and cleanly skips corpus-only golden checks. The
WALMEX build itself requires cached filings under `data/reports/walmex/` (or the
equivalent directory beneath `PDFS_REPORTS_DIR`) and then needs no network.

## Layout

- `src/{download,parse,extract,shared,model,excel,eval}/` — **vendored** (do not edit; re-sync via
  `scripts/vendor_sync.py --sync`).
- `src/bloomberg/` — template emit + ingest (the `[bbg]` side).
- `src/coverage/` — spec parsing, filings fundamentals, valuation math, peer cross-section.
- `src/sheets/` — one module per workbook sheet.
- `inputs/<slug>.md` — the coverage spec (peers, blocks, macro rows).
- `configs/soft.yaml` + vendored `configs/*.yaml` — engine + per-company config.

## Documentation

Start with **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — the canonical reference (data
contracts, file schemas, module map, template registry, how to extend). Then:

| doc | what |
|-----|------|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | data contracts, schemas, module map, extension guide (**read this first**) |
| [ONBOARDING.md](docs/ONBOARDING.md) | step-by-step recipe to add one company |
| [UNIVERSE.md](docs/UNIVERSE.md) | the coverage roster: slug · clave · template · sector · XBRL |
| [VISION.md](docs/VISION.md) | what the engine is and its non-goals |
| [REUSE_MAP.md](docs/REUSE_MAP.md) | what is vendored from the parent repo and what is net-new |
| [DECISIONS.md](docs/DECISIONS.md) | the load-bearing design decisions |
| [PHASES.md](docs/PHASES.md) | build phases + the data-flow diagram |
| [ROADMAP.md](docs/ROADMAP.md) | known limitations and next steps |
