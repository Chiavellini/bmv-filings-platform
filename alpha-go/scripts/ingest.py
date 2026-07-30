"""ingest CLI — download + parse configured sources into the corpus.

    python3 scripts/ingest.py --config configs/alpha_go.yaml [--source walmex] [--force-download]

Wraps src.corpus.ingest (which wraps the vendored download + parse phases).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/alpha_go.yaml")
    ap.add_argument("--source", help="ingest only this source slug (default: all)")
    ap.add_argument("--corpus-dir", default="data/corpus")
    ap.add_argument("--force-download", action="store_true")
    args = ap.parse_args()

    from src.corpus.ingest import ingest_all, ingest_source

    config = yaml.safe_load(Path(args.config).read_text())
    corpus_dir = Path(args.corpus_dir)

    if args.source:
        source = next(s for s in config["sources"] if s["slug"] == args.source)
        ingest_source(source, corpus_dir, force_download=args.force_download)
    else:
        ingest_all(config, corpus_dir, force_download=args.force_download)


if __name__ == "__main__":
    main()
