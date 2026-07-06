"""Durable graph-frontier discovery jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import AgentJob, SectionClean
from app.graph.frontier import discover_and_sync_graph_frontier
from app.orchestration.state import upsert_processing_state

JOB_TYPE = "graph_frontier_discover_v1"
MODEL_VERSION = "graph-frontier-v1"

settings = get_settings()


def article_frontier_section_key(title_id: int) -> str:
    """Return the synthetic queue key for article-level frontier work."""

    return f"article:{int(title_id)}"


async def enqueue_graph_frontier_job(
    session: AsyncSession,
    *,
    title: str,
    title_id: int,
    priority: int = 32,
    force: bool = False,
) -> int:
    """Create one graph-frontier job for the article."""

    section_key = article_frontier_section_key(title_id)
    stmt = insert(AgentJob).values(
        job_type=JOB_TYPE,
        status="pending",
        priority=priority,
        title_id=title_id,
        section_key=section_key,
        payload_json={
            "title": title,
            "title_id": int(title_id),
            "max_l1_articles": settings.graph_frontier_l1_limit,
            "max_l1_articles_to_cache": settings.graph_frontier_l1_cache_limit,
            "max_l2_links_per_l1": settings.graph_frontier_l2_links_per_l1,
            "l2_source_scope": settings.graph_frontier_l2_source_scope,
        },
        attempts=0,
        max_attempts=2,
        locked_by=None,
        locked_at=None,
        last_error=None,
        completed_at=None,
    )
    set_values: dict[str, Any] = {
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
            where=AgentJob.status.in_(["failed", "retry"]),
        )

    result = await session.execute(stmt)
    await _sync_graph_frontier_state(session, title=title, title_id=title_id, section_key=section_key)
    await session.commit()
    return result.rowcount or 0


async def process_graph_frontier_job(session: AsyncSession, job: AgentJob) -> None:
    """Cache selected L1 intros, discover L2 links, and sync the frontier to Neo4j."""

    payload = job.payload_json or {}
    title = str(payload.get("title") or "")
    if not title:
        title = await _title_from_sections(session, job.title_id)
    sections = await _load_article_sections(session, job.title_id)
    if not sections:
        raise ValueError(f"No cached sections found for graph frontier title_id={job.title_id}")

    result = await discover_and_sync_graph_frontier(
        session,
        title=title,
        title_id=job.title_id,
        sections=sections,
        max_l1_articles=int(payload.get("max_l1_articles") or settings.graph_frontier_l1_limit),
        max_l1_articles_to_cache=int(
            payload.get("max_l1_articles_to_cache") or settings.graph_frontier_l1_cache_limit
        ),
        max_l2_links_per_l1=int(payload.get("max_l2_links_per_l1") or settings.graph_frontier_l2_links_per_l1),
        l2_source_scope=str(payload.get("l2_source_scope") or settings.graph_frontier_l2_source_scope),
    )

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
        title_id=job.title_id,
        section_key="",
        area="graph_frontier",
        state="completed",
        expected_count=max(1, len(result.l1_articles) + len(result.l2_articles)),
        completed_count=len(result.l1_articles) + len(result.l2_articles),
        detail=(
            f"Graph frontier discovered {len(result.l1_articles)} L1 article(s), "
            f"{len(result.l2_articles)} L2 article(s), cached {result.l1_articles_cached} L1 intro article(s)."
        ),
        reason="Durable graph-frontier worker completed L1/L2 link framework discovery.",
        source="graph_frontier_worker",
        metadata={
            "l1_articles": len(result.l1_articles),
            "l2_articles": len(result.l2_articles),
            "l0_to_l1_edges": result.l0_to_l1_edges,
            "l1_to_l2_edges": result.l1_to_l2_edges,
            "l1_articles_cached": result.l1_articles_cached,
            "unresolved_links": result.unresolved_links,
            "l2_source_scope": str(payload.get("l2_source_scope") or settings.graph_frontier_l2_source_scope),
            "model_version": MODEL_VERSION,
        },
        commit=False,
    )
    await session.commit()


async def _sync_graph_frontier_state(
    session: AsyncSession,
    *,
    title: str,
    title_id: int,
    section_key: str,
) -> None:
    result = await session.execute(
        select(AgentJob.status, AgentJob.last_error)
        .where(AgentJob.job_type == JOB_TYPE)
        .where(AgentJob.section_key == section_key)
    )
    row = result.first()
    status = str(row[0]) if row else "pending"
    last_error = row[1] if row else None
    state = _job_status_to_processing_state(status)
    await upsert_processing_state(
        session,
        title_id=title_id,
        section_key="",
        area="graph_frontier",
        state=state,
        expected_count=1,
        completed_count=1 if state == "completed" else 0,
        pending_count=1 if state == "pending" else 0,
        running_count=1 if state == "running" else 0,
        failed_count=1 if state == "attention" else 0,
        detail=f"Graph frontier job for {title} is {status}.",
        reason="Durable graph-frontier discovery is queued behind the first render.",
        last_error=last_error,
        source="graph_frontier_enqueue",
        metadata={
            "model_version": MODEL_VERSION,
            "max_l1_articles": settings.graph_frontier_l1_limit,
            "max_l1_articles_to_cache": settings.graph_frontier_l1_cache_limit,
            "max_l2_links_per_l1": settings.graph_frontier_l2_links_per_l1,
            "l2_source_scope": settings.graph_frontier_l2_source_scope,
        },
        commit=False,
    )


async def _load_article_sections(session: AsyncSession, title_id: int) -> list[SectionClean]:
    result = await session.execute(
        select(SectionClean)
        .where(SectionClean.title_id == title_id)
        .order_by(SectionClean.heading_id.asc(), SectionClean.id.asc())
    )
    return list(result.scalars().all())


async def _title_from_sections(session: AsyncSession, title_id: int) -> str:
    result = await session.execute(
        select(SectionClean.title)
        .where(SectionClean.title_id == title_id)
        .order_by(SectionClean.heading_id.asc(), SectionClean.id.asc())
        .limit(1)
    )
    return str(result.scalar_one_or_none() or title_id)


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
