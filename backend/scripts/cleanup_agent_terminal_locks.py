"""Clear stale worker lock fields from terminal agent-job states."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.db.database import async_session_factory, close_engine


TERMINAL_STATUSES = ("succeeded", "failed", "paused_core_mode")


async def main() -> None:
    async with async_session_factory() as session:
        result = await session.execute(
            text(
                """
                UPDATE timeline_v4.agent_job
                SET locked_by = NULL,
                    locked_at = NULL,
                    updated_at = now()
                WHERE status = ANY(:terminal_statuses)
                  AND (locked_by IS NOT NULL OR locked_at IS NOT NULL)
                """
            ),
            {"terminal_statuses": list(TERMINAL_STATUSES)},
        )
        await session.commit()
        print(f"Cleared stale lock fields from {result.rowcount or 0} terminal agent job row(s).")
    await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
