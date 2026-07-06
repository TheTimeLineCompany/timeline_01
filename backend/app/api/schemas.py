"""API response/request schemas."""

from typing import Any
from datetime import datetime

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    """Article search result."""

    title: str
    title_id: int


class ArticleSectionResponse(BaseModel):
    """Reader section response."""

    section_key: str
    title_id: int
    heading_id: int
    heading: str
    level: int | None
    parent_id: int | None
    clean_text: str
    content_html: str
    links: list[dict[str, Any]]
    provenance: dict[str, Any]


class ArticleCoreResponse(BaseModel):
    """Cached article-level L1 digest."""

    summary: str
    topic: dict[str, Any] = Field(default_factory=dict)
    key_entities: list[dict[str, Any]] = Field(default_factory=list)
    dated_spine: list[dict[str, Any]] = Field(default_factory=list)
    model_version: str
    updated_at: datetime | None = None


class ArticleResponse(BaseModel):
    """Reader article response."""

    title: str
    title_id: int
    sections: list[ArticleSectionResponse]
    core: ArticleCoreResponse | None = None
    cached: bool = True
    warnings: list[str] = Field(default_factory=list)


class SeedResponse(BaseModel):
    """Seed enrichment response."""

    section_key: str
    spacy_entities: int
    temporal_matches: int
    warnings: list[str] = Field(default_factory=list)


class RelatedItemResponse(BaseModel):
    """One related-info item."""

    to_title_id: int
    to_title: str
    level: int
    score: float
    signals: dict[str, Any]
    why_text: str
    why_source: str
    model_version: str
    summary: str = ""
    agent_updated_at: datetime | None = None
    why: dict[str, Any] = Field(default_factory=dict)


class RelatedResponse(BaseModel):
    """Related-info response for one section."""

    section_key: str
    items: list[RelatedItemResponse]
    read_path_llm_used: bool = False
    scoring_metrics: dict[str, Any] = Field(default_factory=dict)


class TimelineEventResponse(BaseModel):
    """One timeline event projected from cached temporal rows."""

    id: str
    title_id: int
    source_title_id: int
    source_title: str
    heading_id: int
    source_heading: str
    section_key: str
    heading: str
    time_ref_id: str
    time_kind: str
    label: str
    precision: str | None
    start_date: str | None
    end_date: str | None
    year: int | None
    month: int | None
    day: int | None
    season: str | None
    source: str
    confidence: float
    excerpt: str
    lane: str = "seed"
    domain_lane: str = "Society & People"
    level: int = 0
    track: str = "core"
    relevance_score: float = 1.0
    provenance: dict[str, Any]
    attribution: dict[str, Any] = Field(default_factory=dict)
    why: dict[str, Any] = Field(default_factory=dict)


class TimelineResponse(BaseModel):
    """Timeline projection for one article."""

    title: str
    title_id: int
    events: list[TimelineEventResponse]
    seeded: bool = False
    enrichment_pending: bool = False
    read_path_llm_used: bool = False
    scoring_metrics: dict[str, Any] = Field(default_factory=dict)


class GraphNodeResponse(BaseModel):
    """One node in the article graph projection."""

    id: str
    kind: str
    label: str
    postgres_id: str | int | None = None
    title_id: int | None = None
    title: str | None = None
    section_key: str | None = None
    heading_id: int | None = None
    level: int | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)


class GraphEdgeResponse(BaseModel):
    """One edge in the article graph projection."""

    id: str
    kind: str
    source: str
    target: str
    level: int | None = None
    score: float | None = None
    why_source: str | None = None
    signals: dict[str, Any] = Field(default_factory=dict)


class ArticleGraphResponse(BaseModel):
    """Article graph projection response."""

    title: str
    title_id: int
    nodes: list[GraphNodeResponse]
    edges: list[GraphEdgeResponse]
    synced_to_neo4j: bool = True


class GraphCoverageArticleResponse(BaseModel):
    """Coverage crawler action for one graph-frontier article."""

    title_id: int
    title: str
    level: int
    sections_cached: int
    sections_selected: int
    jobs_enqueued: dict[str, int] = Field(default_factory=dict)
    status: str
    error: str | None = None


class GraphCoverageResponse(BaseModel):
    """Bounded graph coverage crawler response."""

    title: str
    title_id: int
    frontier_considered: int
    articles_processed: int
    jobs_enqueued: dict[str, int] = Field(default_factory=dict)
    articles: list[GraphCoverageArticleResponse]


class TimelineExplainRequest(BaseModel):
    """Request an explicit agent explanation for one timeline event."""

    section_key: str
    time_ref_id: str
    label: str
    domain_lane: str = "Society & People"
    level: int = 0
    track: str = "core"
    relevance_score: float = 1.0


class TimelineExplainResponse(BaseModel):
    """Agent explanation for why a timeline event is relevant."""

    section_key: str
    time_ref_id: str
    why_text: str
    why_source: str
    model_version: str
    generated_at: datetime


class AgentEnqueueResponse(BaseModel):
    """Agent job enqueue response."""

    title: str
    title_id: int
    job_type: str
    sections_considered: int
    jobs_enqueued: int
    force: bool = False


class AgentJobSummary(BaseModel):
    """Small status summary for agent jobs."""

    job_type: str
    status: str
    count: int


class ProcessingAreaStatus(BaseModel):
    """Status for one processing area."""

    key: str
    label: str
    state: str
    pending: int = 0
    running: int = 0
    failed: int = 0
    completed: int = 0
    total: int = 0
    detail: str = ""
    reason: str = ""
    last_error: str | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    elapsed_seconds: float | None = None
    source: str = "derived"


class ProcessingStatusResponse(BaseModel):
    """Article/section processing status summary."""

    title: str | None = None
    title_id: int | None = None
    section_key: str | None = None
    overall_state: str
    areas: list[ProcessingAreaStatus]
    metrics: dict[str, Any] = Field(default_factory=dict)
