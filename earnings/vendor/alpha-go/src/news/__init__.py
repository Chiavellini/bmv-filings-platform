"""Rights-aware external-news corpus: discovery, verified company membership, and indexing."""

from src.news.aliases import CompanyAlias, CompanyAliasResolver, aliases_from_companies
from src.news.catalog import NewsCatalog, NewsCatalogStats
from src.news.models import NewsArticle, NewsMembership

__all__ = [
    "CompanyAlias", "CompanyAliasResolver", "NewsArticle", "NewsCatalog", "NewsCatalogStats",
    "NewsMembership", "aliases_from_companies",
]
