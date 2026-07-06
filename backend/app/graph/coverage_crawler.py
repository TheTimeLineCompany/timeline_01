"""Bounded graph-frontier coverage crawler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.db.models import RelatedCache, SectionClean
from app.graph.driver import execute_query
from app.graph.frontier import discover_and_sync_graph_frontier
from app.orchestration.priorities import compute_candidate_priority
from app.orchestration.state import upsert_processing_state
from app.services import ReaderService
from app.workers.core_digest import enqueue_core_digest_job
from app.workers.embeddings import enqueue_embedding_jobs
from app.workers.related_agent import enqueue_related_jobs, is_agent_related_section
from app.workers.temporal_agent import enqueue_temporal_jobs
from app.workers.timeline_context import enqueue_timeline_context_jobs


@dataclass(frozen=True)
class GraphCoverageArticleResult:
    """One graph-frontier article coverage action."""

    title_id: int
    title: str
    level: int
    sections_cached: int
    sections_selected: int
    jobs_enqueued: dict[str, int] = field(default_factory=dict)
    status: str = "queued"
    error: str | None = None


@dataclass(frozen=True)
class GraphCoverageResult:
    """Summary of a bounded graph coverage crawl."""

    title: str
    title_id: int
    frontier_considered: int
    articles_processed: int
    jobs_enqueued: dict[str, int]
    articles: list[GraphCoverageArticleResult]


async def crawl_article_graph_coverage(
    session: AsyncSession,
    *,
    title: str,
    max_articles: int = 10,
    max_sections_per_article: int = 4,
    force: bool = False,
) -> GraphCoverageResult:
    """Cache and enqueue bounded work for the current article's graph frontier."""

    max_articles = max(0, min(50, int(max_articles)))
    max_sections_per_article = max(1, min(12, int(max_sections_per_article)))
    service = ReaderService(session)
    canonical, title_id, source_sections = await service.get_article(
        title,
        seed=False,
        enrich_ontology=False,
    )
    source_section_keys = [section.section_key for section in source_sections if is_content_section(section)]

    frontier_expansion = await discover_and_sync_graph_frontier(
        session,
        title=canonical,
        title_id=title_id,
        sections=source_sections,
        max_l1_articles=max(50, max_articles * 8),
        max_l1_articles_to_cache=_l1_intro_cache_budget(max_articles),
        max_l2_links_per_l1=_l2_links_budget(max_articles),
    )
    frontier = await _frontier_articles(session, title_id=title_id, section_keys=source_section_keys)
    selected_frontier = _select_frontier_candidates(frontier, limit=max_articles)

    totals = {
        "embedding_generate_v1": 0,
        "temporal_extract_v1": 0,
        "related_l1_l2_explain_v1": 0,
        "core_digest_v1": 0,
        "timeline_context_promote_v1": 0,
    }
    article_results: list[GraphCoverageArticleResult] = []
    source_useful_sections = [section for section in source_sections if is_content_section(section)]
    source_related_sections = [section for section in source_sections if is_agent_related_section(section)]
    source_jobs = {
        "embedding_generate_v1": await enqueue_embedding_jobs(
            session,
            source_useful_sections,
            priority=48,
            force=force,
        ),
        "temporal_extract_v1": await enqueue_temporal_jobs(
            session,
            source_useful_sections,
            priority=70,
            force=force,
        ),
        "related_l1_l2_explain_v1": await enqueue_related_jobs(
            session,
            source_related_sections,
            priority=82,
            force=force,
        ),
        "core_digest_v1": await enqueue_core_digest_job(
            session,
            title_id=title_id,
            title=canonical,
            sections=source_sections,
            priority=30,
            force=force,
        ),
    }
    for key, value in source_jobs.items():
        totals[key] += int(value or 0)

    await upsert_processing_state(
        session,
        title_id=title_id,
        area="graph_frontier",
        state="running" if selected_frontier else ("pending" if any(source_jobs.values()) else "completed"),
        expected_count=max(len(selected_frontier), len(source_related_sections)),
        pending_count=len(source_related_sections) if not selected_frontier and any(source_jobs.values()) else 0,
        running_count=len(selected_frontier),
        detail=(
            f"Graph coverage crawler selected {len(selected_frontier)} frontier article(s) after "
            f"expanding {len(frontier_expansion.l1_articles)} L1 and "
            f"{len(frontier_expansion.l2_articles)} L2 candidate(s)."
        ),
        reason="User requested graph-frontier coverage; source graph and bounded L2 discovery are ensured first.",
        source="graph_coverage_crawler",
        metadata={
            "source_jobs_enqueued": source_jobs,
            "frontier_expansion": {
                "l1_articles": len(frontier_expansion.l1_articles),
                "l2_articles": len(frontier_expansion.l2_articles),
                "l0_to_l1_edges": frontier_expansion.l0_to_l1_edges,
                "l1_to_l2_edges": frontier_expansion.l1_to_l2_edges,
                "l1_articles_cached": frontier_expansion.l1_articles_cached,
                "unresolved_links": frontier_expansion.unresolved_links,
            },
        },
    )

    for candidate in selected_frontier:
        candidate_title = str(candidate["to_title"])
        candidate_level = int(candidate["level"])
        candidate_title_id = int(candidate["to_title_id"])
        priority_components = compute_candidate_priority(
            level=candidate_level,
            link_rank=int(candidate.get("link_order") or 0),
            source_article_link_count=int(candidate.get("source_article_link_count") or 1),
            intro_similarity=float(candidate.get("score") or 0),
            estimated_cost=0.35 if candidate_level == 1 else 0.55,
        )
        base_priority = int(priority_components.priority)
        try:
            resolved_title, resolved_title_id, sections = await service.get_article(
                candidate_title,
                seed=False,
                enrich_ontology=False,
            )
            selected_sections = _select_frontier_sections(
                sections,
                limit=_frontier_section_limit(
                    configured_limit=max_sections_per_article,
                    priority_score=priority_components.S_prio,
                ),
            )
            jobs = {
                "embedding_generate_v1": await enqueue_embedding_jobs(
                    session,
                    selected_sections,
                    priority=base_priority,
                    force=force,
                ),
                "temporal_extract_v1": await enqueue_temporal_jobs(
                    session,
                    selected_sections,
                    priority=min(95, base_priority + 12),
                    force=force,
                ),
                "related_l1_l2_explain_v1": await enqueue_related_jobs(
                    session,
                    [section for section in selected_sections if is_agent_related_section(section)],
                    priority=min(98, base_priority + 18),
                    force=force,
                ),
                "core_digest_v1": await enqueue_core_digest_job(
                    session,
                    title_id=resolved_title_id,
                    title=resolved_title,
                    sections=sections,
                    priority=max(25, base_priority - 8),
                    force=force,
                ),
            }
            for key, value in jobs.items():
                totals[key] += int(value or 0)
            await upsert_processing_state(
                session,
                title_id=resolved_title_id,
                area="graph_frontier",
                state="pending" if any(jobs.values()) else "completed",
                expected_count=max(1, len(selected_sections)),
                pending_count=len(selected_sections) if any(jobs.values()) else 0,
                completed_count=0 if any(jobs.values()) else 1,
                detail=(
                    f"Cached {len(sections)} section(s); selected {len(selected_sections)} "
                    "for graph coverage."
                ),
                reason=f"Reached from {canonical} as L{candidate_level}.",
                source="graph_coverage_crawler",
                metadata={
                    "source_title_id": title_id,
                    "source_title": canonical,
                    "level": candidate_level,
                    "score": candidate.get("score"),
                    "priority": priority_components.as_dict(),
                },
            )
            article_results.append(
                GraphCoverageArticleResult(
                    title_id=resolved_title_id,
                    title=resolved_title,
                    level=candidate_level,
                    sections_cached=len(sections),
                    sections_selected=len(selected_sections),
                    jobs_enqueued=jobs,
                    status="queued" if any(jobs.values()) else "covered",
                )
            )
        except Exception as exc:
            await upsert_processing_state(
                session,
                title_id=candidate_title_id,
                area="graph_frontier",
                state="attention",
                expected_count=1,
                failed_count=1,
                detail=f"Graph coverage failed for {candidate_title}.",
                reason=str(exc),
                last_error=str(exc),
                source="graph_coverage_crawler",
            )
            article_results.append(
                GraphCoverageArticleResult(
                    title_id=candidate_title_id,
                    title=candidate_title,
                    level=candidate_level,
                    sections_cached=0,
                    sections_selected=0,
                    status="failed",
                    error=str(exc),
                )
            )

    timeline_jobs = await enqueue_timeline_context_jobs(
        session,
        source_useful_sections,
        priority=90,
        force=force,
    )
    totals["timeline_context_promote_v1"] += int(timeline_jobs or 0)
    await upsert_processing_state(
        session,
        title_id=title_id,
        area="graph_frontier",
        state="completed",
        expected_count=len(selected_frontier),
        completed_count=sum(1 for item in article_results if item.status in {"queued", "covered"}),
        failed_count=sum(1 for item in article_results if item.status == "failed"),
        detail=f"Graph coverage pass completed for {len(article_results)} frontier article(s).",
        reason="Crawler finished bounded graph-frontier pass.",
        source="graph_coverage_crawler",
        metadata={
            "jobs_enqueued": totals,
            "frontier_expansion": {
                "l1_articles": len(frontier_expansion.l1_articles),
                "l2_articles": len(frontier_expansion.l2_articles),
                "l0_to_l1_edges": frontier_expansion.l0_to_l1_edges,
                "l1_to_l2_edges": frontier_expansion.l1_to_l2_edges,
                "l1_articles_cached": frontier_expansion.l1_articles_cached,
                "unresolved_links": frontier_expansion.unresolved_links,
            },
        },
    )

    return GraphCoverageResult(
        title=canonical,
        title_id=title_id,
        frontier_considered=len(frontier),
        articles_processed=len(article_results),
        jobs_enqueued=totals,
        articles=article_results,
    )


def _l1_intro_cache_budget(max_articles: int) -> int:
    """Bound crawler L1 hydration separately from the fast article-load path."""

    if max_articles <= 0:
        return 0
    return max(4, min(40, max_articles * 2))


def _l2_links_budget(max_articles: int) -> int:
    """Keep L2 discovery broad enough for signal without making one crawl unbounded."""

    if max_articles <= 0:
        return 0
    return max(12, min(48, max_articles * 4))


def _select_frontier_candidates(frontier: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Pick a bounded frontier slice while reserving room for L2 coverage."""

    if limit <= 0 or not frontier:
        return []
    l1_rows = [row for row in frontier if int(row.get("level") or 1) == 1]
    l2_rows = [row for row in frontier if int(row.get("level") or 1) == 2]
    if not l1_rows or not l2_rows or limit == 1:
        return frontier[:limit]

    l2_target = max(1, min(len(l2_rows), limit // 2))
    l1_target = min(len(l1_rows), limit - l2_target)
    selected = l1_rows[:l1_target] + l2_rows[:l2_target]
    if len(selected) < limit:
        selected_ids = {int(row["to_title_id"]) for row in selected}
        for row in frontier:
            try:
                row_id = int(row["to_title_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if row_id in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(row_id)
            if len(selected) >= limit:
                break
    return sorted(
        selected,
        key=lambda item: (-float(item.get("score") or 0), int(item.get("level") or 1), int(item.get("link_order") or 9999), str(item.get("to_title") or "")),
    )


def _frontier_section_limit(*, configured_limit: int, priority_score: float) -> int:
    """Only deepen beyond the intro when a candidate has a strong first-pass prior."""

    configured_limit = max(1, int(configured_limit))
    if configured_limit == 1:
        return 1
    if priority_score >= 0.62:
        return configured_limit
    if priority_score >= 0.52:
        return min(configured_limit, 2)
    return 1


async def _frontier_articles(
    session: AsyncSession,
    *,
    title_id: int,
    section_keys: list[str],
) -> list[dict[str, Any]]:
    graph_rows = await _frontier_articles_from_neo4j(title_id=title_id)
    if graph_rows:
        return graph_rows
    if not section_keys:
        return []
    result = await session.execute(
        select(
            RelatedCache.to_title_id,
            RelatedCache.to_title,
            RelatedCache.level,
            RelatedCache.score,
        )
        .where(RelatedCache.from_section_key.in_(section_keys))
        .where(RelatedCache.to_title_id != title_id)
        .order_by(RelatedCache.score.desc(), RelatedCache.level.asc(), RelatedCache.to_title.asc())
        .limit(500)
    )
    seen: set[int] = set()
    rows: list[dict[str, Any]] = []
    for to_title_id, to_title, level, score in result.all():
        related_id = int(to_title_id)
        if related_id in seen:
            continue
        seen.add(related_id)
        rows.append(
            {
                "to_title_id": related_id,
                "to_title": str(to_title),
                "level": int(level),
                "score": float(score or 0),
                "source_article_link_count": 1,
            }
        )
    return rows


async def _frontier_articles_from_neo4j(*, title_id: int) -> list[dict[str, Any]]:
    """Return L1/L2 frontier articles from the graph backbone."""

    try:
        rows = await execute_query(
            """
            MATCH (focus:V4Article {title_id: $title_id})-[:HAS_SECTION]->(:V4Section)-[r1:LINKS_TO]->(l1:V4Article)
            WHERE l1.title_id <> $title_id
            WITH l1,
                 max(coalesce(r1.score, 0.5)) AS l1_score,
                 min(coalesce(r1.link_order, 9999)) AS l1_order,
                 count(DISTINCT r1) AS l1_link_count
            OPTIONAL MATCH (l1)-[r2:LINKS_TO]->(l2:V4Article)
            WHERE l2.title_id <> $title_id
              AND l2.title_id <> l1.title_id
            WITH collect(DISTINCT {
                    to_title_id: l1.title_id,
                    to_title: l1.title,
                    level: 1,
                    score: l1_score,
                    link_order: l1_order,
                    source_article_link_count: l1_link_count
                 }) AS l1_rows,
                 collect(DISTINCT {
                    to_title_id: l2.title_id,
                    to_title: l2.title,
                    level: 2,
                    score: coalesce(r2.score, 0.38),
                    link_order: coalesce(r2.link_order, 9999),
                    source_article_link_count: 1
                 }) AS l2_rows
            RETURN l1_rows + l2_rows AS rows
            """,
            {"title_id": int(title_id)},
        )
    except Exception:
        return []
    if not rows:
        return []
    seen: set[int] = set()
    output: list[dict[str, Any]] = []
    for row in rows[0].get("rows") or []:
        if not isinstance(row, dict):
            continue
        try:
            related_id = int(row.get("to_title_id"))
        except (TypeError, ValueError):
            continue
        if related_id in seen or related_id == int(title_id):
            continue
        seen.add(related_id)
        output.append(
            {
                "to_title_id": related_id,
                "to_title": str(row.get("to_title") or related_id),
                "level": int(row.get("level") or 1),
                "score": float(row.get("score") or 0),
                "link_order": int(row.get("link_order") or 9999),
                "source_article_link_count": int(row.get("source_article_link_count") or 1),
            }
        )
    return sorted(output, key=lambda item: (-item["score"], item["level"], item["link_order"], item["to_title"]))


def _select_frontier_sections(sections: list[SectionClean], *, limit: int) -> list[SectionClean]:
    useful = [section for section in sections if is_content_section(section)]
    if not useful:
        return []

    def rank(section: SectionClean) -> tuple[int, int]:
        heading = (section.heading or "").strip().lower()
        lead_rank = 0 if heading in {"lead", "introduction", ""} or (section.level or 0) == 0 else 1
        history_rank = 0 if any(token in heading for token in ("history", "background", "overview")) else 1
        return (lead_rank + history_rank, int(section.heading_id))

    return sorted(useful, key=rank)[:limit]
