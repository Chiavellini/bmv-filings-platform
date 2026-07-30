"""Source adapters emit normalized discovery records without mutating the corpus or index."""

from src.sources.base import SourceAdapter, SourceRecord

__all__ = ["SourceAdapter", "SourceRecord"]
