#!/usr/bin/env python3
"""make_manifest.py — pin the outputs/ tree to a git-versioned manifest.

outputs/ is gitignored (parquets + 170 CSVs), so without this there is no
record tying a results file to the commit that produced it. The manifest
(sha256, size, mtime per file + repo HEAD) IS committed; regenerate it after
every results-bearing step.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    entries = {}
    for path in sorted(bs.OUTPUTS_DIR.rglob("*")):
        if not path.is_file() or path.name == "MANIFEST.json":
            continue
        rel = str(path.relative_to(bs.OUTPUTS_DIR))
        stat = path.stat()
        entries[rel] = {
            "sha256": sha256_file(path),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                     .isoformat(timespec="seconds"),
        }
    manifest = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "repo_head": bs.repo_head(),
        "n_files": len(entries),
        "files": entries,
    }
    out = bs.OUTPUTS_DIR / "MANIFEST.json"
    out.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    print(f"MANIFEST.json: {len(entries)} files @ HEAD {manifest['repo_head'][:12]}")


if __name__ == "__main__":
    main()
