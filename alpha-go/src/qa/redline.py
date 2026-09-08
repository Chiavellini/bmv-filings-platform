"""Redline — what changed between two filings of the same company (AlphaSense's 10-K/10-Q
redlines, for BMV quarterlies and annuals).

Pure text work, no LLM: both documents are split into offset-preserving sentences
(:func:`src.qa.summarize.split_sentences`), boilerplate is dropped with the same corpus model
the retriever uses, and the two sentence lists are aligned with :class:`difflib.SequenceMatcher`
on normalized text. The result is three lists — **added**, **removed**, **changed** — each item
anchored to a document offset so the reader can jump to it, plus a tone tally per side so a
shift in management language shows up as a number, not a feeling.

An optional LLM narration (:func:`narrate`) follows the ``[n]`` citation contract used by the
summaries: the numbered change items are handed to the provider and every bullet must cite the
items it draws from. Gated exactly like Q&A (``qa.enabled`` + key); disk-cached; failures
fall back to the deterministic lists.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from src.qa import llm, llm_cache
from src.qa.sentiment import score_sentiment
from src.qa.summarize import split_sentences
from src.search import fusion

_MAX_ITEMS = 40            # per list; a full annual report can differ in thousands of lines
_SIMILAR = 0.6             # ratio above which a removed/added pair reads as "changed"
_NUM_RE = re.compile(r"[-+]?\d[\d,.]*%?")


@dataclass
class Change:
    kind: str                 # "added" | "removed" | "changed"
    text: str                 # new text (added/changed) or old text (removed)
    old_text: str | None      # for "changed": the prior wording
    offset: int               # char offset in the NEW doc (added/changed) or OLD doc (removed)
    tone: str                 # lexicon label of ``text``
    numbers_changed: bool = False   # a figure differs between old and new wording


@dataclass
class ToneTally:
    positive: int = 0
    neutral: int = 0
    negative: int = 0

    @property
    def net(self) -> int:
        return self.positive - self.negative


@dataclass
class Redline:
    old_label: str
    new_label: str
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    old_tone: ToneTally = field(default_factory=ToneTally)
    new_tone: ToneTally = field(default_factory=ToneTally)
    unchanged: int = 0
    narrative: str = ""          # LLM bullets (empty until narrate() succeeds)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)


def _sentences(text: str, boilerplate) -> list[tuple[str, int, int]]:
    out = []
    for s, cs, ce in split_sentences(text):
        if s.lstrip().startswith("#"):
            continue
        try:
            if boilerplate is not None and boilerplate.is_boilerplate(s):
                continue
        except Exception:  # noqa: BLE001 — the model is a precision aid only
            pass
        if boilerplate is None and fusion._is_boilerplate(s):
            continue
        out.append((s, cs, ce))
    return out


def _tally(sentences: list) -> ToneTally:
    t = ToneTally()
    for s, _, _ in sentences:
        label = score_sentiment(s).label
        setattr(t, label, getattr(t, label) + 1)
    return t


def _numbers(s: str) -> list[str]:
    return _NUM_RE.findall(s)


def redline(old_text: str, new_text: str, *, old_label: str = "prior", new_label: str = "latest",
            boilerplate=None, max_items: int = _MAX_ITEMS) -> Redline:
    """Sentence-level diff of two documents, with 'changed' pairs matched by similarity."""
    old_s = _sentences(old_text, boilerplate)
    new_s = _sentences(new_text, boilerplate)
    old_norm = [fusion._normalize_text(s) for s, _, _ in old_s]
    new_norm = [fusion._normalize_text(s) for s, _, _ in new_s]

    result = Redline(old_label=old_label, new_label=new_label,
                     old_tone=_tally(old_s), new_tone=_tally(new_s))
    sm = difflib.SequenceMatcher(a=old_norm, b=new_norm, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            result.unchanged += i2 - i1
            continue
        removed = list(range(i1, i2))
        added = list(range(j1, j2))
        if tag == "replace":
            # Pair up the most similar old/new sentences as "changed"; leftovers are plain
            # additions/removals.
            used_new: set[int] = set()
            for i in removed:
                best, best_r = None, 0.0
                for j in added:
                    if j in used_new:
                        continue
                    r = difflib.SequenceMatcher(a=old_norm[i], b=new_norm[j], autojunk=False).ratio()
                    if r > best_r:
                        best, best_r = j, r
                if best is not None and best_r >= _SIMILAR:
                    used_new.add(best)
                    s_new, cs_new, _ = new_s[best]
                    s_old = old_s[i][0]
                    result.changed.append(Change(
                        kind="changed", text=s_new, old_text=s_old, offset=cs_new,
                        tone=score_sentiment(s_new).label,
                        numbers_changed=_numbers(s_old) != _numbers(s_new)))
                else:
                    s_old, cs_old, _ = old_s[i]
                    result.removed.append(Change(kind="removed", text=s_old, old_text=None,
                                                 offset=cs_old, tone=score_sentiment(s_old).label))
            for j in added:
                if j not in used_new:
                    s_new, cs_new, _ = new_s[j]
                    result.added.append(Change(kind="added", text=s_new, old_text=None,
                                               offset=cs_new, tone=score_sentiment(s_new).label))
        elif tag == "delete":
            for i in removed:
                s_old, cs_old, _ = old_s[i]
                result.removed.append(Change(kind="removed", text=s_old, old_text=None,
                                             offset=cs_old, tone=score_sentiment(s_old).label))
        elif tag == "insert":
            for j in added:
                s_new, cs_new, _ = new_s[j]
                result.added.append(Change(kind="added", text=s_new, old_text=None,
                                           offset=cs_new, tone=score_sentiment(s_new).label))
    # Figures first (a changed number is what an analyst wants to see), then document order.
    result.changed.sort(key=lambda c: (not c.numbers_changed, c.offset))
    result.added = result.added[:max_items]
    result.removed = result.removed[:max_items]
    result.changed = result.changed[:max_items]
    return result


# --------------------------------------------------------------------------------------------
# Optional LLM narration — same [n] contract as the summaries.
# --------------------------------------------------------------------------------------------
_LLM_SYSTEM = (
    "You are a financial-document analyst comparing two consecutive filings of the same company. "
    "You are given numbered CHANGE items (added / removed / changed sentences). Write 3-6 short "
    "bullets on what materially shifted: guidance, risks, figures, tone. Every bullet MUST cite "
    "the item(s) it draws from with their [n] marker(s). Use ONLY the items — never invent. "
    "Keep each item's language (Spanish or English). Answer with the bullets only, one per line "
    "beginning with '- '."
)
_LLM_MAX_TOKENS = 600
_MARKER_RE = re.compile(r"\[(\d+)\]")


def numbered_items(rl: Redline, *, limit: int = 30) -> list[tuple[int, Change]]:
    """The change items an LLM narration may cite, numbered from 1 (changed → added → removed)."""
    items = [*rl.changed, *rl.added, *rl.removed][:limit]
    return list(enumerate(items, start=1))


def narrate(rl: Redline, config: "dict | None") -> Redline:
    """Attach an LLM narrative to ``rl`` when Q&A is enabled and keyed; otherwise return as-is."""
    qa = (config or {}).get("qa", {}) or {}
    if not qa.get("enabled"):
        return rl
    provider = llm.resolve_provider(qa)
    if provider is None or rl.total == 0:
        return rl
    lines = []
    for n, c in numbered_items(rl):
        if c.kind == "changed":
            lines.append(f"[{n}] CHANGED — was: {c.old_text} — now: {c.text}")
        else:
            lines.append(f"[{n}] {c.kind.upper()} — {c.text}")
    excerpt = f"Prior filing: {rl.old_label}\nLatest filing: {rl.new_label}\n\n" + "\n".join(lines)
    use_cache = qa.get("cache", True)
    if use_cache:
        cached = llm_cache.get("redline", provider.model, excerpt)
        if cached is not None and cached.get("text"):
            rl.narrative = cached["text"].strip()
            return rl
    try:
        text = llm.complete(provider, system=_LLM_SYSTEM, user=excerpt + "\n\nWrite the bullets.",
                            max_tokens=_LLM_MAX_TOKENS)
    except Exception:  # noqa: BLE001 — network/SDK failure keeps the deterministic lists
        return rl
    if (text or "").strip():
        rl.narrative = text.strip()
        if use_cache:
            llm_cache.put("redline", provider.model, excerpt, {"text": text})
    return rl
