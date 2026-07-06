"""Timeline cache ORM models."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.db.database import Base

settings = get_settings()
SCHEMA = settings.pg_schema


class RedirectMap(Base):
    """Cached redirect resolution for article titles."""

    __tablename__ = "redirect_map"
    __table_args__ = (
        UniqueConstraint("normalized_from", name="uq_redirect_normalized_from"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_title_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    from_heading: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    normalized_from: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    to_title_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    to_heading: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_to: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class SectionClean(Base):
    """Cached clean section text and extracted links."""

    __tablename__ = "section_clean"
    __table_args__ = (UniqueConstraint("section_key", name="uq_section_clean_key"), {"schema": SCHEMA})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    heading_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    heading: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[int | None] = mapped_column(Integer)
    parent_id: Mapped[int | None] = mapped_column(BigInteger)
    clean_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_html: Mapped[str] = mapped_column(Text, nullable=False)
    links_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class SectionTag(Base):
    """Cached seed or LLM section tags."""

    __tablename__ = "section_tags"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    heading_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    tag_text: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    tag_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    tag_subtype: Mapped[str | None] = mapped_column(String(50))
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TimeDimension(Base):
    """Canonical V4 temporal dimension."""

    __tablename__ = "time_dimension"
    __table_args__ = (UniqueConstraint("time_ref_id", name="uq_v4_time_ref_id"), {"schema": SCHEMA})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    time_ref_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    time_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    precision: Mapped[str | None] = mapped_column(String(20))
    start_date: Mapped[str | None] = mapped_column(String(32))
    end_date: Mapped[str | None] = mapped_column(String(32))
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    month: Mapped[int | None] = mapped_column(Integer)
    day: Mapped[int | None] = mapped_column(Integer)
    season: Mapped[str | None] = mapped_column(String(20))
    era_name: Mapped[str | None] = mapped_column(String(100))
    region_scope: Mapped[str | None] = mapped_column(String(100))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class TimeDimensionCandidate(Base):
    """Review queue for unresolved temporal labels."""

    __tablename__ = "time_dimension_candidate"
    __table_args__ = (
        UniqueConstraint("candidate_key", name="uq_v4_time_candidate_key"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    proposed_time_ref_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    time_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="era")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_section_key: Mapped[str | None] = mapped_column(String(80))
    last_seen_section_key: Mapped[str | None] = mapped_column(String(80))
    source_text_excerpt: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class SectionTime(Base):
    """Join table linking sections to time refs."""

    __tablename__ = "section_time"
    __table_args__ = (
        UniqueConstraint("section_key", "time_ref_id", name="uq_v4_section_time"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    heading_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    time_ref_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RelatedCache(Base):
    """Cached related-information rows."""

    __tablename__ = "related_cache"
    __table_args__ = (
        UniqueConstraint("from_section_key", "to_title_id", "level", name="uq_v4_related"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    to_title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    to_title: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    signals_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    why_text: Mapped[str] = mapped_column(Text, nullable=False)
    why_source: Mapped[str] = mapped_column(String(30), nullable=False, default="template")
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class TimelineContextCache(Base):
    """Cached L1/L2 timeline context rows for an article section."""

    __tablename__ = "timeline_context_cache"
    __table_args__ = (
        UniqueConstraint(
            "from_section_key",
            "source_section_key",
            "time_ref_id",
            "level",
            name="uq_v4_timeline_context",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    from_section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source_title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    source_title: Mapped[str] = mapped_column(Text, nullable=False)
    source_heading_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    source_heading: Mapped[str] = mapped_column(Text, nullable=False)
    source_section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    time_ref_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    track: Mapped[str] = mapped_column(String(40), nullable=False, default="context")
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    signals_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class OntologyVersion(Base):
    """Pinned ontology/scoring configuration version."""

    __tablename__ = "ontology_version"
    __table_args__ = (UniqueConstraint("version_key", name="uq_ontology_version_key"), {"schema": SCHEMA})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    categories_json: Mapped[list[str]] = mapped_column(JSONB, default=list)
    domains_json: Mapped[list[str]] = mapped_column(JSONB, default=list)
    temporal_roles_json: Mapped[list[str]] = mapped_column(JSONB, default=list)
    precision_levels_json: Mapped[list[str]] = mapped_column(JSONB, default=list)
    weights_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    horizons_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    gates_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EntityRegistry(Base):
    """Canonical entity identity and classification registry."""

    __tablename__ = "entity_registry"
    __table_args__ = (UniqueConstraint("entity_id", name="uq_entity_registry_entity_id"), {"schema": SCHEMA})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    canonical_title_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    canonical_title: Mapped[str | None] = mapped_column(Text)
    surface: Mapped[str | None] = mapped_column(Text)
    primary_type: Mapped[str] = mapped_column(String(50), nullable=False, default="CONCEPT", index=True)
    types_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    primary_domain: Mapped[str] = mapped_column(String(80), nullable=False, default="Society & People", index=True)
    domains_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    aliases_json: Mapped[list[str]] = mapped_column(JSONB, default=list)
    document_frequency: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    specificity: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    ontology_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class EntityAliasMap(Base):
    """Entity alias/promotion audit map."""

    __tablename__ = "entity_alias_map"
    __table_args__ = (
        UniqueConstraint("from_entity_id", "to_entity_id", name="uq_entity_alias_pair"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_entity_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    to_entity_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    alias_kind: Mapped[str] = mapped_column(String(40), nullable=False, default="promotion", index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    audit_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ontology_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TaxonomyCandidate(Base):
    """Review queue for proposed taxonomy growth."""

    __tablename__ = "taxonomy_candidate"
    __table_args__ = (
        UniqueConstraint("candidate_kind", "candidate_key", name="uq_taxonomy_candidate_key"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    candidate_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    parent_key: Mapped[str | None] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    examples_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    proposed_by: Mapped[str] = mapped_column(String(80), nullable=False, default="llm")
    ontology_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class MentionCache(Base):
    """Mention-level resolved entity occurrence cache."""

    __tablename__ = "mention_cache"
    __table_args__ = (
        UniqueConstraint("section_key", "char_start", "char_end", "entity_id", name="uq_mention_span_entity"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    heading_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    surface: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    attribution: Mapped[str] = mapped_column(String(40), nullable=False, default="core", index=True)
    salience: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    ontology_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EntityPassageScore(Base):
    """Per-entity per-section component score cache."""

    __tablename__ = "entity_passage_score"
    __table_args__ = (
        UniqueConstraint("entity_id", "section_key", "ontology_version", name="uq_entity_passage_score"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    heading_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    components_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    blend: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    ontology_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class TimeAnchorRegistry(Base):
    """Ontology time-anchor registry for points, intervals, fuzzy and deep time."""

    __tablename__ = "time_anchor_registry"
    __table_args__ = (UniqueConstraint("time_id", name="uq_time_anchor_registry_time_id"), {"schema": SCHEMA})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    time_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    precision: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    calendar: Mapped[str] = mapped_column(String(40), nullable=False, default="gregorian")
    label: Mapped[str] = mapped_column(Text, nullable=False)
    t_start: Mapped[float | None] = mapped_column(Float, index=True)
    t_end: Mapped[float | None] = mapped_column(Float, index=True)
    open_start: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    open_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    center: Mapped[float | None] = mapped_column(Float)
    spread: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    precision_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ontology_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class FactCache(Base):
    """Grounded assertion cache."""

    __tablename__ = "fact_cache"
    __table_args__ = (UniqueConstraint("fact_id", name="uq_fact_cache_fact_id"), {"schema": SCHEMA})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fact_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    heading_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    primary_entity_id: Mapped[str | None] = mapped_column(String(180), index=True)
    other_entity_ids_json: Mapped[list[str]] = mapped_column(JSONB, default=list)
    assertion_kind: Mapped[str] = mapped_column(String(80), nullable=False, default="section_assertion", index=True)
    assertion_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    ontology_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class FactTime(Base):
    """Temporal-role link between facts and time anchors."""

    __tablename__ = "fact_time"
    __table_args__ = (
        UniqueConstraint("fact_id", "time_id", "role", name="uq_fact_time_role"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fact_id: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    heading_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    time_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(80), nullable=False, default="occurred", index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ontology_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ContentRelatednessCache(Base):
    """Component-vector relatedness cache between focus and candidate passages."""

    __tablename__ = "content_relatedness_cache"
    __table_args__ = (
        UniqueConstraint(
            "focus_section_key",
            "candidate_key",
            "ontology_version",
            name="uq_content_relatedness_focus_candidate",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    focus_section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    candidate_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    candidate_title_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    candidate_section_key: Mapped[str | None] = mapped_column(String(80), index=True)
    components_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    raw_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    relevance_norm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    why_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    gates_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    ontology_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class ArticleCore(Base):
    """Article-level L1 digest generated from cached sections."""

    __tablename__ = "article_core"
    __table_args__ = (UniqueConstraint("title_id", "model_version", name="uq_article_core_title_model"), {"schema": SCHEMA})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    topic_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    entities_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    dated_spine_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    source_section_keys_json: Mapped[list[str]] = mapped_column(JSONB, default=list)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class ProcessingState(Base):
    """Observable article/section processing lifecycle state."""

    __tablename__ = "processing_state"
    __table_args__ = (
        UniqueConstraint("title_id", "section_key", "area", name="uq_processing_state_scope_area"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    section_key: Mapped[str] = mapped_column(String(80), nullable=False, default="", index=True)
    area: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="idle", index=True)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    running_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_error: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="derived")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class AgentTrace(Base):
    """Reserved trace store for Phase 3 agent runs."""

    __tablename__ = "agent_trace"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    model_name: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="running")
    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_response: Mapped[str | None] = mapped_column(Text)
    error_text: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class AgentJob(Base):
    """Durable background job queue for Phase 3 agents."""

    __tablename__ = "agent_job"
    __table_args__ = (
        UniqueConstraint("job_type", "section_key", name="uq_agent_job_type_section"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    title_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    section_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    run_after: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
