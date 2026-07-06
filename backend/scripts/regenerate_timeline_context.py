"""Clear and requeue timeline-context promotion for one article."""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import delete

from app.content_filters import is_content_section
from app.db.database import async_session_factory, close_engine
from app.db.models import TimelineContextCache
from app.services import ReaderService
from app.workers.timeline_context import enqueue_timeline_context_jobs


async def main(title: str, *, priority: int, dry_run: bool) -> None:
    try:
        async with async_session_factory() as session:
            service = ReaderService(session)
            canonical, title_id, sections = await service.get_article(title, seed=False, enrich_ontology=True)
            useful_sections = [section for section in sections if is_content_section(section)]
            print(f"Article: {canonical} ({title_id})")
            print(f"Useful sections: {len(useful_sections)}")
            if dry_run:
                print("Dry run: no rows cleared and no jobs enqueued.")
                return

            delete_result = await session.execute(
                delete(TimelineContextCache).where(TimelineContextCache.from_title_id == title_id)
            )
            queued = await enqueue_timeline_context_jobs(session, useful_sections, priority=priority, force=True)
            await session.commit()
            print(f"Deleted {delete_result.rowcount or 0} timeline_context_cache row(s).")
            print(f"Queued/refreshed {queued} timeline-context job(s).")
    finally:
        await close_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Regenerate cached L1/L2 timeline context for one article.")
    parser.add_argument("title", help="Article title, for example 'Abraham Lincoln'.")
    parser.add_argument("--priority", type=int, default=52, help="Queue priority for timeline-context jobs.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect article/section counts without changing data.")
    args = parser.parse_args()
    asyncio.run(main(args.title, priority=args.priority, dry_run=args.dry_run))
