"""Reset running agent jobs after a fresh local app restart.

The startup script kills local worker processes before launching a new worker.
Any job left in ``running`` belonged to an old process and must be made
claimable again.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import text

from app.db.database import async_session_factory, close_engine


RESET_JOB_TYPES = (
    "temporal_extract_v1",
    "related_l1_l2_explain_v1",
    "timeline_context_promote_v1",
    "core_digest_v1",
    "embedding_generate_v1",
    "cpu_entity_precision_v1",
    "graph_frontier_discover_v1",
)


async def main() -> None:
    async with async_session_factory() as session:
        result = await session.execute(
            text(
                """
                UPDATE timeline_v4.agent_job
                SET status = 'retry',
                    locked_by = NULL,
                    locked_at = NULL,
                    run_after = NULL,
                    last_error = 'Reset by local Timeline startup after stale worker cleanup.',
                    updated_at = :now
                WHERE status = 'running'
                  AND job_type = ANY(:job_types)
                """
            ),
            {"now": datetime.now(UTC).replace(tzinfo=None), "job_types": list(RESET_JOB_TYPES)},
        )
        await session.commit()
        print(f"       Reset {result.rowcount or 0} running agent job lock(s).")
    await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
