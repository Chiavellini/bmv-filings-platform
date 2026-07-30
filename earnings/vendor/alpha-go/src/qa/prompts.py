"""Prompt templates for the Q&A synthesis layer.

Kept separate from rag.py so prompt wording can be iterated without touching orchestration.
The system prompt MUST instruct: answer only from the provided snippets; cite each claim by
its source marker; emit the exact NOT_FOUND sentinel rather than guess (anti-hallucination,
mirroring the vendored extractor's evidence-substring discipline).
"""
from __future__ import annotations

# Exact sentinel the model must emit when the snippets don't answer the question, so the
# "not found" path is machine-detectable (rag.answer checks for it verbatim).
NOT_FOUND = "Not found in the corpus."

SYSTEM_PROMPT = (
    "You are a financial-document analyst. Answer ONLY using the numbered snippets provided. "
    "Cite every claim with its [n] marker. If the snippets do not contain the answer, reply "
    f'with exactly "{NOT_FOUND}" — never invent figures or facts.'
)


def build_user_prompt(question: str, numbered_snippets: list[str]) -> str:
    """Assemble the user turn: the numbered evidence snippets + the question.

    Each entry of ``numbered_snippets`` is already prefixed with its marker + provenance by the
    caller (e.g. ``"[1] (BIMBO · 2024-2T · report) …text…"``); this function only lays out the
    blocks and restates the citation contract next to the question.
    """
    blocks = "\n\n".join(s.strip() for s in numbered_snippets if s and s.strip())
    return (
        "Snippets:\n"
        f"{blocks}\n\n"
        f"Question: {question.strip()}\n\n"
        "Answer using only the snippets above, citing each claim as [n]. "
        f'If they do not contain the answer, reply exactly: "{NOT_FOUND}"'
    )
