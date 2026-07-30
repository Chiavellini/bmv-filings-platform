"""Infrastructure tests for scripts/sample_sentiment.py — the UNLABELED draw builder.

These cover the Track-C-expand2 additions only: the ``--exclude`` normalized-text filtering, the
``--seed`` override determinism, and back-compat of ``collect_candidates`` with no exclude set.
No sentiment logic or lexicon is exercised here — selection is polarity-agnostic by design.
"""
from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "sample_sentiment", _ROOT / "scripts" / "sample_sentiment.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = _load_module()


def test_norm_text_lowercases_and_collapses_whitespace():
    assert ss._norm_text("  The   Sales\tROSE.\n") == "the sales rose."
    # case/spacing-only differences normalize to the same key
    assert ss._norm_text("Net SALES rose") == ss._norm_text("net   sales    rose")


def test_load_exclude_keys_reads_text_normalized(tmp_path: Path):
    p = tmp_path / "gold.yaml"
    p.write_text(
        yaml.safe_dump(
            {"snippets": [
                {"id": "s0001", "text": "Net   SALES  rose sharply.", "label": "positive"},
                {"id": "s0002", "text": "Margins CONTRACTED.", "label": "negative"},
            ]}
        ),
        encoding="utf-8",
    )
    keys = ss.load_exclude_keys([str(p)])
    assert keys == {"net sales rose sharply.", "margins contracted."}
    # a near-identical (case/spacing) variant is already covered by the normalized key
    assert ss._norm_text("net sales   rose  SHARPLY.") in keys


def test_load_exclude_keys_merges_multiple_files(tmp_path: Path):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text(yaml.safe_dump({"snippets": [{"text": "Alpha one."}]}), encoding="utf-8")
    b.write_text(yaml.safe_dump({"snippets": [{"text": "Beta two."}]}), encoding="utf-8")
    keys = ss.load_exclude_keys([str(a), str(b)])
    assert keys == {"alpha one.", "beta two."}


_PROSE_DOC = (
    "The company reported that consolidated revenue grew across every operating region "
    "this quarter. Management noted that the integration of the acquired bakery brands "
    "advanced according to the original plan and timeline.\n"
)


def test_collect_candidates_excludes_by_normalized_text(monkeypatch, tmp_path: Path):
    """A snippet in the exclude set is dropped; back-compat: no exclude keeps it."""
    corpus = tmp_path / "corpus"
    (corpus / "acme").mkdir(parents=True)
    (corpus / "acme" / "doc.md").write_text(_PROSE_DOC, encoding="utf-8")
    monkeypatch.setattr(ss, "CORPUS_DIR", corpus)

    # Baseline (no exclude, and empty exclude) — both back-compat with the legacy single call.
    base = ss.collect_candidates()
    assert base == ss.collect_candidates(set())
    assert len(base) >= 1
    texts = [c["text"] for c in base]

    # Excluding the normalized form of one candidate (differing case/spacing) drops exactly it.
    victim = texts[0]
    weird = "   " + victim.upper().replace(" ", "   ") + "  "
    keys = {ss._norm_text(weird)}
    filtered = ss.collect_candidates(keys)
    assert victim not in [c["text"] for c in filtered]
    assert len(filtered) == len(base) - 1


def test_stratified_sample_is_seed_deterministic():
    cands = [
        {"text": f"snippet number {i} here.", "company": "acme", "lang": "en"}
        for i in range(50)
    ]
    a = ss.stratified_sample(list(cands), 10, random.Random(20260708))
    b = ss.stratified_sample(list(cands), 10, random.Random(20260708))
    c = ss.stratified_sample(list(cands), 10, random.Random(99999999))
    texts_a = [r["text"] for r in a]
    texts_b = [r["text"] for r in b]
    texts_c = [r["text"] for r in c]
    assert texts_a == texts_b          # same seed → identical draw
    assert texts_a != texts_c          # different seed → different draw
    assert len(texts_a) == 10
