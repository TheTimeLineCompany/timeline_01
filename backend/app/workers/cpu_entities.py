"""CPU entity precision jobs for high-value sections."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.core.config import get_settings
from app.db.models import AgentJob, SectionClean
from app.ontology.entity_mentions import EntityMentionService
from app.ontology.passage_scores import EntityPassageScoreService
from app.orchestration.state import upsert_processing_state

JOB_TYPE = "cpu_entity_precision_v1"
MODEL_VERSION = "cpu-entity-precision-v1"

settings = get_settings()


async def enqueue_cpu_entity_jobs(
    session: AsyncSession,
    sections: list[SectionClean],
    *,
    priority: int = 38,
    force: bool = False,
    limit: int | None = None,
) -> int:
    """Create precision CPU entity jobs for the highest-priority content sections."""

    section_limit = settings.gliner_decoder_section_limit if limit is None else max(0, int(limit))
    useful_sections = [section for section in sections if is_content_section(section)][:section_limit]
    if not useful_sections or not settings.gliner_decoder_enabled:
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
                "model": settings.gliner_decoder_model,
                "mode": "precision",
            },
            "attempts": 0,
            "max_attempts": 5,
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
    await _sync_cpu_entity_processing_state(session, useful_sections)
    await session.commit()
    return result.rowcount or 0


async def process_cpu_entity_job(session: AsyncSession, job: AgentJob) -> None:
    """Run the decoder-large precision pass for one section and rescore mentions."""

    section = await _load_section(session, job.section_key)
    if section is None:
        raise ValueError(f"Cached section not found: {job.section_key}")
    if not is_content_section(section):
        await _mark_job_done(session, job, section, "Skipped non-content section.", {})
        return

    mentions = EntityMentionService(session)
    scores = EntityPassageScoreService(session)
    mention_result = await mentions.enrich_article(
        [section],
        precision=True,
        precision_section_limit=1,
    )
    score_result = await scores.score_article([section])
    source_counts = dict(mention_result.get("source_counts") or {})
    detail = (
        f"CPU entity precision completed: {mention_result.get('mentions', 0)} mention(s), "
        f"{score_result.get('scores', 0)} passage score(s)."
    )
    await _mark_job_done(session, job, section, detail, source_counts)


async def _sync_cpu_entity_processing_state(session: AsyncSession, sections: list[SectionClean]) -> None:
    status_result = await session.execute(
        select(AgentJob.section_key, AgentJob.status, AgentJob.last_error)
        .where(AgentJob.job_type == JOB_TYPE)
        .where(AgentJob.section_key.in_([section.section_key for section in sections]))
    )
    job_states = {str(section_key): (str(status), last_error) for section_key, status, last_error in status_result.all()}
    for section in sections:
        status, last_error = job_states.get(section.section_key, ("pending", None))
        state = _job_status_to_processing_state(status)
        await upsert_processing_state(
            session,
            title_id=section.title_id,
            section_key=section.section_key,
            area="cpu_entities",
            state=state,
            expected_count=1,
            completed_count=1 if state == "completed" else 0,
            pending_count=1 if state == "pending" else 0,
            running_count=1 if state == "running" else 0,
            failed_count=1 if state == "attention" else 0,
            detail=f"CPU entity precision job is {status}.",
            reason="Synchronized from durable job state after enqueue.",
            last_error=last_error,
            source="cpu_entity_enqueue",
            metadata={"model": settings.gliner_decoder_model},
            commit=False,
        )


async def _load_section(session: AsyncSession, section_key: str) -> SectionClean | None:
    result = await session.execute(select(SectionClean).where(SectionClean.section_key == section_key))
    return result.scalar_one_or_none()


async def _mark_job_done(
    session: AsyncSession,
    job: AgentJob,
    section: SectionClean,
    detail: str,
    source_counts: dict[str, int],
) -> None:
    now = datetime.utcnow()
    job.status = "succeeded"
    job.completed_at = now
    job.last_error = None
    job.locked_by = None
    job.locked_at = None
    job.run_after = None
    job.updated_at = now
    await upsert_processing_state(
        session,
        title_id=section.title_id,
        section_key=section.section_key,
        area="cpu_entities",
        state="completed",
        expected_count=1,
        completed_count=1,
        detail=detail,
        reason="CPU entity precision worker completed this section.",
        source="cpu_entity_worker",
        metadata={
            "model": settings.gliner_decoder_model,
            "source_counts": source_counts,
            "broad_model": settings.gliner2_model,
        },
        commit=False,
    )
    await session.commit()


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
