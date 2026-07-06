"""Requeue related-agent jobs for stale rows missing evidence offsets."""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select, text

from app.db.database import async_session_factory, close_engine
from app.db.models import SectionClean
from app.workers.related_agent import SOURCE, enqueue_related_jobs


async def _find_section_keys(title: str | None, limit: int) -> list[str]:
    title_filter = ""
    params: dict[str, object] = {"source": SOURCE, "limit": limit}
    if title:
        title_filter = "AND lower(sc.title) = lower(:title)"
        params["title"] = title

    query = text(
        f"""
        SELECT DISTINCT sc.section_key
        FROM timeline_v4.related_cache rc
        JOIN timeline_v4.section_clean sc
          ON sc.section_key = rc.from_section_key
        WHERE rc.why_source = :source
          {title_filter}
          AND (
            rc.signals_json -> 'agent_related_v1' ->> 'evidence_char_start' IS NULL
            OR rc.signals_json -> 'agent_related_v1' ->> 'evidence_char_end' IS NULL
          )
        ORDER BY sc.section_key
        LIMIT :limit
        """
    )
    async with async_session_factory() as session:
        result = await session.execute(query, params)
        return [str(row[0]) for row in result.all()]


async def _load_sections(section_keys: list[str]) -> list[SectionClean]:
    if not section_keys:
        return []
    async with async_session_factory() as session:
        result = await session.execute(
            select(SectionClean).where(SectionClean.section_key.in_(section_keys)).order_by(SectionClean.section_key)
        )
        return list(result.scalars().all())


async def main(*, title: str | None, limit: int, priority: int, dry_run: bool) -> None:
    section_keys = await _find_section_keys(title, limit)
    sections = await _load_sections(section_keys)
    print(f"Found {len(sections)} section(s) with stale/missing related-agent evidence offsets.")
    for section in sections[:20]:
        print(f"- {section.title} / {section.heading} [{section.section_key}]")
    if len(sections) > 20:
        print(f"... {len(sections) - 20} more")

    if dry_run or not sections:
        await close_engine()
        return

    async with async_session_factory() as session:
        queued = await enqueue_related_jobs(session, sections, priority=priority, force=True)
        await session.commit()
        print(f"Requeued {queued} related-agent job(s).")
    await close_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Requeue related-agent jobs for rows generated before evidence offsets were persisted."
    )
    parser.add_argument("--title", help="Exact article title to refresh, for example 'Abraham Lincoln'.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum distinct sections to requeue.")
    parser.add_argument("--priority", type=int, default=32, help="Queue priority for forced refresh jobs.")
    parser.add_argument("--dry-run", action="store_true", help="Print matching sections without requeueing jobs.")
    args = parser.parse_args()
    asyncio.run(main(title=args.title, limit=args.limit, priority=args.priority, dry_run=args.dry_run))
