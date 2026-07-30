"""One-shot, idempotent repair for the F&B upload collision found during manual QA.

Preserves the uploaded Actinver sector report under its own ID with all covered-company
memberships, then restores Bimbo's official 2024-3T source and index record from the immutable
reports tree. The production semantic database is updated incrementally with the certified model.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.corpus.manifest import Document, CorpusManifest, load_manifest, save_manifest
from src.corpus.upload import add_uploaded_document
from src.index.build import add_document_to_index
from src.index.embeddings import get_embedder
from src.index.store import IndexStore


def main() -> None:
    corpus = ROOT / "data" / "corpus"
    db = ROOT / "data" / "index" / "alpha_go_expanded_semantic.db"
    official_md_source = ROOT.parent / "data" / "reports" / "bimbo" / "2024-3T.md"
    official_pdf_source = ROOT.parent / "data" / "reports" / "bimbo" / "2024-3T.pdf"
    old_collision_pdf = corpus / "bimbo" / "2024-3T.pdf"
    repaired_label = "2024-3T-actinver-food-bev"
    repaired_pdf = corpus / "bimbo" / f"{repaired_label}.pdf"

    for required in (official_md_source, official_pdf_source, old_collision_pdf):
        if not required.exists():
            raise FileNotFoundError(required)

    config = yaml.safe_load((ROOT / "configs" / "alpha_go.yaml").read_text(encoding="utf-8"))
    config["index"]["db_path"] = str(db)
    embedder, model_name = get_embedder(config)
    if getattr(embedder, "semantic_quality", "") != "multilingual":
        raise RuntimeError(f"Refusing repair with non-semantic embedder: {model_name}")

    store = IndexStore(db)
    store.connect()

    # On the first run the collided path holds F&B; on a repeated run use the already-separated
    # copy. Read bytes before restoring the official file.
    fb_source = repaired_pdf if repaired_pdf.exists() else old_collision_pdf
    fb_bytes = fb_source.read_bytes()
    fb_stats = add_uploaded_document(
        store, config,
        targets=[
            {"slug": "bimbo", "company": "Grupo Bimbo", "industry": "food"},
            {"slug": "femsa", "company": "FEMSA", "industry": "conglomerate"},
            {"slug": "kof", "company": "Coca-Cola FEMSA", "industry": "beverage"},
            {"slug": "ac", "company": "Arca Continental", "industry": "beverage"},
            {"slug": "becle", "company": "Becle", "industry": "beverage"},
            {"slug": "gruma", "company": "Gruma", "industry": "food"},
        ],
        doc_type_key="analyst_research",
        title="Food & Bev: Open the bottles",
        label=repaired_label,
        filename="F&B_Inicio_pdf.pdf",
        data=fb_bytes,
        embedder=embedder,
        language="en",
        corpus_dir=corpus,
    )

    official_md = official_md_source.read_text(encoding="utf-8")
    official_md_path = corpus / "bimbo" / "2024-3T.md"
    official_pdf_path = corpus / "bimbo" / "2024-3T.pdf"
    shutil.copy2(official_md_source, official_md_path)
    shutil.copy2(official_pdf_source, official_pdf_path)
    official = Document(
        doc_id="bimbo/2024-3T",
        company="bimbo",
        period="2024-3T",
        doc_type="quarterly_release",
        title="Grupo Bimbo 2024-3T",
        source_url=None,
        pdf_path="data/corpus/bimbo/2024-3T.pdf",
        markdown_path="data/corpus/bimbo/2024-3T.md",
        language="en",
        industry="food",
        memberships=[{"company": "bimbo", "industry": "food"}],
        extra={},
        source_path="bimbo/2024-3T.md",
        source_format="md",
        original_path="data/corpus/bimbo/2024-3T.pdf",
        original_format="pdf",
        content_sha256=hashlib.sha256(official_md.encode("utf-8")).hexdigest(),
    )
    official_stats = add_document_to_index(store, official, official_md, config, embedder=embedder)

    manifest = load_manifest(corpus)
    by_id = {doc.doc_id: doc for doc in manifest.documents}
    by_id[official.doc_id] = official
    save_manifest(CorpusManifest(documents=list(by_id.values())), corpus)

    conn = store.connect()
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    print({
        "integrity": integrity,
        "model": model_name,
        "documents": store.count("documents"),
        "chunks": store.count("chunks"),
        "embeddings": store.count("embeddings"),
        "fb": fb_stats,
        "official_bimbo": official_stats,
    })
    store.close()


if __name__ == "__main__":
    main()
