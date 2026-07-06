"""Remove invalid L2 rows that point back to the main article."""

from __future__ import annotations

import asyncio

from sqlalchemy import String, cast, delete, exists, func, or_, select

from app.db.database import async_session_factory, close_engine
from app.db.models import RelatedCache, SectionClean, SectionTag, SectionTime, TimelineContextCache


async def main() -> None:
    async with async_session_factory() as session:
        malformed_filter = SectionClean.title == cast(SectionClean.title_id, String)
        malformed_count_result = await session.execute(
            select(func.count()).select_from(SectionClean).where(malformed_filter)
        )
        malformed_count = int(malformed_count_result.scalar_one() or 0)

        related_stmt = delete(RelatedCache).where(
            RelatedCache.level == 2,
            exists(
                select(1)
                .select_from(SectionClean)
                .where(SectionClean.section_key == RelatedCache.from_section_key)
                .where(SectionClean.title_id == RelatedCache.to_title_id)
            ),
        )
        related_result = await session.execute(related_stmt)

        context_stmt = delete(TimelineContextCache).where(
            or_(
                TimelineContextCache.source_title_id == TimelineContextCache.from_title_id,
                TimelineContextCache.source_title.op("~")(r"^[0-9]+$"),
                exists(
                    select(1)
                    .select_from(SectionClean)
                    .where(SectionClean.section_key == TimelineContextCache.source_section_key)
                    .where(malformed_filter)
                ),
                (
                    TimelineContextCache.level == 2
                )
                & (
                    TimelineContextCache.relevance_score < 0.72
                )
                & ~TimelineContextCache.signals_json["why_source"].astext.in_(["agent_related_v1"]),
            ),
        )
        context_result = await session.execute(context_stmt)

        tag_result = await session.execute(
            delete(SectionTag).where(
                exists(
                    select(1)
                    .select_from(SectionClean)
                    .where(SectionClean.section_key == SectionTag.section_key)
                    .where(malformed_filter)
                )
            )
        )
        time_result = await session.execute(
            delete(SectionTime).where(
                exists(
                    select(1)
                    .select_from(SectionClean)
                    .where(SectionClean.section_key == SectionTime.section_key)
                    .where(malformed_filter)
                )
            )
        )
        section_result = await session.execute(delete(SectionClean).where(malformed_filter))
        malformed_deleted = {
            "section_tags": tag_result.rowcount or 0,
            "section_time": time_result.rowcount or 0,
            "section_clean": section_result.rowcount or 0,
        }
        await session.commit()

        print(f"related_cache_self_l2_deleted: {related_result.rowcount or 0}")
        print(f"timeline_context_self_l2_deleted: {context_result.rowcount or 0}")
        print(f"malformed_numeric_title_sections: {malformed_count}")
        for name, count in malformed_deleted.items():
            print(f"malformed_{name}_deleted: {count}")

    await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
