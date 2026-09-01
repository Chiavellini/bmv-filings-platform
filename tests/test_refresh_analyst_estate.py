from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import refresh_analyst_estate as refresh


def test_refresh_builds_registry_directed_estate_commands(tmp_path: Path, monkeypatch) -> None:
    estate = tmp_path / "estate"
    bridge = SimpleNamespace(
        estate_root=estate,
        catalog_path=estate / "catalog.db",
        alpha_go_corpus_dir=estate / "projections" / "alpha-go",
        alpha_go_index_path=estate / "indexes" / "alpha_go.db",
    )
    monkeypatch.setattr(refresh, "_require_estate", lambda: bridge)
    original_is_file = Path.is_file

    def installed_runtime(path: Path) -> bool:
        if path.name in {"refresh-quarterly-estate", "process-estate-outbox"}:
            return True
        if path.name == "python" and path.parent.name == "bin":
            return True
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", installed_runtime)
    calls: list[tuple[str, list[str], Path, bool]] = []

    def capture(label, argv, *, cwd, dry_run):
        calls.append((label, argv, cwd, dry_run))

    monkeypatch.setattr(refresh, "_run", capture)

    assert refresh.main(["--only", "ac", "--dry-run"]) == 0
    assert [label for label, *_rest in calls] == [
        "Documentos trimestrales dirigidos",
        "Derivados e índice Alpha",
        "Noticias dirigidas (GDELT, metadatos y enlaces)",
        "Noticias dirigidas (Google News RSS, metadatos y enlaces)",
    ]

    quarterly = calls[0][1]
    assert quarterly[1:3] == ["sync", "--apply"]
    assert quarterly[-2:] == ["--only", "ac"]
    assert "--allow-coverage-gaps" in quarterly

    delivery = calls[1][1]
    assert str(bridge.catalog_path) in delivery
    assert str(bridge.alpha_go_index_path) in delivery
    assert str(bridge.alpha_go_corpus_dir) in delivery

    news = calls[2][1]
    assert news[-2:] == ["--companies", "ac"]
    assert str(estate / "news" / "catalog.db") in news
    assert str(estate / "news" / "corpus") in news

    google = calls[3][1]
    assert google[-2:] == ["--companies", "ac"]
    assert str(estate / "news" / "catalog.db") in google
    assert str(estate / "news" / "corpus") in google
