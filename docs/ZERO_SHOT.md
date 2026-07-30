# Zero-shot extraction — running the engine on a company it has never seen

The extraction layer's end goal is to fill a Segments sheet for a company with **no
tuned config and no ground truth**. This doc is the playbook for that cold-start
path and for reading its GT-free scorecard.

## The three-check robustness model

1. **Engine** — the deterministic tier cascade + accounting-identity validator +
   verification gate (STRONG / suspects / worklist).
2. **Independent LLM cross-check (advisory)** — DeepSeek re-extracts the same
   metrics by label, blind to the engine's values; agreement within tolerance is
   the only accuracy proxy available without ground truth. It NEVER gates — it
   feeds check 3. (`--crosscheck` on the pipeline, or `llm_crosscheck.enabled`
   in config; requires `DEEPSEEK_API_KEY`; see `src/extract/llm_crosscheck.py`.)
3. **Manual verification** — the `/onboard-company` Stage-5 loop
   (`scripts/verify_extraction.py`, blind subagents, `data/verified/<slug>.csv`
   pins). Disagreeing cells from check 2 are the first cells to verify.

## Running a zero-shot scorecard

```bash
.venv/bin/python scripts/zero_shot_eval.py <slug> [...]      # reads the estate view READ-ONLY
.venv/bin/python scripts/zero_shot_eval.py gap --no-crosscheck --table
```

- Metric surface = soft's template key lists (`soft/configs/soft.yaml`:
  industrial 22 / financials 10 / reit 15) — the keys downstream consumers need.
- Output: `outputs/_zero_shot/<slug>.md` + `outputs/_zero_shot/zero_shot_scorecard.md`.
- The pdfplumber `table` tier is OFF by default (it can stall on large BMV
  filings); `--table` opts in.
- XBRL-text companies (most of the estate) exercise the bmv/xbrl path; only
  companies with real parsed PDFs test the full PDF cascade.

## Reading the numbers (2026-07-28 pilot, 8 companies)

| Company | Source | Keys | Cell cov | Validator | Mean conf | LLM agree |
|---|---|---|---|---|---|---|
| banorte | pdf-parsed | 9/10 | 61% | 90% | 0.83 | 0% |
| grupo_bafar | pdf-parsed | 13/22 | 52% | 100% | 0.85 | 28% |
| cemex | xbrl-text | 18/22 | 66% | 71% | 0.77 | 28% |
| gap | xbrl-text | 14/22 | 43% | 95% | 0.84 | 23% |
| danhos | xbrl-text | 8/15 | 35% | 100% | 0.88 | 16% |
| alsea | xbrl-text | 18/22 | 60% | 61% | 0.76 | 12% |
| gcc | xbrl-text | 15/22 | 56% | 56% | 0.68 | 29% |
| fibra_uno | pdf-parsed | 5/15 | 8% | 100% | 0.89 | 100% |

**Honest conclusions:**

- **Cold-start extraction is NOT deliverable-grade.** Coverage 8–66%, agreement
  0–29% on meaningful samples. A zero-shot company needs the onboarding loop
  (config + verification) before its numbers are trusted.
- **Internal confidence over-reports cold-start quality** — cells at conf 0.90
  scored 0/3 agreement (gcc revenue). Use the agreement rate, not mean conf,
  as the cold-start quality signal.
- Disagreements decompose into: 1000× scale mismatches (unit sensing), wrong-row
  grabs (e.g. "Other Accounts Receivable" over "Trade"), and a minority of
  near-misses. The first two classes are exactly what config levers (`table_scale`,
  aliases, `income_note`) fix during onboarding.
- Many cells are *uncheckable* (LLM returns null / unquotable evidence on big
  numeric tables) — a cross-check harness improvement, not an engine fault.
- The cross-check already caught two real engine bugs on a CERTIFIED company's
  non-certified keys (soriana accounts_receivable/payable matching the "Other …"
  rows) — see `docs/EXTRACTION_ROADMAP.md` P2.

**Next step (recorded in the roadmap):** expand the pilot to all 36 zero-shot
estate slugs once the wrong-row alias fix lands, and track the agreement rate as
the engine's cold-start KPI.

## Estate etiquette

The estate view (`data/document_estate/views/reports/`) is read-only for all
extraction tooling; downloads/parses land in `data/reports/<slug>` (see
`docs/DOCUMENT_ESTATE.md`). The eval dedupes `__alpha_*`/`__root`/`__soft`
period variants via `report_index`.
