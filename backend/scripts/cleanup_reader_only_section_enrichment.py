"""Remove enrichment artifacts for reader-only sections.

References, bibliography, external links, and similar sections stay in
section_clean so the reader can display them. They should not contribute tags,
times, related cards, or timeline context.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, select

from app.content_filters import is_content_section
from app.db.database import async_session_factory, close_engine
from app.db.models import RelatedCache, SectionClean, SectionTag, SectionTime, TimelineContextCache


async def main() -> None:
    async with async_session_factory() as session:
        result = await session.execute(select(SectionClean))
        reader_only = [
            section.section_key
            for section in result.scalars().all()
            if not is_content_section(section)
        ]
        if not reader_only:
            print("reader_only_sections: 0")
            return

        deleted = {}
        for name, stmt in {
            "section_tags": delete(SectionTag).where(SectionTag.section_key.in_(reader_only)),
            "section_time": delete(SectionTime).where(SectionTime.section_key.in_(reader_only)),
            "related_cache": delete(RelatedCache).where(RelatedCache.from_section_key.in_(reader_only)),
            "timeline_context_from": delete(TimelineContextCache).where(
                TimelineContextCache.from_section_key.in_(reader_only)
            ),
            "timeline_context_source": delete(TimelineContextCache).where(
                TimelineContextCache.source_section_key.in_(reader_only)
            ),
        }.items():
            result = await session.execute(stmt)
            deleted[name] = result.rowcount or 0
        await session.commit()

        print(f"reader_only_sections: {len(reader_only)}")
        for name, count in deleted.items():
            print(f"{name}: {count}")

    await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
