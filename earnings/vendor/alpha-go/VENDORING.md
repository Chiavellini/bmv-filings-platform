# Earnings alpha-go frozen fork

`earnings/vendor/alpha-go/` is a repository-local frozen fork derived from the
top-level `alpha-go/` subproject. Earnings keeps this copy so its original and
v2 research modes remain reproducible while v3 can use the current root
implementation. Changes to the top-level project do not flow into this fork
automatically.

The release boundary recognizes these Earnings-specific source divergences:

- `src/corpus/upload.py`
- `src/index/build.py`
- `src/index/chunker.py`
- `src/index/embeddings.py`
- `src/shared/paths.py`

Any additional difference from top-level `alpha-go/` is drift and must be
reviewed before syncing. Only the frozen source and required configuration
belong in Git. Package installations, compiled extensions, catalogs, downloaded
documents, and provider snapshots belong to the external data estate.

This provenance note does not claim or grant an external license. Publication
is governed by the repository owner's rights and the repository-level license;
third-party data and provider content remain outside that boundary.
