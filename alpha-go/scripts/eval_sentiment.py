"""eval_sentiment — score the offline sentiment engine against a labeled gold set.

    python3 scripts/eval_sentiment.py
    python3 scripts/eval_sentiment.py --labels eval/sentiment_labels.yaml --split all
    python3 scripts/eval_sentiment.py --labels eval/sentiment_sample_labeled.yaml --split test
    python3 scripts/eval_sentiment.py --misses          # also print every misclassification

Reports accuracy (with a 95% Wilson CI), macro-F1, per-class precision/recall, a confusion
matrix, and per-slice breakdowns by language / company / hard-case tag. Run it BEFORE and AFTER
any lexicon/threshold change — a lift must show here before it is claimed (mirrors the
retrieval-eval invariant). Pure lexicon engine; never touches the network.

NOTE: ``eval/sentiment_labels.yaml`` is the SELF-AUTHORED, tuned-against set — dev-only, an
inflated ceiling. Headline numbers must come from an independently-labeled held-out split.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.qa.sentiment import score_sentiment          # noqa: E402
from src.qa.sentiment_eval import LABELS, load_labels, score   # noqa: E402


def _fmt_confusion(confusion: dict) -> str:
    head = "gold \\ pred |" + "".join(f"{p[:4]:>8}" for p in LABELS)
    lines = [head, "-" * len(head)]
    for g in LABELS:
        row = f"{g:>11} |" + "".join(f"{confusion[g][p]:>8}" for p in LABELS)
        lines.append(row)
    return "\n".join(lines)


def _print_slices(title: str, slices: dict) -> None:
    if not slices:
        return
    print(f"\n  by {title}:")
    print(f"    {'slice':<14}{'n':>4}{'acc':>8}{'macro-F1':>10}   95% CI")
    for key, m in slices.items():
        lo, hi = m.accuracy_ci
        print(f"    {key:<14}{m.n:>4}{m.accuracy:>8.2%}{m.macro_f1:>10.3f}   "
              f"[{lo:.2%}, {hi:.2%}]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labels", default="eval/sentiment_labels.yaml")
    ap.add_argument("--split", default="all", choices=["all", "dev", "test"],
                    help="score only this split (records with no split field count as dev)")
    ap.add_argument("--misses", action="store_true",
                    help="print every misclassified snippet")
    args = ap.parse_args()

    snippets = load_labels(ROOT / args.labels, split=args.split)
    if not snippets:
        sys.exit(f"no labeled snippets in {args.labels} for split={args.split}")

    report = score(score_sentiment, snippets, split=args.split)

    n_es = sum(1 for s in snippets if s.lang == "es")
    n_en = sum(1 for s in snippets if s.lang == "en")
    lo, hi = report.accuracy_ci
    print(f"{report.n} snippets (es={n_es}, en={n_en}) · split={args.split} · "
          f"labels={args.labels} · engine=lexicon")
    print(f"  accuracy:  {report.accuracy:.2%}   95% CI [{lo:.2%}, {hi:.2%}]")
    print(f"  macro-F1:  {report.macro_f1:.4f}")
    print("\n  per class   precision  recall     f1   support")
    for lbl in LABELS:
        m = report.per_class[lbl]
        print(f"  {lbl:>9}   {m.precision:>8.2%}  {m.recall:>6.2%}  {m.f1:>5.3f}  {m.support:>8}")

    print("\n  confusion matrix (rows = gold, cols = predicted):")
    for line in _fmt_confusion(report.confusion).splitlines():
        print("  " + line)

    _print_slices("language", report.by_lang)
    _print_slices("company", report.by_company)
    _print_slices("tag (hard cases)", report.by_tag)

    misses = report.misses()
    print(f"\n  {len(misses)} miss(es) of {report.n}")
    if args.misses:
        for r in misses:
            tag = f" [{','.join(r.tags)}]" if r.tags else ""
            print(f"    - gold={r.gold:>8} pred={r.predicted:>8} ({r.lang}){tag}: {r.text[:90]}")


if __name__ == "__main__":
    main()
