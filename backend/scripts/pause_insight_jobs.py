"""Pause or resume Timeline insight jobs for Core-mode system checks."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from sqlalchemy import update

from app.db.database import async_session_factory, close_engine
from app.db.models import AgentJob

INSIGHT_JOB_TYPES = [
    "core_digest_v1",
    "temporal_extract_v1",
    "related_l1_l2_explain_v1",
]
CPU_SAFE_JOB_TYPES = [
    "embedding_generate_v1",
    "cpu_entity_precision_v1",
    "timeline_context_promote_v1",
]

PAUSED_STATUS = "paused_core_mode"


async def pause_jobs() -> int:
    async with async_session_factory() as session:
        result = await session.execute(
            update(AgentJob)
            .where(AgentJob.job_type.in_(INSIGHT_JOB_TYPES))
            .where(AgentJob.status.in_(["pending", "retry"]))
            .values(status=PAUSED_STATUS, updated_at=datetime.utcnow())
        )
        await session.commit()
        return result.rowcount or 0


async def resume_jobs() -> int:
    async with async_session_factory() as session:
        result = await session.execute(
            update(AgentJob)
            .where(AgentJob.job_type.in_(INSIGHT_JOB_TYPES))
            .where(AgentJob.status == PAUSED_STATUS)
            .values(status="pending", run_after=None, updated_at=datetime.utcnow())
        )
        await session.commit()
        return result.rowcount or 0

async def main(action: str) -> None:
    try:
        count = await (resume_jobs() if action == "resume" else pause_jobs())
        print({"action": action, "rows_updated": count, "job_types": INSIGHT_JOB_TYPES})
    finally:
        await close_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pause or resume insight jobs.")
    parser.add_argument("action", choices=["pause", "resume"], nargs="?", default="pause")
    args = parser.parse_args()
    asyncio.run(main(args.action))
