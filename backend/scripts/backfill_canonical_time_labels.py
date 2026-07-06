"""Backfill canonical labels for time_dimension rows."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db.database import async_session_factory, close_engine
from app.db.models import TimeDimension
from app.workers.temporal_agent import canonical_time_label


async def main() -> None:
    async with async_session_factory() as session:
        result = await session.execute(select(TimeDimension))
        rows = list(result.scalars().all())
        updated = 0
        for row in rows:
            label = canonical_time_label(row.time_ref_id, fallback=row.label)
            if row.label != label:
                row.label = label
                updated += 1
        await session.commit()
        print(f"Backfilled {updated} canonical time label(s).")
    await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
