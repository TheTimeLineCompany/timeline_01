"""Graph-first article-load orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import case, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.core.config import get_settings
from app.db.models import AgentJob, SectionClean
from app.graph.backbone import sync_article_processing_coverage_to_neo4j
from app.orchestration.state import upsert_processing_state
from app.workers.core_digest import enqueue_core_digest_job
from app.workers.cpu_entities import enqueue_cpu_entity_jobs
from app.workers.embeddings import enqueue_embedding_jobs
from app.workers.graph_frontier import enqueue_graph_frontier_job
from app.workers.related_cache import enqueue_related_cache_jobs
from app.workers.related_agent import enqueue_related_sweep_pack_jobs, enqueue_section_insight_jobs
from app.workers.temporal_agent import enqueue_temporal_jobs
from app.workers.timeline_context import enqueue_timeline_context_jobs


@dataclass(frozen=True)
class ArticlePipelineResult:
    """Observable summary for one article-load orchestration pass."""

    title_id: int
    section_count: int
    content_section_count: int
    jobs_enqueued: dict[str, int] = field(default_factory=dict)
    graph_nodes: int = 0
    graph_edges: int = 0


class ArticleLoadGraphState(TypedDict, total=False):
    """LangGraph state for one article-load orchestration pass."""

    session: AsyncSession
    title: str
    title_id: int
    sections: list[SectionClean]
    content_sections: list[SectionClean]
    enable_insights: bool
    agent_temporal: bool
    agent_related: bool
    related_warmup_limit: int
    force: bool
    source: str
    jobs_enqueued: dict[str, int]
    graph_nodes: int
    graph_edges: int
    frontier_l1_count: int
    frontier_l2_count: int
    frontier_edges: int


async def orchestrate_article_load(
    session: AsyncSession,
    *,
    title: str,
    title_id: int,
    sections: list[SectionClean],
    enable_insights: bool = False,
    agent_temporal: bool = False,
    agent_related: bool = False,
    related_warmup_limit: int = 0,
    force: bool = False,
    source: str = "article_load",
) -> ArticlePipelineResult:
    """Run the non-blocking article-load orchestration sequence.

    This function must stay read-path safe: it can write durable cache/job/state
    rows and lightweight graph projections, but it must not call the LLM.
    """

    result_state = await article_load_graph().ainvoke(
        {
            "session": session,
            "title": title,
            "title_id": title_id,
            "sections": sections,
            "enable_insights": enable_insights,
            "agent_temporal": agent_temporal,
            "agent_related": agent_related,
            "related_warmup_limit": related_warmup_limit,
            "force": force,
            "source": source,
            "jobs_enqueued": {},
            "graph_nodes": 0,
            "graph_edges": 0,
            "frontier_l1_count": 0,
            "frontier_l2_count": 0,
            "frontier_edges": 0,
        }
    )
    content_sections = result_state.get("content_sections", useful_sections(sections))
    jobs = result_state.get("jobs_enqueued", {})
    return ArticlePipelineResult(
        title_id=title_id,
        section_count=len(sections),
        content_section_count=len(content_sections),
        jobs_enqueued=jobs,
        graph_nodes=int(result_state.get("graph_nodes") or 0),
        graph_edges=int(result_state.get("graph_edges") or 0),
    )


def article_load_graph() -> Any:
    """Return the compiled LangGraph article-load orchestration graph."""

    return _ARTICLE_LOAD_GRAPH


def useful_sections(sections: list[SectionClean]) -> list[SectionClean]:
    """Return reader/content sections that should receive enrichment."""

    return [section for section in sections if is_content_section(section)]


async def _node_article_shell_ready(state: ArticleLoadGraphState) -> dict[str, Any]:
    sections = state["sections"]
    content_sections = useful_sections(sections)
    await _mark_article_shell_ready(
        state["session"],
        title_id=state["title_id"],
        sections=sections,
        content_sections=content_sections,
        agent_temporal=bool(state.get("agent_temporal", True)),
        agent_related=bool(state.get("agent_related", True)),
        enable_insights=bool(state.get("enable_insights", False)),
        source=str(state.get("source") or "article_load"),
    )
    await _sync_pipeline_coverage(state["session"], title_id=state["title_id"], sections=sections)
    return {"content_sections": content_sections}


async def _node_graph_framework(state: ArticleLoadGraphState) -> dict[str, Any]:
    graph_nodes = 1 + len(state["sections"])
    graph_edges = len(state["sections"])
    await _mark_graph_framework_state(
        state["session"],
        title_id=state["title_id"],
        sections=state["sections"],
        graph_nodes=graph_nodes,
        graph_edges=graph_edges,
        frontier_l1_count=0,
        frontier_l2_count=0,
        frontier_edges=0,
        frontier_l1_cached=0,
        unresolved_links=0,
        source=str(state.get("source") or "article_load"),
    )
    await _sync_pipeline_coverage(state["session"], title_id=state["title_id"], sections=state["sections"])
    return {
        "graph_nodes": graph_nodes,
        "graph_edges": graph_edges,
        "frontier_l1_count": 0,
        "frontier_l2_count": 0,
        "frontier_edges": 0,
    }


async def _node_l0_enrichment_fanout(state: ArticleLoadGraphState) -> dict[str, Any]:
    session = state["session"]
    sections = state["sections"]
    content_sections = state.get("content_sections") or useful_sections(sections)
    title_id = state["title_id"]
    force = bool(state.get("force", False))
    enable_insights = bool(state.get("enable_insights", False))
    jobs: dict[str, int] = {}
    jobs["graph_frontier"] = await enqueue_graph_frontier_job(
        session,
        title=state["title"],
        title_id=title_id,
        priority=32,
        force=force,
    )
    jobs["embeddings"] = await enqueue_embedding_jobs(
        session,
        content_sections,
        priority=35,
        force=force,
    )
    jobs["cpu_entities"] = await enqueue_cpu_entity_jobs(
        session,
        content_sections,
        priority=37 if force else 52,
        force=force,
    )
    jobs["related_cache"] = await enqueue_related_cache_jobs(
        session,
        content_sections,
        priority=42 if force else 54,
        force=force,
    )
    if not enable_insights:
        await _mark_enrichment_fanout_state(
            session,
            title_id=title_id,
            content_sections=content_sections,
            jobs=jobs,
            source=str(state.get("source") or "article_load"),
            enable_insights=enable_insights,
        )
        await _prioritize_active_article_jobs(
            session,
            title_id=title_id,
            source=str(state.get("source") or "article_load"),
        )
        await _sync_pipeline_coverage(session, title_id=title_id, sections=sections)
        return {"jobs_enqueued": jobs}

    jobs["core_digest"] = await enqueue_core_digest_job(
        session,
        title_id=title_id,
        title=state["title"],
        sections=sections,
        priority=30,
        force=force,
    )
    if state.get("agent_temporal", True):
        jobs["temporal"] = await enqueue_temporal_jobs(
            session,
            content_sections,
            priority=40 if force else 75,
            force=force,
        )
    if state.get("agent_related", True):
        warmup_limit = int(state.get("related_warmup_limit") or 0)
        related_sections = content_sections[:warmup_limit] if warmup_limit else content_sections
        focus_limit = max(0, int(get_settings().llm_focus_topk_routes or 0))
        focus_sections = related_sections[:]
        focus_selected = sorted(
            focus_sections,
            key=lambda section: (
                -len(section.links_json or []),
                int(section.heading_id or 0),
                section.section_key,
            ),
        )[:focus_limit]
        focus_keys = {section.section_key for section in focus_selected}
        jobs["section_insight"] = await enqueue_section_insight_jobs(
            session,
            focus_selected,
            priority=24 if force else 32,
            force=force,
            limit=focus_limit,
        )
        sweep_sections = [section for section in related_sections if section.section_key not in focus_keys]
        jobs["related_sweep_pack"] = await enqueue_related_sweep_pack_jobs(
            session,
            sweep_sections,
            priority=45 if force else 62,
            force=force,
            pack_size=max(1, min(8, int(get_settings().llm_sections_per_temporal_call or 4))),
        )
        jobs["timeline_context"] = await enqueue_timeline_context_jobs(
            session,
            related_sections,
            priority=55 if force else 68,
            force=force,
        )

    await _mark_enrichment_fanout_state(
        session,
        title_id=title_id,
        content_sections=content_sections,
        jobs=jobs,
        source=str(state.get("source") or "article_load"),
        enable_insights=enable_insights,
    )
    await _prioritize_active_article_jobs(
        session,
        title_id=title_id,
        source=str(state.get("source") or "article_load"),
    )
    await _sync_pipeline_coverage(session, title_id=title_id, sections=sections)
    return {"jobs_enqueued": jobs}


async def _sync_pipeline_coverage(
    session: AsyncSession,
    *,
    title_id: int,
    sections: list[SectionClean],
) -> None:
    section_keys = [section.section_key for section in sections]
    await sync_article_processing_coverage_to_neo4j(
        session,
        title_id=title_id,
        section_keys=section_keys,
    )


def _build_article_load_graph() -> Any:
    graph = StateGraph(ArticleLoadGraphState)
    graph.add_node("article_shell_ready", _node_article_shell_ready)
    graph.add_node("graph_framework", _node_graph_framework)
    graph.add_node("l0_enrichment_fanout", _node_l0_enrichment_fanout)
    graph.add_edge(START, "article_shell_ready")
    graph.add_edge("article_shell_ready", "graph_framework")
    graph.add_edge("graph_framework", "l0_enrichment_fanout")
    graph.add_edge("l0_enrichment_fanout", END)
    return graph.compile()


_ARTICLE_LOAD_GRAPH = _build_article_load_graph()


async def _mark_article_shell_ready(
    session: AsyncSession,
    *,
    title_id: int,
    sections: list[SectionClean],
    content_sections: list[SectionClean],
    agent_temporal: bool,
    agent_related: bool,
    enable_insights: bool,
    source: str,
) -> None:
    content_count = len(content_sections)
    await upsert_processing_state(
        session,
        title_id=title_id,
        area="article_load",
        state="completed",
        expected_count=content_count,
        completed_count=content_count,
        detail=f"Article shell returned with {content_count} content section(s).",
        reason="Article shell is available; downstream graph and enrichment work is tracked separately.",
        source=source,
        metadata={
            "agent_temporal_requested": agent_temporal,
            "agent_related_requested": agent_related,
            "enable_insights": enable_insights,
            "section_count": len(sections),
        },
    )


async def _mark_graph_framework_state(
    session: AsyncSession,
    *,
    title_id: int,
    sections: list[SectionClean],
    graph_nodes: int,
    graph_edges: int,
    frontier_l1_count: int,
    frontier_l2_count: int,
    frontier_edges: int,
    frontier_l1_cached: int,
    unresolved_links: int,
    source: str,
) -> None:
    state = "completed" if graph_nodes or frontier_l1_count else "pending"
    frontier_ready = frontier_l1_count or frontier_l2_count or frontier_edges
    await upsert_processing_state(
        session,
        title_id=title_id,
        area="graph_framework",
        state=state,
        expected_count=max(1, len(sections) + frontier_l1_count + frontier_l2_count),
        completed_count=(len(sections) + frontier_l1_count + frontier_l2_count) if state == "completed" else 0,
        pending_count=0 if state == "completed" else len(sections),
        detail=(
            (
                f"Graph frontier synced: projection={graph_nodes} node(s)/{graph_edges} edge(s); "
                f"frontier={frontier_l1_count} L1, {frontier_l2_count} L2, {frontier_edges} link edge(s)."
            )
            if frontier_ready
            else f"Graph shell ready with {len(sections)} section node(s); L1/L2 frontier is queued."
        ),
        reason=(
            "Cheap L0/L1/L2 link framework is available in Neo4j; enrichment/scoring can refine it later."
            if frontier_ready
            else "Article display should not wait for link resolution; graph-frontier worker expands L1/L2 after render."
        ),
        source=source,
        metadata={
            "graph_nodes": graph_nodes,
            "graph_edges": graph_edges,
            "frontier_l1_count": frontier_l1_count,
            "frontier_l2_count": frontier_l2_count,
            "frontier_edges": frontier_edges,
            "frontier_l1_cached": frontier_l1_cached,
            "unresolved_links": unresolved_links,
            "stage": "graph_frontier_discovery",
        },
    )


async def _mark_enrichment_fanout_state(
    session: AsyncSession,
    *,
    title_id: int,
    content_sections: list[SectionClean],
    jobs: dict[str, int],
    source: str,
    enable_insights: bool,
) -> None:
    expected = len(content_sections)
    pending = sum(max(0, count) for count in jobs.values())
    await upsert_processing_state(
        session,
        title_id=title_id,
        area="l0_enrichment",
        state="pending" if pending else "completed",
        expected_count=expected,
        completed_count=0 if pending else expected,
        pending_count=pending,
        detail=(
            f"L0 enrichment fanout queued {pending} job mutation(s)."
            if pending
            else "L0 enrichment fanout found existing jobs/cache rows."
        ),
        reason=(
            (
                "Core mode is active: only graph frontier runs behind the first render; insights are disabled."
                if not enable_insights
                else "Graph frontier, embeddings, CPU entity precision, core digest, temporal, related, and "
                "timeline-context work run behind the first render."
            )
        ),
        source=source,
        metadata={"jobs_enqueued": jobs, "enable_insights": enable_insights},
    )


async def _prioritize_active_article_jobs(
    session: AsyncSession,
    *,
    title_id: int,
    source: str,
    defer_seconds: int = 120,
) -> None:
    """Prefer the article the user just loaded without killing in-flight work."""

    now = datetime.utcnow()
    active_job_types = [
        "core_digest_v1",
        "embedding_generate_v1",
        "cpu_entity_precision_v1",
        "related_cache_build_v1",
        "graph_frontier_discover_v1",
        "temporal_extract_v1",
        "section_insight_v1",
        "related_sweep_pack_v1",
        "related_l1_l2_explain_v1",
        "timeline_context_promote_v1",
    ]
    active_stmt = (
        update(AgentJob)
        .where(AgentJob.title_id == title_id)
        .where(AgentJob.job_type.in_(active_job_types))
        .where(AgentJob.status.in_(["pending", "retry"]))
        .values(
            priority=case(
                (AgentJob.job_type == "section_insight_v1", 18),
                (AgentJob.job_type == "core_digest_v1", 19),
                (AgentJob.job_type.in_(["graph_frontier_discover_v1", "embedding_generate_v1", "cpu_entity_precision_v1"]), 20),
                (AgentJob.job_type == "related_cache_build_v1", 22),
                (AgentJob.job_type == "temporal_extract_v1", 30),
                (AgentJob.job_type == "related_sweep_pack_v1", 42),
                (AgentJob.job_type == "related_l1_l2_explain_v1", 45),
                (AgentJob.job_type == "timeline_context_promote_v1", 50),
                else_=AgentJob.priority,
            ),
            run_after=None,
            updated_at=now,
        )
    )
    active_result = await session.execute(active_stmt)

    deferred_stmt = (
        update(AgentJob)
        .where(AgentJob.title_id != title_id)
        .where(AgentJob.job_type.in_(active_job_types))
        .where(AgentJob.status.in_(["pending", "retry"]))
        .where(AgentJob.priority >= 25)
        .values(
            run_after=now + timedelta(seconds=defer_seconds),
            updated_at=now,
        )
    )
    deferred_result = await session.execute(deferred_stmt)
    await upsert_processing_state(
        session,
        title_id=title_id,
        area="active_article_focus",
        state="completed",
        expected_count=1,
        completed_count=1,
        detail=(
            f"Focused active article queue: boosted {active_result.rowcount or 0} job(s), "
            f"deferred {deferred_result.rowcount or 0} non-active pending job(s)."
        ),
        reason="The currently loaded article should not compete equally with stale/background enrichment.",
        source=source,
        metadata={
            "boosted_jobs": active_result.rowcount or 0,
            "deferred_jobs": deferred_result.rowcount or 0,
            "defer_seconds": defer_seconds,
            "running_jobs_left_untouched": True,
        },
        commit=False,
    )
    await session.commit()
