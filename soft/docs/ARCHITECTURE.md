# soft — Architecture & Schema Reference

The canonical reference for the Soft Coverage Engine. It documents every data contract, file
format, module responsibility, and the extension model, so a future collaborator (human or agent)
can understand and extend the project without reading all the code.

- **What it is**: give it a company → it produces an equity-research coverage-summary Excel
  workbook. Sheet 1 = fundamental valuation metrics, per a sector template.
- **How data flows**: fundamentals from **BMV XBRL** (no per-company tuning) + market data /
  estimates / ratios from a **Bloomberg fill-in template** → reconciled → workbook.
- **Self-contained**: vendors the parent repo's infra by copy (see [REUSE_MAP.md](REUSE_MAP.md)).

---

## 1. Directory map

```
soft/
├── pyproject.toml            # pythonpath=["."]; self-contained import root
├── configs/
│   ├── soft.yaml             # engine config: per-template fundamentals lists
│   ├── <slug>.yaml           # per-company: name/ticker/unit + ir_website.xbrl_ticker
│   └── *.yaml                # VENDORED parent configs (walmex.yaml, metric_search.yaml, …)
├── inputs/
│   ├── <slug>.md             # coverage SPEC (template, peers, macro) — human-authored
│   └── <slug>.bloomberg.csv  # EMITTED fill-in template (blank; user pastes BBG values)
├── data/
│   ├── reports/<slug>/xbrl/  # cached BMV XBRL facts (primed by fetch_xbrl_batch.py)
│   └── bloomberg/<slug>.csv  # the FILLED Bloomberg pack (user-provided)
├── outputs/<Name>/{excel,csv,validation}/   # generated deliverables
├── src/
│   ├── bloomberg/  schema.py · template.py · ingest.py      # the [bbg] side
│   ├── coverage/   spec.py · fundamentals.py · valuation.py · peers.py · validate.py
│   ├── sheets/     valuation_sheet.py                        # workbook renderer
│   └── {download,parse,extract,shared,model,excel,eval}/     # VENDORED (do not edit)
├── scripts/  gen_universe.py · emit_bloomberg_template.py · fetch_xbrl_batch.py · build_coverage.py · vendor_sync.py
├── docs/     ARCHITECTURE.md (this) · VISION · REUSE_MAP · PHASES · DECISIONS · ROADMAP · ONBOARDING · UNIVERSE
└── tests/
```

---

## 2. Data flow

```
inputs/<slug>.md ──parse_spec──▶ CoverageSpec ──┬─▶ emit_bloomberg_template ─▶ inputs/<slug>.bloomberg.csv
                                                 │        (user fills) ─▶ data/bloomberg/<slug>.csv
configs/<slug>.yaml (xbrl_ticker) ──────────────┤                              │ load_pack
                                                 │                              ▼
data/reports/<slug>/xbrl ──load_fundamentals──▶ Fundamentals ──┐          BloombergPack
                                                                ▼          │
                                              build_model(spec, fundamentals, pack) ─▶ CoverageModel
                                                                ▼
                    build_valuation_workbook + write_csv + write_report
                                                                ▼
                          outputs/<Name>/{excel/<Name>.xlsx, csv/<slug>_coverage.csv, validation/<slug>_validation.md}
```

Entry point: `scripts/build_coverage.py inputs/<slug>.md`.

---

## 3. Source-tagging model (native-first)

**Bloomberg is the last resort.** Resolve every cell in order, stop at the first hit:

| order | tag | origin |
|-------|-----|--------|
| 1 | `[filing]` | BMV XBRL fact (subject **and peers**) via the vendored cascade |
| 2 | `[calc]` | native derivation — EBITDA=EBIT+D&A, tangible book=equity−intangibles−goodwill, shares=NI÷EPS, FCF, ROE/ROA/margins (`fundamentals._derive_native`) |
| 3 | `[price]` | Yahoo Finance `<SYMBOL>.MX` (`download/market_data.py`) — last + FY-close |
| 4 | `[macro]` | Banxico SIE / INEGI (`download/macro.py`) — free token via env |
| 5 | `[bbg]` | the filled **residual** template — only what 1–4 couldn't supply |

The native layer (`coverage/native.py::build_native_pack`) assembles 1–4 into a `BloombergPack`;
`merge_packs` overlays the residual Bloomberg pack (native wins). `build_coverage` then re-emits
`inputs/<slug>.bloomberg.csv` via `required_rows(spec, filled=…)` so it contains **only the
residual** (net debt, forward estimates, dividend, bank/REIT operating ratios, unresolved prices).

**Reconciliation** (`valuation._reconcile`): use a `[filing]`/`[calc]` value only if it passes a
scale-plausibility guard (`_plausible` vs revenue); otherwise fall back to `[bbg]`. Guard is disabled
for banks (assets ≫ revenue). Annual (financial-sector) XBRL uses a latest-full-year duration
fallback (`fundamentals._latest_duration_facts`) so bank NI/EPS/revenue come natively.

---

## 4. Data contracts (schemas)

### 4.1 Coverage spec — `inputs/<slug>.md`  (parsed by `coverage/spec.py::parse_spec`)

Markdown. **Slug = the H1 name slugified** (`Grupo Mexico` → `grupo_mexico`); it keys the config,
reports cache, Bloomberg pack, and output dir.

```markdown
# <Company Name>          ← H1 → display name & slug (required)
Ticker: <TICKER MM>        ← optional, cosmetic
IR: <url>                  ← optional

## Settings                ← key: value lines
- currency: MXN
- units: millions
- history_years: 5
- template: industrial | financials | reit     ← selects the default block set
- peer_currency: mixed     ← optional; "mixed" suppresses absolute cross-company rows

## Peers                   ← "- <display> {<peer_slug>}"; peers are compared as columns
- SCCO US {scco}

## Segments                ← optional; only used by opt-in sum_of_the_parts
- Mexico {mexico}

## Macro                   ← "- <label> {<key>}"; one row each, filled from the pack
- Mexico GDP growth, % {gdp_growth}
```

`CoverageSpec` fields: `slug, name, ticker, ir_url, currency, units, history_years, template,
peer_currency, blocks[], peers[Peer], segments[Labelled], macro[Labelled]`. An explicit `## Blocks`
section overrides the template default (`TEMPLATES` in spec.py).

### 4.2 Company config — `configs/<slug>.yaml`  (minimal, no custom extractor)

```yaml
company: {name: <Name>, ticker: "<TICKER MM>", currency: MXN, exchange: BMV, unit: millions}
tier_precedence: {default: [xbrl, bmv, prose, search, table, calc]}
ir_website: {xbrl_ticker: "<BMV CLAVE>"}   # quote if it contains '&' (e.g. "PE&OLES")
```
`ir_website.xbrl_ticker` is the trigger: the fundamentals adapter fetches that clave's XBRL. Older
tuned parent configs (walmex.yaml etc.) may carry richer extractor rules — soft uses them as-is.

### 4.3 Bloomberg template / pack — `inputs/<slug>.bloomberg.csv` → `data/bloomberg/<slug>.csv`

**Long-format CSV, one row per cell the engine needs from the terminal.** Emitted blank by
`emit_bloomberg_template.py`; the user fills the **`value`** column and saves it to
`data/bloomberg/<slug>.csv`. Emit and ingest are symmetric (`bloomberg/{template,ingest}.py`).

Columns:

| column | meaning |
|--------|---------|
| `entity` | subject slug, a peer slug, a segment key, a macro key, or the subject slug for history rows |
| `entity_kind` | `subject` · `peer` · `segment` · `macro` · `history` |
| `field` | the metric key (e.g. `px_last`, `ebitda_ltm`, `book_value`, `ffo`, `nim`) |
| `period` | empty except `history` rows (the fiscal year, e.g. `2024`) |
| `label` | human description (peer rows are prefixed with the peer's ticker) |
| `unit` | `price` · `shares` · `currency` · `pct` · `x` · `count` · `index` |
| `bbg_hint` | the Bloomberg field/override that produces the value (e.g. `PX_LAST`, `NET_INT_MARGIN`) |
| `value` | **← the only column you fill**; leave blank where XBRL already covers it |

Which fields appear is derived from the enabled blocks (`schema.required_rows`): a company only gets
the cells its template needs (industrial gets `net_debt`/`ebitda_ltm`; banks get `nim`/`cet1`; REITs
get `ffo`/`nav_ps`). Rules:
- **subject market data** (price, shares, net debt) and **estimates** (fwd EPS, dividend) — always Bloomberg.
- **subject fundamentals that XBRL supplies** (revenue, net income, equity, assets) — leave **blank**;
  Bloomberg only used as a fallback. **EBITDA** is not in XBRL → always fill it.
- **peer fundamentals** — always fill (soft does not extract peer filings).
- **bank/REIT ratios** (NIM, CET1, ROTE, efficiency, cost of risk, occupancy, LTV, cap rate, FFO,
  NAV, distribution yield) — always Bloomberg (not derivable from XBRL).
- **history rows** — the subject's price at each fiscal-year close (drives the mean-reversion band).

Ingest (`ingest.load_pack`) returns a `BloombergPack{subject{}, peers{slug:{}}, segment_multiples{},
history{year:px}, macro{}}` plus a `missing[]` list (blank cells) and non-fatal `warnings[]`.

### 4.4 Fundamentals — `coverage/fundamentals.py::load_fundamentals`

Runs the vendored cascade over BMV XBRL (`_extract_via_xbrl`, retries `include_annual` for
financial-sector issuers) → `Fundamentals{slug, frame(wide per-period), periods[], current_period,
ltm{key:value}, annual{year:{key:value}}, flow_keys, confidence}`. Flows (income/cashflow) roll up
to trailing-4-quarter LTM; stocks/counts take the latest. Period labels: `YYYY-NT` (quarter) or
`YYYY-FY`/`YYYY` (annual → fiscal-year-end).

### 4.5 Coverage model — `coverage/valuation.py::build_model`

`CoverageModel{slug, name, currency, units, current_period, blocks[Block], warnings[]}`, where
`Block{id, title, rows[Cell], cross_section?}` and `Cell{label, value, unit, source, note}`.
`cross_section` (peer columns) is attached to snapshot-style blocks. This model is the single
computed representation — the workbook, CSV, and validation report all read from it.

### 4.6 Outputs — `outputs/<Name>/`

- `excel/<Name>.xlsx` — Sheet 1. `[bbg]`/`[filing]` render as hard blue input cells; the industrial
  snapshot's headline multiples + peer medians are **live Excel formulas** (edit a price → re-price).
- `csv/<slug>_coverage.csv` — `block,label,value,unit,source,note` (the flattened model).
- `validation/<slug>_validation.md` — coverage sanity checks + accounting identities + blank-cell worklist.

---

## 5. Template registry

`spec.TEMPLATES` maps a template → its default block set. Each block id resolves to a builder in
`valuation._BUILDERS`. The three templates:

| template | for | default blocks | key multiples | fundamentals list (soft.yaml) |
|----------|-----|----------------|---------------|-------------------------------|
| `industrial` | miners, materials, telecom, food, retail, industrials | snapshot_multiples · historical_multiples · financial_analysis · macro_sector (opt-in: sum_of_the_parts, replacement_value) | P/E, EV/EBITDA, EV/Sales, P/BV, P/FCF | `industrial_fundamentals` |
| `financials` | banks, brokers, insurers, afores, exchange | bank_snapshot · bank_returns · bank_growth · bank_historical · macro_sector | P/E, **P/BV, P/TBV** (no EV/EBITDA) | `financials_fundamentals` |
| `reit` | FIBRAs, real estate | reit_snapshot · reit_metrics · reit_historical · macro_sector | **P/FFO, P/AFFO, P/NAV**, EV/EBITDA, distribution yield | `reit_fundamentals` |

### Adding a new template (the bank/REIT pattern)
1. `spec.py`: add `<X>_BLOCKS`, append to `KNOWN_BLOCKS`, register in `TEMPLATES`.
2. `peers.py`: add `MULTIPLES_<X>` + `company_multiples_<x>` (reuse `build_cross_section(multiples=,
   multiples_fn=)`).
3. `valuation.py`: write `block_<x>_*` builders (reuse `_reconcile`, `_band`, `build_cross_section`);
   register them in `_BUILDERS`.
4. `schema.py`: add a `_<X>` FieldSpec group gated to the new block ids (mark XBRL-derivable fields
   as subject+peer fallbacks; ratios as subject-only Bloomberg). Add block ids to shared
   `px_last`/`shares_out`/`px_fy_close` FieldSpecs if the blocks need market/history data.
5. `sheets/valuation_sheet.py`: cross-section blocks render automatically via
   `_render_cross_section_block`; extend `_bank_multiple_defs` to include `MULTIPLES_<X>`.
6. `soft.yaml`: add `<x>_fundamentals`; `build_coverage.py`: add the template→list branch.
7. Add `tests/test_<x>_valuation.py` (math + template selector + block set).

---

## 6. Module responsibilities

| module | responsibility |
|--------|----------------|
| `coverage/spec.py` | parse `inputs/<slug>.md` → `CoverageSpec`; template → default blocks |
| `bloomberg/schema.py` | field registry (`FieldSpec`), `required_rows(spec)`, `BloombergPack` |
| `bloomberg/template.py` | emit the blank fill-in CSV |
| `bloomberg/ingest.py` | parse the filled CSV → validated `BloombergPack` |
| `coverage/fundamentals.py` | fetch + extract BMV XBRL → `Fundamentals` (LTM + annual) + `_derive_native` (EBITDA/tangible-book/shares/FCF) |
| `coverage/native.py` | assemble the native pack (peer XBRL + prices + macro); `merge_packs`; `filled_cells` (gap-driven template) |
| `download/market_data.py` | Yahoo Finance price fetcher (last + FY-close), series-suffix resolution, cache |
| `download/macro.py` | Banxico SIE + INEGI macro fetcher (token via env) |
| `coverage/peers.py` | `CompanyInputs`, per-template multiple functions, `build_cross_section` |
| `coverage/valuation.py` | block builders + `build_model` (the metric dictionary + reconciliation) |
| `coverage/validate.py` | coverage sanity + accounting identities → validation report |
| `sheets/valuation_sheet.py` | render the model to Excel (live formulas + peer columns) |
| `src/{extract,download,model,excel,shared,eval,parse}` | VENDORED parent infra (never edit) |

---

## 7. Scripts & commands

| command | does |
|---------|------|
| `python3 scripts/gen_universe.py` | (re)generate all configs + specs + templates + `docs/UNIVERSE.md` from the classification table |
| `python3 scripts/emit_bloomberg_template.py inputs/<slug>.md` | emit one blank Bloomberg template |
| `python3 scripts/fetch_xbrl_batch.py [--only …] [--template …]` | prime the XBRL cache (network) |
| `python3 scripts/build_coverage.py inputs/<slug>.md` | build one company's workbook end-to-end |
| `python3 scripts/vendor_sync.py --check/--sync` | show/refresh vendored parent infra |
| `python3 -m pytest -m "not network and not model"` | run the offline test suite |

---

## 8. Universe & conventions

- **Slug rule**: `re.sub(r"[^a-z0-9]+","_", name.lower()).strip("_")`. One slug = one company across
  config / spec / reports / pack / output.
- **Sector → template → peers**: the source of truth is `scripts/gen_universe.py::UNIVERSE`
  (rendered to `docs/UNIVERSE.md`). Peers = sector-mates (single-currency MXN).
- **XBRL availability**: financial-sector issuers are **annual-only**; 10 names are absent from the
  archive → Bloomberg-only (see `gen_universe.ABSENT`, incl. FEMSA=user's "FMX").
- **Currency caveat**: some issuers report XBRL in USD (Grupo México, Orbia, AMX) while listing in
  MXN → input Bloomberg market data in the matching currency, or set the config currency. Same-source
  ratios (margins, ROA) are unaffected.

See [ROADMAP.md](ROADMAP.md) for known limitations and next steps.
