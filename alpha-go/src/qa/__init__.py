"""Q&A layer — optional generative synthesis over retrieved snippets (RAG).

OFF BY DEFAULT. This is the ONLY place alpha-go calls an LLM; all search/retrieval is
deterministic and offline. Answers must cite the Hits they draw from. Requires
ANTHROPIC_API_KEY and config qa.enabled = true. See docs/ROADMAP.md Phase 5.
"""
