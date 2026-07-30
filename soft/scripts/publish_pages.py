#!/usr/bin/env python3
"""Autonomous publisher — push the freshest coverage matrices to GitHub Pages.

A headless launchd/cron job cannot update a claude.ai Artifact (only a live Claude agent holding the
Artifact tool can). So the *auto-updateable* deliverable is served from a self-hosted location the
shell can write to on its own: a dedicated public GitHub repo with Pages enabled.

Each run:
  1. Reads the freshest body-only deliverable HTML from ``outputs/_master/`` (dense + core matrices,
     written by :mod:`build_dense` / :mod:`build_master`).
  2. Wraps each into a standalone HTML document (the deliverables omit doctype/html/head/body so they
     can drop straight into the claude.ai Artifact skeleton — a browser needs the wrapper).
  3. Writes a small landing ``index.html`` linking both, with a "Last updated" stamp.
  4. Commits the site working tree and (when ``push=True``) pushes it → GitHub Pages serves the update.

The site working tree (``deploy/site/``) is its OWN git repo pointing at the public Pages repo; the
parent project repo ignores it (see ``.gitignore``). Publishing is **guarded**: ``refresh_daily`` only
calls this with ``push=True`` when ``SOFT_PUBLISH=1`` (set by the scheduled launchd run), so ad-hoc
local refreshes never push.

    python3 scripts/publish_pages.py                 # assemble + commit + push
    python3 scripts/publish_pages.py --no-push       # assemble + commit only (dry, no network)

Required for a push: ``SOFT_PAGES_REPO=owner/repository``.
Optional: ``SOFT_SITE_DIR``.
"""
from __future__ import annotations

import argparse
import html as _html
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO = os.environ.get("SOFT_PAGES_REPO", "").strip()
SITE_DIR = Path(os.environ.get("SOFT_SITE_DIR", str(ROOT / "deploy" / "site")))
MASTER_DIR = ROOT / "outputs" / "_master"

# (source body-only file, published filename, human label, one-line blurb). Order = landing order.
DELIVERABLES = [
    (MASTER_DIR / "soft_coverage_dense.html", "dense.html", "Dense 100% matrix",
     "Every cell a real computed value — reduced on both axes to zero N/A."),
    (MASTER_DIR / "soft_coverage_master.html", "core.html", "Core matrix",
     "The full core universe (free-price names); honest N/A where a metric is inapplicable."),
]

def _pages_url(repository: str) -> str:
    """Return the project-Pages URL for an explicit ``owner/repository`` value."""
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        return ""
    owner, name = parts
    return f"https://{owner.lower()}.github.io/{name}/"


PAGES_URL = _pages_url(REPO)


def _wrap(body_only: str) -> str:
    """Wrap a body-only deliverable (``<title>…<style>…<div class="wrap">…``) into a valid standalone
    HTML document so it renders as a real page on GitHub Pages. The in-body ``<title>``/``<style>`` are
    tolerated by browsers; we only need to supply the doctype, charset, and viewport."""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '</head>\n<body>\n'
        f'{body_only}\n'
        '</body>\n</html>\n'
    )


_LANDING_CSS = """
:root{ color-scheme:light dark; --bg:#f7f9fc; --panel:#fff; --ink:#1f2a44; --muted:#5b6577;
  --line:#dbe2ec; --accent:#1f6fd6; }
@media (prefers-color-scheme:dark){ :root{ --bg:#0f1420; --panel:#151b28; --ink:#e6ebf3;
  --muted:#93a0b5; --line:#263149; --accent:#5b9bf0; } }
*{ box-sizing:border-box; } html,body{ margin:0; }
body{ background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system,BlinkMacSystemFont,
  "Segoe UI",Roboto,Helvetica,Arial,sans-serif; padding:48px 20px; }
.wrap{ max-width:640px; margin:0 auto; }
h1{ font-size:22px; margin:0 0 4px; } .sub{ color:var(--muted); margin:0 0 28px; font-size:13px; }
a.card{ display:block; text-decoration:none; color:inherit; background:var(--panel);
  border:1px solid var(--line); border-radius:12px; padding:18px 20px; margin:0 0 14px;
  transition:border-color .15s,transform .05s; }
a.card:hover{ border-color:var(--accent); transform:translateY(-1px); }
.card h2{ margin:0 0 4px; font-size:16px; color:var(--accent); }
.card p{ margin:0; color:var(--muted); font-size:13px; }
footer{ color:var(--muted); font-size:12px; margin-top:24px; border-top:1px solid var(--line);
  padding-top:14px; }
"""


def _landing(pages: list[tuple[str, str, str]], updated: str) -> str:
    """Landing ``index.html`` — self-contained, theme-aware, links each published page.
    ``pages`` = list of (filename, label, blurb)."""
    cards = "\n".join(
        f'  <a class="card" href="{_html.escape(fn)}"><h2>{_html.escape(label)}</h2>'
        f'<p>{_html.escape(blurb)}</p></a>'
        for fn, label, blurb in pages
    )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<title>Soft Coverage — Mexican equity valuation matrices</title>\n'
        f'<style>{_LANDING_CSS}</style>\n</head>\n<body>\n  <div class="wrap">\n'
        '  <h1>Soft Coverage</h1>\n'
        '  <p class="sub">Daily valuation coverage matrices for BMV-listed companies · '
        'free data (BMV-XBRL filings + Yahoo prices).</p>\n'
        f'{cards}\n'
        f'  <footer>Last updated: <b>{_html.escape(updated)}</b> · prices refresh every weekday '
        'after the BMV close · fundamentals from the latest quarterly filing.</footer>\n'
        '  </div>\n</body>\n</html>\n'
    )


def assemble(site_dir: Path, date: str) -> list[Path]:
    """Copy+wrap each present deliverable into ``site_dir`` and write ``index.html``. Pure filesystem
    (no git, no network). Returns the list of written paths. Skips a deliverable whose source is
    missing (a partial refresh must not blank the site) but still lands the ones that exist."""
    site_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    landing_pages: list[tuple[str, str, str]] = []
    for src, out_name, label, blurb in DELIVERABLES:
        if not src.exists():
            print(f"[publish] skip {out_name} — source missing ({src.name})")
            continue
        dest = site_dir / out_name
        dest.write_text(_wrap(src.read_text(encoding="utf-8")), encoding="utf-8")
        written.append(dest)
        landing_pages.append((out_name, label, blurb))
    # GitHub Pages skips Jekyll processing when this marker is present (our filenames are plain, but
    # underscores in future assets would otherwise be dropped).
    (site_dir / ".nojekyll").write_text("", encoding="utf-8")
    index = site_dir / "index.html"
    index.write_text(_landing(landing_pages, date), encoding="utf-8")
    written.append(index)
    return written


def _git(site_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(site_dir), *args],
                          capture_output=True, text=True, check=check)


def publish(site_dir: Path, *, push: bool, date: str, message: str | None = None) -> bool:
    """Stage the site tree, commit if there is a diff, and (when ``push``) push to the Pages remote.
    Returns True if a commit was made. Non-fatal on nothing-to-commit."""
    if not (site_dir / ".git").exists():
        repository_hint = (
            f" Expected remote: {REPO}."
            if REPO
            else " Set SOFT_PAGES_REPO=owner/repository before configuring a remote."
        )
        raise SystemExit(
            f"{site_dir} is not a git repo — run the one-time setup first "
            f"(see deploy/README.md).{repository_hint}"
        )
    if push and not PAGES_URL:
        raise SystemExit(
            "SOFT_PAGES_REPO must be set to owner/repository before publishing"
        )
    _git(site_dir, "add", "-A")
    status = _git(site_dir, "status", "--porcelain").stdout.strip()
    if not status:
        print("[publish] no changes to commit (matrices unchanged since last publish)")
        return False
    msg = message or f"refresh {date}"
    _git(site_dir, "commit", "-m", msg)
    print(f"[publish] committed: {msg}")
    if push:
        _git(site_dir, "push")
        print(f"[publish] pushed → {PAGES_URL}")
    else:
        print("[publish] --no-push: committed locally, not pushed")
    return True


def run(*, push: bool, date: str | None = None, message: str | None = None) -> None:
    date = date or _today()
    written = assemble(SITE_DIR, date)
    print(f"[publish] assembled {len(written)} file(s) in {SITE_DIR}")
    publish(SITE_DIR, push=push, date=date, message=message)


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-push", action="store_true", help="assemble + commit only (no network push)")
    ap.add_argument("--date", default=None, help="date stamp for the landing page (YYYY-MM-DD)")
    ap.add_argument("--message", default=None, help="commit message override")
    args = ap.parse_args()
    run(push=not args.no_push, date=args.date, message=args.message)


if __name__ == "__main__":
    main()
