# Alpha Go evidence, bilingual search, and news build checklist

This checklist is deliberately ordered.  A later layer may reuse an earlier layer, but it may
not weaken the exact, source-verifiable search contract.

## 1. Evidence-first document results

- [x] Keep exact mention counting independent from expanded and semantic discovery.
- [x] Resolve every PDF-backed mention to a physical page using parser page markers.
- [x] Render a cropped page image around the located phrase, with visible highlight boxes.
- [x] Show one mention card per occurrence: source, page, passage-level sentiment, and full-file
      escape hatch.
- [x] Use a clearly labelled text card for HTML, Markdown, and unlocatable PDFs; never fabricate
      a screenshot.
- [x] Cache rendered crops and page lookups so a broad search remains interactive.
- [x] Test page mapping, crop generation, missing-PDF fallback, and result-card rendering.

## 2. Spanish / English search contract

- [x] Keep the literal lane case- and accent-insensitive, with no translated terms in its count.
- [x] Make curated financial equivalents bidirectional between English and Spanish and label them
      as related wording.
- [x] Require a real multilingual embedding model for conceptual ES ↔ EN discovery; do not call a
      hashing fallback conceptual search.
- [x] Keep language detection transparent and use the existing bilingual sentiment lexicon on the
      matched passage.
- [x] Add unseen English and Spanish upload fixtures plus cross-language exact/expanded/semantic
      regression tests.

## 3. Separate news corpus

- [x] Keep a separate news acquisition catalog and corpus while indexing verified News articles
      into the main search index as a selectable document type beside filings.
- [x] Store canonical URL, publisher, publication time, language, source identity, content hash,
      entitlement, and all verified company memberships.
- [x] Add a deterministic company-alias resolver with evidence and false-positive controls.
- [x] Build provider-neutral ingestion.  The default mode stores metadata, permitted snippets, and
      links only; full text requires an explicitly licensed adapter.
- [x] Deduplicate same-URL and syndicated articles without discarding multi-company memberships.
- [x] Add a News page with corpus search, Industry → Company → Article scope, mention cards,
      trends, refresh status, and publisher links.
- [x] Add approved RSS and a no-key, metadata/link-only discovery sync entrypoint; the latter uses
      per-company exact phrase queries and preserves the query alias as membership evidence.

## 4. Acceptance sweep

- [x] Test document and news scopes independently and together in the dashboard.
- [x] Validate unknown-company uploads in Spanish and English.
- [x] Validate originals, page crops, links, filters, exact counts, sentiment, and trends.
- [x] Run the complete test suite and record corpus/index integrity checks.
