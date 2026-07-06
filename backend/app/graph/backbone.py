"""Neo4j graph backbone projection for article neighborhoods."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MentionCache, ProcessingState, RelatedCache, SectionClean, SectionTime
from app.graph.driver import execute_query, execute_write
from app.graph.schema import ARTICLE_KIND, GRAPH_SCHEMA_VERSION


@dataclass(frozen=True)
class GraphProjection:
    """Backend graph projection returned to the API layer."""

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


def article_node_id(title_id: int) -> str:
    """Stable graph node ID backed by the Postgres wiki title ID."""

    return f"article:{int(title_id)}"


def section_node_id(section_key: str) -> str:
    """Stable graph node ID backed by the Postgres section key."""

    return f"section:{section_key}"


async def build_article_graph_projection(
    session: AsyncSession,
    *,
    title: str,
    title_id: int,
    sections: list[SectionClean],
    related_limit: int = 200,
    sync_neo4j: bool = True,
) -> GraphProjection:
    """Build and optionally sync the current article graph neighborhood."""

    section_keys = [section.section_key for section in sections]
    coverage = await _coverage_by_scope(session, title_id=title_id, section_keys=section_keys)
    related_rows = await _related_rows(session, section_keys, related_limit)
    related_title_ids = sorted({int(row.to_title_id) for row in related_rows})
    related_coverage = await _article_coverage_by_title_ids(session, related_title_ids)

    nodes: list[dict[str, Any]] = [
        {
            "id": article_node_id(title_id),
            "kind": "article",
            "postgres_id": title_id,
            "title_id": title_id,
            "title": title,
            "label": title,
            "coverage": coverage.get("", {}),
            "metrics": {"section_count": len(sections)},
        }
    ]
    edges: list[dict[str, Any]] = []

    for index, section in enumerate(sections):
        nodes.append(
            {
                "id": section_node_id(section.section_key),
                "kind": "section",
                "postgres_id": section.section_key,
                "section_key": section.section_key,
                "title_id": section.title_id,
                "heading_id": section.heading_id,
                "title": section.title,
                "label": section.heading or "Lead",
                "level": section.level,
                "coverage": coverage.get(section.section_key, {}),
                "metrics": {
                    "link_count": len(section.links_json or []),
                    "order": index,
                },
            }
        )
        edges.append(
            {
                "id": f"has-section:{title_id}:{section.section_key}",
                "kind": "HAS_SECTION",
                "source": article_node_id(title_id),
                "target": section_node_id(section.section_key),
                "signals": {"order": index},
            }
        )

    related_node_ids: set[str] = set()
    l1_by_section_and_title = {
        (row.from_section_key, _normalize_title(row.to_title)): int(row.to_title_id)
        for row in related_rows
        if int(row.level) == 1
    }
    l1_to_l2_edges_seen: set[tuple[int, int]] = set()
    for row in related_rows:
        related_id = article_node_id(row.to_title_id)
        if related_id not in related_node_ids:
            related_node_ids.add(related_id)
            nodes.append(
                {
                    "id": related_id,
                    "kind": "related_article",
                    "postgres_id": row.to_title_id,
                    "title_id": row.to_title_id,
                    "title": row.to_title,
                    "label": row.to_title,
                    "level": row.level,
                    "coverage": related_coverage.get(int(row.to_title_id), {}),
                    "metrics": {},
                }
            )
        signals = row.signals_json or {}
        components = signals.get("components") or {}
        via_title = signals.get("via_title")
        edges.append(
            {
                "id": f"related:{row.from_section_key}:{row.to_title_id}:{row.level}",
                "kind": "RELATED_TO",
                "source": section_node_id(row.from_section_key),
                "target": related_id,
                "level": row.level,
                "score": row.score,
                "why_source": row.why_source,
                "signals": {
                    "score": row.score,
                    "level": row.level,
                    "via_title": via_title,
                    "why_source": row.why_source,
                    "embedding_similarity": signals.get("embedding_similarity"),
                    "intro_embedding_similarity": signals.get("intro_embedding_similarity"),
                    "broad_embedding_similarity": signals.get("broad_embedding_similarity"),
                    "entity_signal": (components.get("content") or {}).get("S_entity"),
                    "domain_signal": (components.get("content") or {}).get("S_domain"),
                    "graph_signal": signals.get("graph_signal"),
                    "temporal": components.get("temporal") or {},
                    "gates": signals.get("gates") or {},
                },
            }
        )
        if int(row.level) == 2 and isinstance(via_title, str) and via_title.strip():
            l1_title_id = l1_by_section_and_title.get((row.from_section_key, _normalize_title(via_title)))
            if l1_title_id is None:
                continue
            edge_pair = (l1_title_id, int(row.to_title_id))
            if edge_pair in l1_to_l2_edges_seen:
                continue
            l1_to_l2_edges_seen.add(edge_pair)
            edges.append(
                {
                    "id": f"l1-to-l2:{row.from_section_key}:{l1_title_id}:{row.to_title_id}",
                    "kind": "LINKS_TO",
                    "source": article_node_id(l1_title_id),
                    "target": related_id,
                    "level": row.level,
                    "score": row.score,
                    "why_source": row.why_source,
                    "signals": {
                        "score": row.score,
                        "level": row.level,
                        "via_title": via_title,
                        "from_section_key": row.from_section_key,
                        "why_source": row.why_source,
                    },
                }
            )

    if sync_neo4j:
        await sync_projection_to_neo4j(title=title, title_id=title_id, nodes=nodes, edges=edges)

    return GraphProjection(nodes=nodes, edges=edges)


async def build_article_graph_projection_from_neo4j(
    session: AsyncSession | None = None,
    *,
    title: str,
    title_id: int,
    sections: list[SectionClean],
    max_nodes: int = 500,
    min_relevance: float = 0.0,
) -> GraphProjection:
    """Build the article graph projection from the Neo4j frontier backbone.

    This is the preferred graph source. Postgres `related_cache` remains useful
    for scored/enriched related rows, but it should not be the authority for the
    L0/L1/L2 relationship skeleton.
    """

    try:
        rows = await execute_query(
            """
            MATCH (focus:V4Article {title_id: $title_id})
            OPTIONAL MATCH (focus)-[:HAS_SECTION]->(s:V4Section)
            WITH focus, collect(DISTINCT properties(s)) AS section_rows
            OPTIONAL MATCH (focus)-[:HAS_SECTION]->(source_section:V4Section)-[r1:LINKS_TO]->(l1:V4Article)
            WHERE l1.title_id <> $title_id
            WITH focus, section_rows,
                 collect(DISTINCT {
                    section_key: source_section.section_key,
                    section_title_id: source_section.title_id,
                    section_heading_id: source_section.heading_id,
                    article: properties(l1),
                    rel: properties(r1)
                 }) AS l1_rows
            OPTIONAL MATCH (focus)-[:HAS_SECTION]->(:V4Section)-[:LINKS_TO]->(parent_l1:V4Article)-[r2:LINKS_TO]->(l2:V4Article)
            WHERE l2.title_id <> $title_id
              AND l2.title_id <> parent_l1.title_id
            RETURN properties(focus) AS focus,
                   section_rows,
                   l1_rows,
                   collect(DISTINCT {
                    parent: properties(parent_l1),
                    article: properties(l2),
                    rel: properties(r2)
                   }) AS l2_rows
            """,
            {"title_id": int(title_id)},
        )
    except Exception:
        return GraphProjection(nodes=[], edges=[])

    if not rows:
        return GraphProjection(nodes=[], edges=[])
    row = rows[0]
    focus = _clean_neo4j_props(row.get("focus") or {})
    if not focus:
        return GraphProjection(nodes=[], edges=[])

    section_order = {section.section_key: index for index, section in enumerate(sections)}
    section_fallback = {section.section_key: section for section in sections}
    nodes: list[dict[str, Any]] = [
        {
            "id": article_node_id(title_id),
            "kind": "article",
            "postgres_id": title_id,
            "title_id": title_id,
            "title": str(focus.get("title") or title),
            "label": str(focus.get("label") or focus.get("title") or title),
            "coverage": _json_prop(focus.get("coverage_json")),
            "metrics": _json_prop(focus.get("metrics_json")) | {"section_count": len(sections)},
            "link_extraction": _link_extraction_payload(focus),
        }
    ]
    edges: list[dict[str, Any]] = []

    raw_sections = [
        _clean_neo4j_props(item)
        for item in row.get("section_rows") or []
        if isinstance(item, dict) and item.get("section_key")
    ]
    sections_by_key: dict[str, dict[str, Any]] = {}
    for raw in raw_sections:
        key = str(raw.get("section_key") or "")
        if not key or key in sections_by_key:
            continue
        fallback = section_fallback.get(key)
        sections_by_key[key] = raw | {
            "heading": raw.get("heading") or (fallback.heading if fallback else "Lead"),
            "title": raw.get("title") or (fallback.title if fallback else title),
            "heading_id": int(raw.get("heading_id") or (fallback.heading_id if fallback else 0)),
            "title_id": int(raw.get("title_id") or title_id),
            "level": raw.get("level") if raw.get("level") is not None else (fallback.level if fallback else None),
        }
    if not sections_by_key:
        for section in sections:
            sections_by_key[section.section_key] = {
                "section_key": section.section_key,
                "title_id": int(section.title_id),
                "heading_id": int(section.heading_id),
                "title": section.title,
                "heading": section.heading or "Lead",
                "level": section.level,
                "metrics_json": json.dumps({"link_count": len(section.links_json or [])}),
            }

    for section_key_value, section in sorted(
        sections_by_key.items(),
        key=lambda item: (section_order.get(item[0], 10_000), int(item[1].get("heading_id") or 0)),
    ):
        metrics = _json_prop(section.get("metrics_json"))
        metrics.setdefault("order", section_order.get(section_key_value, len(metrics)))
        nodes.append(
            {
                "id": section_node_id(section_key_value),
                "kind": "section",
                "postgres_id": section_key_value,
                "section_key": section_key_value,
                "title_id": int(section.get("title_id") or title_id),
                "heading_id": int(section.get("heading_id") or 0),
                "title": section.get("title") or title,
                "label": section.get("heading") or "Lead",
                "level": section.get("level"),
                "coverage": _json_prop(section.get("coverage_json")),
                "metrics": metrics,
                "link_extraction": _link_extraction_payload(section),
            }
        )
        edges.append(
            {
                "id": f"has-section:{title_id}:{section_key_value}",
                "kind": "HAS_SECTION",
                "source": article_node_id(title_id),
                "target": section_node_id(section_key_value),
                "signals": {"order": metrics.get("order")},
            }
        )

    added_articles: set[int] = {int(title_id)}
    added_edges: set[str] = {edge["id"] for edge in edges}

    for item in row.get("l1_rows") or []:
        if not isinstance(item, dict):
            continue
        source_section_key = str(item.get("section_key") or "")
        article = _clean_neo4j_props(item.get("article") or {})
        rel = _clean_neo4j_props(item.get("rel") or {})
        candidate_title_id = _safe_int(article.get("title_id"))
        if not source_section_key or candidate_title_id is None or candidate_title_id == int(title_id):
            continue
        score = _frontier_score(rel, default=0.5)
        if score < min_relevance:
            continue
        _append_article_node(
            nodes,
            added_articles,
            article,
            title_id=candidate_title_id,
            level=1,
            score=score,
        )
        edge_id = f"related:{source_section_key}:{candidate_title_id}:1"
        if edge_id in added_edges:
            continue
        added_edges.add(edge_id)
        edges.append(
            {
                "id": edge_id,
                "kind": "LINKS_TO",
                "source": section_node_id(source_section_key),
                "target": article_node_id(candidate_title_id),
                "level": 1,
                "score": score,
                "why_source": rel.get("source") or "graph_frontier",
                "signals": {
                    "score": score,
                    "level": 1,
                    "link_order": rel.get("link_order"),
                    "label": rel.get("label"),
                    "why_source": rel.get("source") or "graph_frontier",
                    "graph_signal": {"hops": 1, "path_count": 1},
                },
            }
        )

    for item in row.get("l2_rows") or []:
        if not isinstance(item, dict):
            continue
        parent = _clean_neo4j_props(item.get("parent") or {})
        article = _clean_neo4j_props(item.get("article") or {})
        rel = _clean_neo4j_props(item.get("rel") or {})
        parent_title_id = _safe_int(parent.get("title_id"))
        candidate_title_id = _safe_int(article.get("title_id"))
        if (
            parent_title_id is None
            or candidate_title_id is None
            or candidate_title_id in {int(title_id), parent_title_id}
        ):
            continue
        score = _frontier_score(rel, default=0.38)
        if score < min_relevance:
            continue
        _append_article_node(
            nodes,
            added_articles,
            article,
            title_id=candidate_title_id,
            level=2,
            score=score,
        )
        edge_id = f"l1-to-l2:{parent_title_id}:{candidate_title_id}"
        if edge_id in added_edges:
            continue
        added_edges.add(edge_id)
        edges.append(
            {
                "id": edge_id,
                "kind": "LINKS_TO",
                "source": article_node_id(parent_title_id),
                "target": article_node_id(candidate_title_id),
                "level": 2,
                "score": score,
                "why_source": rel.get("source") or "graph_frontier",
                "signals": {
                    "score": score,
                    "level": 2,
                    "via_title": parent.get("title"),
                    "link_order": rel.get("link_order"),
                    "label": rel.get("label"),
                    "why_source": rel.get("source") or "graph_frontier",
                    "graph_signal": {"hops": 2, "path_count": 1},
                },
            }
        )

    if session is not None:
        await _overlay_related_cache_scores(
            session,
            nodes=nodes,
            edges=edges,
            section_keys=list(sections_by_key.keys()),
        )
        await _overlay_processing_coverage(
            session,
            nodes=nodes,
            title_id=title_id,
        )

    if max_nodes > 0 and len(nodes) > max_nodes:
        allowed = {node["id"] for node in nodes[:max_nodes]}
        nodes = nodes[:max_nodes]
        edges = [edge for edge in edges if edge["source"] in allowed and edge["target"] in allowed]

    return GraphProjection(nodes=nodes, edges=edges)


async def _overlay_related_cache_scores(
    session: AsyncSession,
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    section_keys: list[str],
) -> None:
    """Overlay authoritative related-cache relevance scores onto graph frontier.

    Neo4j stores the neutral link framework. `related_cache` stores the richer
    relevance score and component signals after embeddings/entities/times run.
    Graph promotion should use the latter whenever available.
    """

    if not section_keys:
        return
    result = await session.execute(
        select(RelatedCache).where(RelatedCache.from_section_key.in_(section_keys))
    )
    rows = list(result.scalars().all())
    if not rows:
        return

    by_section_target_level: dict[tuple[str, int, int], RelatedCache] = {}
    by_target_level: dict[tuple[int, int], RelatedCache] = {}
    for row in rows:
        key = (str(row.from_section_key), int(row.to_title_id), int(row.level))
        existing = by_section_target_level.get(key)
        if existing is None or float(row.score) > float(existing.score):
            by_section_target_level[key] = row
        aggregate_key = (int(row.to_title_id), int(row.level))
        existing = by_target_level.get(aggregate_key)
        if existing is None or float(row.score) > float(existing.score):
            by_target_level[aggregate_key] = row

    node_by_article_id = {
        int(node["title_id"]): node
        for node in nodes
        if node.get("kind") in {"article", "related_article"} and _safe_int(node.get("title_id")) is not None
    }
    section_node_ids = {str(node.get("id") or "") for node in nodes if node.get("kind") == "section"}
    for (target_title_id, level), row in by_target_level.items():
        node = node_by_article_id.get(target_title_id)
        if not node:
            node = {
                "id": article_node_id(target_title_id),
                "kind": "related_article",
                "postgres_id": target_title_id,
                "title_id": target_title_id,
                "title": row.to_title,
                "label": row.to_title,
                "level": level,
                "coverage": {},
                "metrics": {},
                "link_extraction": {"state": "missing"},
            }
            nodes.append(node)
            node_by_article_id[target_title_id] = node
        metrics = dict(node.get("metrics") or {})
        metrics["score"] = float(row.score)
        metrics["relevance_to_focus"] = float(row.score)
        metrics["related_cache_score"] = float(row.score)
        metrics["related_cache_level"] = level
        metrics["score_source"] = "related_cache"
        metrics["why_source"] = row.why_source
        metrics["primary_domain"] = ((row.signals_json or {}).get("components") or {}).get("primary_domain") or metrics.get("primary_domain")
        node["metrics"] = metrics

    existing_edge_ids = {str(edge.get("id") or "") for edge in edges}
    for row in rows:
        source_id = section_node_id(str(row.from_section_key))
        target_id = article_node_id(int(row.to_title_id))
        if source_id not in section_node_ids or int(row.to_title_id) not in node_by_article_id:
            continue
        edge_id = f"related-cache:{row.from_section_key}:{int(row.to_title_id)}:{int(row.level)}"
        if edge_id in existing_edge_ids:
            continue
        existing_edge_ids.add(edge_id)
        related_signals = dict(row.signals_json or {})
        related_signals["score"] = float(row.score)
        related_signals["score_source"] = "related_cache"
        related_signals["related_cache_score"] = float(row.score)
        related_signals["related_cache_id"] = row.id
        related_signals["why_source"] = row.why_source
        edges.append(
            {
                "id": edge_id,
                "kind": "RELATED_TO",
                "source": source_id,
                "target": target_id,
                "level": int(row.level),
                "score": float(row.score),
                "score_source": "related_cache",
                "why_source": row.why_source,
                "signals": related_signals,
            }
        )

    for edge in edges:
        if edge.get("kind") == "HAS_SECTION":
            continue
        target_title_id = _safe_int(str(edge.get("target") or "").replace("article:", ""))
        if target_title_id is None:
            continue
        level = _safe_int(edge.get("level")) or 1
        source_section_key = str(edge.get("source") or "").replace("section:", "")
        row = by_section_target_level.get((source_section_key, target_title_id, level))
        if row is None:
            row = by_target_level.get((target_title_id, level))
        if row is None:
            continue
        structural_score = float(edge.get("score") or 0.0)
        score = float(row.score)
        signals = dict(edge.get("signals") or {})
        related_signals = dict(row.signals_json or {})
        signals.update(related_signals)
        signals["score"] = score
        signals["structural_score"] = structural_score
        signals["score_source"] = "related_cache"
        signals["related_cache_score"] = float(row.score)
        signals["related_cache_id"] = row.id
        signals["why_source"] = row.why_source
        edge["score"] = score
        edge["structural_score"] = structural_score
        edge["score_source"] = "related_cache"
        edge["why_source"] = row.why_source
        edge["signals"] = signals


async def _overlay_processing_coverage(
    session: AsyncSession,
    *,
    nodes: list[dict[str, Any]],
    title_id: int,
) -> None:
    """Overlay durable Postgres processing state onto projected graph nodes."""

    section_keys = [
        str(node.get("section_key"))
        for node in nodes
        if node.get("kind") == "section" and node.get("section_key")
    ]
    related_title_ids = sorted(
        {
            int(node["title_id"])
            for node in nodes
            if node.get("kind") in {"article", "related_article"}
            and int(node.get("title_id") or 0) != int(title_id)
        }
    )
    focus_coverage = await _coverage_by_scope(session, title_id=title_id, section_keys=section_keys)
    related_coverage = await _article_coverage_by_title_ids(session, related_title_ids)

    for node in nodes:
        kind = node.get("kind")
        if kind == "section":
            node["coverage"] = focus_coverage.get(str(node.get("section_key")), node.get("coverage") or {})
        elif kind in {"article", "related_article"}:
            node_title_id = int(node.get("title_id") or 0)
            if node_title_id == int(title_id):
                node["coverage"] = focus_coverage.get("", node.get("coverage") or {})
            else:
                node["coverage"] = related_coverage.get(node_title_id, node.get("coverage") or {})


def _append_article_node(
    nodes: list[dict[str, Any]],
    added_articles: set[int],
    article: dict[str, Any],
    *,
    title_id: int,
    level: int,
    score: float,
) -> None:
    if title_id in added_articles:
        return
    added_articles.add(title_id)
    metrics = _json_prop(article.get("metrics_json"))
    metrics["structural_score"] = score
    metrics["score_source"] = "structural_frontier"
    metrics["score"] = 0.0
    metrics["relevance_to_focus"] = 0.0
    nodes.append(
        {
            "id": article_node_id(title_id),
            "kind": "related_article",
            "postgres_id": title_id,
            "title_id": title_id,
            "title": article.get("title") or str(title_id),
            "label": article.get("label") or article.get("title") or str(title_id),
            "level": level,
            "coverage": _json_prop(article.get("coverage_json")),
            "metrics": metrics,
            "link_extraction": _link_extraction_payload(article),
        }
    )


def _frontier_score(rel: dict[str, Any], *, default: float) -> float:
    for key in ("score", "relevance_score", "weight"):
        value = rel.get(key)
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            continue
    link_order = rel.get("link_order")
    try:
        order = max(0, int(link_order))
    except (TypeError, ValueError):
        return default
    order_bonus = max(0.0, 0.12 - min(order, 24) * 0.004)
    return max(0.0, min(1.0, default + order_bonus))


def _json_prop(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _link_extraction_payload(props: dict[str, Any]) -> dict[str, Any]:
    state = str(props.get("link_extraction_state") or "missing")
    return {
        "state": state,
        "scope": props.get("link_extraction_scope"),
        "expected": _safe_int(props.get("link_extraction_expected")) or 0,
        "completed": _safe_int(props.get("link_extraction_completed")) or 0,
        "error": props.get("link_extraction_error"),
        "version": props.get("link_extraction_version"),
        "updated_at": str(props.get("link_extraction_updated_at") or "") or None,
        "started_at": str(props.get("link_extraction_started_at") or "") or None,
        "completed_at": str(props.get("link_extraction_completed_at") or "") or None,
    }


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_neo4j_props(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


async def sync_projection_to_neo4j(
    *,
    title: str,
    title_id: int,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Write the article neighborhood projection to Neo4j."""

    articles = [
        _neo4j_node_payload(node)
        for node in nodes
        if node["kind"] in {"article", "related_article"}
    ]
    sections = [_neo4j_node_payload(node) for node in nodes if node["kind"] == "section"]
    has_section = [_neo4j_edge_payload(edge) for edge in edges if edge["kind"] == "HAS_SECTION"]
    links_to = [_neo4j_edge_payload(edge) for edge in edges if edge["kind"] == "LINKS_TO"]
    related = [_neo4j_edge_payload(edge) for edge in edges if edge["kind"] == "RELATED_TO"]

    await execute_write(
        """
        UNWIND $articles AS row
        MERGE (a:V4Article {title_id: row.title_id})
        SET a.title = row.title,
            a.label = row.label,
            a.kind = $article_kind,
            a.postgres_id = row.postgres_id,
            a.coverage_json = row.coverage,
            a.metrics_json = row.metrics,
            a.graph_schema_version = $graph_schema_version,
            a.updated_at = datetime()
        """,
        {
            "articles": articles,
            "article_kind": ARTICLE_KIND,
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
        },
    )
    await execute_write(
        """
        UNWIND $sections AS row
        MERGE (s:V4Section {section_key: row.section_key})
        SET s.postgres_id = row.postgres_id,
            s.title_id = row.title_id,
            s.heading_id = row.heading_id,
            s.title = row.title,
            s.heading = row.label,
            s.level = row.level,
            s.coverage_json = row.coverage,
            s.metrics_json = row.metrics,
            s.graph_schema_version = $graph_schema_version,
            s.updated_at = datetime()
        """,
        {"sections": sections, "graph_schema_version": GRAPH_SCHEMA_VERSION},
    )
    await execute_write(
        """
        UNWIND $edges AS row
        MATCH (a:V4Article {title_id: $title_id})
        MATCH (s:V4Section {section_key: replace(row.target, 'section:', '')})
        MERGE (a)-[r:HAS_SECTION]->(s)
        SET r.signals_json = row.signals,
            r.updated_at = datetime()
        """,
        {"title_id": title_id, "title": title, "edges": has_section},
    )
    await execute_write(
        """
        UNWIND $edges AS row
        MATCH (s:V4Section {section_key: replace(row.source, 'section:', '')})
        MATCH (a:V4Article {title_id: toInteger(replace(row.target, 'article:', ''))})
        MERGE (s)-[r:LINKS_TO]->(a)
        SET r.score = row.score,
            r.source = coalesce(row.why_source, 'graph_frontier'),
            r.method = 'wikilink',
            r.why_source = row.why_source,
            r.signals_json = row.signals,
            r.graph_schema_version = $graph_schema_version,
            r.updated_at = datetime()
        """,
        {"edges": links_to, "graph_schema_version": GRAPH_SCHEMA_VERSION},
    )
    await execute_write(
        """
        UNWIND $edges AS row
        MATCH (s:V4Section {section_key: replace(row.source, 'section:', '')})
        MATCH (a:V4Article {title_id: toInteger(replace(row.target, 'article:', ''))})
        MERGE (s)-[r:RELATED_TO]->(a)
        SET r.score = row.score,
            r.method = coalesce(row.method, 'composite'),
            r.why_source = row.why_source,
            r.signals_json = row.signals,
            r.graph_schema_version = $graph_schema_version,
            r.updated_at = datetime()
        """,
        {"edges": related, "graph_schema_version": GRAPH_SCHEMA_VERSION},
    )


async def sync_article_processing_coverage_to_neo4j(
    session: AsyncSession,
    *,
    title_id: int,
    section_keys: list[str],
) -> None:
    """Mirror current Postgres processing state onto Neo4j graph nodes."""

    coverage = await _coverage_by_scope(session, title_id=title_id, section_keys=section_keys)
    section_rows = [
        {
            "section_key": section_key,
            "coverage": json.dumps(coverage.get(section_key) or {}, sort_keys=True),
        }
        for section_key in section_keys
    ]
    article_coverage = json.dumps(coverage.get("") or {}, sort_keys=True)
    await execute_write(
        """
        MATCH (a:V4Article {title_id: $title_id})
        SET a.coverage_json = $coverage,
            a.coverage_updated_at = datetime()
        """,
        {"title_id": int(title_id), "coverage": article_coverage},
    )
    await execute_write(
        """
        UNWIND $sections AS row
        MATCH (s:V4Section {section_key: row.section_key})
        SET s.coverage_json = row.coverage,
            s.coverage_updated_at = datetime()
        """,
        {"sections": section_rows},
    )


async def _coverage_by_scope(
    session: AsyncSession,
    *,
    title_id: int,
    section_keys: list[str],
) -> dict[str, dict[str, Any]]:
    result = await session.execute(
        select(ProcessingState).where(
            ProcessingState.title_id == title_id,
            ProcessingState.section_key.in_(["", *section_keys]),
        )
    )
    coverage: dict[str, dict[str, Any]] = {}
    for row in result.scalars().all():
        scope = row.section_key or ""
        coverage.setdefault(scope, {})[row.area] = _coverage_payload(row)
    await _overlay_focus_cache_coverage(
        session,
        coverage=coverage,
        title_id=title_id,
        section_keys=section_keys,
    )
    return coverage


async def _article_coverage_by_title_ids(
    session: AsyncSession,
    title_ids: list[int],
) -> dict[int, dict[str, Any]]:
    if not title_ids:
        return {}
    result = await session.execute(
        select(ProcessingState).where(
            ProcessingState.title_id.in_(title_ids),
            ProcessingState.section_key == "",
        )
    )
    coverage: dict[int, dict[str, Any]] = {}
    for row in result.scalars().all():
        coverage.setdefault(int(row.title_id), {})[row.area] = _coverage_payload(row)
    await _overlay_article_cache_coverage(session, coverage=coverage, title_ids=title_ids)
    return coverage


def _coverage_payload(row: ProcessingState) -> dict[str, Any]:
    updated_at = row.updated_at or row.created_at
    return {
        "coverage_version": "processing_state_v1",
        "state": row.state,
        "pending": row.pending_count,
        "running": row.running_count,
        "failed": row.failed_count,
        "completed": row.completed_count,
        "total": row.expected_count,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "source": row.source,
        "detail": row.detail,
        "reason": row.reason,
    }


async def _overlay_focus_cache_coverage(
    session: AsyncSession,
    *,
    coverage: dict[str, dict[str, Any]],
    title_id: int,
    section_keys: list[str],
) -> None:
    """Overlay real cache presence onto focus article/section coverage.

    ProcessingState is workflow bookkeeping. For graph cards, persisted cache
    rows are the source of truth for whether entities/times are available.
    """

    scopes = ["", *section_keys]
    mention_counts = await _count_cache_by_section(
        session,
        model=MentionCache,
        title_id=title_id,
        section_keys=section_keys,
    )
    time_counts = await _count_cache_by_section(
        session,
        model=SectionTime,
        title_id=title_id,
        section_keys=section_keys,
    )
    for scope in scopes:
        if mention_counts.get(scope, 0) > 0:
            _set_cache_coverage(
                coverage.setdefault(scope, {}),
                area="entity_mentions",
                count=mention_counts[scope],
                source="mention_cache",
            )
        if time_counts.get(scope, 0) > 0:
            _set_cache_coverage(
                coverage.setdefault(scope, {}),
                area="temporal",
                count=time_counts[scope],
                source="section_time",
            )


async def _overlay_article_cache_coverage(
    session: AsyncSession,
    *,
    coverage: dict[int, dict[str, Any]],
    title_ids: list[int],
) -> None:
    """Overlay article-level cache presence for related article nodes."""

    if not title_ids:
        return
    section_result = await session.execute(
        select(SectionClean.title_id, func.count())
        .where(SectionClean.title_id.in_(title_ids))
        .group_by(SectionClean.title_id)
    )
    for title_id, count in section_result.all():
        _set_cache_coverage(
            coverage.setdefault(int(title_id), {}),
            area="article_load",
            count=int(count or 0),
            source="section_clean",
        )
    mention_result = await session.execute(
        select(MentionCache.title_id, func.count())
        .where(MentionCache.title_id.in_(title_ids))
        .group_by(MentionCache.title_id)
    )
    for title_id, count in mention_result.all():
        _set_cache_coverage(
            coverage.setdefault(int(title_id), {}),
            area="entity_mentions",
            count=int(count or 0),
            source="mention_cache",
        )
    time_result = await session.execute(
        select(SectionTime.title_id, func.count())
        .where(SectionTime.title_id.in_(title_ids))
        .group_by(SectionTime.title_id)
    )
    for title_id, count in time_result.all():
        _set_cache_coverage(
            coverage.setdefault(int(title_id), {}),
            area="temporal",
            count=int(count or 0),
            source="section_time",
        )


async def _count_cache_by_section(
    session: AsyncSession,
    *,
    model: type[MentionCache] | type[SectionTime],
    title_id: int,
    section_keys: list[str],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    title_result = await session.execute(
        select(func.count())
        .select_from(model)
        .where(model.title_id == title_id)
    )
    counts[""] = int(title_result.scalar_one() or 0)
    if not section_keys:
        return counts
    section_result = await session.execute(
        select(model.section_key, func.count())
        .where(model.title_id == title_id)
        .where(model.section_key.in_(section_keys))
        .group_by(model.section_key)
    )
    for section_key, count in section_result.all():
        counts[str(section_key)] = int(count or 0)
    return counts


def _set_cache_coverage(
    area_coverage: dict[str, Any],
    *,
    area: str,
    count: int,
    source: str,
) -> None:
    if count <= 0:
        return
    existing = area_coverage.get(area)
    if isinstance(existing, dict) and str(existing.get("state")) in {"completed", "done", "running"}:
        return
    area_coverage[area] = {
        "coverage_version": "cache_presence_v1",
        "state": "completed",
        "pending": 0,
        "running": 0,
        "failed": 0,
        "completed": count,
        "total": count,
        "updated_at": None,
        "source": source,
        "detail": f"{count} persisted cache row(s) available.",
        "reason": "Derived from persisted cache rows.",
    }


def _neo4j_node_payload(node: dict[str, Any]) -> dict[str, Any]:
    payload = dict(node)
    payload["postgres_id"] = str(node.get("postgres_id") or "")
    payload["coverage"] = json.dumps(node.get("coverage") or {}, sort_keys=True)
    payload["metrics"] = json.dumps(node.get("metrics") or {}, sort_keys=True)
    return payload


def _neo4j_edge_payload(edge: dict[str, Any]) -> dict[str, Any]:
    payload = dict(edge)
    payload["signals"] = json.dumps(edge.get("signals") or {}, sort_keys=True)
    return payload


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").replace("_", " ").strip().casefold())


async def _related_rows(
    session: AsyncSession,
    section_keys: list[str],
    limit: int,
) -> list[RelatedCache]:
    if not section_keys:
        return []
    result = await session.execute(
        select(RelatedCache)
        .where(RelatedCache.from_section_key.in_(section_keys))
        .order_by(RelatedCache.score.desc(), RelatedCache.level.asc(), RelatedCache.to_title.asc())
        .limit(limit)
    )
    return list(result.scalars().all())
