# Analyst Console

The Analyst Console is the permanent GitHub Pages launchpad for the platform's
independent projects. It is not a replacement dashboard: Alpha Go still opens
in Alpha Go, matrices open in their generated HTML view, and workbooks open in
Excel.

## Destination Mac: one-time preparation

The person preparing the destination Mac installs the repository environment
and its loopback bridge once:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python scripts/install_analyst_console.py install
```

Then open
[`https://chiavellini.github.io/bmv-filings-platform/`](https://chiavellini.github.io/bmv-filings-platform/)
in Safari, choose **File → Add to Dock**, and keep **BMV** in the Dock. This
GitHub Pages URL—not a `file://` path or `localhost`—is the permanent entry
point. Closing it or restarting the Mac never requires a manual restart.

The external Estate must be mounted for estate-backed actions. When it is not
mounted, the launchpad remains available and reports the connection problem
without attempting a fallback write.

## Local bridge

The one-time installer registers the bridge to start automatically at login and
restart if it exits. It does not open a terminal or browser window. GitHub Pages
remains available when the bridge is missing; local actions are visibly disabled
until the Mac has been prepared.

Reinstall or remove the bridge with:

```bash
.venv/bin/python scripts/install_analyst_console.py install
.venv/bin/python scripts/install_analyst_console.py uninstall
```

## Extractor

The **EXTRACTOR** card groups the two workbook-producing flows. Both run in the
local bridge with allowlisted argv arrays and never leave the Mac.

### PDF observations

**Extraer de un PDF** takes one analyst-supplied PDF and returns the requested
metrics as observations:

1. The browser uploads the PDF to `POST /api/extractor/upload` as a raw
   `application/pdf` body (60 MB limit, `%PDF-` magic check, sanitized
   basename). The bridge stores it privately under
   `data/analyst_console/extractor/<id>/source.pdf` and answers with the period
   inferred from the filename (for example `Reporte_2T25.pdf` → `2025-2T`).
2. The analyst chooses catalog metrics (the universal financial registry plus
   the selected company's certified rows) and may add free-text metric names,
   one per line. Free-text names are searched by their exact label through the
   deterministic table/regex tiers and surface with lower confidence.
3. `POST /api/extractor/jobs` validates the request (metric keys, optional
   company, optional `YYYY-NT`/`YYYY-FY` period override, CSV/Excel/both,
   table reading on or off) and runs
   `scripts/extract_pdf_observations.py <request> --output-dir outputs/extractor/<id>`.

Several PDFs can go into one request (up to 12), for example four quarters of
the same issuer. Each file keeps its own period, inferred from the filename and
editable in the file list; with more than one file every period must be present
and distinct, because the shared pipeline collapses files that resolve to the
same period. The request itself lives in its own private directory and only
references the uploaded files.

The script writes `<archivo>_observaciones.csv` (one row per metric per period
with value, prior, unit, confidence, validation and source snippet) and/or
`<archivo>_observaciones.xlsx` with an `Observaciones` sheet and a `Resumen`
sheet listing every requested metric as found or not found (with the number of
periods covered when several files were processed). A `summary.json` next to
them feeds the launchpad: the completion toast reports how many metrics were
found and which ones lack evidence, the card shows *Extrayendo…* while a run is
in progress, and *Abrir último resultado* reopens the newest output. When a job
fails, the toast shows the last error line of its log. A run with no evidence
still completes so the analyst can open the summary. The Tier-4 LLM fallback
stays off unless the script is invoked with `--llm` and an `ANTHROPIC_API_KEY`
is configured.

### Segments model requests

**Generar una hoja de segmentos** starts from the canonical issuer universe and
collects the ordered sections, financial metrics, and calculated rows. The
chooser always includes the universal financial registry; it adds only the
selected company's certified model metrics. Company-specific rows from the
analyst's AC, Becle, FEMSA, KIMBER, KOF, LAB, and TBBB models are therefore
available without leaking one company's KPIs into another.

For each run the bridge writes a private Markdown specification and matching
normalized analyst-request CSV under `data/analyst_console/requests/`. Both are
passed to `scripts/build_segments.py`, preserving the pinned-metric,
analyst-fidelity, and fresh-Estate publication gates.

## Directed Estate refresh

**Actualizar biblioteca completa** is the operator-facing fleet refresh. After
the displayed confirmation it:

1. refreshes quarterly documents for active issuers using only enabled sources
   in `configs/issuers.yaml`;
2. drains verified derivatives into the USB Estate and its Alpha projection;
3. refreshes the configured Alpha news universe through the rights-aware GDELT
   and Google News RSS metadata/link adapters; and
4. runs RSS only when feeds have been explicitly approved in
   `alpha-go/configs/news.yaml`.

The news catalog and corpus live under `<estate>/news/`; publisher article
bodies are not copied by the default GDELT path. The refresh refuses a missing
Estate, concurrent launchpad work, or an unmanaged Alpha process. A managed
Alpha process is closed before the index is updated and can be relaunched from
the launchpad afterwards.

## Publishing

`.github/workflows/analyst-console-pages.yml` publishes
`src/analyst_console/static/` after launchpad changes land on `main`. The
manifest uses repository-relative paths so Safari installs the GitHub Pages URL
correctly from the project subdirectory.

## Safety contract

- The bridge refuses non-loopback bindings.
- Browser actions require a custom request header and an allowed Origin.
- Cross-origin access is limited to `https://chiavellini.github.io` and the
  loopback diagnostic page.
- The UI selects from allowlisted argv arrays; it has no arbitrary shell or path
  endpoint.
- Estate-writing operations require their displayed confirmation phrase.
- A process-wide writer lock serializes estate mutations.
- The full Estate refresh closes managed Alpha before writing its index.
- Job metadata and logs stay under ignored `data/analyst_console/`.
- Generated outputs remain in their existing project directories.
- Estate data, command output, job logs, and generated workbooks are never
  uploaded to GitHub Pages.
