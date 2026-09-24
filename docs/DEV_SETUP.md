# Dev setup — the iCloud eviction problem (and how to work around it)

> **RESOLVED (2026-07): the working checkout moved to a non-synced disk**, so
> the eviction problem below no longer applies and
> the standalone `scripts/materialize.py` CLI has since been removed. This document is kept
> for reference in case the repo is ever cloned back onto an iCloud-synced location
> (`~/Desktop`, `~/Documents`).
>
> **Still live, and the one thing to carry to another Mac:** use a Python 3.13
> whose `pyexpat` works. Homebrew's python3.14 build is missing `expat`, which
> breaks `openpyxl` imports and therefore every Excel build — silently, at import
> time. `.python-version` pins 3.13.9; do not substitute the machine's default
> `python3`, which on a current Mac may well be 3.14.

## Symptom (historical)

This repo lived on the iCloud-synced Desktop (`~/Desktop/pdfs`). Under load, macOS
`fileproviderd` **evicts files to a "dataless" placeholder** — the file still `exists()`
and `stat`s fine, but the bytes are gone until re-fetched. The first read then **blocks
on the iCloud fetch**, which is currently throttled to **~90 seconds per file**. This
makes long runs hang or time out mid-way:

- `pytest tests/` stalls the first time a test reads an evicted corpus file.
- `python3 -m src.eval.compare_extractions <slug>` and `scripts/build_segments.py` stall
  reading `data/reports/<slug>/*.md` / `*_facts.json`.
- Even source files (`src/**.py`, `scripts/**.py`) get evicted, so imports can hang.

Check what's evicted:
```bash
stat -f "%Sf" src/extract/tiered_extract.py     # "...,dataless" ⇒ evicted
ls -lRO data/reports/<slug> | grep -c dataless   # count evicted files in a corpus
```

## Mitigation — warm the cache before a heavy run

The standalone `scripts/materialize.py` CLI **no longer exists.** Warming is now
automatic and library-only: `src/shared/materialize.py` provides `materialize()`
and `request_download()`, and `tests/conftest.py` fires a non-blocking warm of
`data/reports` at session start. Opt out with `PDFS_MATERIALIZE=0` — which is
what CI and `scripts/certify_clean_clone.py` both set.

Run it first, then your `pytest` / `compare_extractions` / `build_segments` reads hit
local files and complete. The test suite also self-heals: `tests/conftest.py` materializes
each corpus file before reading it and fires a background warm at session start (opt out
with `PDFS_MATERIALIZE=0`).

**Honest caveat:** materialization speed is Apple's sync throttle, not something code can
fix. At ~90 s/file, warming a whole company corpus takes many minutes and `--all` (2486
files) can take hours. The tool makes runs *complete instead of hanging*; it does not make
them *fast* while the repo is on iCloud.

## Permanent fix — get the repo off the synced Desktop

The only way to stop eviction entirely:

- **Move the repo:** `mv ~/Desktop/pdfs ~/dev/pdfs` (anywhere outside `~/Desktop` and
  `~/Documents`), then reopen from the new path. Nothing is version-controlled under
  `data/reports` / `downloads` / `outputs` (all git-ignored caches), so only your local
  clone + these caches move. **Or**
- **Disable Desktop & Documents sync:** System Settings → Apple ID → iCloud → iCloud Drive
  → Options → uncheck "Desktop & Documents Folders".

After relocating, no warming is needed at all. See
`docs/EXTRACTION_ROADMAP.md` P1.
