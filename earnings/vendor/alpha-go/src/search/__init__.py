"""Search layer — hybrid retrieval over the index, with filters and highlighted snippets.

This is the heart of the local AlphaSense: a query in, a ranked list of cited, highlighted
hits out — fully offline and deterministic. See docs/architecture/system_design.md
(Search phase) and docs/ROADMAP.md Phase 3.
"""
