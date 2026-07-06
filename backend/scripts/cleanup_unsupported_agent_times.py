"""Remove agent temporal rows whose evidence is not present in the section text."""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, select

from app.db.database import async_session_factory, close_engine
from app.db.models import SectionClean, SectionTime, TimeDimension
from app.workers.temporal_agent import SOURCE, _find_evidence_start


async def main() -> None:
    async with async_session_factory() as session:
        result = await session.execute(
            select(SectionTime, SectionClean, TimeDimension)
            .join(SectionClean, SectionClean.section_key == SectionTime.section_key)
            .join(TimeDimension, TimeDimension.time_ref_id == SectionTime.time_ref_id)
            .where(SectionTime.source == SOURCE)
        )
        bad_ids: list[int] = []
        for section_time, section, time_dim in result.all():
            evidence = str((time_dim.metadata_json or {}).get("evidence") or "").strip()
            if not evidence or _find_evidence_start(section.clean_text or "", evidence) < 0:
                bad_ids.append(section_time.id)

        if bad_ids:
            await session.execute(delete(SectionTime).where(SectionTime.id.in_(bad_ids)))
            await session.commit()
        print(f"Deleted {len(bad_ids)} unsupported agent temporal row(s).")
    await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
