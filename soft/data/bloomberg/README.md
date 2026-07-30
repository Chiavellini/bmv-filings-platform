# data/bloomberg/

Filled Bloomberg packs, one per company: `<slug>.csv` (long format; see
`src/bloomberg/template.py`). `scripts/build_coverage.py` reads them.

## ⚠️ `walmex.csv` is ILLUSTRATIVE SAMPLE DATA

The committed `walmex.csv` contains **plausible placeholder values, not real terminal data**, so
the pipeline can be demonstrated end-to-end. Peer/segment/macro/price figures are made up. COST US
is intentionally left blank (USD peer → cross-currency; see `docs/ROADMAP.md`).

Replace it with a real export from your Bloomberg terminal before trusting any output. To refresh
the blank template: `python3 scripts/emit_bloomberg_template.py inputs/walmex.md`.
