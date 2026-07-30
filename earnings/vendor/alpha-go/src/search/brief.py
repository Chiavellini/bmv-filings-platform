"""Deterministic evidence-brief model and offline exports."""
from __future__ import annotations

import csv
import html
import io
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class EvidenceItem:
    doc_id: str
    company: str
    period: str | None
    doc_type: str
    title: str
    text: str
    markdown_path: str
    source_path: str | None = None
    char_start: int | None = None


@dataclass
class EvidenceBrief:
    """Small, serializable set of analyst-selected passages."""
    query: str = ""
    items: list[EvidenceItem] = field(default_factory=list)

    def add(self, item: EvidenceItem) -> None:
        if not any(existing.doc_id == item.doc_id and existing.text == item.text
                   for existing in self.items):
            self.items.append(item)

    def remove(self, doc_id: str, text: str) -> None:
        self.items = [i for i in self.items if not (i.doc_id == doc_id and i.text == text)]

    def to_csv(self) -> str:
        out = io.StringIO()
        fields = list(asdict(EvidenceItem("", "", None, "", "", "", "")).keys())
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(item) for item in self.items)
        return out.getvalue()

    def to_html(self) -> str:
        cards = []
        for i, item in enumerate(self.items, 1):
            source = item.source_path or item.markdown_path
            cards.append(
                f"<article><h2>[{i}] {html.escape(item.company)} · "
                f"{html.escape(item.period or '—')}</h2>"
                f"<p><b>{html.escape(item.title)}</b> · {html.escape(item.doc_type)}<br>"
                f"<small>{html.escape(source)}</small></p>"
                f"<blockquote>{html.escape(item.text)}</blockquote></article>"
            )
        title = f"Evidence brief{': ' + html.escape(self.query) if self.query else ''}"
        return ("<!doctype html><meta charset='utf-8'><title>" + title + "</title>"
                "<style>body{font:16px system-ui;max-width:960px;margin:2rem auto;color:#20242a}"
                "article{border-top:1px solid #ccd3da;padding:1rem 0}"
                "blockquote{white-space:pre-wrap;line-height:1.5;margin-left:0}</style>"
                f"<h1>{title}</h1>" + "".join(cards))
