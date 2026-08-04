# Verification subagent prompt template (Stage 5 — gate loop)

Spawn ONE subagent per worklist cell (or per period batching a few cells). The subagent confirms the
true value from the primary source, **blind to the extractor's output**. Resolve the source through
the Estate catalog configured by `PDFS_DOCUMENT_ESTATE`/`estate.json`; never assume a legacy mount
name. Fill the `<...>` slots.

---

You are verifying extracted financial values against the primary-source quarterly report. Read ONLY
the report artifact(s) resolved from the configured Estate catalog — do NOT look at any extraction
output, CSV, or code:
- `<resolved-markdown-path>`
- `<resolved-pdf-path>` when the Markdown table is garbled/rotated; the PDF renders tables visually

For period **<period>**, find the **QUARTERLY** (single-quarter, NOT year-to-date / NOT full-year)
value, in **<unit, e.g. MXN millions>**, for each key below. For Q4 reports, take the
fourth-quarter column, not the annual/12M column.

| key | what it is | extractor's suspect value (for context only — verify independently) |
|---|---|---|
| <key1> | <plain-language definition of the line> | <suspect value> — <reason flagged> |
| <key2> | <...> | <...> |

Conversion rules: `"$X.X billion"` → ×1000 to reach millions; `"MXN XXX million"` / `"$X,XXX millones"`
→ that number; parenthetical or "pérdida"/"loss" → negative. If a value is **genuinely not disclosed**
in this report, return `null` (the harness will blank any wrong extraction). If the source is
**genuinely unreadable or ambiguous** for a cell (garbled in both md and PDF, two irreconcilable
candidates), return `"value": "UNRESOLVED"` with a note explaining exactly what you checked — the
extracted value will ship with a red-flag comment recording the failed verification.

Return ONLY a JSON array (no prose), one object per key:
```json
[{"period":"<period>","key":"<key1>","value":<number or null>,"note":"<exact source quote>"},
 {"period":"<period>","key":"<key2>","value":<number or null>,"note":"..."}]
```
If an extracted value is actually CORRECT, return it as `value` with note `"confirmed correct — <why>"`
(e.g. a genuinely low quarter the magnitude heuristic false-flagged). Quote the exact source line in
every `note`.

---

**Collecting results:** concatenate all subagents' arrays into one `verdicts.json`, then:
```bash
python3 scripts/verify_extraction.py <slug> --apply verdicts.json   # → data/verified/<slug>.csv
python3 scripts/verify_extraction.py <slug>                          # rebuild + re-score
```
`value: null` is stored as the `BLANK` sentinel (not-disclosed → blanks the cell);
`"UNRESOLVED"` is stored as-is (value untouched; red-flag comment ships; the cell stops
re-entering the worklist and doesn't block STRONG).
