# Quarterly-PDF source onboarding compiler

`scripts/discover_ir_sources.py` turns official BMV issuer metadata and static
IR pages into a reviewable `configs/issuers.yaml` proposal without using an
LLM. It is intended to remove repetitive page inspection from the primary-PDF
coverage backlog, and it never writes the document estate.

## Safety and evidence contract

The default is a registry dry run. The compiler:

1. selects issuers from the read-only primary-PDF readiness gaps;
2. combines URLs already present in the registry/catalog;
3. when those are absent, fetches the BMV capital-issuer directory once, maps
   the canonical ticker to its exact profile ID, verifies the profile's
   displayed `Clave`, and reads its official `Web:` URL;
4. accepts an optional compact seed file only for unresolved exceptions;
5. fetches a bounded number of static HTML pages and `/sitemap.xml` entries;
6. classifies links with deterministic quarterly and exclusion rules;
7. downloads at most three newest candidate documents by default;
8. accepts a sample only if all of these checks pass:
   - the complete response is at least 8,000 bytes and begins with `%PDF-`;
   - text from the first three pages identifies the issuer;
   - that text confirms the exact quarter and year advertised by the link;
9. infers a direct URL template only when two different verified periods render
   back to their exact final URLs;
10. emits confidence, BMV seed resolution, source-page attempts, candidate reasons, PDF hashes,
   redirects, byte sizes, matched identity terms and any failure.

Annual reports, presentations, webcasts, transcripts, infographics,
sustainability material, certificates and raw XBRL payloads are rejected before
proposal promotion. A human-readable quarterly PDF whose filename contains
`XBRL` remains eligible and must pass the same content checks. A single verified
quarter receives `medium` confidence and cannot be patched or applied. Only
two-or-more-period `high` proposals are patch-eligible, and they still require
review.

The cache lives under `.cache/source_onboarding/`, outside the shared estate.
The BMV directory is one cached fleet-wide response; parsed profile Web fields
are cached per ticker/profile ID. Proposals are keyed by the algorithm version,
issuer identity, readiness gaps, seeds and bounds. Repeating a run within seven
days reuses the exact proposal. Use `--refresh` after a quarter changes or a
source is repaired.

## Automatic BMV seeds and exception seed format

The existing catalog often contains only BMV XBRL URLs. Those are deliberately
not treated as IR/PDF seeds. Instead, the default provider retrieves the
official BMV capital directory and then only the profiles in the selected
cohort. The profile ticker must match the registry identity before its external
Web URL becomes a seed. Missing, duplicate and malformed records are emitted as
`ticker_missing`, `ticker_ambiguous`, `website_missing`, or `profile_failed`.

A matching BMV profile is a lead, not proof that its `Web:` target is the
issuer's IR site. Trusts can point to a trustee (for example, an issuer profile
may lead to Actinver), and corporate pages can contain malformed links. The
crawler skips malformed anchors, but it will not propose or certify a source
unless downloaded PDF text independently matches both the issuer and the exact
quarter. An irrelevant trustee site therefore ends in `needs_seed` or
`no_verified_pdf`, never a registry patch.

For those exceptions, provide the smallest useful manual input—normally one
official corporate or IR page—rather than full HTML or PDFs:

```yaml
issuers:
  alpek:
    - https://www.alpek.com/investors/
  alsea: https://www.alsea.net/investors/
```

Manual seeds override no registry data; they supplement automatic inputs. A
`needs_seed` result after BMV and the seed file are exhausted is explicit; it is
not a failed or fabricated source. Use `--no-bmv-directory-seeds` only for
offline fixture work or a deliberate manual-only audit.

## Dry-run proposals

```bash
# One issuer, structured evidence to a file.
.venv/bin/python scripts/discover_ir_sources.py \
  --issuer alpek \
  --seed-file /path/to/official-ir-seeds.yaml \
  --output /tmp/alpek-source-proposal.json

# Reproducible five-issuer sample from all unconfigured readiness gaps.
.venv/bin/python scripts/discover_ir_sources.py \
  --seed-file /path/to/official-ir-seeds.yaml \
  --random-sample 5 \
  --sample-seed 20260801 \
  --output /tmp/source-sample.json

# The no-argument cohort is bounded to ten issuers. Expand intentionally.
.venv/bin/python scripts/discover_ir_sources.py --all \
  --output /tmp/all-source-proposals.json

# Retry the network and ignore a still-valid proposal cache.
.venv/bin/python scripts/discover_ir_sources.py \
  --issuer alpek \
  --seed-file /path/to/official-ir-seeds.yaml \
  --refresh
```

The command may populate only its regenerable cache. It does not change the
registry, catalog, objects, report views or consumer indexes.

## Reviewable patch and explicit apply

```bash
# Generate a normal unified diff. Only high-confidence proposals appear.
.venv/bin/python scripts/discover_ir_sources.py \
  --issuer alpek \
  --seed-file /path/to/official-ir-seeds.yaml \
  --patch-output /tmp/alpek-issuers.patch \
  --output /tmp/alpek-source-proposal.json

# Explicit mutation: requires at least one --issuer. Existing IR mappings are
# never overwritten, and the complete temporary registry must pass the
# canonical loader before atomic replacement.
.venv/bin/python scripts/discover_ir_sources.py \
  --issuer alpek \
  --seed-file /path/to/official-ir-seeds.yaml \
  --apply \
  --output /tmp/alpek-source-apply.json
```

Prefer the patch workflow for cohort review. `--apply` returns status `3` when
no eligible new overlay was added. Neither route performs an estate sync;
promotion still goes through the root quarterly acquisition command.

## Live canary mode

`--verify-live` is a separate production-adapter canary, not the generic source
proposal crawler. It invokes each issuer's configured
`InvestorRelationsAdapter` or `BmvIssuerPdfAdapter`, requires an explicit exact
period, excludes every other quarter, retries the bounded adapter operation,
saves only independently verified PDFs under a caller-provided non-estate
directory, and exits nonzero if any issuer fails. A perfectly valid stale PDF
cannot pass:

```bash
canary_dir="$(mktemp -d)"
.venv/bin/python scripts/discover_ir_sources.py \
  --verify-live \
  --issuer walmex \
  --issuer sports_world \
  --issuer liverpool \
  --issuer femsa \
  --issuer qualitas \
  --expected-period 2026-2T \
  --staging-dir "$canary_dir" \
  --output /tmp/quarterly-source-canaries.json
```

For reproducible reliability sampling, omit explicit issuers and sample only
from the configured primary-PDF population:

```bash
canary_dir="$(mktemp -d)"
.venv/bin/python scripts/discover_ir_sources.py \
  --verify-live \
  --random-sample 5 \
  --sample-seed 20260801 \
  --expected-period 2026-2T \
  --staging-dir "$canary_dir" \
  --output /tmp/random-quarterly-source-canaries.json
```

The JSON includes adapter discovery layers/issues, candidate periods, each
saved absolute path, exact final URL, selected and expected periods, SHA-256,
byte size, and issuer/period decisions. Canary files are not catalogued and can
be discarded after inspection.

## What remains deliberately manual

The compiler does not invent a domain when BMV has no usable Web field, execute
JavaScript, solve bot challenges, or accept an image-only PDF as issuer
evidence. These cases remain `needs_seed` or `validation_failed`. Add a verified
official URL, use the existing browser-capable acquisition adapter where
justified, or record a narrow source-specific exception; do not lower the PDF
identity gate.
