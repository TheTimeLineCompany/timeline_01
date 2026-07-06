"""Read-only adapter for remote V4 Wikipedia tables."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.text_cleaner import WikiLink, clean_wikitext, extract_wikilinks


@dataclass
class WikiSection:
    """A Wikipedia section row from V4 tables."""

    title_id: int
    heading_id: int
    heading: str
    level: int | None
    parent_id: int | None
    content_raw: str
    clean_text: str = field(default="", repr=False)
    links: list[WikiLink] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not self.clean_text:
            self.clean_text = clean_wikitext(self.content_raw)
        if not self.links:
            self.links = extract_wikilinks(self.content_raw)


@dataclass
class WikiArticle:
    """A canonical article with sections."""

    title: str
    title_id: int
    sections: list[WikiSection]

    @property
    def all_links(self) -> list[str]:
        """Return unique link targets in article order."""

        seen: set[str] = set()
        out: list[str] = []
        for section in self.sections:
            for link in section.links:
                if link.target not in seen:
                    seen.add(link.target)
                    out.append(link.target)
        return out


class WikiAdapter:
    """Read-only V4 wiki table access."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search_titles(self, query: str, limit: int = 15) -> list[dict[str, object]]:
        """Search article titles with V4 lookup table."""

        normalized = " ".join((query or "").split()).strip()
        if len(normalized) < 2:
            return []
        bounded_limit = max(1, min(limit, 50))

        try:
            rows = await self._search_cached_titles(normalized, bounded_limit)
            if rows:
                return rows
        except DBAPIError:
            # The local search cache is an optimization. If it has not been
            # bootstrapped yet, preserve the reader by falling back to source.
            await self.session.rollback()

        fallback_rows = await self._search_source_titles(normalized, bounded_limit)
        if fallback_rows:
            return fallback_rows

        return []

    async def _search_source_titles(self, normalized: str, limit: int) -> list[dict[str, object]]:
        """Search source lookup in exact/prefix/contains phases to avoid broad scans."""

        output: list[dict[str, object]] = []
        seen: set[int] = set()

        phases = [
            (
                """
                SELECT heading, title_id
                FROM public."wiki_content_lookup_V4"
                WHERE lower(heading) = lower(:raw_query)
                ORDER BY CASE WHEN heading = :raw_query THEN 0 ELSE 1 END, length(heading), heading
                LIMIT :limit
                """,
                {"raw_query": normalized, "limit": limit},
            ),
            (
                """
                SELECT heading, title_id
                FROM public."wiki_content_lookup_V4"
                WHERE heading ILIKE :prefix
                  AND lower(heading) <> lower(:raw_query)
                ORDER BY length(heading), heading
                LIMIT :limit
                """,
                {"raw_query": normalized, "prefix": f"{normalized}%", "limit": limit},
            ),
            (
                """
                SELECT heading, title_id
                FROM public."wiki_content_lookup_V4"
                WHERE heading ILIKE :query
                  AND heading NOT ILIKE :prefix
                  AND lower(heading) <> lower(:raw_query)
                ORDER BY length(heading), heading
                LIMIT :limit
                """,
                {"raw_query": normalized, "query": f"%{normalized}%", "prefix": f"{normalized}%", "limit": limit},
            ),
        ]

        for sql, params in phases:
            if len(output) >= limit:
                break
            params = {**params, "limit": max(1, limit - len(output))}
            result = await self.session.execute(text(sql), params)
            for heading, title_id in result.fetchall():
                numeric_id = int(title_id)
                if numeric_id in seen:
                    continue
                seen.add(numeric_id)
                output.append({"title": str(heading), "title_id": numeric_id})
                if len(output) >= limit:
                    break
            if output and len(normalized) >= 5:
                return output
        return output

    async def _search_source_titles_legacy(self, normalized: str, limit: int) -> list[dict[str, object]]:
        """Original broad search kept for comparison during tuning."""

        result = await self.session.execute(
            text(
                """
                SELECT heading, title_id
                FROM public."wiki_content_lookup_V4"
                WHERE heading ILIKE :query
                ORDER BY
                    CASE
                        WHEN lower(heading) = lower(:raw_query) THEN 0
                        WHEN heading ILIKE :prefix THEN 1
                        ELSE 2
                    END,
                    length(heading)
                LIMIT :limit
                """
            ),
            {
                "query": f"%{normalized}%",
                "prefix": f"{normalized}%",
                "raw_query": normalized,
                "limit": limit,
            },
        )
        return [{"title": row[0], "title_id": int(row[1])} for row in result.fetchall()]

    async def _search_cached_titles(self, normalized: str, limit: int) -> list[dict[str, object]]:
        """Use the V4 indexed title cache for fast exact/prefix/contains search."""

        normalized_lc = normalized.lower()
        result = await self.session.execute(
            text(
                """
                SELECT heading, title_id, rank
                FROM (
                    SELECT heading, title_id, 0 AS rank
                    FROM timeline_v4.title_search_cache
                    WHERE heading_lc = :query_lc

                    UNION ALL

                    SELECT heading, title_id, 1 AS rank
                    FROM timeline_v4.title_search_cache
                    WHERE heading_lc LIKE :prefix_lc
                      AND heading_lc <> :query_lc

                    UNION ALL

                    SELECT heading, title_id, 2 AS rank
                    FROM timeline_v4.title_search_cache
                    WHERE heading_lc LIKE :contains_lc
                      AND heading_lc NOT LIKE :prefix_lc
                      AND heading_lc <> :query_lc
                ) AS candidates
                ORDER BY rank ASC, length(heading) ASC, heading ASC
                LIMIT :limit
                """
            ),
            {
                "query_lc": normalized_lc,
                "prefix_lc": f"{normalized_lc}%",
                "contains_lc": f"%{normalized_lc}%",
                "limit": limit * 3,
            },
        )
        seen: set[int] = set()
        output: list[dict[str, object]] = []
        for heading, title_id, _rank in result.fetchall():
            numeric_id = int(title_id)
            if numeric_id in seen:
                continue
            seen.add(numeric_id)
            output.append({"title": str(heading), "title_id": numeric_id})
            if len(output) >= limit:
                break
        return output

    async def resolve_lookup(self, title: str) -> tuple[str, int] | None:
        """Resolve an input title to canonical lookup row."""

        normalized = " ".join((title or "").replace("_", " ").split()).strip()
        if not normalized:
            return None

        result = await self.session.execute(
            text(
                """
                SELECT heading, title_id
                FROM public."wiki_content_lookup_V4"
                WHERE lower(heading) = lower(:title)
                ORDER BY CASE WHEN heading = :title THEN 0 ELSE 1 END, length(heading)
                LIMIT 1
                """
            ),
            {"title": normalized},
        )
        row = result.fetchone()
        if row:
            return str(row[0]), int(row[1])

        result = await self.session.execute(
            text(
                """
                SELECT heading, title_id
                FROM public."wiki_content_lookup_V4"
                WHERE heading ILIKE :query
                ORDER BY CASE WHEN heading ILIKE :prefix THEN 0 ELSE 1 END, length(heading)
                LIMIT 1
                """
            ),
            {"query": f"%{normalized}%", "prefix": f"{normalized}%"},
        )
        row = result.fetchone()
        if row:
            return str(row[0]), int(row[1])
        return None

    async def get_article_by_title_id(self, title: str, title_id: int) -> WikiArticle | None:
        """Load article sections by resolved title id."""

        result = await self.session.execute(
            text(
                """
                SELECT heading_id, heading, level, parent_id, content
                FROM public."wiki_content_CSV_V4"
                WHERE title_id = :title_id
                ORDER BY heading_id ASC
                """
            ),
            {"title_id": title_id},
        )
        rows = result.fetchall()
        sections = [
            WikiSection(
                title_id=title_id,
                heading_id=int(row[0]),
                heading=str(row[1] or ""),
                level=None if row[2] is None else int(row[2]),
                parent_id=None if row[3] is None else int(row[3]),
                content_raw=str(row[4] or ""),
            )
            for row in rows
            if row[4]
        ]
        if not sections:
            return None
        return WikiArticle(title=title, title_id=title_id, sections=sections)

    async def get_article(self, title: str) -> WikiArticle | None:
        """Resolve and load an article."""

        lookup = await self.resolve_lookup(title)
        if lookup is None:
            return None
        canonical, title_id = lookup
        return await self.get_article_by_title_id(canonical, title_id)
