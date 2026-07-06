"""Section parsing and cache writes."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.provenance import make_provenance
from app.db.models import SectionClean
from app.ingestion.text_cleaner import safe_html_from_wikitext
from app.ingestion.wiki_adapter import WikiArticle, WikiSection

settings = get_settings()


def section_key(title_id: int, heading_id: int) -> str:
    """Return the stable V4 section key."""

    return f"{title_id}:{heading_id}"


class SectionCacheService:
    """Parse and cache section text/links."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_cached_section(self, key: str) -> SectionClean | None:
        """Return cached section if present."""

        result = await self.session.execute(select(SectionClean).where(SectionClean.section_key == key))
        return result.scalar_one_or_none()

    async def cache_article(self, article: WikiArticle) -> list[SectionClean]:
        """Parse/cache all sections for an article."""

        rows: list[SectionClean] = []
        for section in article.sections:
            rows.append(await self.cache_section(article, section))
        return rows

    async def cache_section(self, article: WikiArticle, section: WikiSection) -> SectionClean:
        """Parse/cache one section."""

        key = section_key(section.title_id, section.heading_id)
        run_id = f"parse:{uuid.uuid4()}"
        provenance = make_provenance(
            title_id=section.title_id,
            heading_id=section.heading_id,
            char_start=0,
            char_end=len(section.content_raw or ""),
            parser_version=settings.parser_version,
            model_version=settings.model_version,
            run_id=run_id,
        )
        links_json: list[dict[str, Any]] = [
            {
                "target": link.target,
                "label": link.label,
                "char_start": link.char_start,
                "char_end": link.char_end,
            }
            for link in section.links
        ]
        values = {
            "section_key": key,
            "title_id": section.title_id,
            "heading_id": section.heading_id,
            "title": article.title,
            "heading": section.heading or ("Introduction" if section.level == 0 else ""),
            "level": section.level,
            "parent_id": section.parent_id,
            "clean_text": section.clean_text,
            "content_html": safe_html_from_wikitext(section.content_raw),
            "links_json": links_json,
            "provenance_json": provenance,
            "parser_version": settings.parser_version,
        }
        stmt = insert(SectionClean).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_section_clean_key",
            set_={
                "title": stmt.excluded.title,
                "heading": stmt.excluded.heading,
                "level": stmt.excluded.level,
                "parent_id": stmt.excluded.parent_id,
                "clean_text": stmt.excluded.clean_text,
                "content_html": stmt.excluded.content_html,
                "links_json": stmt.excluded.links_json,
                "provenance_json": stmt.excluded.provenance_json,
                "parser_version": stmt.excluded.parser_version,
            },
        )
        await self.session.execute(stmt)
        await self.session.commit()
        cached = await self.get_cached_section(key)
        if cached is None:
            raise RuntimeError(f"Section cache write failed: {key}")
        return cached
