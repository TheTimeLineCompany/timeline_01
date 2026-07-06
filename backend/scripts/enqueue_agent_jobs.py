"""Enqueue V4 agent jobs for one article.

Examples:
    python scripts/enqueue_agent_jobs.py "Abraham Lincoln" --limit 3 --force
"""

from __future__ import annotations

import argparse
import asyncio

from app.db.database import async_session_factory, close_engine
from app.services import ReaderService
from app.workers.temporal_agent import JOB_TYPE, enqueue_temporal_jobs


async def enqueue(title: str, limit: int, force: bool) -> None:
    async with async_session_factory() as session:
        reader = ReaderService(session)
        canonical, title_id, sections = await reader.get_article(title, seed=False)
        selected = sections[:limit] if limit else sections
        count = await enqueue_temporal_jobs(session, selected, priority=50, force=force)
        print(
            f"Article: {canonical!r} title_id={title_id} "
            f"sections_considered={len(selected)} job_type={JOB_TYPE} jobs_enqueued={count}"
        )
    await close_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Enqueue V4 temporal agent jobs.")
    parser.add_argument("title", nargs="?", default="Abraham Lincoln")
    parser.add_argument("--limit", type=int, default=0, help="Maximum sections to enqueue; 0 means all.")
    parser.add_argument("--force", action="store_true", help="Reset existing jobs to pending.")
    args = parser.parse_args()
    asyncio.run(enqueue(args.title, args.limit, args.force))


if __name__ == "__main__":
    main()
