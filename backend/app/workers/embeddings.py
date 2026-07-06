"""CPU embedding generation jobs for cached sections."""

from __future__ import annotations

import asyncio
import threading
import json
import time
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.core.config import get_settings
from app.db.models import AgentJob, SectionClean
from app.orchestration.state import upsert_processing_state

JOB_TYPE = "embedding_generate_v1"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

settings = get_settings()
_MODEL: Any | None = None
_MODEL_LOCK = threading.Lock()
_TORCH_CONFIGURED = False


async def enqueue_embedding_jobs(
    session: AsyncSession,
    sections: list[SectionClean],
    *,
    priority: int = 62,
    force: bool = False,
) -> int:
    """Create section embedding jobs for cached content sections."""

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
                "embedding_model": EMBEDDING_MODEL,
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
    set_values = {
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
            where=AgentJob.status == "failed",
        )

    result = await session.execute(stmt)
    await _sync_embedding_processing_state(session, useful_sections)
    await session.commit()
    return result.rowcount or 0


async def process_embedding_job(session: AsyncSession, job: AgentJob) -> None:
    """Generate and persist one same-article batch of section embeddings."""

    extra_jobs: list[AgentJob] = []
    try:
        extra_jobs = await _claim_sibling_embedding_jobs(session, job, limit=_embedding_batch_size() - 1)
        jobs = [job, *extra_jobs]
        sections = await _load_sections(session, [item.section_key for item in jobs])
        section_by_key = {section.section_key: section for section in sections}

        missing_primary = section_by_key.get(job.section_key) is None
        if missing_primary:
            raise ValueError(f"Cached section not found: {job.section_key}")

        work: list[tuple[AgentJob, SectionClean]] = []
        for item in jobs:
            section = section_by_key.get(item.section_key)
            if section is None:
                await _mark_job_failed(session, item, ValueError(f"Cached section not found: {item.section_key}"))
                continue
            if not is_content_section(section):
                await _mark_job_done(session, item, section, "Skipped non-content section.", commit=False)
                continue
            work.append((item, section))

        if not work:
            await session.commit()
            return

        started = time.perf_counter()
        vectors = await asyncio.to_thread(_encode_texts, [section.clean_text or "" for _, section in work])
        await _upsert_embeddings(session, [section for _, section in work], vectors)
        latency_ms = int((time.perf_counter() - started) * 1000)
        per_section_ms = int(latency_ms / max(1, len(work)))
        for item, section in work:
            await _mark_job_done(
                session,
                item,
                section,
                (
                    f"Embedding generated in batch of {len(work)} section(s); "
                    f"{latency_ms} ms total, ~{per_section_ms} ms/section."
                ),
                commit=False,
            )
        await session.commit()
    except Exception as exc:
        for extra in extra_jobs:
            await _mark_job_failed(session, extra, exc)
        await session.commit()
        raise


async def _claim_sibling_embedding_jobs(session: AsyncSession, primary: AgentJob, *, limit: int) -> list[AgentJob]:
    if limit <= 0:
        return []
    now = datetime.utcnow()
    result = await session.execute(
        select(AgentJob)
        .where(
            AgentJob.job_type == JOB_TYPE,
            AgentJob.title_id == primary.title_id,
            AgentJob.id != primary.id,
            AgentJob.status.in_(["pending", "retry"]),
            AgentJob.attempts < AgentJob.max_attempts,
            or_(AgentJob.run_after.is_(None), AgentJob.run_after <= now),
        )
        .order_by(AgentJob.priority.asc(), AgentJob.created_at.asc(), AgentJob.id.asc())
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    jobs = list(result.scalars().all())
    for job in jobs:
        job.status = "running"
        job.attempts += 1
        job.locked_by = primary.locked_by
        job.locked_at = now
        job.updated_at = now
        await upsert_processing_state(
            session,
            title_id=job.title_id,
            section_key=job.section_key,
            area="embeddings",
            state="running",
            expected_count=1,
            running_count=1,
            detail=f"{JOB_TYPE} sibling job claimed by {primary.locked_by}.",
            reason="Embedding worker is processing this section in a batch.",
            source="embedding_worker",
            commit=False,
        )
    return jobs


async def _sync_embedding_processing_state(session: AsyncSession, sections: list[SectionClean]) -> None:
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
            area="embeddings",
            state=state,
            expected_count=1,
            completed_count=1 if state == "completed" else 0,
            pending_count=1 if state == "pending" else 0,
            running_count=1 if state == "running" else 0,
            failed_count=1 if state == "attention" else 0,
            detail=f"Embedding job is {status}.",
            reason="Synchronized from durable job state after enqueue.",
            last_error=last_error,
            source="embedding_enqueue",
            commit=False,
        )


async def _load_section(session: AsyncSession, section_key: str) -> SectionClean | None:
    result = await session.execute(select(SectionClean).where(SectionClean.section_key == section_key))
    return result.scalar_one_or_none()


async def _load_sections(session: AsyncSession, section_keys: list[str]) -> list[SectionClean]:
    if not section_keys:
        return []
    result = await session.execute(select(SectionClean).where(SectionClean.section_key.in_(section_keys)))
    return list(result.scalars().all())


async def _upsert_embeddings(
    session: AsyncSession,
    sections: list[SectionClean],
    vectors: list[list[float]],
) -> None:
    for section, vector in zip(sections, vectors, strict=False):
        vector_literal = "[" + ",".join(f"{value:.8f}" for value in vector) + "]"
        await session.execute(
            text(
                f"""
                INSERT INTO "{settings.pg_schema}".section_embedding
                    (section_key, title_id, heading_id, embedding, embedding_model, provenance_json, created_at, updated_at)
                VALUES (
                    :section_key,
                    :title_id,
                    :heading_id,
                    CAST(:embedding AS vector),
                    :embedding_model,
                    CAST(:provenance_json AS jsonb),
                    now(),
                    now()
                )
                ON CONFLICT (section_key) DO UPDATE
                    SET embedding = EXCLUDED.embedding,
                        embedding_model = EXCLUDED.embedding_model,
                        provenance_json = EXCLUDED.provenance_json,
                        updated_at = now()
                """
            ),
            {
                "section_key": section.section_key,
                "title_id": section.title_id,
                "heading_id": section.heading_id,
                "embedding": vector_literal,
                "embedding_model": EMBEDDING_MODEL,
                "provenance_json": json.dumps(section.provenance_json or {}),
            },
        )


async def _mark_job_done(
    session: AsyncSession,
    job: AgentJob,
    section: SectionClean,
    detail: str,
    *,
    commit: bool = True,
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
        area="embeddings",
        state="completed",
        expected_count=1,
        completed_count=1,
        detail=detail,
        reason="Embedding worker completed this section.",
        source="embedding_worker",
        metadata={"embedding_model": EMBEDDING_MODEL},
        commit=False,
    )
    if commit:
        await session.commit()


async def _mark_job_failed(session: AsyncSession, job: AgentJob, exc: Exception) -> None:
    now = datetime.utcnow()
    job.last_error = str(exc)
    job.locked_by = None
    job.locked_at = None
    job.updated_at = now
    if job.attempts >= job.max_attempts:
        job.status = "failed"
        job.completed_at = now
        state = "attention"
    else:
        job.status = "retry"
        job.run_after = None
        state = "pending"
    await upsert_processing_state(
        session,
        title_id=job.title_id,
        section_key=job.section_key,
        area="embeddings",
        state=state,
        expected_count=1,
        completed_count=0,
        pending_count=1 if state == "pending" else 0,
        failed_count=1 if state == "attention" else 0,
        detail=f"{JOB_TYPE} job did not complete.",
        reason=str(exc),
        last_error=str(exc),
        source="embedding_worker",
        commit=False,
    )


def _encode_texts(texts: list[str]) -> list[list[float]]:
    with _MODEL_LOCK:
        model = _model()
        vectors = model.encode(
            texts,
            batch_size=min(_embedding_batch_size(), max(1, len(texts))),
            show_progress_bar=False,
            normalize_embeddings=True,
        )
    return [[float(value) for value in vector.tolist()] for vector in vectors]


def _model() -> Any:
    global _MODEL
    if _MODEL is None:
        _configure_torch_threads()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("sentence-transformers is required for embedding jobs") from exc
        _MODEL = SentenceTransformer(EMBEDDING_MODEL)
    return _MODEL


def _configure_torch_threads() -> None:
    """Bound PyTorch CPU use so embeddings use spare CPU without starving the app."""

    global _TORCH_CONFIGURED
    if _TORCH_CONFIGURED:
        return
    try:
        import torch

        torch.set_num_threads(max(1, int(settings.embedding_torch_threads)))
        try:
            torch.set_num_interop_threads(max(1, int(settings.embedding_torch_interop_threads)))
        except RuntimeError:
            pass
    except Exception:
        pass
    _TORCH_CONFIGURED = True


def _embedding_batch_size() -> int:
    return max(1, min(256, int(settings.embedding_batch_size)))


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
