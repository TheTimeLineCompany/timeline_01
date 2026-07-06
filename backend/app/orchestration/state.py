"""Observable processing-state ledger helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentJob, ProcessingState


async def upsert_processing_state(
    session: AsyncSession,
    *,
    title_id: int,
    area: str,
    state: str,
    section_key: str | None = None,
    expected_count: int = 0,
    completed_count: int = 0,
    pending_count: int = 0,
    running_count: int = 0,
    failed_count: int = 0,
    detail: str = "",
    reason: str = "",
    last_error: str | None = None,
    source: str = "derived",
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> None:
    """Insert or update one processing-state row."""

    now = datetime.utcnow()
    row = {
        "title_id": title_id,
        "section_key": section_key or "",
        "area": area,
        "state": state,
        "expected_count": max(0, int(expected_count)),
        "completed_count": max(0, int(completed_count)),
        "pending_count": max(0, int(pending_count)),
        "running_count": max(0, int(running_count)),
        "failed_count": max(0, int(failed_count)),
        "detail": detail,
        "reason": reason,
        "last_error": last_error,
        "source": source,
        "metadata_json": metadata or {},
        "updated_at": now,
    }
    stmt = insert(ProcessingState).values(row)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_processing_state_scope_area",
        set_={
            "state": stmt.excluded.state,
            "expected_count": stmt.excluded.expected_count,
            "completed_count": stmt.excluded.completed_count,
            "pending_count": stmt.excluded.pending_count,
            "running_count": stmt.excluded.running_count,
            "failed_count": stmt.excluded.failed_count,
            "detail": stmt.excluded.detail,
            "reason": stmt.excluded.reason,
            "last_error": stmt.excluded.last_error,
            "source": stmt.excluded.source,
            "metadata_json": stmt.excluded.metadata_json,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await session.execute(stmt)
    if commit:
        await session.commit()


async def processing_state_updated_at(
    session: AsyncSession,
    *,
    title_id: int,
    area: str,
    section_key: str | None = None,
) -> datetime | None:
    """Return the last update timestamp for a processing-state row."""

    result = await session.execute(
        select(ProcessingState.updated_at, ProcessingState.created_at)
        .where(ProcessingState.title_id == title_id)
        .where(ProcessingState.section_key == (section_key or ""))
        .where(ProcessingState.area == area)
        .limit(1)
    )
    row = result.first()
    if not row:
        return None
    return row[0] or row[1]


async def reconcile_stale_running_work(
    session: AsyncSession,
    *,
    title_id: int | None = None,
    section_keys: list[str] | None = None,
    older_than_minutes: int = 12,
    commit: bool = True,
) -> dict[str, int]:
    """Release stale running locks and mark old running state rows as stale.

    This is intentionally conservative: only rows already marked `running` and
    older than the threshold are touched. Agent jobs are made retryable so the
    worker pool can continue; processing-state rows are marked stale so the UI
    can explain why work is no longer actively moving.
    """

    now = datetime.utcnow()
    stale_before = now - timedelta(minutes=max(1, int(older_than_minutes)))
    scoped_section_keys = set(section_keys or [])

    job_conditions = [AgentJob.status == "running", AgentJob.locked_at < stale_before]
    exhausted_retry_conditions = [AgentJob.status == "retry", AgentJob.attempts >= AgentJob.max_attempts]
    state_conditions = [
        ProcessingState.state == "running",
        func.coalesce(ProcessingState.updated_at, ProcessingState.created_at) < stale_before,
    ]
    if title_id is not None:
        job_conditions.append(AgentJob.title_id == int(title_id))
        exhausted_retry_conditions.append(AgentJob.title_id == int(title_id))
        state_conditions.append(ProcessingState.title_id == int(title_id))
    if scoped_section_keys:
        article_job_key = f"article:{title_id}" if title_id is not None else None
        job_keys = set(scoped_section_keys)
        if article_job_key:
            job_keys.add(article_job_key)
        job_conditions.append(AgentJob.section_key.in_(sorted(job_keys)))
        exhausted_retry_conditions.append(AgentJob.section_key.in_(sorted(job_keys)))
        state_conditions.append(
            ProcessingState.section_key.in_(sorted(scoped_section_keys | {""}))
        )

    jobs_result = await session.execute(select(AgentJob).where(*job_conditions))
    stale_jobs = list(jobs_result.scalars().all())
    for job in stale_jobs:
        retryable = int(job.attempts or 0) < int(job.max_attempts or 0)
        job.status = "retry" if retryable else "failed"
        job.locked_by = None
        job.locked_at = None
        job.run_after = now if retryable else job.run_after
        job.completed_at = now if not retryable else None
        job.last_error = (
            f"Released stale running lock after {max(1, int(older_than_minutes))} minute(s)."
            if retryable
            else "Stale running lock exceeded max attempts."
        )
        job.updated_at = now

    exhausted_result = await session.execute(select(AgentJob).where(*exhausted_retry_conditions))
    exhausted_jobs = list(exhausted_result.scalars().all())
    for job in exhausted_jobs:
        job.status = "failed"
        job.locked_by = None
        job.locked_at = None
        job.completed_at = now
        job.last_error = job.last_error or "Retry attempts exhausted."
        job.updated_at = now

    states_result = await session.execute(select(ProcessingState).where(*state_conditions))
    stale_states = list(states_result.scalars().all())
    for row in stale_states:
        metadata = dict(row.metadata_json or {})
        metadata["stale_reconciled_at"] = now.isoformat()
        metadata["previous_state"] = row.state
        row.state = "stale"
        row.pending_count = max(int(row.pending_count or 0), int(row.running_count or 0))
        row.running_count = 0
        row.reason = (
            f"Marked stale because no update arrived for {max(1, int(older_than_minutes))} minute(s). "
            "The worker queue will retry matching jobs when possible."
        )
        row.last_error = row.last_error or "Stale running state reconciled."
        row.metadata_json = metadata
        row.updated_at = now

    if commit and (stale_jobs or exhausted_jobs or stale_states):
        await session.commit()
    return {
        "agent_jobs": len(stale_jobs),
        "exhausted_retry_jobs": len(exhausted_jobs),
        "processing_states": len(stale_states),
    }
