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

## Segments model requests

**Generar un modelo** collects the full Segments contract: company and ticker,
Investor Relations URL, report limit, and the ordered sections, canonical
financial metrics, and calculated rows. Existing input files can be loaded as
editable templates, or a request can start blank.

For each run the bridge writes a private Markdown specification and matching
normalized analyst-request CSV under `data/analyst_console/requests/`. Both are
passed to `scripts/build_segments.py`, preserving the pinned-metric,
analyst-fidelity, and fresh-Estate publication gates.

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
- Job metadata and logs stay under ignored `data/analyst_console/`.
- Generated outputs remain in their existing project directories.
- Estate data, command output, job logs, and generated workbooks are never
  uploaded to GitHub Pages.
