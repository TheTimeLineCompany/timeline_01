"""Background worker for L1/L2 timeline-context promotion."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.db.models import AgentJob, SectionClean
from app.orchestration.state import upsert_processing_state
from app.timeline.context_service import TimelineContextService

JOB_TYPE = "timeline_context_promote_v1"


async def enqueue_timeline_context_jobs(
    session: AsyncSession,
    sections: list[SectionClean],
    *,
    priority: int = 95,
    force: bool = False,
) -> int:
    """Create or refresh timeline-context promotion jobs for source sections."""

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
    if force:
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_job_type_section",
            set_={
                "status": "pending",
                "priority": stmt.excluded.priority,
                "title_id": stmt.excluded.title_id,
                "payload_json": stmt.excluded.payload_json,
                "attempts": 0,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "completed_at": None,
                "updated_at": func.now(),
            },
        )
    else:
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_job_type_section",
            set_={
                "status": "pending",
                "priority": stmt.excluded.priority,
                "title_id": stmt.excluded.title_id,
                "payload_json": stmt.excluded.payload_json,
                "attempts": 0,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "completed_at": None,
                "updated_at": func.now(),
            },
            where=AgentJob.status.in_(["pending", "retry", "failed"]),
        )

    result = await session.execute(stmt)
    status_result = await session.execute(
        select(AgentJob.section_key, AgentJob.status, AgentJob.last_error)
        .where(AgentJob.job_type == JOB_TYPE)
        .where(AgentJob.section_key.in_([section.section_key for section in useful_sections]))
    )
    job_states = {str(section_key): (str(status), last_error) for section_key, status, last_error in status_result.all()}
    for section in useful_sections:
        status, last_error = job_states.get(section.section_key, ("pending", None))
        state = _job_status_to_processing_state(status)
        await upsert_processing_state(
            session,
            title_id=section.title_id,
            section_key=section.section_key,
            area="timeline_context",
            state=state,
            expected_count=1,
            completed_count=1 if state == "completed" else 0,
            pending_count=1 if state == "pending" else 0,
            running_count=1 if state == "running" else 0,
            failed_count=1 if state == "attention" else 0,
            detail=f"Timeline-context job is {status}.",
            reason="Synchronized from durable job state after enqueue.",
            last_error=last_error,
            source="timeline_context_enqueue",
            commit=False,
        )
    await session.commit()
    return result.rowcount or 0


async def process_timeline_context_job(session: AsyncSession, job: AgentJob) -> None:
    """Promote cached related/temporal rows into timeline context for one section."""

    section = await _load_section(session, job.section_key)
    if section is None:
        raise ValueError(f"Cached section not found: {job.section_key}")

    result = await TimelineContextService(session).build_for_article(
        [section],
        section_limit=0,
        related_limit=200,
    )
    now = datetime.utcnow()
    _apply_timeline_context_job_result(job, pending=result.pending, now=now)
    await upsert_processing_state(
        session,
        title_id=section.title_id,
        section_key=section.section_key,
        area="timeline_context",
        state="pending" if result.pending else "completed",
        expected_count=max(1, result.rows_upserted + result.temporal_jobs_enqueued),
        completed_count=result.rows_upserted if result.rows_upserted else (0 if result.pending else 1),
        pending_count=1 if result.pending else 0,
        detail=(
            f"Promoted {result.rows_upserted} context row(s); "
            f"queued {result.temporal_jobs_enqueued} temporal job(s)."
        ),
        reason="Timeline-context worker completed this pass.",
        source="timeline_context_worker",
        metadata={
            "rows_upserted": result.rows_upserted,
            "temporal_jobs_enqueued": result.temporal_jobs_enqueued,
            "pending": result.pending,
        },
        commit=False,
    )
    await session.commit()


async def _load_section(session: AsyncSession, section_key: str) -> SectionClean | None:
    result = await session.execute(select(SectionClean).where(SectionClean.section_key == section_key))
    return result.scalar_one_or_none()


def _job_status_to_processing_state(status: str) -> str:
    if status == "succeeded":
        return "completed"
    if status == "running":
        return "running"
    if status in {"pending", "retry"}:
        return "pending"
    if status == "failed":
        return "attention"
    return "idle"


def _apply_timeline_context_job_result(  # type: ignore[no-untyped-def]
    job,
    *,
    pending: bool,
    now: datetime,
    retry_delay_seconds: int = 30,
) -> None:
    """Persist dependency-aware timeline-context job completion state.

    A pending build result means prerequisite related/temporal work was queued or is
    still running. The job should not burn one of its retry attempts for that case,
    and it must not be marked succeeded until context promotion actually has a
    complete dependency pass.
    """

    if pending:
        job.status = "pending"
        job.attempts = max(0, int(job.attempts or 0) - 1)
        job.completed_at = None
        job.last_error = None
        job.locked_by = None
        job.locked_at = None
        job.run_after = now + timedelta(seconds=retry_delay_seconds)
    else:
        job.status = "succeeded"
        job.completed_at = now
        job.last_error = None
        job.locked_by = None
        job.locked_at = None
        job.run_after = None
    job.updated_at = now
