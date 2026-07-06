"""Graphology-serializable graph response builder."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.db.models import EntityRegistry, MentionCache, SectionClean, SectionTime, TimeDimension, TimelineContextCache
from app.graph.backbone import build_article_graph_projection, build_article_graph_projection_from_neo4j


def article_key(title_id: int) -> str:
    """Spec-stable article node key."""

    return f"art:{int(title_id)}"


def section_key(title_id: int, heading_id: int) -> str:
    """Spec-stable section node key."""

    return f"sec:{int(title_id)}:{int(heading_id)}"


async def _attach_node_extraction_previews(session: AsyncSession, nodes: list[dict[str, Any]]) -> None:
    """Attach compact text/entity/time previews to graph nodes for relationship cards."""

    title_ids: set[int] = set()
    section_keys: set[str] = set()
    for node in nodes:
        attributes = node.get("attributes") or {}
        title_id = attributes.get("title_id")
        if isinstance(title_id, int):
            title_ids.add(title_id)
        section_value = attributes.get("section_key")
        if isinstance(section_value, str) and section_value:
            section_keys.add(section_value)

    entity_by_title: dict[int, list[dict[str, Any]]] = {}
    entity_by_section: dict[str, list[dict[str, Any]]] = {}
    time_by_title: dict[int, list[dict[str, Any]]] = {}
    time_by_section: dict[str, list[dict[str, Any]]] = {}
    excerpt_by_title: dict[int, str] = {}
    excerpt_by_section: dict[str, str] = {}

    if title_ids or section_keys:
        section_conditions = []
        if title_ids:
            section_conditions.append(SectionClean.title_id.in_(title_ids))
        if section_keys:
            section_conditions.append(SectionClean.section_key.in_(section_keys))
        section_result = await session.execute(
            select(SectionClean)
            .where(or_(*section_conditions))
            .order_by(SectionClean.title_id, SectionClean.heading_id)
        )
        sections_by_title: dict[int, list[SectionClean]] = {}
        for section in section_result.scalars().all():
            excerpt = _clean_excerpt(section.clean_text or "")
            if excerpt:
                excerpt_by_section[str(section.section_key)] = excerpt
            sections_by_title.setdefault(int(section.title_id), []).append(section)
        for title_id, title_sections in sections_by_title.items():
            intro = _best_intro_section(title_sections)
            if intro is not None:
                excerpt = _clean_excerpt(intro.clean_text or "")
                if excerpt:
                    excerpt_by_title[title_id] = excerpt

        entity_conditions = []
        if title_ids:
            entity_conditions.append(MentionCache.title_id.in_(title_ids))
        if section_keys:
            entity_conditions.append(MentionCache.section_key.in_(section_keys))
        entity_result = await session.execute(
            select(MentionCache, EntityRegistry)
            .join(EntityRegistry, EntityRegistry.entity_id == MentionCache.entity_id)
            .where(or_(*entity_conditions))
            .order_by(
                MentionCache.title_id,
                MentionCache.section_key,
                MentionCache.salience.desc(),
                MentionCache.confidence.desc(),
            )
        )
        for mention, entity in entity_result.all():
            payload = {
                "surface": mention.surface,
                "entity_id": mention.entity_id,
                "type": entity.primary_type,
                "domain": entity.primary_domain,
                "source": mention.source,
                "confidence": round(float(mention.confidence or 0), 3),
                "salience": round(float(mention.salience or 0), 3),
            }
            _append_unique_preview(entity_by_title.setdefault(int(mention.title_id), []), payload, key="entity_id", max_items=18)
            _append_unique_preview(entity_by_section.setdefault(str(mention.section_key), []), payload, key="entity_id", max_items=18)

        time_conditions = []
        if title_ids:
            time_conditions.append(SectionTime.title_id.in_(title_ids))
        if section_keys:
            time_conditions.append(SectionTime.section_key.in_(section_keys))
        time_result = await session.execute(
            select(SectionTime, TimeDimension)
            .join(TimeDimension, TimeDimension.time_ref_id == SectionTime.time_ref_id)
            .where(or_(*time_conditions))
            .order_by(SectionTime.title_id, SectionTime.section_key, TimeDimension.year, TimeDimension.start_date)
        )
        for section_time, time_dimension in time_result.all():
            payload = {
                "label": time_dimension.label,
                "time_ref_id": time_dimension.time_ref_id,
                "kind": time_dimension.time_kind,
                "precision": time_dimension.precision,
                "year": time_dimension.year,
                "source": section_time.source,
                "confidence": round(float(section_time.confidence or 0), 3),
            }
            _append_unique_preview(time_by_title.setdefault(int(section_time.title_id), []), payload, key="time_ref_id", max_items=18)
            _append_unique_preview(time_by_section.setdefault(str(section_time.section_key), []), payload, key="time_ref_id", max_items=18)

    for node in nodes:
        attributes = node.get("attributes") or {}
        kind = str(attributes.get("kind") or "")
        title_id = attributes.get("title_id")
        section_value = attributes.get("section_key")
        if kind == "section" and isinstance(section_value, str):
            entities = entity_by_section.get(section_value, [])
            timeline = time_by_section.get(section_value, [])
            intro_excerpt = excerpt_by_section.get(section_value, "")
        elif isinstance(title_id, int):
            entities = entity_by_title.get(title_id, [])
            timeline = time_by_title.get(title_id, [])
            intro_excerpt = excerpt_by_title.get(title_id, "")
        else:
            entities = []
            timeline = []
            intro_excerpt = ""
        attributes["intro_excerpt"] = intro_excerpt
        attributes["entity_preview"] = {
            "count": len(entities),
            "items": entities[:10],
            "truncated": max(0, len(entities) - 10),
        }
        attributes["timeline_preview"] = {
            "count": len(timeline),
            "items": timeline[:10],
            "truncated": max(0, len(timeline) - 10),
        }


def _best_intro_section(sections: list[SectionClean]) -> SectionClean | None:
    useful = [section for section in sections if is_content_section(section)]
    if not useful:
        return None
    intro_sections = [
        section
        for section in useful
        if (section.level or 0) == 0 or (section.heading or "").strip().lower() in {"lead", "introduction", "overview"}
    ]
    return sorted(intro_sections or useful, key=lambda section: (int(section.level or 99), int(section.heading_id)))[0]


def _clean_excerpt(text: str, *, max_chars: int = 520) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    clipped = compact[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{clipped}..."


def _append_unique_preview(items: list[dict[str, Any]], payload: dict[str, Any], *, key: str, max_items: int) -> None:
    if len(items) >= max_items:
        return
    value = payload.get(key)
    if value is not None and any(existing.get(key) == value for existing in items):
        return
    items.append(payload)


async def build_graphology_article_graph(
    session: AsyncSession,
    *,
    title: str,
    title_id: int,
    sections: list[SectionClean],
    levels: int = 2,
    min_relevance: float = 0.0,
    max_nodes: int = 1000,
    include_back_edges: bool = True,
    include_cross_edges: bool = True,
) -> dict[str, Any]:
    """Return a graphology-importable article neighborhood graph."""

    projection_cap = 0 if max_nodes <= 0 else max(max_nodes * 3, max_nodes + 500)
    projection = await build_article_graph_projection_from_neo4j(
        session,
        title=title,
        title_id=title_id,
        sections=sections,
        max_nodes=projection_cap,
        min_relevance=min_relevance,
    )
    graph_source = "neo4j_frontier"
    if len(projection.nodes) <= 1:
        projection = await build_article_graph_projection(
            session,
            title=title,
            title_id=title_id,
            sections=sections,
            related_limit=max(0, max_nodes - len(sections) - 1),
            sync_neo4j=False,
        )
        graph_source = "postgres_related_cache_fallback"
    scored_nodes = _nodes_with_related_scores(projection.nodes, projection.edges)
    nodes_by_old_id = {node["id"]: node for node in scored_nodes}
    node_key_by_old_id: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []

    for node in scored_nodes:
        converted = _convert_node(node, focus_title_id=title_id)
        if converted is None:
            continue
        node_key_by_old_id[node["id"]] = converted["key"]
        nodes.append(converted)

    edges: list[dict[str, Any]] = []
    for edge in projection.edges:
        converted = _convert_edge(
            edge,
            node_key_by_old_id=node_key_by_old_id,
            nodes_by_old_id=nodes_by_old_id,
            focus_title_id=title_id,
            min_relevance=min_relevance,
        )
        if converted is None:
            continue
        edges.append(converted)

    timeline_context = await _timeline_context_annotations(session, title_id)
    _apply_timeline_context_annotations(nodes, edges, timeline_context)
    _apply_relevance_promotion_floor(nodes, edges)

    if max_nodes > 0 and len(nodes) > max_nodes:
        nodes, edges = _cap_graphology_nodes(nodes, edges, max_nodes=max_nodes)

    await _attach_node_extraction_previews(session, nodes)

    return {
        "attributes": {
            "focus_title_id": title_id,
            "focus_title": title,
            "levels": levels,
            "lens": None,
            "min_relevance": min_relevance,
            "include_back_edges": include_back_edges,
            "include_cross_edges": include_cross_edges,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "graph_source": graph_source,
            "versions": {
                "ontology": "ontology-v0.1",
                "model": "graphology-projection-v1",
                "parser": "v4",
            },
        },
        "nodes": nodes,
        "edges": edges,
    }


async def _timeline_context_annotations(session: AsyncSession, title_id: int) -> dict[int, dict[str, Any]]:
    """Return temporal-context annotations for relevant graph article IDs.

    Graph promotion is a relevance decision. Timeline-context rows only tell us
    that dated context exists for a related article, so they annotate the node
    but do not define its visibility.
    """

    result = await session.execute(
        select(
            TimelineContextCache.source_title_id,
            func.max(TimelineContextCache.relevance_score).label("score"),
            func.count().label("context_rows"),
        )
        .where(TimelineContextCache.from_title_id == title_id)
        .where(TimelineContextCache.source_title_id != title_id)
        .group_by(TimelineContextCache.source_title_id)
    )
    annotations: dict[int, dict[str, Any]] = {}
    for source_title_id, score, context_rows in result.all():
        if source_title_id is None:
            continue
        annotations[int(source_title_id)] = {
            "timeline_context_score": max(0.0, min(1.0, float(score or 0.0))),
            "context_rows": int(context_rows or 0),
        }
    return annotations


def _apply_timeline_context_annotations(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    annotations: dict[int, dict[str, Any]],
) -> None:
    """Overlay timeline-context metadata without changing relevance visibility."""

    if not annotations:
        return

    annotated_keys: set[str] = set()
    for node in nodes:
        attributes = node.get("attributes") or {}
        try:
            title_id = int(attributes.get("title_id") or 0)
        except (TypeError, ValueError):
            continue
        annotation = annotations.get(title_id)
        if not annotation:
            continue
        annotated_keys.add(str(node.get("key") or ""))
        attributes["timeline_context_rows"] = int(annotation.get("context_rows") or 0)
        attributes["timeline_context_score"] = round(float(annotation.get("timeline_context_score") or 0.0), 4)
        attributes["timeline_context_available"] = attributes["timeline_context_rows"] > 0

    for edge in edges:
        attributes = edge.get("attributes") or {}
        if attributes.get("relation") == "has_section":
            continue
        if edge.get("source") not in annotated_keys and edge.get("target") not in annotated_keys:
            continue
        source_title_id = _edge_node_title_id(nodes, str(edge.get("source") or ""))
        target_title_id = _edge_node_title_id(nodes, str(edge.get("target") or ""))
        annotation = annotations.get(source_title_id) or annotations.get(target_title_id) or {}
        attributes["timeline_context_rows"] = int(annotation.get("context_rows") or 0)
        attributes["timeline_context_score"] = round(float(annotation.get("timeline_context_score") or 0.0), 4)
        attributes["timeline_context_available"] = attributes["timeline_context_rows"] > 0


def _apply_relevance_promotion_floor(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    strong_threshold: float = 0.62,
    fallback_floor: float = 0.42,
    min_promoted: int = 12,
    max_promoted: int = 72,
    max_l1_routes_per_section: int = 4,
    max_l2_routes_per_section: int = 3,
) -> None:
    """Promote section-scoped weighted relevance routes.

    Promotion is deliberately route-first. A related article becomes visible
    because at least one source section has a weighted related-cache route to it.
    Structural wikilink proximity is preserved separately, but it does not
    promote anything.
    """

    for node in nodes:
        attributes = node.get("attributes") or {}
        if attributes.get("kind") != "article" or int(attributes.get("level") or 0) == 0:
            continue
        attributes["visibility"] = "hidden"
        attributes["display_state"] = "candidate_hidden"
        attributes.pop("promotion_source", None)
        attributes.pop("promotion_reason", None)

    for edge in edges:
        attributes = edge.get("attributes") or {}
        if attributes.get("relation") == "has_section":
            continue
        attributes["visibility"] = "hidden"
        attributes["display_state"] = "candidate_hidden"
        attributes.pop("promotion_source", None)
        attributes.pop("promotion_reason", None)

    scored_routes = [
        edge
        for edge in edges
        if str((edge.get("attributes") or {}).get("relation") or "") == "related_to"
        and str((edge.get("attributes") or {}).get("score_source") or "") == "related_cache"
    ]
    if not scored_routes:
        return

    node_by_key = {str(node.get("key") or ""): node for node in nodes}
    by_section: dict[str, list[dict[str, Any]]] = {}
    for route in scored_routes:
        by_section.setdefault(str(route.get("source") or ""), []).append(route)

    promoted_routes: list[dict[str, Any]] = []
    promoted_route_keys: set[str] = set()
    for section_routes in by_section.values():
        for level, level_limit in ((1, max_l1_routes_per_section), (2, max_l2_routes_per_section)):
            ranked_routes = sorted(
                [
                    edge
                    for edge in section_routes
                    if int((edge.get("attributes") or {}).get("level") or 1) == level
                    and float((edge.get("attributes") or {}).get("score") or 0.0) >= fallback_floor
                ],
                key=lambda edge: (
                    -_route_promotion_score(edge),
                    str(edge.get("target") or ""),
                ),
            )
            for edge in ranked_routes[:level_limit]:
                key = str(edge.get("key") or "")
                if key in promoted_route_keys:
                    continue
                promoted_routes.append(edge)
                promoted_route_keys.add(key)

    if len(promoted_routes) < min_promoted:
        ranked_all = sorted(
            [
                edge
                for edge in scored_routes
                if float((edge.get("attributes") or {}).get("score") or 0.0) >= fallback_floor
            ],
            key=lambda edge: (
                -_route_promotion_score(edge),
                int((edge.get("attributes") or {}).get("level") or 1),
                str(edge.get("target") or ""),
            ),
        )
        for edge in ranked_all:
            key = str(edge.get("key") or "")
            if key in promoted_route_keys:
                continue
            promoted_routes.append(edge)
            promoted_route_keys.add(key)
            if len(promoted_routes) >= min_promoted:
                break

    promoted_routes = sorted(
        promoted_routes,
        key=lambda edge: (
            -_route_promotion_score(edge),
            int((edge.get("attributes") or {}).get("level") or 1),
            str(edge.get("source") or ""),
            str(edge.get("target") or ""),
        ),
    )[:max_promoted]
    promoted_route_keys = {str(edge.get("key") or "") for edge in promoted_routes}
    promoted_node_keys = {str(edge.get("target") or "") for edge in promoted_routes}

    for edge in promoted_routes:
        attributes = edge.get("attributes") or {}
        score = float(attributes.get("score") or 0.0)
        attributes["visibility"] = "promoted"
        attributes["display_state"] = "candidate_promoted"
        attributes["strength"] = _strength(score)
        attributes["promotion_source"] = "section_weighted_route"
        attributes["promotion_reason"] = (
            "strong_threshold" if score >= strong_threshold else "top_relevance_floor"
        )
        if int(attributes.get("level") or 1) == 2:
            attributes["promotion_reason"] = f"l2_bridge_{attributes['promotion_reason']}"

    for node_key in promoted_node_keys:
        node = node_by_key.get(node_key)
        if not node:
            continue
        attributes = node.get("attributes") or {}
        scored_incident = [
            float((edge.get("attributes") or {}).get("score") or 0.0)
            for edge in promoted_routes
            if str(edge.get("target") or "") == node_key
        ]
        best_score = max(scored_incident) if scored_incident else float(attributes.get("score") or 0.0)
        attributes["visibility"] = "promoted"
        attributes["display_state"] = "candidate_promoted"
        attributes["strength"] = _strength(best_score)
        attributes["best_route_score"] = best_score
        attributes["promoted_route_count"] = len(scored_incident)
        attributes["promoted_l2_route_count"] = len(
            [
                edge
                for edge in promoted_routes
                if str(edge.get("target") or "") == node_key
                and int((edge.get("attributes") or {}).get("level") or 1) == 2
            ]
        )
        attributes["promotion_source"] = "section_weighted_route"
        attributes["promotion_reason"] = "has_promoted_section_route"

    for edge in edges:
        attributes = edge.get("attributes") or {}
        if attributes.get("relation") == "has_section":
            continue
        if str(edge.get("key") or "") in promoted_route_keys:
            continue
        if edge.get("source") not in promoted_node_keys and edge.get("target") not in promoted_node_keys:
            continue
        if str(attributes.get("score_source") or "") != "related_cache":
            continue
        score = float(attributes.get("score") or attributes.get("weight") or 0.0)
        attributes["visibility"] = "context"
        attributes["display_state"] = "candidate_context"
        attributes["strength"] = _strength(score)


def _route_promotion_score(edge: dict[str, Any]) -> float:
    """Return route score used for graph promotion ordering."""

    attributes = edge.get("attributes") or {}
    score = float(attributes.get("score") or 0.0)
    if int(attributes.get("level") or 1) != 2:
        return score
    components = attributes.get("components") or {}
    backlink = _safe_float(components.get("backlink"))
    via_bonus = 0.025 if attributes.get("via_title") else 0.0
    return min(1.0, score + (backlink * 0.04) + via_bonus)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _edge_node_title_id(nodes: list[dict[str, Any]], key: str) -> int:
    for node in nodes:
        if node.get("key") != key:
            continue
        try:
            return int((node.get("attributes") or {}).get("title_id") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _cap_graphology_nodes(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    max_nodes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cap graph output without starving L2 nodes.

    The input order is core article, sections, L1, then L2. A naive prefix cap
    therefore drops L2 first. For graph inspection that is exactly the wrong
    failure mode, so keep all core nodes and split remaining capacity between
    L1 and L2, reserving more room for L2 when it exists.
    """

    if max_nodes <= 0 or len(nodes) <= max_nodes:
        return nodes, edges

    core_nodes = [
        node
        for node in nodes
        if str((node.get("attributes") or {}).get("kind") or "") == "section"
        or int((node.get("attributes") or {}).get("level") or 0) == 0
    ]
    related_nodes = [node for node in nodes if node not in core_nodes]
    related_nodes.sort(
        key=lambda node: (
            0 if str((node.get("attributes") or {}).get("visibility") or "") == "promoted" else 1,
            -float((node.get("attributes") or {}).get("score") or 0),
        )
    )
    l1_nodes = [node for node in related_nodes if int((node.get("attributes") or {}).get("level") or 1) == 1]
    l2_nodes = [node for node in related_nodes if int((node.get("attributes") or {}).get("level") or 1) == 2]
    other_nodes = [node for node in related_nodes if node not in l1_nodes and node not in l2_nodes]

    if len(core_nodes) >= max_nodes:
        selected = core_nodes[:max_nodes]
    else:
        remaining = max_nodes - len(core_nodes)
        if l2_nodes:
            l2_target = min(len(l2_nodes), max(1, int(remaining * 0.6)))
            l1_target = min(len(l1_nodes), max(0, remaining - l2_target))
            unused = remaining - l1_target - l2_target
            if unused > 0:
                extra_l2 = min(len(l2_nodes) - l2_target, unused)
                l2_target += extra_l2
                unused -= extra_l2
            if unused > 0:
                extra_l1 = min(len(l1_nodes) - l1_target, unused)
                l1_target += extra_l1
                unused -= extra_l1
            selected = [*core_nodes, *l1_nodes[:l1_target], *l2_nodes[:l2_target], *other_nodes[:unused]]
        else:
            selected = [*core_nodes, *related_nodes[:remaining]]

    allowed = {node["key"] for node in selected}
    capped_edges = [edge for edge in edges if edge["source"] in allowed and edge["target"] in allowed]
    return selected, capped_edges


def _nodes_with_related_scores(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    best_scores: dict[str, float] = {}
    for edge in edges:
        if str(edge.get("kind") or "") == "HAS_SECTION":
            continue
        if str(edge.get("score_source") or (edge.get("signals") or {}).get("score_source") or "") != "related_cache":
            continue
        target = str(edge.get("target") or "")
        try:
            score = float(edge.get("score") or (edge.get("signals") or {}).get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        best_scores[target] = max(best_scores.get(target, 0.0), score)

    output: list[dict[str, Any]] = []
    for node in nodes:
        score = best_scores.get(str(node.get("id") or ""))
        if score is None:
            output.append(node)
            continue
        copied = dict(node)
        metrics = dict(copied.get("metrics") or {})
        metrics["best_route_score"] = max(float(metrics.get("best_route_score") or 0), score)
        if str(metrics.get("score_source") or "") == "related_cache":
            metrics["score"] = max(float(metrics.get("score") or 0), score)
            metrics["relevance_to_focus"] = metrics["score"]
        copied["metrics"] = metrics
        output.append(copied)
    return output


def _convert_node(node: dict[str, Any], *, focus_title_id: int) -> dict[str, Any] | None:
    kind = str(node.get("kind") or "")
    if kind in {"article", "related_article"}:
        title_id = int(node["title_id"])
        level = 0 if title_id == focus_title_id else int(node.get("level") or 1)
        score = 1.0 if title_id == focus_title_id else _metric_score(node)
        coverage = _normalize_coverage(node.get("coverage") or {})
        processing_state = _overall_coverage_state(
            coverage,
            required_layers=("seed", "core", "entities", "temporal") if level > 0 else ("seed", "core", "entities", "temporal", "related"),
        )
        link_extraction = _normalize_link_extraction(node.get("link_extraction") or {})
        visibility = _visibility_for_node(level=level, score=score)
        return {
            "key": article_key(title_id),
            "attributes": {
                "kind": "article",
                "level": min(2, max(0, level)),
                "label": node.get("label") or node.get("title") or str(title_id),
                "title_id": title_id,
                "node_type": "CONCEPT",
                "primary_domain": _domain_from_node(node),
                "specificity": 0.5,
                "centrality": 1.0 if title_id == focus_title_id else 0.35,
                "relevance_to_focus": score,
                "score": score,
                "score_source": (node.get("metrics") or {}).get("score_source") or "structural_frontier",
                "related_cache_score": (node.get("metrics") or {}).get("related_cache_score"),
                "best_route_score": (node.get("metrics") or {}).get("best_route_score"),
                "structural_score": (node.get("metrics") or {}).get("structural_score"),
                "strength": _strength(score),
                "visibility": visibility,
                "display_state": "visible_core" if visibility == "core" else f"candidate_{visibility}",
                "processing_state": processing_state,
                "link_extraction": link_extraction,
                "link_extraction_state": link_extraction["state"],
                "coverage": coverage,
                "embedding_state": _embedding_state(node.get("coverage") or {}),
                "provenance": {"title_id": title_id},
                "versions": {"ontology": "ontology-v0.1", "model": "graphology-projection-v1", "parser": "v4"},
            },
        }
    if kind == "section":
        title_id = int(node["title_id"])
        heading_id = int(node["heading_id"])
        coverage = _normalize_coverage(node.get("coverage") or {})
        processing_state = _overall_coverage_state(coverage)
        link_extraction = _normalize_link_extraction(node.get("link_extraction") or {})
        return {
            "key": section_key(title_id, heading_id),
            "attributes": {
                "kind": "section",
                "level": 0,
                "label": node.get("label") or "Section",
                "title_id": title_id,
                "heading_id": heading_id,
                "section_key": node.get("section_key"),
                "node_type": "SECTION",
                "primary_domain": _domain_from_node(node),
                "specificity": min(1.0, float((node.get("metrics") or {}).get("link_count") or 0) / 30),
                "centrality": 0.55,
                "relevance_to_focus": 1.0,
                "score": 1.0,
                "strength": "core",
                "visibility": "core",
                "display_state": "visible_core",
                "processing_state": processing_state,
                "link_extraction": link_extraction,
                "link_extraction_state": link_extraction["state"],
                "coverage": coverage,
                "embedding_state": _embedding_state(node.get("coverage") or {}),
                "provenance": {"title_id": title_id, "heading_id": heading_id},
                "versions": {"ontology": "ontology-v0.1", "model": "graphology-projection-v1", "parser": "v4"},
                "order": (node.get("metrics") or {}).get("order"),
            },
        }
    return None


def _convert_edge(
    edge: dict[str, Any],
    *,
    node_key_by_old_id: dict[str, str],
    nodes_by_old_id: dict[str, dict[str, Any]],
    focus_title_id: int,
    min_relevance: float,
) -> dict[str, Any] | None:
    source = node_key_by_old_id.get(str(edge.get("source")))
    target = node_key_by_old_id.get(str(edge.get("target")))
    if not source or not target or source == target:
        return None
    kind = str(edge.get("kind") or "")
    if kind == "HAS_SECTION":
        relation = "has_section"
        weight = 1.0
        directed = True
    elif kind == "L1_TO_L2":
        relation = "l1_to_l2"
        weight = float(edge.get("score") or (edge.get("signals") or {}).get("score") or 0)
        directed = True
    elif kind == "RELATED_TO":
        relation = "related_to"
        weight = float(edge.get("score") or (edge.get("signals") or {}).get("score") or 0)
        directed = True
    elif kind == "LINKS_TO":
        relation = "wikilink"
        weight = float(edge.get("score") or (edge.get("signals") or {}).get("score") or 0)
        directed = True
    else:
        relation = "wikilink"
        weight = float(edge.get("score") or (edge.get("signals") or {}).get("score") or 0)
        directed = True
    if weight < min_relevance:
        return None
    signals = edge.get("signals") or {}
    source_node = nodes_by_old_id.get(str(edge.get("source"))) or {}
    target_node = nodes_by_old_id.get(str(edge.get("target"))) or {}
    target_title_id = int(target_node.get("title_id") or 0)
    source_title_id = int(source_node.get("title_id") or 0)
    is_back_to_l0 = relation != "has_section" and (
        source_title_id == focus_title_id or target_title_id == focus_title_id
    )
    visibility = "core" if relation == "has_section" else _visibility_for_edge(weight)
    return {
        "key": str(edge.get("id") or f"{relation}:{source}->{target}"),
        "source": source,
        "target": target,
        "attributes": {
            "relation": relation,
            "weight": max(0.0, min(1.0, weight)),
            "score": max(0.0, min(1.0, weight)),
            "strength": _strength(weight),
            "visibility": visibility,
            "display_state": "visible_core" if visibility == "core" else f"candidate_{visibility}",
            "components": {
                "embedding_cosine": signals.get("embedding_similarity")
                or signals.get("intro_embedding_similarity")
                or signals.get("broad_embedding_similarity"),
                "graph_distance": signals.get("graph_signal") or ((signals.get("components") or {}).get("content") or {}).get("S_graph"),
                "backlink": signals.get("backlink_signal") or ((signals.get("components") or {}).get("content") or {}).get("S_backlink"),
                "temporal_proximity": signals.get("temporal") or ((signals.get("components") or {}).get("temporal") or {}),
                "entity_overlap": signals.get("entity_signal") or ((signals.get("components") or {}).get("content") or {}).get("S_entity"),
            },
            "score_source": edge.get("score_source") or signals.get("score_source") or "structural_frontier",
            "related_cache_score": signals.get("related_cache_score"),
            "structural_score": edge.get("structural_score") or signals.get("structural_score"),
            "confidence": 0.95 if relation == "has_section" else max(0.35, min(0.95, weight)),
            "directed": directed,
            "back_to_l0": is_back_to_l0,
            "shared": [],
            "via_title": signals.get("via_title"),
            "provenance": {
                "source": edge.get("source"),
                "target": edge.get("target"),
                "why_source": edge.get("why_source"),
            },
            "versions": {"ontology": "ontology-v0.1", "model": "graphology-projection-v1", "parser": "v4"},
            "level": edge.get("level"),
        },
    }


def _normalize_coverage(coverage: dict[str, Any]) -> dict[str, dict[str, Any]]:
    layers = {
        "seed": "missing",
        "core": "missing",
        "entities": "missing",
        "temporal": "missing",
        "related": "missing",
        "review": "missing",
    }
    area_to_layer = {
        "article_load": "seed",
        "embeddings": "core",
        "core_digest": "core",
        "entity_mentions": "entities",
        "entity_scores": "entities",
        "temporal": "temporal",
        "timeline_context": "temporal",
        "related_agent": "related",
        "graph_frontier": "related",
    }
    output = {layer: {"state": state, "version": None} for layer, state in layers.items()}
    for area, raw in coverage.items():
        layer = area_to_layer.get(str(area))
        if not layer:
            continue
        source_state = str((raw or {}).get("state") or "missing") if isinstance(raw, dict) else "missing"
        output[layer] = {
            "state": _coverage_state(source_state),
            "version": str((raw or {}).get("source") or area) if isinstance(raw, dict) else str(area),
        }
    return output


def _normalize_link_extraction(value: dict[str, Any]) -> dict[str, Any]:
    raw_state = str(value.get("state") or "missing")
    state = _coverage_state(raw_state)
    return {
        "state": state,
        "scope": value.get("scope"),
        "expected": int(value.get("expected") or 0),
        "completed": int(value.get("completed") or 0),
        "error": value.get("error"),
        "version": value.get("version"),
        "updated_at": value.get("updated_at"),
        "started_at": value.get("started_at"),
        "completed_at": value.get("completed_at"),
    }


def _coverage_state(state: str) -> str:
    if state in {"completed", "done"}:
        return "done"
    if state in {"running"}:
        return "running"
    if state in {"attention", "failed"}:
        return "failed"
    if state in {"stale"}:
        return "stale"
    if state in {"pending", "retry", "queued", "partial", "incomplete"}:
        return "incomplete"
    return "missing"


def _overall_coverage_state(
    coverage: dict[str, dict[str, Any]],
    *,
    required_layers: tuple[str, ...] = ("seed", "core", "entities", "temporal", "related"),
) -> str:
    # Review is a later human/agent adjudication lane. Treating it as required
    # made otherwise completed graph nodes look permanently "processing".
    states = [str((coverage.get(layer) or {}).get("state") or "missing") for layer in required_layers]
    present_states = [state for state in states if state != "missing"]
    if not present_states:
        return "missing"
    if "failed" in present_states:
        return "failed"
    if "running" in present_states:
        return "running"
    if "stale" in present_states:
        return "stale"
    if "incomplete" in present_states:
        return "incomplete"
    if any(state == "missing" for state in states):
        return "incomplete"
    return "done"


def _visibility_for_node(*, level: int, score: float) -> str:
    if level == 0:
        return "core"
    if score >= 0.72:
        return "promoted"
    return "hidden"


def _visibility_for_edge(score: float) -> str:
    if score >= 0.72:
        return "promoted"
    return "hidden"


def _strength(score: float) -> str:
    if score >= 0.72:
        return "high"
    if score >= 0.52:
        return "medium"
    return "low"


def _embedding_state(coverage: dict[str, Any]) -> str:
    embeddings = coverage.get("embeddings")
    if not isinstance(embeddings, dict):
        return "missing"
    state = _coverage_state(str(embeddings.get("state") or "missing"))
    if state == "done":
        return "present"
    if state == "stale":
        return "stale"
    return "missing"


def _domain_from_node(node: dict[str, Any]) -> str:
    topic = (node.get("metrics") or {}).get("primary_domain")
    if isinstance(topic, str) and topic:
        return topic
    return "Society & People"


def _metric_score(node: dict[str, Any]) -> float:
    metrics = node.get("metrics") or {}
    score = metrics.get("score") or metrics.get("relevance_to_focus")
    try:
        return max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        return 0.45
