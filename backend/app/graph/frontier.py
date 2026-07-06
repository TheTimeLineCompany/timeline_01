"""Cheap graph-frontier discovery from cached wiki links."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.core.config import get_settings
from app.db.models import SectionClean
from app.graph.driver import execute_write
from app.graph.schema import (
    ARTICLE_KIND,
    GRAPH_SCHEMA_VERSION,
    LINK_EXTRACTION_VERSION,
    SCOPE_ARTICLE_INTRO_OUTGOING_LINKS,
    SCOPE_SECTION_OUTGOING_LINKS,
)
from app.ingestion.text_cleaner import normalize_title_target
from app.ingestion.redirects import RedirectResolver
from app.orchestration.state import upsert_processing_state
from app.services import ReaderService

settings = get_settings()


@dataclass(frozen=True)
class FrontierArticle:
    """Resolved linked article in the graph frontier."""

    title_id: int
    title: str
    level: int


@dataclass(frozen=True)
class GraphFrontierResult:
    """Summary for one cheap graph-frontier discovery pass."""

    l1_articles: list[FrontierArticle] = field(default_factory=list)
    l2_articles: list[FrontierArticle] = field(default_factory=list)
    has_section_edges: int = 0
    l0_to_l1_edges: int = 0
    l1_to_l2_edges: int = 0
    l1_articles_cached: int = 0
    unresolved_links: int = 0


async def expand_section_graph_frontier(
    session: AsyncSession,
    *,
    focus_title_id: int,
    section: SectionClean,
    max_l2_links_per_l1: int = 0,
    expand_l2: bool = False,
) -> GraphFrontierResult:
    """Lazy-expand one L0 section into L1 and then L2 links.

    This is the click-time version of the Core graph loop. It is deterministic
    and CPU/DB/Neo4j only: resolve the selected section's L1 links first, then
    load each selected L1 intro/first section and sync its L2 links.
    """

    resolver = RedirectResolver(session)
    reader = ReaderService(session)
    l1_rows: list[dict[str, Any]] = []
    l0_to_l1_edges: list[dict[str, Any]] = []
    unresolved = 0

    section_l1_articles: dict[int, dict[str, Any]] = {}
    section_l1_edges: list[dict[str, Any]] = []
    seen_l1: set[int] = set()
    await _mark_section_link_extraction(
        section_key=section.section_key,
        state="running",
        scope=SCOPE_SECTION_OUTGOING_LINKS,
        expected=len(section.links_json or []),
        completed=0,
    )
    for order, link in enumerate(section.links_json or []):
        target = _frontier_target(link)
        if not target:
            continue
        resolved = await resolver.resolve_title(target)
        if resolved is None:
            unresolved += 1
            continue
        linked_title, linked_title_id = resolved
        linked_title_id = int(linked_title_id)
        if linked_title_id == int(focus_title_id) or linked_title_id in seen_l1:
            continue
        seen_l1.add(linked_title_id)
        row = {
            "title_id": linked_title_id,
            "title": linked_title,
            "count": 1,
            "first_order": order,
            "sections": {section.section_key},
        }
        l1_rows.append(row)
        section_l1_articles[linked_title_id] = {
            "title_id": linked_title_id,
            "title": linked_title,
            "level": 1,
            "kind": "frontier_article",
        }
        edge = {
            "section_key": section.section_key,
            "section_title_id": int(section.title_id),
            "section_heading_id": int(section.heading_id),
            "to_title_id": linked_title_id,
            "to_title": linked_title,
            "link_order": order,
            "label": (link or {}).get("label"),
            "char_start": (link or {}).get("char_start"),
            "char_end": (link or {}).get("char_end"),
        }
        section_l1_edges.append(edge)
        l0_to_l1_edges.append(edge)

    if section_l1_articles:
        await _sync_article_nodes_to_neo4j(list(section_l1_articles.values()))
    if section_l1_edges:
        await _sync_l0_to_l1_edges_to_neo4j(section_l1_edges)
    await _mark_section_link_extraction(
        section_key=section.section_key,
        state="completed",
        scope=SCOPE_SECTION_OUTGOING_LINKS,
        expected=len(section.links_json or []),
        completed=len(section_l1_edges),
    )

    l2_result = (
        await expand_l1_graph_frontier_many(
            session,
            focus_title_id=focus_title_id,
            l1_articles=l1_rows,
            max_l2_links_per_l1=max_l2_links_per_l1,
        )
        if expand_l2
        else GraphFrontierResult()
    )
    return GraphFrontierResult(
        l1_articles=[
            FrontierArticle(title_id=int(row["title_id"]), title=str(row["title"]), level=1)
            for row in l1_rows
        ],
        l2_articles=l2_result.l2_articles,
        has_section_edges=0,
        l0_to_l1_edges=len(l0_to_l1_edges),
        l1_to_l2_edges=l2_result.l1_to_l2_edges,
        l1_articles_cached=l2_result.l1_articles_cached,
        unresolved_links=unresolved + l2_result.unresolved_links,
    )


async def expand_l1_graph_frontier_many(
    session: AsyncSession,
    *,
    focus_title_id: int,
    l1_articles: list[dict[str, Any]],
    max_l2_links_per_l1: int = 0,
) -> GraphFrontierResult:
    """Lazy-expand a list of L1 articles into L2 links."""

    resolver = RedirectResolver(session)
    reader = ReaderService(session)
    l2_by_title_id: dict[int, dict[str, Any]] = {}
    l1_to_l2_edges: list[dict[str, Any]] = []
    cached_l1_count = 0
    unresolved = 0

    for l1 in l1_articles:
        result = await _expand_one_l1_to_l2(
            resolver=resolver,
            reader=reader,
            focus_title_id=focus_title_id,
            l1_title_id=int(l1["title_id"]),
            l1_title=str(l1["title"]),
            max_l2_links_per_l1=max_l2_links_per_l1,
        )
        cached_l1_count += result["cached_l1_count"]
        unresolved += result["unresolved"]
        for row in result["l2_rows"]:
            l2_entry = l2_by_title_id.setdefault(
                int(row["title_id"]),
                {
                    "title_id": int(row["title_id"]),
                    "title": str(row["title"]),
                    "count": 0,
                    "first_order": int(row["first_order"]),
                    "parents": set(),
                },
            )
            l2_entry["count"] += int(row.get("count") or 1)
            l2_entry["first_order"] = min(int(l2_entry["first_order"]), int(row["first_order"]))
            l2_entry["parents"].update(row.get("parents") or set())
        l1_to_l2_edges.extend(result["edges"])

    ranked_l2 = sorted(
        l2_by_title_id.values(),
        key=lambda row: (-int(row["count"]), int(row["first_order"]), str(row["title"]).casefold()),
    )
    return GraphFrontierResult(
        l2_articles=[
            FrontierArticle(title_id=int(row["title_id"]), title=str(row["title"]), level=2)
            for row in ranked_l2
        ],
        l1_to_l2_edges=len(l1_to_l2_edges),
        l1_articles_cached=cached_l1_count,
        unresolved_links=unresolved,
    )


async def expand_l1_graph_frontier(
    session: AsyncSession,
    *,
    focus_title_id: int,
    l1_title_id: int,
    l1_title: str,
    max_l2_links_per_l1: int = 0,
) -> GraphFrontierResult:
    """Lazy-expand one L1 article into L2 links."""

    return await expand_l1_graph_frontier_many(
        session,
        focus_title_id=focus_title_id,
        l1_articles=[{"title_id": int(l1_title_id), "title": l1_title, "count": 1, "first_order": 0}],
        max_l2_links_per_l1=max_l2_links_per_l1,
    )


async def discover_and_sync_graph_frontier(
    session: AsyncSession,
    *,
    title: str,
    title_id: int,
    sections: list[SectionClean],
    max_l1_articles: int = 200,
    max_l1_articles_to_cache: int = 0,
    max_l2_links_per_l1: int = 0,
    l2_source_scope: str = "intro_l1",
) -> GraphFrontierResult:
    """Resolve cheap L1/L2 link framework and sync it to Neo4j.

    This is intentionally CPU/DB only. It does not call the LLM and it does not
    require embeddings or related-cache scoring. The output is a broad structural
    frontier that later enrichment/scoring can refine.
    """

    resolver = RedirectResolver(session)
    reader = ReaderService(session)

    l0_sections = sorted(
        [section for section in sections if is_content_section(section)],
        key=lambda section: (int(section.level or 99), int(section.heading_id)),
    )
    intro_section_keys = _intro_section_keys(l0_sections)
    l1_by_title_id: dict[int, dict[str, Any]] = {}
    l0_to_l1_edges: list[dict[str, Any]] = []
    unresolved = 0

    await _sync_frontier_progress(
        session,
        title_id=title_id,
        stage="base_graph",
        state="running",
        expected=max(1, len(l0_sections)),
        completed=0,
        l1_count=0,
        l2_count=0,
        edge_count=0,
        detail=f"Graph frontier started. Syncing {len(l0_sections)} content section(s) before L2 expansion.",
    )

    await _sync_base_graph_to_neo4j(title=title, title_id=title_id, sections=sections)

    for section_index, section in enumerate(l0_sections, start=1):
        section_l1_articles: dict[int, dict[str, Any]] = {}
        section_l1_edges: list[dict[str, Any]] = []
        await _mark_section_link_extraction(
            section_key=section.section_key,
            state="running",
            scope=SCOPE_SECTION_OUTGOING_LINKS,
            expected=len(section.links_json or []),
            completed=0,
        )
        for order, link in enumerate(section.links_json or []):
            target = _frontier_target(link)
            if not target:
                continue
            resolved = await resolver.resolve_title(target)
            if resolved is None:
                unresolved += 1
                continue
            linked_title, linked_title_id = resolved
            linked_title_id = int(linked_title_id)
            if linked_title_id == int(title_id):
                continue
            l1_entry = l1_by_title_id.setdefault(
                linked_title_id,
                {
                    "title_id": linked_title_id,
                    "title": linked_title,
                    "count": 0,
                    "first_order": order,
                    "sections": set(),
                },
            )
            l1_entry["count"] += 1
            l1_entry["first_order"] = min(int(l1_entry["first_order"]), order)
            l1_entry["sections"].add(section.section_key)
            section_l1_articles[linked_title_id] = {
                "title_id": linked_title_id,
                "title": linked_title,
                "level": 1,
                "kind": "frontier_article",
            }
            edge = {
                "section_key": section.section_key,
                "section_title_id": int(section.title_id),
                "section_heading_id": int(section.heading_id),
                "to_title_id": linked_title_id,
                "to_title": linked_title,
                "link_order": order,
                "label": (link or {}).get("label"),
                "char_start": (link or {}).get("char_start"),
                "char_end": (link or {}).get("char_end"),
            }
            section_l1_edges.append(edge)
            l0_to_l1_edges.append(edge)
        if section_l1_articles:
            await _sync_article_nodes_to_neo4j(list(section_l1_articles.values()))
        if section_l1_edges:
            await _sync_l0_to_l1_edges_to_neo4j(section_l1_edges)
        await _mark_section_link_extraction(
            section_key=section.section_key,
            state="completed",
            scope=SCOPE_SECTION_OUTGOING_LINKS,
            expected=len(section.links_json or []),
            completed=len(section_l1_edges),
        )
        await _sync_frontier_progress(
            session,
            title_id=title_id,
            stage="l0_to_l1",
            state="running",
            expected=max(1, len(l0_sections)),
            completed=section_index,
            l1_count=len(l1_by_title_id),
            l2_count=0,
            edge_count=len(l0_to_l1_edges),
            detail=(
                f"L0 link extraction {section_index}/{len(l0_sections)} section(s); "
                f"{len(l1_by_title_id)} L1 article(s), {len(l0_to_l1_edges)} section link edge(s)."
            ),
        )

    ranked_l1 = sorted(
        l1_by_title_id.values(),
        key=lambda row: (-int(row["count"]), int(row["first_order"]), str(row["title"]).casefold()),
    )
    if max_l1_articles > 0:
        ranked_l1 = ranked_l1[:max_l1_articles]
        retained_l1_ids = {int(row["title_id"]) for row in ranked_l1}
        l0_to_l1_edges = [edge for edge in l0_to_l1_edges if int(edge["to_title_id"]) in retained_l1_ids]

    l2_by_title_id: dict[int, dict[str, Any]] = {}
    l1_to_l2_edges: list[dict[str, Any]] = []
    cached_l1_count = 0
    l1_for_l2 = _l1_articles_for_l2_expansion(
        ranked_l1,
        intro_section_keys=intro_section_keys,
        source_scope=l2_source_scope,
    )
    if max_l1_articles_to_cache > 0:
        l1_for_l2 = l1_for_l2[:max_l1_articles_to_cache]

    await _sync_frontier_progress(
        session,
        title_id=title_id,
        stage="l1_to_l2",
        state="running" if l1_for_l2 else "completed",
        expected=max(1, len(l1_for_l2)),
        completed=0 if l1_for_l2 else 1,
        l1_count=len(ranked_l1),
        l2_count=0,
        edge_count=len(l0_to_l1_edges),
        detail=(
            f"L1 framework ready with {len(ranked_l1)} article(s). "
            f"Expanding {len(l1_for_l2)} intro-linked L1 article(s) for L2."
        ),
    )

    for l1_index, l1 in enumerate(l1_for_l2, start=1):
        result = await _expand_one_l1_to_l2(
            resolver=resolver,
            reader=reader,
            focus_title_id=title_id,
            l1_title_id=int(l1["title_id"]),
            l1_title=str(l1["title"]),
            max_l2_links_per_l1=max_l2_links_per_l1,
        )
        cached_l1_count += result["cached_l1_count"]
        unresolved += result["unresolved"]
        for row in result["l2_rows"]:
            l2_entry = l2_by_title_id.setdefault(
                int(row["title_id"]),
                {
                    "title_id": int(row["title_id"]),
                    "title": str(row["title"]),
                    "count": 0,
                    "first_order": int(row["first_order"]),
                    "parents": set(),
                },
            )
            l2_entry["count"] += int(row.get("count") or 1)
            l2_entry["first_order"] = min(int(l2_entry["first_order"]), int(row["first_order"]))
            l2_entry["parents"].update(row.get("parents") or set())
        l1_to_l2_edges.extend(result["edges"])
        await _sync_frontier_progress(
            session,
            title_id=title_id,
            stage="l1_to_l2",
            state="running",
            expected=max(1, len(l1_for_l2)),
            completed=l1_index,
            l1_count=len(ranked_l1),
            l2_count=len(l2_by_title_id),
            edge_count=len(l0_to_l1_edges) + len(l1_to_l2_edges),
            detail=(
                f"L2 link extraction {l1_index}/{len(l1_for_l2)} L1 article(s); "
                f"{len(l2_by_title_id)} L2 article(s), {len(l1_to_l2_edges)} L1-to-L2 edge(s)."
            ),
        )

    ranked_l2 = sorted(
        l2_by_title_id.values(),
        key=lambda row: (-int(row["count"]), int(row["first_order"]), str(row["title"]).casefold()),
    )

    return GraphFrontierResult(
        l1_articles=[
            FrontierArticle(title_id=int(row["title_id"]), title=str(row["title"]), level=1)
            for row in ranked_l1
        ],
        l2_articles=[
            FrontierArticle(title_id=int(row["title_id"]), title=str(row["title"]), level=2)
            for row in ranked_l2
        ],
        has_section_edges=len(sections),
        l0_to_l1_edges=len(l0_to_l1_edges),
        l1_to_l2_edges=len(l1_to_l2_edges),
        l1_articles_cached=cached_l1_count,
        unresolved_links=unresolved,
    )


def _frontier_links_from_sections(sections: list[SectionClean], *, max_links: int) -> list[dict[str, Any]]:
    content_sections = [section for section in sections if is_content_section(section)]
    if not content_sections:
        return []
    intro_sections = [section for section in content_sections if _is_intro_section(section)]
    ranked_sections = intro_sections or sorted(
        content_sections,
        key=lambda section: (int(section.level or 99), int(section.heading_id)),
    )[:1]
    links: list[dict[str, Any]] = []
    for section in ranked_sections:
        links.extend(section.links_json or [])
        if max_links > 0 and len(links) >= max_links:
            break
    return links[:max_links] if max_links > 0 else links


async def _expand_one_l1_to_l2(
    *,
    resolver: RedirectResolver,
    reader: ReaderService,
    focus_title_id: int,
    l1_title_id: int,
    l1_title: str,
    max_l2_links_per_l1: int,
) -> dict[str, Any]:
    await _mark_article_link_extraction(
        title_id=l1_title_id,
        state="running",
        scope=SCOPE_ARTICLE_INTRO_OUTGOING_LINKS,
        expected=0,
        completed=0,
    )
    try:
        _, resolved_l1_id, l1_sections = await asyncio.wait_for(
            reader.get_article(
                l1_title,
                seed=False,
                enrich_ontology=False,
            ),
            timeout=max(5.0, float(settings.graph_frontier_article_load_timeout_seconds)),
        )
    except asyncio.TimeoutError:
        await _mark_article_link_extraction(
            title_id=l1_title_id,
            state="failed",
            scope=SCOPE_ARTICLE_INTRO_OUTGOING_LINKS,
            expected=0,
            completed=0,
            error=(
                "Timed out while loading article intro for outgoing link extraction "
                f"after {settings.graph_frontier_article_load_timeout_seconds:.0f}s."
            ),
        )
        return {"cached_l1_count": 0, "unresolved": 0, "l2_rows": [], "edges": []}
    except Exception:
        await _mark_article_link_extraction(
            title_id=l1_title_id,
            state="failed",
            scope=SCOPE_ARTICLE_INTRO_OUTGOING_LINKS,
            expected=0,
            completed=0,
            error="Unable to load article intro for outgoing link extraction.",
        )
        return {"cached_l1_count": 0, "unresolved": 0, "l2_rows": [], "edges": []}

    if int(resolved_l1_id) != int(l1_title_id):
        l1_title_id = int(resolved_l1_id)
        await _mark_article_link_extraction(
            title_id=l1_title_id,
            state="running",
            scope=SCOPE_ARTICLE_INTRO_OUTGOING_LINKS,
            expected=0,
            completed=0,
        )
    l1_intro_links = _frontier_links_from_sections(l1_sections, max_links=max_l2_links_per_l1)
    current_l2_articles: dict[int, dict[str, Any]] = {}
    current_l2_edges: list[dict[str, Any]] = []
    l2_rows: list[dict[str, Any]] = []
    unresolved = 0
    seen_l2: set[int] = set()
    for order, link in enumerate(l1_intro_links):
        target = _frontier_target(link)
        if not target:
            continue
        resolved = await resolver.resolve_title(target)
        if resolved is None:
            unresolved += 1
            continue
        l2_title, l2_title_id = resolved
        l2_title_id = int(l2_title_id)
        if l2_title_id in {int(focus_title_id), int(l1_title_id)} or l2_title_id in seen_l2:
            continue
        seen_l2.add(l2_title_id)
        current_l2_articles[l2_title_id] = {
            "title_id": l2_title_id,
            "title": l2_title,
            "level": 2,
            "kind": "frontier_article",
        }
        l2_rows.append(
            {
                "title_id": l2_title_id,
                "title": l2_title,
                "count": 1,
                "first_order": order,
                "parents": {int(l1_title_id)},
            }
        )
        current_l2_edges.append(
            {
                "from_title_id": int(l1_title_id),
                "from_title": l1_title,
                "to_title_id": l2_title_id,
                "to_title": l2_title,
                "link_order": order,
                "label": (link or {}).get("label"),
                "char_start": (link or {}).get("char_start"),
                "char_end": (link or {}).get("char_end"),
            }
        )
    if current_l2_articles:
        await _sync_article_nodes_to_neo4j(list(current_l2_articles.values()))
    if current_l2_edges:
        await _sync_l1_to_l2_edges_to_neo4j(current_l2_edges)
    await _mark_article_link_extraction(
        title_id=l1_title_id,
        state="completed",
        scope=SCOPE_ARTICLE_INTRO_OUTGOING_LINKS,
        expected=len(l1_intro_links),
        completed=len(current_l2_edges),
    )
    return {
        "cached_l1_count": 1,
        "unresolved": unresolved,
        "l2_rows": l2_rows,
        "edges": current_l2_edges,
    }


def _l1_articles_for_l2_expansion(
    ranked_l1: list[dict[str, Any]],
    *,
    intro_section_keys: set[str],
    source_scope: str,
) -> list[dict[str, Any]]:
    """Return the L1 articles whose intros should be loaded for L2 discovery."""

    scope = (source_scope or "intro_l1").strip().lower()
    if scope in {"all", "all_l1"}:
        return ranked_l1
    if scope in {"none", "off", "disabled"}:
        return []
    intro_l1 = [
        row
        for row in ranked_l1
        if set(str(key) for key in (row.get("sections") or set())) & intro_section_keys
    ]
    return intro_l1 or ranked_l1


def _intro_section_keys(sections: list[SectionClean]) -> set[str]:
    """Return section keys that define the article's first-frame context."""

    intro_sections = [section for section in sections if _is_intro_section(section)]
    if intro_sections:
        return {section.section_key for section in intro_sections}
    first_content = sorted(sections, key=lambda section: (int(section.level or 99), int(section.heading_id)))[:1]
    return {section.section_key for section in first_content}


def _is_intro_section(section: SectionClean) -> bool:
    heading = (section.heading or "").strip().lower()
    return (section.level or 0) == 0 or heading in {"lead", "introduction", "overview"}


def _frontier_target(link: Any) -> str | None:
    """Return a graph-frontier-safe article target."""

    if not isinstance(link, dict):
        return None
    return normalize_title_target(str(link.get("target") or ""))


async def _sync_frontier_progress(
    session: AsyncSession,
    *,
    title_id: int,
    stage: str,
    state: str,
    expected: int,
    completed: int,
    l1_count: int,
    l2_count: int,
    edge_count: int,
    detail: str,
) -> None:
    """Write observable graph-frontier progress between deterministic loop steps."""

    await upsert_processing_state(
        session,
        title_id=title_id,
        section_key="",
        area="graph_frontier",
        state=state,
        expected_count=max(1, int(expected)),
        completed_count=max(0, int(completed)),
        running_count=1 if state == "running" else 0,
        detail=detail,
        reason="Graph frontier is a deterministic CPU/DB/Neo4j loop and should report partial progress.",
        source="graph_frontier_progress",
        metadata={
            "stage": stage,
            "l1_articles": int(l1_count),
            "l2_articles": int(l2_count),
            "link_edges": int(edge_count),
            "model_version": LINK_EXTRACTION_VERSION,
        },
        commit=True,
    )


async def _sync_frontier_to_neo4j(
    *,
    title: str,
    title_id: int,
    sections: list[SectionClean],
    l1_articles: list[dict[str, Any]],
    l2_articles: list[dict[str, Any]],
    l0_to_l1_edges: list[dict[str, Any]],
    l1_to_l2_edges: list[dict[str, Any]],
) -> None:
    await _sync_base_graph_to_neo4j(title=title, title_id=title_id, sections=sections)
    frontier_articles = [
        *[
            {"title_id": int(row["title_id"]), "title": str(row["title"]), "level": 1, "kind": "frontier_article"}
            for row in l1_articles
        ],
        *[
            {"title_id": int(row["title_id"]), "title": str(row["title"]), "level": 2, "kind": "frontier_article"}
            for row in l2_articles
        ],
    ]
    await _sync_article_nodes_to_neo4j(frontier_articles)
    await _sync_l0_to_l1_edges_to_neo4j(l0_to_l1_edges)
    await _sync_l1_to_l2_edges_to_neo4j(l1_to_l2_edges)


async def _sync_base_graph_to_neo4j(
    *,
    title: str,
    title_id: int,
    sections: list[SectionClean],
) -> None:
    articles = [{"title_id": int(title_id), "title": title}]
    section_rows = _section_rows(sections)
    await _sync_article_nodes_to_neo4j(articles)
    await _sync_section_nodes_to_neo4j(section_rows)
    await _sync_has_section_edges_to_neo4j(title_id=title_id, section_rows=section_rows)


def _section_rows(sections: list[SectionClean]) -> list[dict[str, Any]]:
    return [
        {
            "section_key": section.section_key,
            "title_id": int(section.title_id),
            "heading_id": int(section.heading_id),
            "title": section.title,
            "heading": section.heading or "Lead",
            "level": section.level,
            "metrics": json.dumps({"link_count": len(section.links_json or [])}, sort_keys=True),
        }
        for section in sections
    ]


async def _sync_article_nodes_to_neo4j(articles: list[dict[str, Any]]) -> None:
    if not articles:
        return
    article_by_id: dict[int, dict[str, Any]] = {}
    for article in articles:
        article_by_id[int(article["title_id"])] = article
    await execute_write(
        """
        UNWIND $articles AS row
        MERGE (a:V4Article {title_id: row.title_id})
        SET a.title = row.title,
            a.label = row.title,
            a.kind = $article_kind,
            a.graph_schema_version = $graph_schema_version,
            a.updated_at = datetime()
        """,
        {
            "articles": list(article_by_id.values()),
            "article_kind": ARTICLE_KIND,
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
        },
    )


async def _mark_article_link_extraction(
    *,
    title_id: int,
    state: str,
    scope: str,
    expected: int,
    completed: int,
    error: str | None = None,
) -> None:
    await execute_write(
        """
        MATCH (a:V4Article {title_id: $title_id})
        SET a.link_extraction_state = $state,
            a.link_extraction_scope = $scope,
            a.link_extraction_expected = $expected,
            a.link_extraction_completed = $completed,
            a.link_extraction_error = $error,
            a.link_extraction_version = $version,
            a.graph_schema_version = $schema_version,
            a.link_extraction_updated_at = datetime(),
            a.link_extraction_started_at = CASE
                WHEN $state = 'running' THEN datetime()
                ELSE a.link_extraction_started_at
            END,
            a.link_extraction_completed_at = CASE
                WHEN $state IN ['completed', 'failed'] THEN datetime()
                ELSE a.link_extraction_completed_at
            END
        """,
        {
            "title_id": int(title_id),
            "state": state,
            "scope": scope,
            "expected": int(expected),
            "completed": int(completed),
            "error": error,
            "version": LINK_EXTRACTION_VERSION,
            "schema_version": GRAPH_SCHEMA_VERSION,
        },
    )


async def _sync_section_nodes_to_neo4j(section_rows: list[dict[str, Any]]) -> None:
    if not section_rows:
        return
    await execute_write(
        """
        UNWIND $sections AS row
        MERGE (s:V4Section {section_key: row.section_key})
        SET s.title_id = row.title_id,
            s.heading_id = row.heading_id,
            s.title = row.title,
            s.heading = row.heading,
            s.level = row.level,
            s.metrics_json = row.metrics,
            s.graph_schema_version = $graph_schema_version,
            s.updated_at = datetime()
        """,
        {"sections": section_rows, "graph_schema_version": GRAPH_SCHEMA_VERSION},
    )


async def _mark_section_link_extraction(
    *,
    section_key: str,
    state: str,
    scope: str,
    expected: int,
    completed: int,
    error: str | None = None,
) -> None:
    await execute_write(
        """
        MATCH (s:V4Section {section_key: $section_key})
        SET s.link_extraction_state = $state,
            s.link_extraction_scope = $scope,
            s.link_extraction_expected = $expected,
            s.link_extraction_completed = $completed,
            s.link_extraction_error = $error,
            s.link_extraction_version = $version,
            s.graph_schema_version = $schema_version,
            s.link_extraction_updated_at = datetime(),
            s.link_extraction_started_at = CASE
                WHEN $state = 'running' THEN datetime()
                ELSE s.link_extraction_started_at
            END,
            s.link_extraction_completed_at = CASE
                WHEN $state IN ['completed', 'failed'] THEN datetime()
                ELSE s.link_extraction_completed_at
            END
        """,
        {
            "section_key": section_key,
            "state": state,
            "scope": scope,
            "expected": int(expected),
            "completed": int(completed),
            "error": error,
            "version": LINK_EXTRACTION_VERSION,
            "schema_version": GRAPH_SCHEMA_VERSION,
        },
    )


async def _sync_has_section_edges_to_neo4j(*, title_id: int, section_rows: list[dict[str, Any]]) -> None:
    if not section_rows:
        return
    await execute_write(
        """
        UNWIND $sections AS row
        MATCH (a:V4Article {title_id: $title_id})
        MATCH (s:V4Section {section_key: row.section_key})
        MERGE (a)-[r:HAS_SECTION]->(s)
        SET r.source = 'graph_frontier',
            r.updated_at = datetime()
        """,
        {"title_id": int(title_id), "sections": section_rows},
    )


async def _sync_l0_to_l1_edges_to_neo4j(edges: list[dict[str, Any]]) -> None:
    if not edges:
        return
    await execute_write(
        """
        UNWIND $edges AS row
        MATCH (s:V4Section {section_key: row.section_key})
        MATCH (a:V4Article {title_id: row.to_title_id})
        MERGE (s)-[r:LINKS_TO]->(a)
        SET r.source = 'graph_frontier',
            r.method = 'wikilink',
            r.link_order = row.link_order,
            r.label = row.label,
            r.char_start = row.char_start,
            r.char_end = row.char_end,
            r.graph_schema_version = $graph_schema_version,
            r.updated_at = datetime()
        """,
        {"edges": edges, "graph_schema_version": GRAPH_SCHEMA_VERSION},
    )


async def _sync_l1_to_l2_edges_to_neo4j(edges: list[dict[str, Any]]) -> None:
    if not edges:
        return
    await execute_write(
        """
        UNWIND $edges AS row
        MATCH (source:V4Article {title_id: row.from_title_id})
        MATCH (target:V4Article {title_id: row.to_title_id})
        MERGE (source)-[r:LINKS_TO]->(target)
        SET r.source = 'graph_frontier',
            r.method = 'wikilink',
            r.link_order = row.link_order,
            r.label = row.label,
            r.char_start = row.char_start,
            r.char_end = row.char_end,
            r.graph_schema_version = $graph_schema_version,
            r.updated_at = datetime()
        """,
        {"edges": edges, "graph_schema_version": GRAPH_SCHEMA_VERSION},
    )
