"""Deterministic company-mention resolver for news ingestion.

No document-trained entity classifier is used here.  An article joins a company corpus only when
an explicitly configured/derived name or safe ticker is present as a whole lexical phrase.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


def normalize(value: str) -> str:
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", value or "") if not unicodedata.combining(c)
    ).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", folded))


@dataclass(frozen=True)
class CompanyAlias:
    company: str
    industry: str | None
    alias: str
    kind: str = "legal_name"      # legal_name | short_name | ticker | manual
    case_sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.company or not normalize(self.alias):
            raise ValueError("company aliases require a company and non-empty alias")


class CompanyAliasResolver:
    def __init__(self, aliases: Iterable[CompanyAlias]):
        unique: dict[tuple[str, str], CompanyAlias] = {}
        for alias in aliases:
            key = (alias.company, normalize(alias.alias))
            # Prefer a manually maintained entry over an auto-derived short name.
            prior = unique.get(key)
            if prior is None or (prior.kind != "manual" and alias.kind == "manual"):
                unique[key] = alias
        self.aliases = tuple(sorted(unique.values(), key=lambda a: (-len(normalize(a.alias)), a.company)))

    def resolve(self, text: str) -> list[CompanyAlias]:
        folded = normalize(text)
        padded = f" {folded} "
        found: list[CompanyAlias] = []
        seen: set[str] = set()
        for alias in self.aliases:
            phrase = normalize(alias.alias)
            if f" {phrase} " in padded and alias.company not in seen:
                found.append(alias)
                seen.add(alias.company)
        return found

    def resolve_verified_aliases(self, aliases: Iterable[str]) -> list[CompanyAlias]:
        """Resolve provider-confirmed aliases without re-parsing publisher content.

        Discovery providers sometimes return only a title and link, while their source-side
        query was run against the complete article.  Those records may join a company only when
        the exact query alias maps to this controlled alias table; a provider cannot supply an
        arbitrary company label.
        """
        by_phrase = {normalize(alias.alias): alias for alias in self.aliases}
        found: list[CompanyAlias] = []
        seen: set[str] = set()
        for value in aliases:
            alias = by_phrase.get(normalize(str(value)))
            if alias is not None and alias.company not in seen:
                found.append(alias)
                seen.add(alias.company)
        return found


_GENERIC_PREFIXES = ("grupo ", "organizacion ", "corporacion ", "compania ", "el puerto de ")


def aliases_from_companies(companies: Iterable[dict], *, manual: Iterable[dict] = ()) -> list[CompanyAlias]:
    """Derive conservative aliases from the BMV catalog, then apply explicit overrides.

    Tickers shorter than four characters are intentionally excluded: symbols such as ``AC`` and
    ``GAP`` are ordinary words/acronyms and would create false automatic memberships.  They can be
    enabled later as a reviewed manual alias with additional provider/entity evidence.
    """
    aliases: list[CompanyAlias] = []
    for row in companies:
        company, industry = str(row.get("slug") or ""), row.get("industry")
        name = str(row.get("company") or "").strip()
        ticker = str(row.get("ticker") or "").strip()
        if not company or not name:
            continue
        aliases.append(CompanyAlias(company, industry, name, "legal_name"))
        normalized = normalize(name)
        for prefix in _GENERIC_PREFIXES:
            if normalized.startswith(prefix):
                short = normalized.removeprefix(prefix).strip()
                if len(short) >= 5:
                    aliases.append(CompanyAlias(company, industry, short, "short_name"))
        if len(ticker) >= 4 and ticker.isalnum():
            aliases.append(CompanyAlias(company, industry, ticker, "ticker"))
    for row in manual:
        aliases.append(CompanyAlias(
            str(row["company"]), row.get("industry"), str(row["alias"]),
            str(row.get("kind") or "manual"), bool(row.get("case_sensitive", False)),
        ))
    return aliases
