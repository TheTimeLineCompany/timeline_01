"""Reader endpoints for V4."""

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    AgentEnqueueResponse,
    AgentJobSummary,
    ArticleCoreResponse,
    ArticleGraphResponse,
    ArticleResponse,
    ArticleSectionResponse,
    GraphCoverageResponse,
    ProcessingAreaStatus,
    ProcessingStatusResponse,
    RelatedItemResponse,
    RelatedResponse,
    SearchResult,
    SeedResponse,
    TimelineExplainRequest,
    TimelineExplainResponse,
    TimelineEventResponse,
    TimelineResponse,
)
from app.core.config import get_settings
from app.content_filters import is_content_section
from app.db.database import get_session
from app.db.models import AgentJob, AgentTrace, ArticleCore, ProcessingState, RelatedCache, SectionClean, SectionTime, TimeDimension, TimelineContextCache
from app.llm.openai_compatible import LocalLLMClient
from app.graph.backbone import (
    build_article_graph_projection,
    build_article_graph_projection_from_neo4j,
    sync_article_processing_coverage_to_neo4j,
)
from app.graph.coverage_crawler import crawl_article_graph_coverage
from app.orchestration.article_pipeline import _prioritize_active_article_jobs, orchestrate_article_load
from app.orchestration.article_pipeline import useful_sections as _pipeline_useful_sections
from app.orchestration.state import reconcile_stale_running_work, upsert_processing_state
from app.related.service import RelatedInfoService
from app.seeds.service import SeedService
from app.services import ReaderService
from app.workers.timeline_context import JOB_TYPE as TIMELINE_CONTEXT_JOB_TYPE
from app.workers.timeline_context import enqueue_timeline_context_jobs
from app.workers.related_agent import FOCUS_JOB_TYPE as SECTION_INSIGHT_JOB_TYPE
from app.workers.related_agent import JOB_TYPE as RELATED_AGENT_JOB_TYPE
from app.workers.related_agent import MODEL_VERSION as RELATED_AGENT_MODEL_VERSION
from app.workers.related_agent import SWEEP_PACK_JOB_TYPE as RELATED_SWEEP_PACK_JOB_TYPE
from app.workers.related_agent import enqueue_related_jobs
from app.workers.related_agent import enqueue_section_insight_jobs
from app.workers.related_agent import is_agent_related_section
from app.workers.core_digest import JOB_TYPE as CORE_DIGEST_JOB_TYPE
from app.workers.core_digest import MODEL_VERSION as CORE_DIGEST_MODEL_VERSION
from app.workers.cpu_entities import JOB_TYPE as CPU_ENTITY_JOB_TYPE
from app.workers.embeddings import JOB_TYPE as EMBEDDING_JOB_TYPE
from app.workers.graph_frontier import JOB_TYPE as GRAPH_FRONTIER_JOB_TYPE
from app.workers.temporal_agent import JOB_TYPE as TEMPORAL_AGENT_JOB_TYPE
from app.workers.temporal_agent import enqueue_temporal_jobs

router = APIRouter()

TIMELINE_EXPLAIN_MODEL_VERSION = "timeline-temporal-explain-v1"


@router.get("/search", response_model=list[SearchResult])
async def search_articles(
    q: str = Query(min_length=2),
    limit: int = 15,
    session: AsyncSession = Depends(get_session),
) -> list[SearchResult]:
    """Search V4 article titles."""

    service = ReaderService(session)
    return [SearchResult(**item) for item in await service.search(q, limit)]


@router.get("/article/{title}", response_model=ArticleResponse)
async def get_article(
    title: str,
    seed: bool = False,
    enable_insights: bool = False,
    agent_temporal: bool = False,
    agent_related: bool = False,
    related_warmup_limit: int = Query(default=0, ge=0, le=200),
    session: AsyncSession = Depends(get_session),
) -> ArticleResponse:
    """Return article sections and optionally enqueue background agent work.

    The enqueue path only writes durable jobs. It does not call the LLM on the
    reader request path.
    """

    service = ReaderService(session)
    try:
        canonical, title_id, sections = await service.get_article(
            title,
            seed=False,
            enrich_ontology=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await orchestrate_article_load(
        session,
        title=canonical,
        title_id=title_id,
        sections=sections,
        enable_insights=enable_insights,
        agent_temporal=enable_insights and agent_temporal,
        agent_related=enable_insights and agent_related,
        related_warmup_limit=related_warmup_limit if enable_insights else 0,
        force=False,
        source="article_load",
    )
    return ArticleResponse(
        title=canonical,
        title_id=title_id,
        sections=[_section_response(section) for section in sections],
        core=await _article_core_response(session, title_id),
        cached=True,
    )


@router.post("/article/{title}/refresh", response_model=ArticleResponse)
async def refresh_article(
    title: str,
    seed: bool = False,
    enable_insights: bool = False,
    agent_temporal: bool = False,
    agent_related: bool = False,
    related_warmup_limit: int = Query(default=0, ge=0, le=200),
    session: AsyncSession = Depends(get_session),
) -> ArticleResponse:
    """Fully refresh one article and its derived V4 caches."""

    service = ReaderService(session)
    try:
        canonical, title_id, sections = await service.refresh_article(
            title,
            seed=False,
            enrich_ontology=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await orchestrate_article_load(
        session,
        title=canonical,
        title_id=title_id,
        sections=sections,
        enable_insights=enable_insights,
        agent_temporal=enable_insights and agent_temporal,
        agent_related=enable_insights and agent_related,
        related_warmup_limit=related_warmup_limit if enable_insights else 0,
        force=True,
        source="article_refresh",
    )
    return ArticleResponse(
        title=canonical,
        title_id=title_id,
        sections=[_section_response(section) for section in sections],
        core=await _article_core_response(session, title_id),
        cached=False,
        warnings=["full_refresh_completed"],
    )


@router.get("/article/{title}/timeline", response_model=TimelineResponse)
async def get_article_timeline(
    title: str,
    seed_missing: bool = True,
    enrich_context: bool = True,
    session: AsyncSession = Depends(get_session),
) -> TimelineResponse:
    """Return structured timeline events from cached temporal rows.

    The read response is backed by `section_time` and `time_dimension`. If no
    temporal cache exists yet and `seed_missing` is true, the endpoint runs the
    CPU temporal seed pass only. Agentic temporal extraction can later write into
    the same tables without changing this response contract.
    """

    service = ReaderService(session)
    try:
        canonical, title_id, sections = await service.get_article(title, seed=False, enrich_ontology=False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    events = await _load_timeline_events(session, canonical, title_id, sections)
    seeded = False
    if not events and seed_missing:
        seed_service = SeedService(session)
        for section in _useful_sections(sections):
            await seed_service.enrich_temporal_only(section)
        events = await _load_timeline_events(session, canonical, title_id, sections)
        seeded = True

    enrichment_pending = False
    if enrich_context and events:
        jobs_enqueued = await enqueue_timeline_context_jobs(
            session,
            _useful_sections(sections),
            priority=68,
            force=False,
        )
        await _prioritize_active_article_jobs(session, title_id=title_id, source="timeline_read")
        enrichment_pending = jobs_enqueued > 0

    return TimelineResponse(
        title=canonical,
        title_id=title_id,
        events=events,
        seeded=seeded,
        enrichment_pending=enrichment_pending,
        scoring_metrics=_timeline_scoring_metrics(events),
    )


@router.get("/article/{title}/graph", response_model=ArticleGraphResponse)
async def get_article_graph(
    title: str,
    sync_neo4j: bool = True,
    related_limit: int = Query(default=200, ge=0, le=1000),
    session: AsyncSession = Depends(get_session),
) -> ArticleGraphResponse:
    """Return the current article graph projection.

    Neo4j frontier data is preferred. The Postgres related-cache projection is a
    migration fallback only and is no longer written back into Neo4j here.
    """

    service = ReaderService(session)
    try:
        canonical, title_id, sections = await service.get_article(title, seed=False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    projection = await build_article_graph_projection_from_neo4j(
        session,
        title=canonical,
        title_id=title_id,
        sections=sections,
        max_nodes=related_limit + len(sections) + 1,
        min_relevance=0.0,
    )
    synced = False
    if len(projection.nodes) <= 1:
        projection = await build_article_graph_projection(
            session,
            title=canonical,
            title_id=title_id,
            sections=sections,
            related_limit=related_limit,
            sync_neo4j=False,
        )
    else:
        synced = sync_neo4j
    return ArticleGraphResponse(
        title=canonical,
        title_id=title_id,
        nodes=projection.nodes,
        edges=projection.edges,
        synced_to_neo4j=synced,
    )


@router.post("/article/{title}/graph/crawl", response_model=GraphCoverageResponse)
async def crawl_article_graph(
    title: str,
    max_articles: int = Query(default=10, ge=0, le=50),
    max_sections_per_article: int = Query(default=4, ge=1, le=12),
    force: bool = False,
    session: AsyncSession = Depends(get_session),
) -> GraphCoverageResponse:
    """Enqueue bounded graph-frontier coverage work for the current article."""

    try:
        result = await crawl_article_graph_coverage(
            session,
            title=title,
            max_articles=max_articles,
            max_sections_per_article=max_sections_per_article,
            force=force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return GraphCoverageResponse(
        title=result.title,
        title_id=result.title_id,
        frontier_considered=result.frontier_considered,
        articles_processed=result.articles_processed,
        jobs_enqueued=result.jobs_enqueued,
        articles=[item.__dict__ for item in result.articles],
    )


@router.post("/timeline/explain", response_model=TimelineExplainResponse)
async def explain_timeline_event(
    request: TimelineExplainRequest,
    session: AsyncSession = Depends(get_session),
) -> TimelineExplainResponse:
    """Generate an explicit temporal-context explanation for one event.

    This is intentionally not called while reading/loading the timeline. It is
    only invoked by the user's "Explain this" action, so the normal read path
    remains cache-backed and GPU-free.
    """

    section = await _load_section_for_timeline_explain(session, request.section_key)
    time_dim = await _load_time_for_timeline_explain(session, request.time_ref_id)
    if section is None or time_dim is None:
        raise HTTPException(status_code=404, detail="Timeline event source was not found")

    generated_at = datetime.utcnow()
    fallback = _timeline_explain_fallback(section, time_dim, request)
    settings = get_settings()
    llm = LocalLLMClient(settings)
    messages = _build_timeline_explain_prompt(section, time_dim, request)
    run_id = f"timeline_explain:{request.section_key}:{request.time_ref_id}:{int(time.time() * 1000)}"
    trace = AgentTrace(
        run_id=run_id,
        step_name=TIMELINE_EXPLAIN_MODEL_VERSION,
        model_name=settings.llm_model,
        status="running",
        input_json={
            "section_key": request.section_key,
            "time_ref_id": request.time_ref_id,
            "label": request.label,
            "domain_lane": request.domain_lane,
            "level": request.level,
            "track": request.track,
            "relevance_score": request.relevance_score,
            "messages": messages,
        },
    )
    session.add(trace)
    await session.commit()

    started = time.perf_counter()
    raw_response = ""
    try:
        response = await llm.chat_completion(messages, temperature=0.1, max_tokens=220)
        raw_response = response["choices"][0]["message"]["content"]
        why_text = _parse_timeline_explain_response(raw_response) or fallback
        trace.status = "succeeded"
        trace.output_json = {"why_text": why_text}
        trace.raw_response = raw_response
        trace.latency_ms = int((time.perf_counter() - started) * 1000)
        trace.usage_json = response.get("usage")
        trace.completed_at = datetime.utcnow()
        await session.commit()
        return TimelineExplainResponse(
            section_key=request.section_key,
            time_ref_id=request.time_ref_id,
            why_text=why_text,
            why_source="agent",
            model_version=TIMELINE_EXPLAIN_MODEL_VERSION,
            generated_at=generated_at,
        )
    except Exception as exc:
        trace.status = "failed"
        trace.raw_response = raw_response or None
        trace.error_text = str(exc)
        trace.latency_ms = int((time.perf_counter() - started) * 1000)
        trace.completed_at = datetime.utcnow()
        await session.commit()
        return TimelineExplainResponse(
            section_key=request.section_key,
            time_ref_id=request.time_ref_id,
            why_text=fallback,
            why_source="template_fallback",
            model_version=TIMELINE_EXPLAIN_MODEL_VERSION,
            generated_at=generated_at,
        )
    finally:
        await llm.aclose()


@router.post("/article/{title}/agent/temporal", response_model=AgentEnqueueResponse)
async def enqueue_article_temporal_agent(
    title: str,
    limit: int = Query(default=0, ge=0, le=200),
    force: bool = False,
    session: AsyncSession = Depends(get_session),
) -> AgentEnqueueResponse:
    """Enqueue background temporal agent jobs for cached article sections."""

    service = ReaderService(session)
    try:
        canonical, title_id, sections = await service.get_article(title, seed=False, enrich_ontology=False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    selected_sections = _useful_sections(sections)
    selected_sections = selected_sections[:limit] if limit else selected_sections
    jobs_enqueued = await enqueue_temporal_jobs(session, selected_sections, priority=50, force=force)
    await _prioritize_active_article_jobs(session, title_id=title_id, source="manual_temporal_agent")
    return AgentEnqueueResponse(
        title=canonical,
        title_id=title_id,
        job_type=TEMPORAL_AGENT_JOB_TYPE,
        sections_considered=len(selected_sections),
        jobs_enqueued=jobs_enqueued,
        force=force,
    )


@router.post("/section/{section_key}/agent/related", response_model=AgentEnqueueResponse)
async def enqueue_section_related_agent(
    section_key: str,
    force: bool = False,
    session: AsyncSession = Depends(get_session),
) -> AgentEnqueueResponse:
    """Enqueue background related-insight agent job for one cached section."""

    service = ReaderService(session)
    section = await service.get_section(section_key)
    if section is None:
        raise HTTPException(status_code=404, detail=f"Cached section not found: {section_key}")
    if force:
        await _flush_related_agent_insights(session, section_key)
    jobs_enqueued = await enqueue_section_insight_jobs(session, [section], priority=24, force=force, limit=1)
    await _prioritize_active_article_jobs(session, title_id=section.title_id, source="manual_related_agent")
    return AgentEnqueueResponse(
        title=section.title,
        title_id=section.title_id,
        job_type=SECTION_INSIGHT_JOB_TYPE,
        sections_considered=1,
        jobs_enqueued=jobs_enqueued,
        force=force,
    )


@router.post("/section/{section_key}/agent/related/{to_title_id}", response_model=AgentEnqueueResponse)
async def enqueue_one_related_agent(
    section_key: str,
    to_title_id: int,
    force: bool = True,
    session: AsyncSession = Depends(get_session),
) -> AgentEnqueueResponse:
    """Enqueue a focused related-insight job for one connected article."""

    service = ReaderService(session)
    section = await service.get_section(section_key)
    if section is None:
        raise HTTPException(status_code=404, detail=f"Cached section not found: {section_key}")
    if force:
        await _flush_related_agent_insights(session, section_key, to_title_id=to_title_id)
    jobs_enqueued = await enqueue_related_jobs(
        session,
        [section],
        priority=25,
        force=True,
        target_title_id=to_title_id,
    )
    await _prioritize_active_article_jobs(session, title_id=section.title_id, source="manual_related_agent_target")
    return AgentEnqueueResponse(
        title=section.title,
        title_id=section.title_id,
        job_type=RELATED_AGENT_JOB_TYPE,
        sections_considered=1,
        jobs_enqueued=jobs_enqueued,
        force=force,
    )


@router.get("/agent/jobs", response_model=list[AgentJobSummary])
async def agent_job_summary(
    session: AsyncSession = Depends(get_session),
) -> list[AgentJobSummary]:
    """Return counts by agent job status."""

    result = await session.execute(
        select(AgentJob.job_type, AgentJob.status, func.count())
        .group_by(AgentJob.job_type, AgentJob.status)
        .order_by(AgentJob.job_type, AgentJob.status)
    )
    areas = [
        AgentJobSummary(job_type=job_type, status=status, count=count)
        for job_type, status, count in result.all()
    ]
    return areas


@router.get("/article/{title}/status", response_model=ProcessingStatusResponse)
async def article_processing_status(
    title: str,
    section_key: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> ProcessingStatusResponse:
    """Return visible processing status for an article or selected section."""

    service = ReaderService(session)
    try:
        canonical, title_id, sections = await service.get_article(title, seed=False, enrich_ontology=False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    content_sections = _useful_sections(sections)
    selected_sections = (
        [section for section in content_sections if section.section_key == section_key]
        if section_key
        else content_sections
    )
    selected_keys = [section.section_key for section in selected_sections]
    await reconcile_stale_running_work(
        session,
        title_id=title_id,
        section_keys=selected_keys,
        older_than_minutes=12,
    )
    areas = await _processing_areas(session, title_id, selected_keys)
    await _persist_processing_areas(session, title_id, section_key, areas)
    await sync_article_processing_coverage_to_neo4j(
        session,
        title_id=title_id,
        section_keys=[section.section_key for section in content_sections],
    )
    return ProcessingStatusResponse(
        title=canonical,
        title_id=title_id,
        section_key=section_key,
        overall_state=_overall_processing_state(areas),
        areas=areas,
        metrics=await _processing_metrics(session, title_id, selected_keys),
    )


@router.post("/section/{section_key}/seed", response_model=SeedResponse)
async def seed_section(
    section_key: str,
    session: AsyncSession = Depends(get_session),
) -> SeedResponse:
    """Run CPU seed enrichment for one cached section."""

    service = ReaderService(session)
    section = await service.get_section(section_key)
    if section is None:
        raise HTTPException(status_code=404, detail=f"Cached section not found: {section_key}")
    result = await SeedService(session).enrich_section(section)
    return SeedResponse(**result)


@router.get("/section/{section_key}/related", response_model=RelatedResponse)
async def related_for_section(
    section_key: str,
    refresh: bool = False,
    limit: int | None = None,
    agent_related: bool = False,
    session: AsyncSession = Depends(get_session),
) -> RelatedResponse:
    """Return cached/deterministic related items for a clicked section."""

    service = ReaderService(session)
    section = await service.get_section(section_key)
    if section is None:
        raise HTTPException(status_code=404, detail=f"Cached section not found: {section_key}")
    if not is_content_section(section):
        return RelatedResponse(section_key=section_key, items=[])
    items = await RelatedInfoService(session).get_related(section, refresh=refresh, limit=limit)
    if agent_related:
        force_agent = await _related_agent_should_force(session, section_key, items)
        await enqueue_related_jobs(session, [section], priority=35, force=force_agent)
    summaries = await _related_summaries(session, items)
    return RelatedResponse(
        section_key=section_key,
        items=[
            RelatedItemResponse(
                to_title_id=item.to_title_id,
                to_title=item.to_title,
                level=item.level,
                score=item.score,
                signals=item.signals_json,
                why_text=item.why_text,
                why_source=item.why_source,
                model_version=item.model_version,
                summary=summaries.get(item.to_title_id, ""),
                agent_updated_at=item.updated_at if item.why_source == "agent_related_v1" else None,
                why=_related_why_object(item),
            )
            for item in items
        ],
        scoring_metrics=_related_scoring_metrics(items),
    )


def _useful_sections(sections: list[SectionClean]) -> list[SectionClean]:
    return _pipeline_useful_sections(sections)


async def _article_insights_enabled(session: AsyncSession, title_id: int) -> bool:
    """Return whether the latest article-load orchestration requested LLM insight lanes."""

    result = await session.execute(
        select(ProcessingState.metadata_json)
        .where(ProcessingState.title_id == title_id)
        .where(ProcessingState.section_key == "")
        .where(ProcessingState.area == "article_load")
        .order_by(ProcessingState.updated_at.desc().nullslast(), ProcessingState.created_at.desc())
        .limit(1)
    )
    metadata = result.scalar_one_or_none() or {}
    return bool(metadata.get("enable_insights")) if isinstance(metadata, dict) else False


async def _processing_areas(
    session: AsyncSession,
    title_id: int,
    section_keys: list[str],
) -> list[ProcessingAreaStatus]:
    expected_sections = len(section_keys)
    insights_enabled = await _article_insights_enabled(session, title_id)
    llm_lane_enabled = bool(get_settings().timeline_v4_llm_lane_enabled)
    related_section_keys = await _agent_related_section_keys(session, section_keys)
    article_job_key = f"article:{title_id}"
    core_jobs = await _job_counts(session, CORE_DIGEST_JOB_TYPE, title_id, [article_job_key])
    graph_frontier_jobs = await _job_counts(session, GRAPH_FRONTIER_JOB_TYPE, title_id, [article_job_key])
    embedding_jobs = await _job_counts(session, EMBEDDING_JOB_TYPE, title_id, section_keys)
    cpu_entity_jobs = await _job_counts(session, CPU_ENTITY_JOB_TYPE, title_id, section_keys)
    temporal_jobs = await _job_counts(session, TEMPORAL_AGENT_JOB_TYPE, title_id, section_keys)
    related_jobs = (
        await _job_counts(
            session,
            [RELATED_AGENT_JOB_TYPE, SECTION_INSIGHT_JOB_TYPE, RELATED_SWEEP_PACK_JOB_TYPE],
            title_id,
            [],
        )
        if related_section_keys
        else {}
    )
    timeline_jobs = await _job_counts(session, TIMELINE_CONTEXT_JOB_TYPE, title_id, section_keys)
    temporal_expected = expected_sections
    related_expected = len(related_section_keys)
    related_job_total = sum(related_jobs.values())
    temporal_error = await _latest_job_error(session, TEMPORAL_AGENT_JOB_TYPE, title_id, section_keys)
    related_error = (
        await _latest_job_error(
            session,
            [RELATED_AGENT_JOB_TYPE, SECTION_INSIGHT_JOB_TYPE, RELATED_SWEEP_PACK_JOB_TYPE],
            title_id,
            [],
        )
        if related_section_keys
        else None
    )
    timeline_error = await _latest_job_error(session, TIMELINE_CONTEXT_JOB_TYPE, title_id, section_keys)
    core_error = await _latest_job_error(session, CORE_DIGEST_JOB_TYPE, title_id, [article_job_key])
    embedding_error = await _latest_job_error(session, EMBEDDING_JOB_TYPE, title_id, section_keys)
    cpu_entity_error = await _latest_job_error(session, CPU_ENTITY_JOB_TYPE, title_id, section_keys)
    graph_frontier_error = await _latest_job_error(session, GRAPH_FRONTIER_JOB_TYPE, title_id, [article_job_key])
    core_rows = await _count_article_core(session, title_id)
    temporal_rows = await _count_section_time(session, title_id, section_keys)
    related_rows = await _count_related_rows(session, section_keys)
    related_gate_counts = await _related_gate_counts(session, section_keys)
    agent_related_rows = await _count_agent_related_rows(session, related_section_keys)
    timeline_rows = await _count_timeline_context_rows(session, title_id, section_keys)
    embedding_rows = await _count_section_embeddings(session, section_keys)
    timeline_eligible_rows = related_gate_counts["timeline_eligible"]
    fast_completed = temporal_rows + related_rows
    fast_total = max(expected_sections, fast_completed)
    temporal_job_completed = temporal_jobs.get("succeeded", 0)
    related_job_completed = related_jobs.get("succeeded", 0)
    deep_completed = temporal_job_completed + related_job_completed + timeline_rows
    deep_jobs = _merge_job_counts(temporal_jobs, related_jobs, timeline_jobs)
    cpu_jobs = _merge_job_counts(embedding_jobs, cpu_entity_jobs)

    temporal_completed = min(temporal_expected, temporal_job_completed) if temporal_expected else temporal_job_completed
    related_completed = min(related_expected, related_job_completed) if related_expected else related_job_completed
    timeline_completed = timeline_rows

    areas = [
        _area_status(
            key="article_load",
            label="Article load",
            jobs={},
            completed=expected_sections,
            total=expected_sections,
            detail=f"Article shell has {expected_sections} content section(s)",
        ),
        _area_status(
            key="graph_framework",
            label="Graph framework",
            jobs={},
            completed=expected_sections,
            total=max(expected_sections, 1),
            detail=(
                f"L0 sections are graph-ready; {related_rows} L1/L2 cached connection row(s) available"
            ),
        ),
        _area_status(
            key="graph_frontier",
            label="Graph frontier",
            jobs=graph_frontier_jobs,
            completed=graph_frontier_jobs.get("succeeded", 0),
            total=max(1, sum(graph_frontier_jobs.values())),
            detail="Durable L1/L2 link discovery and L1 intro caching",
            last_error=graph_frontier_error,
        ),
        _area_status(
            key="l0_enrichment",
            label="L0 enrichment",
            jobs=cpu_jobs if not insights_enabled else _merge_job_counts(cpu_jobs, core_jobs, temporal_jobs),
            completed=min(expected_sections, embedding_rows),
            total=max(expected_sections, embedding_rows),
            detail=(
                f"CPU core lane: {embedding_rows} embedding row(s), {temporal_rows} deterministic temporal row(s), "
                f"{core_rows} core digest row(s)"
            ),
            last_error=embedding_error or cpu_entity_error or (temporal_error if insights_enabled else None) or (core_error if insights_enabled else None),
        ),
        (
            _area_status(
            key="core_digest",
            label="Usable core",
            jobs=core_jobs,
            completed=core_rows,
            total=1,
            detail="L1 article summary, entities, and dated spine",
            last_error=core_error,
            )
            if insights_enabled
            else _disabled_area_status(
                key="core_digest",
                label="Usable core",
                detail="Disabled in core mode. Enable insights to run LLM article summaries, entity roles, and dated spine.",
            )
        ),
        _area_status(
            key="fast_layer",
            label="Fast layer",
            jobs={},
            completed=fast_completed,
            total=fast_total,
            detail="CPU dates, tags, links, and basic scoring",
        ),
        (
            _area_status(
            key="deep_layer",
            label="Deep enrichment",
            jobs=deep_jobs,
            completed=deep_completed,
            total=max(deep_completed, deep_completed + deep_jobs.get("pending", 0) + deep_jobs.get("running", 0)),
            detail="LLM temporal/context upgrades",
            last_error=temporal_error or related_error or timeline_error,
            )
            if insights_enabled
            else _disabled_area_status(
                key="deep_layer",
                label="Deep enrichment",
                detail="Disabled in core mode. CPU graph, embeddings, entities, and deterministic time extraction can run without vLLM.",
            )
        ),
        (
            _area_status(
            key="temporal",
            label="Temporal extraction",
            jobs=temporal_jobs,
            completed=temporal_completed,
            total=max(temporal_expected, temporal_completed),
            detail=f"{temporal_rows} temporal row(s); {temporal_job_completed} of {temporal_expected} section job(s) completed",
            last_error=temporal_error,
            )
            if insights_enabled
            else _area_status(
                key="temporal",
                label="Temporal extraction",
                jobs={},
                completed=temporal_rows,
                total=max(temporal_rows, 1),
                detail=f"CPU/deterministic temporal rows available: {temporal_rows}. LLM temporal extraction is disabled.",
            )
        ),
        _area_status(
            key="connections",
            label="L1/L2 connections",
            jobs={},
            completed=related_rows,
            total=related_rows,
            detail=(
                f"{related_rows} cached connection(s); "
                f"{related_gate_counts['accepted']} accepted; "
                f"{related_gate_counts['agent_eligible']} agent-eligible; "
                f"{related_gate_counts['timeline_eligible']} timeline-eligible"
            ),
        ),
        _area_status(
            key="embeddings",
            label="Embeddings",
            jobs=embedding_jobs,
            completed=min(expected_sections, embedding_rows) if expected_sections else embedding_rows,
            total=max(expected_sections, embedding_rows),
            detail=f"{embedding_rows} of {expected_sections} content section embedding(s) available",
            last_error=embedding_error,
        ),
        _area_status(
            key="cpu_entities",
            label="CPU entities",
            jobs=cpu_entity_jobs,
            completed=cpu_entity_jobs.get("succeeded", 0),
            total=max(sum(cpu_entity_jobs.values()), cpu_entity_jobs.get("succeeded", 0)),
            detail="CPU GLiNER/spaCy ontology extraction and passage scoring",
            last_error=cpu_entity_error,
        ),
        (
            _area_status(
            key="related_agent",
            label="L1/L2 insights",
            jobs=related_jobs,
            completed=related_completed,
            total=max(related_expected, related_job_total, related_completed),
            detail=(
                f"{agent_related_rows} agent insight row(s) across {related_rows} cached connection(s); "
                f"{related_job_completed} of {related_expected} section job(s) completed"
            ),
            last_error=related_error,
            )
            if insights_enabled
            else _disabled_area_status(
                key="related_agent",
                label="L1/L2 insights",
                detail=f"Disabled in core mode. Showing links/scoring only; {related_rows} cached connection row(s) available.",
            )
        ),
        (
            _area_status(
            key="timeline_context",
            label="Timeline context",
            jobs=timeline_jobs,
            completed=timeline_completed,
            total=max(timeline_completed, sum(timeline_jobs.values())),
            detail=f"{timeline_rows} promoted L1/L2 event(s); {timeline_eligible_rows} timeline-eligible connection(s)",
            last_error=timeline_error or temporal_error,
            )
            if insights_enabled
            else _area_status(
                key="timeline_context",
                label="Timeline context",
                jobs=timeline_jobs,
                completed=timeline_completed,
                total=max(timeline_completed, sum(timeline_jobs.values())),
                detail=f"CPU promotion lane only: {timeline_rows} promoted L1/L2 event(s); LLM context is disabled.",
                last_error=timeline_error,
            )
        ),
    ]
    timings = await _processing_area_timings(
        session,
        title_id=title_id,
        section_keys=section_keys,
        related_section_keys=related_section_keys,
        insights_enabled=insights_enabled,
    )
    for area in areas:
        timing = timings.get(area.key)
        if not timing:
            continue
        area.started_at = timing.get("started_at")
        area.completed_at = timing.get("completed_at")
        area.elapsed_seconds = timing.get("elapsed_seconds")
    if insights_enabled and not llm_lane_enabled:
        _mark_llm_lane_disabled_attention(areas)
    return areas


def _merge_job_counts(*job_counts: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for counts in job_counts:
        for key, value in counts.items():
            merged[key] = merged.get(key, 0) + value
    return merged


async def _job_counts(
    session: AsyncSession,
    job_type: str | list[str],
    title_id: int,
    section_keys: list[str],
) -> dict[str, int]:
    job_types = [job_type] if isinstance(job_type, str) else job_type
    conditions = [AgentJob.job_type.in_(job_types), AgentJob.title_id == title_id]
    if section_keys:
        conditions.append(AgentJob.section_key.in_(section_keys))
    result = await session.execute(
        select(
            AgentJob.status,
            AgentJob.attempts,
            AgentJob.max_attempts,
            func.count(),
        )
        .where(*conditions)
        .group_by(AgentJob.status, AgentJob.attempts, AgentJob.max_attempts)
    )
    counts: dict[str, int] = {}
    for status, attempts, max_attempts, count in result.all():
        normalized = str(status)
        if normalized == "retry" and int(attempts or 0) >= int(max_attempts or 0):
            normalized = "failed"
        counts[normalized] = counts.get(normalized, 0) + int(count)
    return counts


async def _processing_area_timings(
    session: AsyncSession,
    *,
    title_id: int,
    section_keys: list[str],
    related_section_keys: list[str],
    insights_enabled: bool,
) -> dict[str, dict[str, Any]]:
    article_job_key = f"article:{title_id}"
    specs: dict[str, tuple[list[str], list[str]]] = {
        "graph_frontier": ([GRAPH_FRONTIER_JOB_TYPE], [article_job_key]),
        "embeddings": ([EMBEDDING_JOB_TYPE], section_keys),
        "cpu_entities": ([CPU_ENTITY_JOB_TYPE], section_keys),
        "l0_enrichment": ([EMBEDDING_JOB_TYPE, CPU_ENTITY_JOB_TYPE], section_keys),
    }
    if insights_enabled:
        specs.update(
            {
                "core_digest": ([CORE_DIGEST_JOB_TYPE], [article_job_key]),
                "temporal": ([TEMPORAL_AGENT_JOB_TYPE], section_keys),
                "related_agent": ([RELATED_AGENT_JOB_TYPE, SECTION_INSIGHT_JOB_TYPE, RELATED_SWEEP_PACK_JOB_TYPE], []),
                "timeline_context": ([TIMELINE_CONTEXT_JOB_TYPE], section_keys),
                "deep_layer": (
                    [
                        TEMPORAL_AGENT_JOB_TYPE,
                        RELATED_AGENT_JOB_TYPE,
                        SECTION_INSIGHT_JOB_TYPE,
                        RELATED_SWEEP_PACK_JOB_TYPE,
                        TIMELINE_CONTEXT_JOB_TYPE,
                    ],
                    [],
                ),
            }
        )
        specs["l0_enrichment"] = (
            [EMBEDDING_JOB_TYPE, CPU_ENTITY_JOB_TYPE, CORE_DIGEST_JOB_TYPE, TEMPORAL_AGENT_JOB_TYPE, SECTION_INSIGHT_JOB_TYPE],
            sorted(set(section_keys) | {article_job_key}),
        )

    output: dict[str, dict[str, Any]] = {}
    for area, (job_types, scoped_keys) in specs.items():
        timing = await _job_timing(session, title_id=title_id, job_types=job_types, section_keys=scoped_keys)
        if timing:
            output[area] = timing
    return output


async def _job_timing(
    session: AsyncSession,
    *,
    title_id: int,
    job_types: list[str],
    section_keys: list[str],
) -> dict[str, Any] | None:
    if not job_types:
        return None
    conditions = [AgentJob.title_id == title_id, AgentJob.job_type.in_(job_types)]
    if section_keys:
        conditions.append(AgentJob.section_key.in_(section_keys))
    result = await session.execute(
        select(
            func.min(AgentJob.created_at),
            func.max(AgentJob.completed_at),
            func.count(),
            func.count().filter(AgentJob.status.in_(["pending", "retry", "running"])),
        ).where(*conditions)
    )
    started_at, completed_at, total, active = result.one()
    if not int(total or 0) or not started_at:
        return None
    end_time = datetime.utcnow() if int(active or 0) else (completed_at or datetime.utcnow())
    elapsed_seconds = max(0.0, (end_time - started_at).total_seconds())
    return {
        "started_at": started_at,
        "completed_at": None if int(active or 0) else completed_at,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


async def _agent_related_section_keys(session: AsyncSession, section_keys: list[str]) -> list[str]:
    if not section_keys:
        return []
    result = await session.execute(
        select(SectionClean)
        .where(SectionClean.section_key.in_(section_keys))
        .where(func.length(func.trim(SectionClean.clean_text)) > 0)
    )
    return [section.section_key for section in result.scalars().all() if is_agent_related_section(section)]


async def _latest_job_error(
    session: AsyncSession,
    job_type: str | list[str],
    title_id: int,
    section_keys: list[str],
) -> str | None:
    job_types = [job_type] if isinstance(job_type, str) else job_type
    conditions = [
        AgentJob.job_type.in_(job_types),
        AgentJob.title_id == title_id,
        AgentJob.last_error.is_not(None),
        AgentJob.last_error != "Reset by local Timeline startup after stale worker cleanup.",
    ]
    if section_keys:
        conditions.append(AgentJob.section_key.in_(section_keys))
    result = await session.execute(
        select(AgentJob.last_error)
        .where(*conditions)
        .order_by(AgentJob.updated_at.desc().nullslast(), AgentJob.id.desc())
        .limit(1)
    )
    value = result.scalar_one_or_none()
    if not value:
        return None
    text = " ".join(str(value).split())
    return text[:220]


async def _count_section_time(session: AsyncSession, title_id: int, section_keys: list[str]) -> int:
    conditions = [SectionTime.title_id == title_id]
    if section_keys:
        conditions.append(SectionTime.section_key.in_(section_keys))
    result = await session.execute(select(func.count()).select_from(SectionTime).where(*conditions))
    return int(result.scalar_one() or 0)


async def _count_article_core(session: AsyncSession, title_id: int) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(ArticleCore)
        .where(ArticleCore.title_id == title_id)
        .where(ArticleCore.model_version == CORE_DIGEST_MODEL_VERSION)
    )
    return int(result.scalar_one() or 0)


async def _count_related_rows(session: AsyncSession, section_keys: list[str]) -> int:
    if not section_keys:
        return 0
    result = await session.execute(
        select(func.count()).select_from(RelatedCache).where(RelatedCache.from_section_key.in_(section_keys))
    )
    return int(result.scalar_one() or 0)


async def _count_agent_related_rows(session: AsyncSession, section_keys: list[str]) -> int:
    if not section_keys:
        return 0
    result = await session.execute(
        select(func.count())
        .select_from(RelatedCache)
        .where(RelatedCache.from_section_key.in_(section_keys))
        .where(RelatedCache.why_source == "agent_related_v1")
    )
    return int(result.scalar_one() or 0)


async def _related_gate_counts(session: AsyncSession, section_keys: list[str]) -> dict[str, int]:
    counts = {"accepted": 0, "agent_eligible": 0, "timeline_eligible": 0}
    if not section_keys:
        return counts
    result = await session.execute(
        select(RelatedCache.signals_json).where(RelatedCache.from_section_key.in_(section_keys))
    )
    for (signals,) in result.fetchall():
        gates = (signals or {}).get("gates") or {}
        if gates.get("accepted"):
            counts["accepted"] += 1
        if gates.get("agent_eligible"):
            counts["agent_eligible"] += 1
        if gates.get("timeline_eligible"):
            counts["timeline_eligible"] += 1
    return counts


async def _count_timeline_context_rows(
    session: AsyncSession,
    title_id: int,
    section_keys: list[str],
) -> int:
    conditions = [TimelineContextCache.from_title_id == title_id]
    if section_keys:
        conditions.append(TimelineContextCache.from_section_key.in_(section_keys))
    result = await session.execute(
        select(func.count()).select_from(TimelineContextCache).where(*conditions)
    )
    return int(result.scalar_one() or 0)


async def _count_section_embeddings(session: AsyncSession, section_keys: list[str]) -> int:
    if not section_keys:
        return 0
    try:
        result = await session.execute(
            text(
                """
                SELECT count(*)
                FROM timeline_v4.section_embedding
                WHERE section_key = ANY(:section_keys)
                """
            ),
            {"section_keys": section_keys},
        )
        return int(result.scalar_one() or 0)
    except Exception:
        return 0


async def _persist_processing_areas(
    session: AsyncSession,
    title_id: int,
    section_key: str | None,
    areas: list[ProcessingAreaStatus],
) -> None:
    existing_result = await session.execute(
        select(ProcessingState.area, ProcessingState.metadata_json)
        .where(ProcessingState.title_id == title_id)
        .where(ProcessingState.section_key == (section_key or ""))
        .where(ProcessingState.area.in_([area.key for area in areas]))
    )
    existing_metadata = {
        str(area): metadata
        for area, metadata in existing_result.all()
        if isinstance(metadata, dict) and metadata
    }
    for area in areas:
        await upsert_processing_state(
            session,
            title_id=title_id,
            section_key=section_key,
            area=area.key,
            state=area.state,
            expected_count=area.total,
            completed_count=area.completed,
            pending_count=area.pending,
            running_count=area.running,
            failed_count=area.failed,
            detail=area.detail,
            reason=area.reason,
            last_error=area.last_error,
            source="status_snapshot",
            metadata=existing_metadata.get(area.key, {}),
            commit=False,
        )
    await session.commit()

    result = await session.execute(
        select(ProcessingState.area, ProcessingState.updated_at, ProcessingState.created_at, ProcessingState.source)
        .where(ProcessingState.title_id == title_id)
        .where(ProcessingState.section_key == (section_key or ""))
        .where(ProcessingState.area.in_([area.key for area in areas]))
    )
    state_rows = {
        str(area): {"updated_at": updated_at or created_at, "source": source}
        for area, updated_at, created_at, source in result.all()
    }
    for area in areas:
        state_row = state_rows.get(area.key)
        if state_row:
            area.updated_at = state_row["updated_at"]
            area.source = str(state_row["source"])


async def _processing_metrics(
    session: AsyncSession,
    title_id: int,
    section_keys: list[str],
) -> dict[str, Any]:
    """Return coarse timing metrics for the visible article/section scope."""

    article_job_key = f"article:{title_id}"
    core_result = await session.execute(
        select(AgentJob.created_at, AgentJob.completed_at, AgentJob.status)
        .where(AgentJob.job_type == CORE_DIGEST_JOB_TYPE)
        .where(AgentJob.title_id == title_id)
        .where(AgentJob.section_key == article_job_key)
        .order_by(AgentJob.updated_at.desc().nullslast(), AgentJob.id.desc())
        .limit(1)
    )
    core_row = core_result.one_or_none()

    scoped_job_types = [
        TEMPORAL_AGENT_JOB_TYPE,
        RELATED_AGENT_JOB_TYPE,
        SECTION_INSIGHT_JOB_TYPE,
        RELATED_SWEEP_PACK_JOB_TYPE,
        TIMELINE_CONTEXT_JOB_TYPE,
    ]
    conditions = [AgentJob.title_id == title_id, AgentJob.job_type.in_(scoped_job_types)]
    if section_keys:
        conditions.append(AgentJob.section_key.in_(section_keys))
    jobs_result = await session.execute(
        select(
            func.min(AgentJob.created_at),
            func.max(AgentJob.completed_at),
            func.min(AgentJob.run_after),
            func.count().filter(AgentJob.status.in_(["pending", "retry"])),
            func.count().filter(AgentJob.status == "running"),
            func.count().filter(AgentJob.status == "failed"),
            func.count(),
        ).where(*conditions)
    )
    created_at, completed_at, oldest_run_after, pending, running, failed, total = jobs_result.one()
    pending = int(pending or 0)
    running = int(running or 0)
    failed = int(failed or 0)
    total = int(total or 0)

    metrics: dict[str, Any] = {
        "core_digest_status": core_row[2] if core_row else None,
        "deep_total_jobs": total,
        "deep_pending_jobs": pending,
        "deep_running_jobs": running,
        "deep_failed_jobs": failed,
    }
    if core_row and core_row[0] and core_row[1]:
        metrics["time_to_usable_core_seconds"] = round((core_row[1] - core_row[0]).total_seconds(), 3)
    if created_at and completed_at and not pending and not running and not failed:
        metrics["time_to_rich_seconds"] = round((completed_at - created_at).total_seconds(), 3)
    elif created_at:
        metrics["time_since_deep_start_seconds"] = round((datetime.utcnow() - created_at).total_seconds(), 3)
    if oldest_run_after:
        metrics["oldest_deferred_job_age_seconds"] = round(
            max(0.0, (datetime.utcnow() - oldest_run_after).total_seconds()),
            3,
        )
    return metrics


def _area_status(
    *,
    key: str,
    label: str,
    jobs: dict[str, int],
    completed: int,
    total: int,
    detail: str,
    last_error: str | None = None,
) -> ProcessingAreaStatus:
    pending = jobs.get("pending", 0) + jobs.get("retry", 0)
    running = jobs.get("running", 0)
    succeeded = jobs.get("succeeded", 0)
    failed = jobs.get("failed", 0)
    completed_count = max(completed, succeeded)
    total_count = max(total, completed_count + pending + running + failed)
    if running:
        state = "running"
    elif pending:
        state = "pending"
    elif failed and completed_count < total_count:
        state = "attention"
    elif total_count and completed_count >= total_count:
        state = "completed"
    elif completed_count:
        state = "incomplete"
    elif failed:
        state = "attention"
    else:
        state = "idle"
    return ProcessingAreaStatus(
        key=key,
        label=label,
        state=state,
        pending=pending,
        running=running,
        failed=failed,
        completed=completed_count,
        total=total_count,
        detail=detail,
        reason=_area_reason(
            state=state,
            pending=pending,
            running=running,
            failed=failed,
            completed=completed_count,
            total=total_count,
            detail=detail,
            last_error=last_error,
        ),
        last_error=last_error,
    )


def _disabled_area_status(*, key: str, label: str, detail: str) -> ProcessingAreaStatus:
    return ProcessingAreaStatus(
        key=key,
        label=label,
        state="disabled",
        pending=0,
        running=0,
        failed=0,
        completed=0,
        total=0,
        detail=detail,
        reason="This lane is intentionally disabled for the current article-load mode.",
        last_error=None,
    )


def _mark_llm_lane_disabled_attention(areas: list[ProcessingAreaStatus]) -> None:
    """Make queued LLM work visibly blocked when the app was launched CPU-only."""

    llm_area_keys = {"core_digest", "deep_layer", "temporal", "related_agent"}
    message = (
        "Insights are enabled for this article, but the backend was started without the LLM worker lane. "
        "Restart with full insight mode, or unset TIMELINE_V4_CORE_MODE, so queued LLM jobs can run."
    )
    for area in areas:
        if area.key not in llm_area_keys:
            continue
        has_unfinished_work = area.pending > 0 or area.running > 0 or area.state in {"pending", "running", "incomplete"}
        if not has_unfinished_work:
            continue
        area.state = "attention"
        area.last_error = message
        area.reason = message


def _area_reason(
    *,
    state: str,
    pending: int,
    running: int,
    failed: int,
    completed: int,
    total: int,
    detail: str,
    last_error: str | None,
) -> str:
    if state == "running":
        return f"{running} job(s) running; {pending} work item(s) queued. {detail}"
    if state == "pending":
        return f"{pending} work item(s) queued or retrying. {detail}"
    if state == "incomplete":
        remaining = max(total - completed, 0)
        parts = [f"{completed} of {total} complete"]
        if remaining:
            parts.append(f"{remaining} remaining")
        if pending or running:
            parts.append(f"{running} running, {pending} queued")
        elif remaining and not failed:
            parts.append("no runnable job is currently queued")
        if failed:
            parts.append(f"{failed} failed")
        if last_error:
            parts.append(f"last error: {last_error}")
        return "; ".join(parts) + "."
    if state == "completed":
        return f"Completed. {detail}"
    if state == "attention":
        if last_error:
            return f"{completed} of {total} complete; {failed} failed job(s). Last error: {last_error}"
        return f"{failed} failed job(s); no usable output yet."
    return f"No active processing. {detail}"


def _overall_processing_state(areas: list[ProcessingAreaStatus]) -> str:
    states = {area.state for area in areas}
    if "running" in states:
        return "running"
    if "pending" in states or "incomplete" in states:
        return "pending"
    if "attention" in states:
        return "attention"
    if areas and all(area.state in {"completed", "idle"} for area in areas):
        return "completed"
    return "idle"


async def _flush_related_agent_insights(
    session: AsyncSession,
    section_key: str,
    *,
    to_title_id: int | None = None,
) -> int:
    """Clear agent insight markers so a forced job visibly refreshes the section."""

    conditions = [
        RelatedCache.from_section_key == section_key,
        RelatedCache.why_source == "agent_related_v1",
    ]
    if to_title_id is not None:
        conditions.append(RelatedCache.to_title_id == to_title_id)

    result = await session.execute(select(RelatedCache).where(*conditions))
    rows = list(result.scalars().all())
    now = datetime.utcnow()
    for row in rows:
        signals = dict(row.signals_json or {})
        signals.pop("agent_related_v1", None)
        row.signals_json = signals
        row.why_source = "template"
        row.model_version = "agent-refresh-pending"
        row.updated_at = now
    await session.commit()
    return len(rows)


async def _related_summaries(
    session: AsyncSession,
    items: list[RelatedCache],
) -> dict[int, str]:
    """Return short deterministic previews for connected article tabs."""

    title_ids = [item.to_title_id for item in items]
    if not title_ids:
        return {}

    result = await session.execute(
        select(SectionClean.title_id, SectionClean.clean_text)
        .where(SectionClean.title_id.in_(title_ids))
        .order_by(SectionClean.title_id.asc(), SectionClean.heading_id.asc())
    )
    summaries: dict[int, str] = {}
    for title_id, clean_text in result.all():
        if title_id in summaries:
            continue
        summaries[int(title_id)] = _short_preview(clean_text or "")
    return summaries


def _short_preview(text: str, max_length: int = 190) -> str:
    clean = " ".join(text.split())
    if not clean:
        return "No cached summary is available yet."
    for marker in [". ", "; ", ": "]:
        index = clean.find(marker)
        if 60 <= index <= max_length:
            return clean[: index + 1]
    if len(clean) <= max_length:
        return clean
    return clean[:max_length].rsplit(" ", 1)[0] + "..."


async def _related_agent_should_force(
    session: AsyncSession,
    section_key: str,
    items: list[RelatedCache],
) -> bool:
    """Return whether the related-agent job should be enqueued/refreshed.

    Template-only rows are allowed after a successful agent pass: weak candidates
    are links, not insights. Requeue only when no job exists, a job failed, or
    rows were explicitly marked stale by the user's re-run/explain action.
    """

    if not items:
        return False

    has_stale_agent_rows = any(
        item.model_version == "agent-refresh-pending"
        or (item.why_source == "agent_related_v1" and item.model_version != RELATED_AGENT_MODEL_VERSION)
        for item in items
    )

    result = await session.execute(
        select(AgentJob.status)
        .where(AgentJob.job_type.in_([RELATED_AGENT_JOB_TYPE, SECTION_INSIGHT_JOB_TYPE, RELATED_SWEEP_PACK_JOB_TYPE]))
        .where(AgentJob.section_key == section_key)
        .order_by(AgentJob.updated_at.desc().nullslast(), AgentJob.id.desc())
        .limit(1)
    )
    status = result.scalar_one_or_none()
    if status in {"pending", "running"}:
        return False
    if status in {None, "failed", "retry"}:
        return True
    return has_stale_agent_rows


def _section_response(section: SectionClean) -> ArticleSectionResponse:
    return ArticleSectionResponse(
        section_key=section.section_key,
        title_id=section.title_id,
        heading_id=section.heading_id,
        heading=section.heading,
        level=section.level,
        parent_id=section.parent_id,
        clean_text=section.clean_text,
        content_html=section.content_html,
        links=section.links_json,
        provenance=section.provenance_json,
    )


async def _article_core_response(session: AsyncSession, title_id: int) -> ArticleCoreResponse | None:
    result = await session.execute(
        select(ArticleCore)
        .where(ArticleCore.title_id == title_id)
        .where(ArticleCore.model_version == CORE_DIGEST_MODEL_VERSION)
        .order_by(ArticleCore.updated_at.desc().nullslast(), ArticleCore.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return ArticleCoreResponse(
        summary=row.summary,
        topic=row.topic_json or {},
        key_entities=row.entities_json or [],
        dated_spine=row.dated_spine_json or [],
        model_version=row.model_version,
        updated_at=row.updated_at or row.created_at,
    )


def _related_why_object(item: RelatedCache) -> dict[str, Any]:
    signals = item.signals_json or {}
    components = signals.get("components") or {}
    gates = signals.get("gates") or {}
    top_components = _top_score_components(components)
    return {
        "kind": "related",
        "score": round(float(item.score), 4),
        "level": item.level,
        "via_title": signals.get("via_title"),
        "shared_entities": signals.get("shared_entities") or [],
        "shared_domains": signals.get("shared_domains") or [],
        "time_overlap": signals.get("time_overlap") or [],
        "top_components": top_components,
        "gate": {
            "accepted": gates.get("accepted"),
            "agent_eligible": gates.get("agent_eligible"),
            "timeline_eligible": gates.get("timeline_eligible"),
            "reasons": gates.get("reasons") or [],
            "signals": gates.get("signals") or {},
        },
        "agent": signals.get("agent_related_v1") or {},
    }


def _timeline_why_object(
    *,
    level: int,
    source: str,
    confidence: float,
    relevance_score: float,
    signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signals = signals or {}
    related_signals = signals.get("related_signals") or {}
    related_components = (related_signals.get("components") or {}) if isinstance(related_signals, dict) else {}
    related_gates = (related_signals.get("gates") or {}) if isinstance(related_signals, dict) else {}
    return {
        "kind": "timeline",
        "score": round(float(relevance_score), 4),
        "level": level,
        "source": source,
        "confidence": round(float(confidence), 4),
        "top_components": _top_score_components(related_components),
        "gate": {
            "accepted": related_gates.get("accepted"),
            "timeline_eligible": related_gates.get("timeline_eligible"),
            "reasons": related_gates.get("reasons") or [],
        },
        "context_gates": {
            "temporal": signals.get("temporal_context_gate") or {},
            "passage": signals.get("passage_context_gate") or {},
        },
        "shared_entities": related_signals.get("shared_entities") if isinstance(related_signals, dict) else [],
        "shared_domains": related_signals.get("shared_domains") if isinstance(related_signals, dict) else [],
        "time_overlap": related_signals.get("time_overlap") if isinstance(related_signals, dict) else [],
        "why_source": signals.get("why_source"),
    }


def _top_score_components(components: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[tuple[str, float]] = []
    for group_name in ("content", "temporal"):
        group = components.get(group_name) or {}
        if isinstance(group, dict):
            for key, value in group.items():
                try:
                    values.append((key, float(value)))
                except (TypeError, ValueError):
                    continue
    for key in ("content_score", "temporal_score", "raw_score"):
        try:
            values.append((key, float(components.get(key))))
        except (TypeError, ValueError):
            continue
    values.sort(key=lambda item: item[1], reverse=True)
    return [{"key": key, "value": round(value, 4)} for key, value in values[:5]]


def _related_scoring_metrics(items: list[RelatedCache]) -> dict[str, Any]:
    scores = [float(item.score) for item in items]
    metrics = _score_distribution(scores)
    metrics["level_counts"] = _level_counts([item.level for item in items])
    metrics["top_component_distribution"] = _component_distribution(
        [item.signals_json or {} for item in items]
    )
    metrics["embedding_available"] = sum(
        1 for item in items if ((item.signals_json or {}).get("embedding_similarity") is not None)
    )
    priority_scores = _numeric_values(
        [
            ((item.signals_json or {}).get("priority") or {}).get("S_prio")
            for item in items
            if isinstance((item.signals_json or {}).get("priority"), dict)
        ]
    )
    metrics["priority_distribution"] = _score_distribution(priority_scores)
    return metrics


def _timeline_scoring_metrics(events: list[TimelineEventResponse]) -> dict[str, Any]:
    context_events = [event for event in events if event.level > 0]
    scores = [float(event.relevance_score) for event in context_events]
    metrics = _score_distribution(scores)
    metrics["level_counts"] = _level_counts([event.level for event in events])
    metrics["domain_counts"] = _value_counts([event.domain_lane for event in events])
    metrics["attribution_counts"] = _value_counts(
        [str((event.attribution or {}).get("status") or "unknown") for event in events]
    )
    metrics["section_attributed_context_count"] = sum(
        1
        for event in context_events
        if (event.attribution or {}).get("status") == "section_attributed_unreviewed"
    )
    metrics["confidence_capped_context_count"] = sum(
        1
        for event in context_events
        if (event.attribution or {}).get("focus_topic_assertion") is False
        and not (event.attribution or {}).get("reviewed")
        and float(event.confidence or 0.0) <= 0.68
    )
    metrics["context_count"] = len(context_events)
    metrics["core_count"] = len(events) - len(context_events)
    return metrics


def _score_distribution(scores: list[float]) -> dict[str, Any]:
    if not scores:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "spread": 0.0,
            "tie_rate": 0.0,
            "mean_nearest_neighbor_gap": 0.0,
        }
    rounded = [round(score, 4) for score in scores]
    sorted_scores = sorted(rounded)
    gaps = [
        abs(sorted_scores[index + 1] - sorted_scores[index])
        for index in range(len(sorted_scores) - 1)
    ]
    tie_count = len(rounded) - len(set(rounded))
    return {
        "count": len(rounded),
        "min": min(rounded),
        "max": max(rounded),
        "spread": round(max(rounded) - min(rounded), 4),
        "tie_rate": round(tie_count / len(rounded), 4),
        "mean_nearest_neighbor_gap": round(sum(gaps) / len(gaps), 4) if gaps else 0.0,
    }


def _numeric_values(values: list[Any]) -> list[float]:
    output: list[float] = []
    for value in values:
        try:
            output.append(float(value))
        except (TypeError, ValueError):
            continue
    return output


def _level_counts(levels: list[int]) -> dict[str, int]:
    return {str(key): value for key, value in _value_counts([str(level) for level in levels]).items()}


def _value_counts(values: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0]))


def _component_distribution(signals_list: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for signals in signals_list:
        top = _top_score_components(signals.get("components") or {})
        if not top:
            counts["none"] = counts.get("none", 0) + 1
            continue
        key = str(top[0]["key"])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


async def _load_timeline_events(
    session: AsyncSession,
    _title: str,
    title_id: int,
    sections: list[SectionClean],
) -> list[TimelineEventResponse]:
    section_by_key = {section.section_key: section for section in sections}
    result = await session.execute(
        select(SectionTime, TimeDimension)
        .join(TimeDimension, TimeDimension.time_ref_id == SectionTime.time_ref_id)
        .where(SectionTime.title_id == title_id)
    )
    events: list[TimelineEventResponse] = []
    for section_time, time_dim in result.all():
        section = section_by_key.get(section_time.section_key)
        if section is None or not is_content_section(section):
            continue
        events.append(
            TimelineEventResponse(
                id=f"{section_time.section_key}:{time_dim.time_ref_id}",
                title_id=section.title_id,
                source_title_id=section.title_id,
                source_title=section.title,
                heading_id=section.heading_id,
                source_heading=section.heading,
                section_key=section.section_key,
                heading=section.heading,
                time_ref_id=time_dim.time_ref_id,
                time_kind=time_dim.time_kind,
                label=time_dim.label,
                precision=time_dim.precision,
                start_date=time_dim.start_date,
                end_date=time_dim.end_date,
                year=time_dim.year,
                month=time_dim.month,
                day=time_dim.day,
                season=time_dim.season,
                source=section_time.source,
                confidence=section_time.confidence,
                excerpt=_timeline_excerpt(section.clean_text, time_dim),
                lane=_timeline_lane(section_time.source),
                domain_lane=_timeline_domain_lane(section, time_dim),
                level=0,
                track="core",
                relevance_score=1.0,
                provenance=section_time.provenance_json,
                attribution={
                    "level": "entity_or_section",
                    "status": "focus_core",
                    "focus_topic_assertion": True,
                    "reviewed": False,
                },
                why=_timeline_why_object(
                    level=0,
                    source=section_time.source,
                    confidence=section_time.confidence,
                    relevance_score=1.0,
                ),
            )
        )
    context_result = await session.execute(
        select(TimelineContextCache, TimeDimension, SectionClean)
        .join(TimeDimension, TimeDimension.time_ref_id == TimelineContextCache.time_ref_id)
        .join(SectionClean, SectionClean.section_key == TimelineContextCache.source_section_key)
        .where(TimelineContextCache.from_title_id == title_id)
        .where(TimelineContextCache.from_section_key.in_(list(section_by_key.keys())))
    )
    for context_row, time_dim, source_section in context_result.all():
        context_signals = context_row.signals_json or {}
        context_confidence = float(context_signals.get("temporal_confidence") or 0.0)
        attribution = context_signals.get("attribution")
        if not isinstance(attribution, dict):
            attribution = {
                "level": "section",
                "status": "section_attributed_unreviewed",
                "focus_topic_assertion": False,
                "reviewed": False,
            }
        context_confidence = _timeline_display_confidence(context_confidence, attribution)
        events.append(
            TimelineEventResponse(
                id=f"context:{context_row.id}:{context_row.from_section_key}:{time_dim.time_ref_id}",
                title_id=title_id,
                source_title_id=context_row.source_title_id,
                source_title=context_row.source_title,
                heading_id=context_row.source_heading_id,
                source_heading=context_row.source_heading,
                section_key=context_row.from_section_key,
                heading=section_by_key.get(context_row.from_section_key).heading
                if context_row.from_section_key in section_by_key
                else context_row.source_heading,
                time_ref_id=time_dim.time_ref_id,
                time_kind=time_dim.time_kind,
                label=time_dim.label,
                precision=time_dim.precision,
                start_date=time_dim.start_date,
                end_date=time_dim.end_date,
                year=time_dim.year,
                month=time_dim.month,
                day=time_dim.day,
                season=time_dim.season,
                source=f"context_l{context_row.level}",
                confidence=context_confidence,
                excerpt=_timeline_excerpt(source_section.clean_text, time_dim),
                lane="context",
                domain_lane=_timeline_domain_lane(source_section, time_dim),
                level=context_row.level,
                track=context_row.track,
                relevance_score=context_row.relevance_score,
                provenance=context_row.provenance_json,
                attribution=attribution,
                why=_timeline_why_object(
                    level=context_row.level,
                    source=f"context_l{context_row.level}",
                    confidence=context_confidence,
                    relevance_score=context_row.relevance_score,
                    signals=context_signals,
                ),
            )
        )
    return sorted(events, key=_timeline_sort_key)


def _timeline_sort_key(event: TimelineEventResponse) -> tuple[int, int, int, str, str]:
    year = event.year or 9999
    month = event.month or 0
    day = event.day or 0
    return year, month, day, event.section_key, event.time_ref_id


def _timeline_display_confidence(confidence: float, attribution: dict[str, Any]) -> float:
    """Cap unreviewed context confidence so it cannot masquerade as reviewed fact."""

    normalized = max(0.0, min(1.0, float(confidence or 0.0)))
    if attribution.get("focus_topic_assertion") is False and not attribution.get("reviewed"):
        return round(min(normalized, 0.68), 4)
    return round(normalized, 4)


async def _load_section_for_timeline_explain(
    session: AsyncSession,
    section_key: str,
) -> SectionClean | None:
    result = await session.execute(
        select(SectionClean).where(SectionClean.section_key == section_key)
    )
    return result.scalar_one_or_none()


async def _load_time_for_timeline_explain(
    session: AsyncSession,
    time_ref_id: str,
) -> TimeDimension | None:
    result = await session.execute(
        select(TimeDimension).where(TimeDimension.time_ref_id == time_ref_id)
    )
    return result.scalar_one_or_none()


def _build_timeline_explain_prompt(
    section: SectionClean,
    time_dim: TimeDimension,
    request: TimelineExplainRequest,
) -> list[dict[str, str]]:
    excerpt = _timeline_excerpt(section.clean_text, time_dim, radius=520)
    system = (
        "You write crisp timeline relevance notes for a history/wiki reader. "
        "Focus on the dated event or period first, then explain why that timing "
        "matters to the selected source section. Do not use filler phrases like "
        "'this is relevant because'. Return strict JSON only."
    )
    user = {
        "source_article": section.title,
        "source_heading": section.heading or "Introduction",
        "timeline_label": time_dim.label,
        "time_kind": time_dim.time_kind,
        "precision": time_dim.precision,
        "start_date": time_dim.start_date,
        "end_date": time_dim.end_date,
        "topic": request.domain_lane,
        "level": request.level,
        "track": request.track,
        "relevance_score": round(request.relevance_score, 3),
        "section_excerpt": excerpt,
        "instructions": [
            "One or two sentences, maximum 45 words.",
            "Start with the dated event/period, not the article title.",
            "Name the temporal relationship explicitly: origin, buildup, turning point, aftermath, publication, reign, lifetime, etc.",
            "Do not invent facts not present in the excerpt or date metadata.",
        ],
        "output_schema": {"why_text": "string"},
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=True)},
    ]


def _parse_timeline_explain_response(raw_response: str) -> str:
    payload = _extract_json_object(raw_response)
    why_text = str(payload.get("why_text") or "").strip()
    return _clean_timeline_why_text(why_text)


def _extract_json_object(raw_response: str) -> dict[str, Any]:
    text = raw_response.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            return {}
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _clean_timeline_why_text(text: str) -> str:
    normalized = " ".join(text.split())
    prefixes = [
        "this is relevant because ",
        "it is relevant because ",
        "this matters because ",
    ]
    lowered = normalized.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            normalized = normalized[len(prefix) :].lstrip()
            break
    if len(normalized) > 360:
        normalized = f"{normalized[:357].rstrip()}..."
    return normalized


def _timeline_explain_fallback(
    section: SectionClean,
    time_dim: TimeDimension,
    request: TimelineExplainRequest,
) -> str:
    heading = section.heading or "Introduction"
    if time_dim.start_date and time_dim.end_date and time_dim.start_date != time_dim.end_date:
        timing = f"{time_dim.label} spans {time_dim.start_date} to {time_dim.end_date}"
    elif time_dim.start_date:
        timing = f"{time_dim.label} anchors to {time_dim.start_date}"
    else:
        timing = f"{time_dim.label} is the cached temporal anchor"
    return _clean_timeline_why_text(
        f"{timing}; it places the {request.domain_lane.lower()} thread from {section.title} / {heading} on the shared timeline."
    )


def _timeline_lane(source: str) -> str:
    if source.startswith("agent"):
        return "agent"
    if source.startswith("spacy"):
        return "seed"
    return "seed"


def _timeline_domain_lane(section: SectionClean, time_dim: TimeDimension) -> str:
    """Infer a v0 domain lane from cached text until taxonomy-backed lanes exist."""

    haystack = " ".join(
        [
            section.title or "",
            section.heading or "",
            section.clean_text[:700] if section.clean_text else "",
            time_dim.label or "",
        ]
    ).lower()
    rules = [
        ("War & Military", ("war", "battle", "military", "army", "navy", "invasion", "siege", "campaign")),
        ("Politics & Government", ("king", "queen", "president", "minister", "government", "election", "parliament", "empire", "state")),
        ("Science & Discovery", ("science", "scientist", "discovery", "physics", "chemistry", "biology", "research")),
        ("Technology & Engineering", ("technology", "engineering", "railway", "engine", "computer", "software", "machine")),
        ("Economy & Finance", ("economy", "trade", "finance", "bank", "market", "tax", "industry", "commercial")),
        ("Arts & Culture", ("book", "film", "music", "painting", "literature", "art", "novel", "poem")),
        ("Religion & Philosophy", ("religion", "church", "temple", "philosophy", "caste", "ritual", "spiritual")),
        ("Law & Treaties", ("law", "treaty", "constitution", "court", "legal", "act", "rights")),
        ("Nature, Environment & Climate", ("climate", "river", "forest", "earthquake", "flood", "drought", "environment")),
        ("Health & Medicine", ("health", "medicine", "disease", "hospital", "pandemic", "medical")),
        ("Exploration & Geography", ("exploration", "expedition", "voyage", "geography", "island", "mountain")),
        ("Media & Portrayals", ("portrayal", "depiction", "media", "television", "game", "documentary")),
    ]
    for lane, keywords in rules:
        if any(keyword in haystack for keyword in keywords):
            return lane
    return "Society & People"


def _timeline_excerpt(text: str, time_dim: TimeDimension, radius: int = 180) -> str:
    clean = " ".join((text or "").split())
    if not clean:
        return ""

    candidates = [time_dim.label]
    if time_dim.start_date:
        candidates.append(time_dim.start_date)
    if time_dim.year:
        candidates.append(str(time_dim.year))

    index = -1
    for candidate in candidates:
        if not candidate:
            continue
        index = clean.lower().find(str(candidate).lower())
        if index >= 0:
            break
    if index < 0:
        index = 0

    start = max(0, index - radius)
    end = min(len(clean), index + radius)
    excerpt = clean[start:end].strip()
    if start > 0:
        excerpt = f"...{excerpt}"
    if end < len(clean):
        excerpt = f"{excerpt}..."
    return excerpt
