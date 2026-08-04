# Ground-truth subagent prompt template (Stage 6 — accuracy certification)

Spawn subagents (≈2–3 periods each) to build INDEPENDENT ground truth. They transcribe true values
straight from the reports, **blind to extractor output**, so the certification isn't circular. Fill
the `<...>` slots. Choose periods that are **stratified across every format era** (early/mid/recent +
any layout shift + a Q4 for the annual-vs-quarterly trap).

---

You are building INDEPENDENT ground-truth for an accuracy audit of <Company>. Transcribe TRUE values
directly from source artifacts resolved through the Estate catalog configured by
`PDFS_DOCUMENT_ESTATE`/`estate.json`. Do NOT look at extraction output, CSV, or code. Read the
Markdown and, **if a table is garbled/rotated, read the PDF** (it renders the table visually):
- `<resolved-periodA-markdown>` (and `<resolved-periodA-pdf>`)
- `<resolved-periodB-markdown>` (and `<resolved-periodB-pdf>`)

For EACH period, find the **QUARTERLY** (single-quarter, NOT year-to-date / NOT full-year) value, in
**<unit, e.g. MXN millions>**, for these keys:
- `<key1>` = <plain-language definition>
- `<key2>` = <definition; note any cross-era reconstruction, e.g. "domestic = segment A + segment B for the quarter">
- `<key3>` = <definition>

Rules:
- Q4 reports show both the quarter and the full-year column → take the **quarter**.
- Convert `"$X.X billion"` → ×1000 (millions); `"$X,XXX millones"` / `"MXN XXX million"` → that number;
  loss/"pérdida"/parenthetical → negative.
- Use `null` for any value that is **genuinely not disclosed** (derived-only figures, not-stated
  counts, ended legacy series). Do NOT compute or guess a disclosed number — read it.

Return ONLY a JSON object (no prose), keyed by period:
```json
{"<periodA>": {"<key1>": <num or null>, "<key2>": <num or null>, "<key3>": <num or null>},
 "<periodB>": {"<key1>": <num or null>, ...}}
```
If you made any judgment call (e.g. two figures reported on different bases, a renamed segment), state
it briefly AFTER the JSON.

---

**Assembling the CSV:** write `data/ground_truth/<slug>-actual.csv` in the exact wide format (mirror
`data/ground_truth/herdez-actual.csv`):
- row 1: `Company Name,description`
- a header row: `,Section (P$mn),<NQ{YY}A tags…>` (period tags map via `compare_extractions.parse_period`,
  e.g. `2016-2T` → `2Q16A`)
- section-header rows (`,Section,,,…` — label set, period cells blank) then data rows (`,Label,v1,v2,…`)
- **Blank** (empty cell) every genuinely-undisclosed value — do not fill it just to raise coverage.

Then register `COMPANIES['<slug>']` in `src/eval/compare_extractions.py` (metric_map `(section,label,
tol)` matching the CSV rows) and run:
```bash
python3 -m src.eval.compare_extractions <slug> --tiers xbrl,bmv,note,search,table,prose
```
(Module form is required — `python3 src/eval/…` fails with ModuleNotFoundError. Include `note`
for any company with an `income_note:` config block, and record the exact tier list used in the
accuracy report.)
