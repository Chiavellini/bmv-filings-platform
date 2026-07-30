"""The GitHub Pages publisher — wrap body-only deliverables into standalone docs and assemble the site
directory (index + pages). Offline: exercises the pure-filesystem path only (no git, no network)."""
from __future__ import annotations

from scripts import publish_pages


def test_pages_url_requires_an_explicit_owner_and_repository():
    assert publish_pages._pages_url("") == ""
    assert publish_pages._pages_url("owner-only") == ""
    assert (
        publish_pages._pages_url("ExampleOrg/coverage")
        == "https://exampleorg.github.io/coverage/"
    )


def test_wrap_makes_a_valid_standalone_document():
    body = '<title>Soft Coverage — Dense</title>\n<style>.x{}</style>\n<div class="wrap">GRID</div>'
    doc = publish_pages._wrap(body)
    assert doc.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in doc
    assert "width=device-width" in doc                     # viewport for mobile
    assert doc.rstrip().endswith("</html>")
    assert "</body>" in doc
    assert body in doc                                     # original content preserved verbatim


def test_assemble_writes_pages_and_linked_index(tmp_path, monkeypatch):
    master = tmp_path / "_master"
    master.mkdir()
    (master / "soft_coverage_dense.html").write_text(
        '<title>Dense</title><div class="wrap">DENSE-GRID</div>', encoding="utf-8")
    (master / "soft_coverage_master.html").write_text(
        '<title>Core</title><div class="wrap">CORE-GRID</div>', encoding="utf-8")
    monkeypatch.setattr(publish_pages, "DELIVERABLES", [
        (master / "soft_coverage_dense.html", "dense.html", "Dense 100% matrix", "d blurb"),
        (master / "soft_coverage_master.html", "core.html", "Core matrix", "c blurb"),
    ])

    site = tmp_path / "site"
    written = publish_pages.assemble(site, "2026-07-15")

    # both deliverables wrapped into standalone docs
    for name, marker in [("dense.html", "DENSE-GRID"), ("core.html", "CORE-GRID")]:
        doc = (site / name).read_text(encoding="utf-8")
        assert doc.startswith("<!doctype html>") and marker in doc
    # landing links both pages + carries the date stamp
    idx = (site / "index.html").read_text(encoding="utf-8")
    assert 'href="dense.html"' in idx and 'href="core.html"' in idx
    assert "Dense 100% matrix" in idx and "Core matrix" in idx
    assert "2026-07-15" in idx
    assert (site / ".nojekyll").exists()
    assert site / "index.html" in written


def test_assemble_skips_missing_source_but_lands_the_rest(tmp_path, monkeypatch):
    master = tmp_path / "_master"
    master.mkdir()
    (master / "soft_coverage_dense.html").write_text(
        '<title>Dense</title><div class="wrap">DENSE-GRID</div>', encoding="utf-8")
    monkeypatch.setattr(publish_pages, "DELIVERABLES", [
        (master / "soft_coverage_dense.html", "dense.html", "Dense 100% matrix", "d blurb"),
        (master / "missing.html", "core.html", "Core matrix", "c blurb"),   # absent source
    ])

    site = tmp_path / "site"
    publish_pages.assemble(site, "2026-07-15")

    assert (site / "dense.html").exists()
    assert not (site / "core.html").exists()               # missing source skipped
    idx = (site / "index.html").read_text(encoding="utf-8")
    assert 'href="dense.html"' in idx
    assert 'href="core.html"' not in idx                   # not linked when absent
