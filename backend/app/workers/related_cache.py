"""CPU weighted related-cache build jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.db.models import AgentJob, RelatedCache, SectionClean
from app.orchestration.state import upsert_processing_state
from app.related.service import RelatedInfoService

JOB_TYPE = "related_cache_build_v1"
MODEL_VERSION = "weighted-related-cache-v1"


async def enqueue_related_cache_jobs(
    session: AsyncSession,
    sections: list[SectionClean],
    *,
    priority: int = 44,
    force: bool = False,
) -> int:
    """Create CPU jobs that build weighted related-cache rows for sections."""

    useful_sections = [section for section in sections if is_content_section(section)]
    if not useful_sections:
        return 0

    values = [
        {
            "job_type": JOB_TYPE,
            "status": "pending",
            "priority": priority,
            "title_id": section.title_id,
            "section_key": section.section_key,
            "payload_json": {
                "title": section.title,
                "heading": section.heading,
                "heading_id": section.heading_id,
                "model": MODEL_VERSION,
            },
            "attempts": 0,
            "max_attempts": 3,
            "locked_by": None,
            "locked_at": None,
            "last_error": None,
            "completed_at": None,
        }
        for section in useful_sections
    ]
    stmt = insert(AgentJob).values(values)
    set_values: dict[str, Any] = {
        "status": "pending",
        "priority": stmt.excluded.priority,
        "title_id": stmt.excluded.title_id,
        "payload_json": stmt.excluded.payload_json,
        "attempts": 0,
        "max_attempts": stmt.excluded.max_attempts,
        "locked_by": None,
        "locked_at": None,
        "last_error": None,
        "completed_at": None,
        "updated_at": func.now(),
    }
    if force:
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_job_type_section",
            set_=set_values,
        )
    else:
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_job_type_section",
            set_=set_values,
            where=AgentJob.status.in_(["failed", "retry"]),
        )

    result = await session.execute(stmt)
    await _sync_related_cache_state(session, useful_sections)
    await session.commit()
    return result.rowcount or 0


async def process_related_cache_job(session: AsyncSession, job: AgentJob) -> None:
    """Build weighted related-cache rows for one section without LLM calls."""

    section = await _load_section(session, job.section_key)
    if section is None:
        raise ValueError(f"Cached section not found: {job.section_key}")
    if not is_content_section(section):
        await _mark_job_done(session, job, section, "Skipped non-content section.", 0)
        return

    rows = await RelatedInfoService(session).get_related(section, refresh=True, limit=200)
    await _mark_job_done(
        session,
        job,
        section,
        f"Weighted related cache built: {len(rows)} row(s).",
        len(rows),
    )
    await session.commit()


async def _load_section(session: AsyncSession, section_key: str) -> SectionClean | None:
    result = await session.execute(select(SectionClean).where(SectionClean.section_key == section_key))
    return result.scalar_one_or_none()


async def _mark_job_done(
    session: AsyncSession,
    job: AgentJob,
    section: SectionClean,
    detail: str,
    row_count: int,
) -> None:
    now = datetime.utcnow()
    job.status = "succeeded"
    job.completed_at = now
    job.last_error = None
    job.locked_by = None
    job.locked_at = None
    job.updated_at = now
    await upsert_processing_state(
        session,
        title_id=section.title_id,
        section_key=section.section_key,
        area="related_cache",
        state="completed",
        expected_count=1,
        completed_count=1,
        detail=detail,
        reason="Weighted related-cache build completed.",
        source="related_cache_worker",
        metadata={"rows": row_count, "model": MODEL_VERSION},
        commit=False,
    )


async def _sync_related_cache_state(session: AsyncSession, sections: list[SectionClean]) -> None:
    if not sections:
        return
    result = await session.execute(
        select(RelatedCache.from_section_key, func.count(RelatedCache.id))
        .where(RelatedCache.from_section_key.in_([section.section_key for section in sections]))
        .group_by(RelatedCache.from_section_key)
    )
    row_counts = {str(section_key): int(count or 0) for section_key, count in result.all()}
    for section in sections:
        rows = row_counts.get(section.section_key, 0)
        await upsert_processing_state(
            session,
            title_id=section.title_id,
            section_key=section.section_key,
            area="related_cache",
            state="completed" if rows else "pending",
            expected_count=1,
            completed_count=1 if rows else 0,
            detail=f"{rows} weighted related-cache row(s) available.",
            reason="Weighted related-cache availability synced.",
            source="related_cache_enqueue",
            metadata={"rows": rows, "model": MODEL_VERSION},
            commit=False,
        )
