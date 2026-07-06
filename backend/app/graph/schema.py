"""Canonical Neo4j schema contract for Timeline.

This module is the code equivalent of an RDBMS schema definition. Keep graph
shape constants, required constraints, node-state defaults, and cleanup checks
here so graph resets/migrations do not depend on ad hoc Cypher copied from
task notes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GRAPH_SCHEMA_VERSION = "neo4j_graph_schema_v2"
LINK_EXTRACTION_VERSION = "graph_frontier_links_v1"

ARTICLE_LABEL = "V4Article"
SECTION_LABEL = "V4Section"
ENTITY_LABEL = "V4Entity"
TIME_FACT_LABEL = "V4TimeFact"
TOPIC_SESSION_LABEL = "V4TopicSession"

ARTICLE_KIND = "article"

REL_HAS_SECTION = "HAS_SECTION"
REL_LINKS_TO = "LINKS_TO"
REL_RELATED_TO = "RELATED_TO"
LEGACY_REL_LINKS_TO_L2 = "LINKS_TO_L2"

LINK_STATE_MISSING = "missing"
LINK_STATE_RUNNING = "running"
LINK_STATE_COMPLETED = "completed"
LINK_STATE_FAILED = "failed"
LINK_STATE_STALE = "stale"
LINK_STATES = {
    LINK_STATE_MISSING,
    LINK_STATE_RUNNING,
    LINK_STATE_COMPLETED,
    LINK_STATE_FAILED,
    LINK_STATE_STALE,
}

SCOPE_SECTION_OUTGOING_LINKS = "section_outgoing_links"
SCOPE_ARTICLE_INTRO_OUTGOING_LINKS = "article_intro_outgoing_links"
SCOPE_ARTICLE_OUTGOING_LINKS = "article_outgoing_links"


@dataclass(frozen=True)
class GraphConstraint:
    """One required Neo4j constraint."""

    name: str
    cypher: str


GRAPH_CONSTRAINTS: tuple[GraphConstraint, ...] = (
    GraphConstraint(
        "v4_topic_id",
        "CREATE CONSTRAINT v4_topic_id IF NOT EXISTS FOR (n:V4TopicSession) REQUIRE n.id IS UNIQUE",
    ),
    GraphConstraint(
        "v4_article_id",
        "CREATE CONSTRAINT v4_article_id IF NOT EXISTS FOR (n:V4Article) REQUIRE n.title_id IS UNIQUE",
    ),
    GraphConstraint(
        "v4_section_key",
        "CREATE CONSTRAINT v4_section_key IF NOT EXISTS FOR (n:V4Section) REQUIRE n.section_key IS UNIQUE",
    ),
    GraphConstraint(
        "v4_entity_key",
        "CREATE CONSTRAINT v4_entity_key IF NOT EXISTS FOR (n:V4Entity) REQUIRE n.key IS UNIQUE",
    ),
    GraphConstraint(
        "v4_time_ref",
        "CREATE CONSTRAINT v4_time_ref IF NOT EXISTS FOR (n:V4TimeFact) REQUIRE n.time_ref_id IS UNIQUE",
    ),
)


GRAPH_SCHEMA_VALIDATION_QUERY = """
MATCH (a:V4Article)
WITH count(a) AS articles
CALL () {
    MATCH (x:V4Article)
    WHERE x.title_id IS NOT NULL
    WITH x.title_id AS title_id, count(*) AS n
    WHERE n > 1
    RETURN count(*) AS duplicate_article_ids
}
CALL () {
    MATCH (s:V4Section)
    WHERE s.section_key IS NOT NULL
    WITH s.section_key AS section_key, count(*) AS n
    WHERE n > 1
    RETURN count(*) AS duplicate_section_keys
}
CALL () { MATCH (x:V4Article) WHERE x.level IS NOT NULL RETURN count(x) AS article_level_props }
CALL () { MATCH (x:V4Article) WHERE x.kind <> 'article' OR x.kind IS NULL RETURN count(x) AS non_article_kind }
CALL () { MATCH (x:V4Article) WHERE x.link_extraction_state IS NULL RETURN count(x) AS article_missing_link_state }
CALL () { MATCH (x:V4Article) WHERE x.graph_schema_version IS NULL RETURN count(x) AS article_missing_schema_version }
CALL () { MATCH (s:V4Section) WHERE s.link_extraction_state IS NULL RETURN count(s) AS section_missing_link_state }
CALL () { MATCH (s:V4Section) WHERE s.graph_schema_version IS NULL RETURN count(s) AS section_missing_schema_version }
CALL () { MATCH ()-[r:LINKS_TO_L2]->() RETURN count(r) AS legacy_links_to_l2_edges }
RETURN articles,
       duplicate_article_ids,
       duplicate_section_keys,
       article_level_props,
       non_article_kind,
       article_missing_link_state,
       article_missing_schema_version,
       section_missing_link_state,
       section_missing_schema_version,
       legacy_links_to_l2_edges
"""

GRAPH_SCHEMA_PRE_CONSTRAINT_CLEANUP_QUERIES: tuple[str, ...] = (
    """
    MATCH (a:V4Article)
    WHERE a.title_id IS NOT NULL
    WITH a.title_id AS title_id, collect(a) AS nodes
    WHERE size(nodes) > 1
    UNWIND tail(nodes) AS dupe
    WITH head(nodes) AS keep, dupe
    OPTIONAL MATCH (dupe)-[r:HAS_SECTION]->(s:V4Section)
    MERGE (keep)-[nr:HAS_SECTION]->(s)
    SET nr += properties(r)
    """,
    """
    MATCH (a:V4Article)
    WHERE a.title_id IS NOT NULL
    WITH a.title_id AS title_id, collect(a) AS nodes
    WHERE size(nodes) > 1
    UNWIND tail(nodes) AS dupe
    WITH head(nodes) AS keep, dupe
    OPTIONAL MATCH (s:V4Section)-[r:RELATED_TO]->(dupe)
    MERGE (s)-[nr:RELATED_TO]->(keep)
    SET nr += properties(r)
    """,
    """
    MATCH (a:V4Article)
    WHERE a.title_id IS NOT NULL
    WITH a.title_id AS title_id, collect(a) AS nodes
    WHERE size(nodes) > 1
    UNWIND tail(nodes) AS dupe
    WITH head(nodes) AS keep, dupe
    OPTIONAL MATCH (dupe)-[r:LINKS_TO_L2]->(target:V4Article)
    MERGE (keep)-[nr:LINKS_TO]->(target)
    SET nr += properties(r),
        nr.migrated_from = 'LINKS_TO_L2'
    """,
    """
    MATCH (a:V4Article)
    WHERE a.title_id IS NOT NULL
    WITH a.title_id AS title_id, collect(a) AS nodes
    WHERE size(nodes) > 1
    UNWIND tail(nodes) AS dupe
    WITH head(nodes) AS keep, dupe
    OPTIONAL MATCH (source:V4Article)-[r:LINKS_TO_L2]->(dupe)
    MERGE (source)-[nr:LINKS_TO]->(keep)
    SET nr += properties(r),
        nr.migrated_from = 'LINKS_TO_L2'
    """,
    """
    MATCH (a:V4Article)
    WHERE a.title_id IS NOT NULL
    WITH a.title_id AS title_id, collect(a) AS nodes
    WHERE size(nodes) > 1
    WITH head(nodes) AS keep, tail(nodes) AS dupes
    UNWIND dupes AS dupe
    SET keep.title = coalesce(keep.title, dupe.title),
        keep.label = coalesce(keep.label, dupe.label),
        keep.coverage_json = coalesce(keep.coverage_json, dupe.coverage_json),
        keep.metrics_json = coalesce(keep.metrics_json, dupe.metrics_json),
        keep.link_extraction_state = coalesce(keep.link_extraction_state, dupe.link_extraction_state),
        keep.link_extraction_scope = coalesce(keep.link_extraction_scope, dupe.link_extraction_scope),
        keep.link_extraction_expected = coalesce(keep.link_extraction_expected, dupe.link_extraction_expected),
        keep.link_extraction_completed = coalesce(keep.link_extraction_completed, dupe.link_extraction_completed)
    DETACH DELETE dupe
    """,
    """
    MATCH (s:V4Section)
    WHERE s.section_key IS NOT NULL
    WITH s.section_key AS section_key, collect(s) AS nodes
    WHERE size(nodes) > 1
    UNWIND tail(nodes) AS dupe
    WITH head(nodes) AS keep, dupe
    OPTIONAL MATCH (a:V4Article)-[r:HAS_SECTION]->(dupe)
    MERGE (a)-[nr:HAS_SECTION]->(keep)
    SET nr += properties(r)
    """,
    """
    MATCH (s:V4Section)
    WHERE s.section_key IS NOT NULL
    WITH s.section_key AS section_key, collect(s) AS nodes
    WHERE size(nodes) > 1
    UNWIND tail(nodes) AS dupe
    WITH head(nodes) AS keep, dupe
    OPTIONAL MATCH (dupe)-[r:RELATED_TO]->(a:V4Article)
    MERGE (keep)-[nr:RELATED_TO]->(a)
    SET nr += properties(r)
    """,
    """
    MATCH (s:V4Section)
    WHERE s.section_key IS NOT NULL
    WITH s.section_key AS section_key, collect(s) AS nodes
    WHERE size(nodes) > 1
    WITH head(nodes) AS keep, tail(nodes) AS dupes
    UNWIND dupes AS dupe
    SET keep.title = coalesce(keep.title, dupe.title),
        keep.heading = coalesce(keep.heading, dupe.heading),
        keep.title_id = coalesce(keep.title_id, dupe.title_id),
        keep.heading_id = coalesce(keep.heading_id, dupe.heading_id),
        keep.coverage_json = coalesce(keep.coverage_json, dupe.coverage_json),
        keep.metrics_json = coalesce(keep.metrics_json, dupe.metrics_json),
        keep.link_extraction_state = coalesce(keep.link_extraction_state, dupe.link_extraction_state)
    DETACH DELETE dupe
    """,
)

GRAPH_SCHEMA_CLEANUP_QUERIES: tuple[str, ...] = (
    """
    MATCH (source:V4Article)-[old:LINKS_TO_L2]->(target:V4Article)
    MERGE (source)-[new:LINKS_TO]->(target)
    SET new += properties(old),
        new.source = coalesce(old.source, 'graph_frontier'),
        new.method = coalesce(old.method, 'wikilink'),
        new.migrated_from = 'LINKS_TO_L2',
        new.graph_schema_version = 'neo4j_graph_schema_v2',
        new.updated_at = datetime()
    DELETE old
    """,
    """
    MATCH (a:V4Article)
    REMOVE a.level
    SET a.kind = 'article',
        a.link_extraction_state = coalesce(a.link_extraction_state, 'missing'),
        a.link_extraction_scope = coalesce(a.link_extraction_scope, 'article_outgoing_links'),
        a.link_extraction_expected = coalesce(a.link_extraction_expected, 0),
        a.link_extraction_completed = coalesce(a.link_extraction_completed, 0),
        a.link_extraction_version = coalesce(a.link_extraction_version, 'graph_frontier_links_v1'),
        a.graph_schema_version = 'neo4j_graph_schema_v2',
        a.link_extraction_updated_at = coalesce(a.link_extraction_updated_at, datetime())
    """,
    """
    MATCH (s:V4Section)
    SET s.link_extraction_state = coalesce(s.link_extraction_state, 'missing'),
        s.link_extraction_scope = coalesce(s.link_extraction_scope, 'section_outgoing_links'),
        s.link_extraction_expected = coalesce(s.link_extraction_expected, 0),
        s.link_extraction_completed = coalesce(s.link_extraction_completed, 0),
        s.link_extraction_version = coalesce(s.link_extraction_version, 'graph_frontier_links_v1'),
        s.graph_schema_version = 'neo4j_graph_schema_v2',
        s.link_extraction_updated_at = coalesce(s.link_extraction_updated_at, datetime())
    """,
)


def link_extraction_payload(
    *,
    state: str,
    scope: str,
    expected: int,
    completed: int,
    error: str | None = None,
) -> dict[str, Any]:
    """Return a validated link-extraction state payload."""

    normalized_state = state if state in LINK_STATES else LINK_STATE_MISSING
    return {
        "state": normalized_state,
        "scope": scope,
        "expected": max(0, int(expected)),
        "completed": max(0, int(completed)),
        "error": error,
        "version": LINK_EXTRACTION_VERSION,
        "schema_version": GRAPH_SCHEMA_VERSION,
    }
