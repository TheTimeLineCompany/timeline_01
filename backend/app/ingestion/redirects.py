"""Redirect resolution for V4 wiki titles."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import RedirectMap
from app.ingestion.text_cleaner import detect_redirect_target, normalize_title_target
from app.ingestion.wiki_adapter import WikiAdapter

settings = get_settings()


class RedirectResolver:
    """Resolve article titles through redirect stubs with caching."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.adapter = WikiAdapter(session)

    async def resolve_title(self, title: str, *, max_depth: int = 5) -> tuple[str, int] | None:
        """Resolve input title to final canonical title/title_id."""

        normalized = normalize_title_target(title)
        if normalized is None:
            return None

        cached = await self._get_cached(normalized)
        if cached is not None:
            return cached

        seen: set[int] = set()
        current_title = normalized
        first_title = normalized
        first_id: int | None = None

        for depth in range(max_depth + 1):
            lookup = await self.adapter.resolve_lookup(current_title)
            if lookup is None:
                return None
            heading, title_id = lookup
            if first_id is None:
                first_id = title_id
            if title_id in seen:
                return heading, title_id
            seen.add(title_id)

            raw = await self._get_lead_content(title_id)
            redirect_target = detect_redirect_target(raw or "")
            if redirect_target is None:
                await self._cache_redirect(
                    from_title_id=first_id,
                    from_heading=first_title,
                    to_title_id=title_id,
                    to_heading=heading,
                    depth=depth,
                )
                return heading, title_id
            current_title = redirect_target

        return None

    async def _get_cached(self, normalized_title: str) -> tuple[str, int] | None:
        result = await self.session.execute(
            text(
                f"""
                SELECT to_heading, to_title_id
                FROM "{settings.pg_schema}".redirect_map
                WHERE normalized_from = :normalized
                LIMIT 1
                """
            ),
            {"normalized": normalized_title.lower()},
        )
        row = result.fetchone()
        if row and row[1] is not None:
            return str(row[0]), int(row[1])
        return None

    async def _get_lead_content(self, title_id: int) -> str | None:
        result = await self.session.execute(
            text(
                """
                SELECT content
                FROM public."wiki_content_CSV_V4"
                WHERE title_id = :title_id
                ORDER BY heading_id ASC
                LIMIT 1
                """
            ),
            {"title_id": title_id},
        )
        row = result.fetchone()
        return None if row is None else str(row[0] or "")

    async def _cache_redirect(
        self,
        *,
        from_title_id: int | None,
        from_heading: str,
        to_title_id: int,
        to_heading: str,
        depth: int,
    ) -> None:
        normalized_from = normalize_title_target(from_heading) or from_heading
        normalized_to = normalize_title_target(to_heading) or to_heading
        stmt = insert(RedirectMap).values(
            from_title_id=from_title_id,
            from_heading=from_heading,
            normalized_from=normalized_from.lower(),
            to_title_id=to_title_id,
            to_heading=to_heading,
            normalized_to=normalized_to.lower(),
            depth=depth,
            parser_version=settings.parser_version,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_redirect_normalized_from",
            set_={
                "from_title_id": stmt.excluded.from_title_id,
                "to_title_id": stmt.excluded.to_title_id,
                "to_heading": stmt.excluded.to_heading,
                "normalized_to": stmt.excluded.normalized_to,
                "depth": stmt.excluded.depth,
                "parser_version": stmt.excluded.parser_version,
            },
        )
        await self.session.execute(stmt)
        await self.session.commit()
