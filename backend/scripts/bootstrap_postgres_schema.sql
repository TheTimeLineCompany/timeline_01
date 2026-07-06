CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS timeline_v4;

CREATE TABLE IF NOT EXISTS timeline_v4.redirect_map (
    id serial PRIMARY KEY,
    from_title_id bigint NULL,
    from_heading text NOT NULL,
    normalized_from text NOT NULL,
    to_title_id bigint NULL,
    to_heading text NOT NULL,
    normalized_to text NOT NULL,
    depth integer NOT NULL DEFAULT 0,
    parser_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_redirect_from_title_id UNIQUE (from_title_id),
    CONSTRAINT uq_redirect_normalized_from UNIQUE (normalized_from)
);

CREATE INDEX IF NOT EXISTS ix_redirect_map_from_title_id ON timeline_v4.redirect_map (from_title_id);
CREATE INDEX IF NOT EXISTS ix_redirect_map_normalized_from ON timeline_v4.redirect_map (normalized_from);
CREATE INDEX IF NOT EXISTS ix_redirect_map_to_title_id ON timeline_v4.redirect_map (to_title_id);
CREATE INDEX IF NOT EXISTS ix_redirect_map_normalized_to ON timeline_v4.redirect_map (normalized_to);

CREATE TABLE IF NOT EXISTS timeline_v4.section_clean (
    id serial PRIMARY KEY,
    section_key varchar(80) NOT NULL,
    title_id bigint NOT NULL,
    heading_id bigint NOT NULL,
    title text NOT NULL,
    heading text NOT NULL,
    level integer NULL,
    parent_id bigint NULL,
    clean_text text NOT NULL,
    content_html text NOT NULL,
    links_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    parser_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_section_clean_key UNIQUE (section_key)
);

CREATE INDEX IF NOT EXISTS ix_section_clean_section_key ON timeline_v4.section_clean (section_key);
CREATE INDEX IF NOT EXISTS ix_section_clean_title_id ON timeline_v4.section_clean (title_id);
CREATE INDEX IF NOT EXISTS ix_section_clean_heading_id ON timeline_v4.section_clean (heading_id);

CREATE TABLE IF NOT EXISTS timeline_v4.section_tags (
    id serial PRIMARY KEY,
    section_key varchar(80) NOT NULL,
    title_id bigint NOT NULL,
    heading_id bigint NOT NULL,
    tag_text text NOT NULL,
    tag_type varchar(50) NOT NULL,
    tag_subtype varchar(50) NULL,
    source varchar(50) NOT NULL,
    confidence double precision NOT NULL DEFAULT 1.0,
    char_start integer NULL,
    char_end integer NULL,
    provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_section_tags_section_key ON timeline_v4.section_tags (section_key);
CREATE INDEX IF NOT EXISTS ix_section_tags_title_id ON timeline_v4.section_tags (title_id);
CREATE INDEX IF NOT EXISTS ix_section_tags_heading_id ON timeline_v4.section_tags (heading_id);
CREATE INDEX IF NOT EXISTS ix_section_tags_tag_text ON timeline_v4.section_tags (tag_text);
CREATE INDEX IF NOT EXISTS ix_section_tags_tag_type ON timeline_v4.section_tags (tag_type);
CREATE INDEX IF NOT EXISTS ix_section_tags_source ON timeline_v4.section_tags (source);

CREATE TABLE IF NOT EXISTS timeline_v4.time_dimension (
    id serial PRIMARY KEY,
    time_ref_id varchar(128) NOT NULL,
    time_kind varchar(20) NOT NULL,
    label varchar(255) NOT NULL,
    precision varchar(20) NULL,
    start_date varchar(32) NULL,
    end_date varchar(32) NULL,
    year integer NULL,
    month integer NULL,
    day integer NULL,
    season varchar(20) NULL,
    era_name varchar(100) NULL,
    region_scope varchar(100) NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    active boolean NOT NULL DEFAULT true,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_v4_time_ref_id UNIQUE (time_ref_id)
);

CREATE INDEX IF NOT EXISTS ix_time_dimension_time_ref_id ON timeline_v4.time_dimension (time_ref_id);
CREATE INDEX IF NOT EXISTS ix_time_dimension_year ON timeline_v4.time_dimension (year);

CREATE TABLE IF NOT EXISTS timeline_v4.time_dimension_candidate (
    id serial PRIMARY KEY,
    candidate_key varchar(128) NOT NULL,
    label varchar(255) NOT NULL,
    proposed_time_ref_id varchar(128) NOT NULL,
    time_kind varchar(20) NOT NULL DEFAULT 'era',
    status varchar(20) NOT NULL DEFAULT 'pending',
    mention_count integer NOT NULL DEFAULT 1,
    first_seen_section_key varchar(80) NULL,
    last_seen_section_key varchar(80) NULL,
    source_text_excerpt text NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_v4_time_candidate_key UNIQUE (candidate_key)
);

CREATE INDEX IF NOT EXISTS ix_time_dimension_candidate_candidate_key ON timeline_v4.time_dimension_candidate (candidate_key);
CREATE INDEX IF NOT EXISTS ix_time_dimension_candidate_proposed_time_ref_id ON timeline_v4.time_dimension_candidate (proposed_time_ref_id);

CREATE TABLE IF NOT EXISTS timeline_v4.section_time (
    id serial PRIMARY KEY,
    section_key varchar(80) NOT NULL,
    title_id bigint NOT NULL,
    heading_id bigint NOT NULL,
    time_ref_id varchar(128) NOT NULL,
    source varchar(50) NOT NULL,
    confidence double precision NOT NULL DEFAULT 1.0,
    provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamp NOT NULL DEFAULT now(),
    CONSTRAINT uq_v4_section_time UNIQUE (section_key, time_ref_id)
);

CREATE INDEX IF NOT EXISTS ix_section_time_section_key ON timeline_v4.section_time (section_key);
CREATE INDEX IF NOT EXISTS ix_section_time_title_id ON timeline_v4.section_time (title_id);
CREATE INDEX IF NOT EXISTS ix_section_time_heading_id ON timeline_v4.section_time (heading_id);
CREATE INDEX IF NOT EXISTS ix_section_time_time_ref_id ON timeline_v4.section_time (time_ref_id);

CREATE TABLE IF NOT EXISTS timeline_v4.section_embedding (
    id bigserial PRIMARY KEY,
    section_key text NOT NULL UNIQUE,
    title_id bigint NOT NULL,
    heading_id bigint NOT NULL,
    embedding vector(384) NOT NULL,
    embedding_model text NOT NULL,
    provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NULL
);

CREATE INDEX IF NOT EXISTS idx_v4_section_embedding_title ON timeline_v4.section_embedding (title_id);

CREATE TABLE IF NOT EXISTS timeline_v4.related_cache (
    id serial PRIMARY KEY,
    from_section_key varchar(80) NOT NULL,
    to_title_id bigint NOT NULL,
    to_title text NOT NULL,
    level integer NOT NULL,
    score double precision NOT NULL,
    signals_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    why_text text NOT NULL,
    why_source varchar(30) NOT NULL DEFAULT 'template',
    provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    parser_version varchar(80) NOT NULL,
    model_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_v4_related UNIQUE (from_section_key, to_title_id, level)
);

CREATE INDEX IF NOT EXISTS ix_related_cache_from_section_key ON timeline_v4.related_cache (from_section_key);
CREATE INDEX IF NOT EXISTS ix_related_cache_to_title_id ON timeline_v4.related_cache (to_title_id);
CREATE INDEX IF NOT EXISTS ix_related_cache_level ON timeline_v4.related_cache (level);
CREATE INDEX IF NOT EXISTS ix_related_cache_score ON timeline_v4.related_cache (score);

CREATE TABLE IF NOT EXISTS timeline_v4.timeline_context_cache (
    id serial PRIMARY KEY,
    from_title_id bigint NOT NULL,
    from_section_key varchar(80) NOT NULL,
    source_title_id bigint NOT NULL,
    source_title text NOT NULL,
    source_heading_id bigint NOT NULL,
    source_heading text NOT NULL,
    source_section_key varchar(80) NOT NULL,
    time_ref_id varchar(128) NOT NULL,
    level integer NOT NULL,
    track varchar(40) NOT NULL DEFAULT 'context',
    relevance_score double precision NOT NULL,
    signals_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_v4_timeline_context UNIQUE (from_section_key, source_section_key, time_ref_id, level)
);

CREATE INDEX IF NOT EXISTS ix_timeline_context_cache_from_title_id ON timeline_v4.timeline_context_cache (from_title_id);
CREATE INDEX IF NOT EXISTS ix_timeline_context_cache_from_section_key ON timeline_v4.timeline_context_cache (from_section_key);
CREATE INDEX IF NOT EXISTS ix_timeline_context_cache_source_title_id ON timeline_v4.timeline_context_cache (source_title_id);
CREATE INDEX IF NOT EXISTS ix_timeline_context_cache_source_heading_id ON timeline_v4.timeline_context_cache (source_heading_id);
CREATE INDEX IF NOT EXISTS ix_timeline_context_cache_source_section_key ON timeline_v4.timeline_context_cache (source_section_key);
CREATE INDEX IF NOT EXISTS ix_timeline_context_cache_time_ref_id ON timeline_v4.timeline_context_cache (time_ref_id);
CREATE INDEX IF NOT EXISTS ix_timeline_context_cache_level ON timeline_v4.timeline_context_cache (level);
CREATE INDEX IF NOT EXISTS ix_timeline_context_cache_relevance_score ON timeline_v4.timeline_context_cache (relevance_score);

CREATE TABLE IF NOT EXISTS timeline_v4.ontology_version (
    id serial PRIMARY KEY,
    version_key varchar(80) NOT NULL,
    status varchar(30) NOT NULL DEFAULT 'active',
    categories_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    domains_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    temporal_roles_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    precision_levels_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    weights_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    horizons_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    gates_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamp NOT NULL DEFAULT now(),
    CONSTRAINT uq_ontology_version_key UNIQUE (version_key)
);

CREATE INDEX IF NOT EXISTS ix_ontology_version_version_key ON timeline_v4.ontology_version (version_key);
CREATE INDEX IF NOT EXISTS ix_ontology_version_status ON timeline_v4.ontology_version (status);

CREATE TABLE IF NOT EXISTS timeline_v4.entity_registry (
    id serial PRIMARY KEY,
    entity_id varchar(180) NOT NULL,
    canonical_title_id bigint NULL,
    canonical_title text NULL,
    surface text NULL,
    primary_type varchar(50) NOT NULL DEFAULT 'CONCEPT',
    types_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    primary_domain varchar(80) NOT NULL DEFAULT 'Society & People',
    domains_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    aliases_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    document_frequency integer NOT NULL DEFAULT 0,
    specificity double precision NOT NULL DEFAULT 0.5,
    ontology_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_entity_registry_entity_id UNIQUE (entity_id)
);

CREATE INDEX IF NOT EXISTS ix_entity_registry_entity_id ON timeline_v4.entity_registry (entity_id);
CREATE INDEX IF NOT EXISTS ix_entity_registry_canonical_title_id ON timeline_v4.entity_registry (canonical_title_id);
CREATE INDEX IF NOT EXISTS ix_entity_registry_primary_type ON timeline_v4.entity_registry (primary_type);
CREATE INDEX IF NOT EXISTS ix_entity_registry_primary_domain ON timeline_v4.entity_registry (primary_domain);
CREATE INDEX IF NOT EXISTS ix_entity_registry_ontology_version ON timeline_v4.entity_registry (ontology_version);

CREATE TABLE IF NOT EXISTS timeline_v4.entity_alias_map (
    id serial PRIMARY KEY,
    from_entity_id varchar(180) NOT NULL,
    to_entity_id varchar(180) NOT NULL,
    alias_kind varchar(40) NOT NULL DEFAULT 'promotion',
    confidence double precision NOT NULL DEFAULT 1.0,
    audit_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    ontology_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    CONSTRAINT uq_entity_alias_pair UNIQUE (from_entity_id, to_entity_id)
);

CREATE INDEX IF NOT EXISTS ix_entity_alias_map_from_entity_id ON timeline_v4.entity_alias_map (from_entity_id);
CREATE INDEX IF NOT EXISTS ix_entity_alias_map_to_entity_id ON timeline_v4.entity_alias_map (to_entity_id);
CREATE INDEX IF NOT EXISTS ix_entity_alias_map_alias_kind ON timeline_v4.entity_alias_map (alias_kind);
CREATE INDEX IF NOT EXISTS ix_entity_alias_map_ontology_version ON timeline_v4.entity_alias_map (ontology_version);

CREATE TABLE IF NOT EXISTS timeline_v4.taxonomy_candidate (
    id serial PRIMARY KEY,
    candidate_kind varchar(40) NOT NULL,
    candidate_key varchar(160) NOT NULL,
    label text NOT NULL,
    parent_key varchar(160) NULL,
    status varchar(30) NOT NULL DEFAULT 'pending',
    mention_count integer NOT NULL DEFAULT 1,
    examples_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    proposed_by varchar(80) NOT NULL DEFAULT 'llm',
    ontology_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_taxonomy_candidate_key UNIQUE (candidate_kind, candidate_key)
);

CREATE INDEX IF NOT EXISTS ix_taxonomy_candidate_candidate_kind ON timeline_v4.taxonomy_candidate (candidate_kind);
CREATE INDEX IF NOT EXISTS ix_taxonomy_candidate_candidate_key ON timeline_v4.taxonomy_candidate (candidate_key);
CREATE INDEX IF NOT EXISTS ix_taxonomy_candidate_parent_key ON timeline_v4.taxonomy_candidate (parent_key);
CREATE INDEX IF NOT EXISTS ix_taxonomy_candidate_status ON timeline_v4.taxonomy_candidate (status);
CREATE INDEX IF NOT EXISTS ix_taxonomy_candidate_ontology_version ON timeline_v4.taxonomy_candidate (ontology_version);

CREATE TABLE IF NOT EXISTS timeline_v4.mention_cache (
    id serial PRIMARY KEY,
    section_key varchar(80) NOT NULL,
    title_id bigint NOT NULL,
    heading_id bigint NOT NULL,
    entity_id varchar(180) NOT NULL,
    surface text NOT NULL,
    char_start integer NOT NULL,
    char_end integer NOT NULL,
    attribution varchar(40) NOT NULL DEFAULT 'core',
    salience double precision NOT NULL DEFAULT 0.5,
    confidence double precision NOT NULL DEFAULT 0.5,
    source varchar(50) NOT NULL,
    provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    parser_version varchar(80) NOT NULL,
    model_version varchar(80) NOT NULL,
    ontology_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    CONSTRAINT uq_mention_span_entity UNIQUE (section_key, char_start, char_end, entity_id)
);

CREATE INDEX IF NOT EXISTS ix_mention_cache_section_key ON timeline_v4.mention_cache (section_key);
CREATE INDEX IF NOT EXISTS ix_mention_cache_title_id ON timeline_v4.mention_cache (title_id);
CREATE INDEX IF NOT EXISTS ix_mention_cache_heading_id ON timeline_v4.mention_cache (heading_id);
CREATE INDEX IF NOT EXISTS ix_mention_cache_entity_id ON timeline_v4.mention_cache (entity_id);
CREATE INDEX IF NOT EXISTS ix_mention_cache_attribution ON timeline_v4.mention_cache (attribution);
CREATE INDEX IF NOT EXISTS ix_mention_cache_source ON timeline_v4.mention_cache (source);
CREATE INDEX IF NOT EXISTS ix_mention_cache_ontology_version ON timeline_v4.mention_cache (ontology_version);

CREATE TABLE IF NOT EXISTS timeline_v4.entity_passage_score (
    id serial PRIMARY KEY,
    entity_id varchar(180) NOT NULL,
    section_key varchar(80) NOT NULL,
    title_id bigint NOT NULL,
    heading_id bigint NOT NULL,
    components_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    blend double precision NOT NULL DEFAULT 0.0,
    provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_version varchar(80) NOT NULL,
    ontology_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_entity_passage_score UNIQUE (entity_id, section_key, ontology_version)
);

CREATE INDEX IF NOT EXISTS ix_entity_passage_score_entity_id ON timeline_v4.entity_passage_score (entity_id);
CREATE INDEX IF NOT EXISTS ix_entity_passage_score_section_key ON timeline_v4.entity_passage_score (section_key);
CREATE INDEX IF NOT EXISTS ix_entity_passage_score_title_id ON timeline_v4.entity_passage_score (title_id);
CREATE INDEX IF NOT EXISTS ix_entity_passage_score_heading_id ON timeline_v4.entity_passage_score (heading_id);
CREATE INDEX IF NOT EXISTS ix_entity_passage_score_blend ON timeline_v4.entity_passage_score (blend);
CREATE INDEX IF NOT EXISTS ix_entity_passage_score_ontology_version ON timeline_v4.entity_passage_score (ontology_version);

CREATE TABLE IF NOT EXISTS timeline_v4.time_anchor_registry (
    id serial PRIMARY KEY,
    time_id varchar(160) NOT NULL,
    kind varchar(40) NOT NULL,
    precision varchar(30) NOT NULL,
    calendar varchar(40) NOT NULL DEFAULT 'gregorian',
    label text NOT NULL,
    t_start double precision NULL,
    t_end double precision NULL,
    open_start boolean NOT NULL DEFAULT false,
    open_end boolean NOT NULL DEFAULT false,
    center double precision NULL,
    spread double precision NULL,
    confidence double precision NOT NULL DEFAULT 0.8,
    precision_score double precision NOT NULL DEFAULT 0.5,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    ontology_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_time_anchor_registry_time_id UNIQUE (time_id)
);

CREATE INDEX IF NOT EXISTS ix_time_anchor_registry_time_id ON timeline_v4.time_anchor_registry (time_id);
CREATE INDEX IF NOT EXISTS ix_time_anchor_registry_kind ON timeline_v4.time_anchor_registry (kind);
CREATE INDEX IF NOT EXISTS ix_time_anchor_registry_precision ON timeline_v4.time_anchor_registry (precision);
CREATE INDEX IF NOT EXISTS ix_time_anchor_registry_t_start ON timeline_v4.time_anchor_registry (t_start);
CREATE INDEX IF NOT EXISTS ix_time_anchor_registry_t_end ON timeline_v4.time_anchor_registry (t_end);
CREATE INDEX IF NOT EXISTS ix_time_anchor_registry_ontology_version ON timeline_v4.time_anchor_registry (ontology_version);

CREATE TABLE IF NOT EXISTS timeline_v4.fact_cache (
    id serial PRIMARY KEY,
    fact_id varchar(180) NOT NULL,
    section_key varchar(80) NOT NULL,
    title_id bigint NOT NULL,
    heading_id bigint NOT NULL,
    primary_entity_id varchar(180) NULL,
    other_entity_ids_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    assertion_kind varchar(80) NOT NULL DEFAULT 'section_assertion',
    assertion_text text NOT NULL,
    confidence double precision NOT NULL DEFAULT 0.5,
    provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    parser_version varchar(80) NOT NULL,
    model_version varchar(80) NOT NULL,
    ontology_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_fact_cache_fact_id UNIQUE (fact_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_cache_fact_id ON timeline_v4.fact_cache (fact_id);
CREATE INDEX IF NOT EXISTS ix_fact_cache_section_key ON timeline_v4.fact_cache (section_key);
CREATE INDEX IF NOT EXISTS ix_fact_cache_title_id ON timeline_v4.fact_cache (title_id);
CREATE INDEX IF NOT EXISTS ix_fact_cache_heading_id ON timeline_v4.fact_cache (heading_id);
CREATE INDEX IF NOT EXISTS ix_fact_cache_primary_entity_id ON timeline_v4.fact_cache (primary_entity_id);
CREATE INDEX IF NOT EXISTS ix_fact_cache_assertion_kind ON timeline_v4.fact_cache (assertion_kind);
CREATE INDEX IF NOT EXISTS ix_fact_cache_ontology_version ON timeline_v4.fact_cache (ontology_version);

CREATE TABLE IF NOT EXISTS timeline_v4.fact_time (
    id serial PRIMARY KEY,
    fact_id varchar(180) NOT NULL,
    section_key varchar(80) NOT NULL,
    title_id bigint NOT NULL,
    heading_id bigint NOT NULL,
    time_id varchar(160) NOT NULL,
    role varchar(80) NOT NULL DEFAULT 'occurred',
    confidence double precision NOT NULL DEFAULT 0.5,
    source varchar(50) NOT NULL,
    provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    ontology_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    CONSTRAINT uq_fact_time_role UNIQUE (fact_id, time_id, role)
);

CREATE INDEX IF NOT EXISTS ix_fact_time_fact_id ON timeline_v4.fact_time (fact_id);
CREATE INDEX IF NOT EXISTS ix_fact_time_section_key ON timeline_v4.fact_time (section_key);
CREATE INDEX IF NOT EXISTS ix_fact_time_title_id ON timeline_v4.fact_time (title_id);
CREATE INDEX IF NOT EXISTS ix_fact_time_heading_id ON timeline_v4.fact_time (heading_id);
CREATE INDEX IF NOT EXISTS ix_fact_time_time_id ON timeline_v4.fact_time (time_id);
CREATE INDEX IF NOT EXISTS ix_fact_time_role ON timeline_v4.fact_time (role);
CREATE INDEX IF NOT EXISTS ix_fact_time_source ON timeline_v4.fact_time (source);
CREATE INDEX IF NOT EXISTS ix_fact_time_ontology_version ON timeline_v4.fact_time (ontology_version);

CREATE TABLE IF NOT EXISTS timeline_v4.content_relatedness_cache (
    id serial PRIMARY KEY,
    focus_section_key varchar(80) NOT NULL,
    candidate_key varchar(180) NOT NULL,
    candidate_title_id bigint NULL,
    candidate_section_key varchar(80) NULL,
    components_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_score double precision NOT NULL DEFAULT 0.0,
    relevance_norm double precision NOT NULL DEFAULT 0.0,
    why_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    gates_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_version varchar(80) NOT NULL,
    ontology_version varchar(80) NOT NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_content_relatedness_focus_candidate UNIQUE (focus_section_key, candidate_key, ontology_version)
);

CREATE INDEX IF NOT EXISTS ix_content_relatedness_cache_focus_section_key ON timeline_v4.content_relatedness_cache (focus_section_key);
CREATE INDEX IF NOT EXISTS ix_content_relatedness_cache_candidate_key ON timeline_v4.content_relatedness_cache (candidate_key);
CREATE INDEX IF NOT EXISTS ix_content_relatedness_cache_candidate_title_id ON timeline_v4.content_relatedness_cache (candidate_title_id);
CREATE INDEX IF NOT EXISTS ix_content_relatedness_cache_candidate_section_key ON timeline_v4.content_relatedness_cache (candidate_section_key);
CREATE INDEX IF NOT EXISTS ix_content_relatedness_cache_raw_score ON timeline_v4.content_relatedness_cache (raw_score);
CREATE INDEX IF NOT EXISTS ix_content_relatedness_cache_relevance_norm ON timeline_v4.content_relatedness_cache (relevance_norm);
CREATE INDEX IF NOT EXISTS ix_content_relatedness_cache_ontology_version ON timeline_v4.content_relatedness_cache (ontology_version);

CREATE TABLE IF NOT EXISTS timeline_v4.processing_state (
    id serial PRIMARY KEY,
    title_id bigint NOT NULL,
    section_key varchar(80) NOT NULL DEFAULT '',
    area varchar(80) NOT NULL,
    state varchar(30) NOT NULL DEFAULT 'idle',
    expected_count integer NOT NULL DEFAULT 0,
    completed_count integer NOT NULL DEFAULT 0,
    pending_count integer NOT NULL DEFAULT 0,
    running_count integer NOT NULL DEFAULT 0,
    failed_count integer NOT NULL DEFAULT 0,
    detail text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    last_error text NULL,
    source varchar(80) NOT NULL DEFAULT 'derived',
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    CONSTRAINT uq_processing_state_scope_area UNIQUE (title_id, section_key, area)
);

CREATE INDEX IF NOT EXISTS ix_processing_state_title_id ON timeline_v4.processing_state (title_id);
CREATE INDEX IF NOT EXISTS ix_processing_state_section_key ON timeline_v4.processing_state (section_key);
CREATE INDEX IF NOT EXISTS ix_processing_state_area ON timeline_v4.processing_state (area);
CREATE INDEX IF NOT EXISTS ix_processing_state_state ON timeline_v4.processing_state (state);

ALTER TABLE IF EXISTS timeline_v4.redirect_map
    DROP CONSTRAINT IF EXISTS uq_redirect_from_title_id;

CREATE TABLE IF NOT EXISTS timeline_v4.agent_trace (
    id serial PRIMARY KEY,
    run_id varchar(100) NOT NULL,
    step_name varchar(100) NOT NULL,
    model_name varchar(300) NULL,
    status varchar(50) NOT NULL DEFAULT 'running',
    input_json jsonb NULL,
    output_json jsonb NULL,
    raw_response text NULL,
    error_text text NULL,
    latency_ms integer NULL,
    usage_json jsonb NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    completed_at timestamp NULL
);

CREATE INDEX IF NOT EXISTS ix_agent_trace_run_id ON timeline_v4.agent_trace (run_id);
CREATE INDEX IF NOT EXISTS ix_agent_trace_step_name ON timeline_v4.agent_trace (step_name);

CREATE TABLE IF NOT EXISTS timeline_v4.agent_job (
    id serial PRIMARY KEY,
    job_type varchar(100) NOT NULL,
    status varchar(30) NOT NULL DEFAULT 'pending',
    priority integer NOT NULL DEFAULT 100,
    title_id bigint NOT NULL,
    section_key varchar(80) NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    locked_by varchar(100) NULL,
    locked_at timestamp NULL,
    run_after timestamp NULL,
    last_error text NULL,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NULL,
    completed_at timestamp NULL,
    CONSTRAINT uq_agent_job_type_section UNIQUE (job_type, section_key)
);

CREATE INDEX IF NOT EXISTS ix_agent_job_job_type ON timeline_v4.agent_job (job_type);
CREATE INDEX IF NOT EXISTS ix_agent_job_status ON timeline_v4.agent_job (status);
CREATE INDEX IF NOT EXISTS ix_agent_job_priority ON timeline_v4.agent_job (priority);
CREATE INDEX IF NOT EXISTS ix_agent_job_title_id ON timeline_v4.agent_job (title_id);
CREATE INDEX IF NOT EXISTS ix_agent_job_section_key ON timeline_v4.agent_job (section_key);
