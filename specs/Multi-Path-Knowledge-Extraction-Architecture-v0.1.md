# Multi-Path Knowledge Extraction Architecture v0.1

Status: Draft for review (updated 2026-06-28: added relevance-trap defense (Sec. 4.6),
entity-noise reality check, deep-time weak-spot note, embedding-benchmark caveat,
canonical graph vocabulary)
Date: 2026-06-28
Applies to: Timeline article loading, graph buildout, enrichment, timeline projection, and agent insights

## 1. Summary

Timeline should not use one extractor, one LLM prompt, or one agent path for everything.

The benchmark results point to a multi-path architecture:

- deterministic parsing and graph construction first;
- CPU extractors for broad, cheap coverage;
- the deterministic temporal normalizer for conventional historical dates;
- custom deterministic normalizers for deep time and project-specific temporal shapes;
- embeddings for fast semantic relevance;
- GPU LLMs only for selected high-value work;
- Neo4j as the relationship/state backbone;
- Postgres as the canonical cache, provenance, and versioned fact store.

The core application must work without insights enabled. Article display, section parsing, L1 link extraction, L2 link discovery, and graph browsing should not require GPU activity.

LLM and agent work should be opt-in at the orchestration level, state-tracked, cancelable, and safe to rerun.

## 2. Why This Change Is Needed

The current system is trying to do too many enrichment jobs through a small number of agent paths. That creates several problems:

- GPU work can start before the deterministic graph is fully ready.
- LLM JSON fragility can stall orchestration.
- Some cached insights survive refreshes when they should be invalidated.
- Section status can show partial or running without actual CPU/GPU work happening.
- L1/L2 article processing can become numerically capped instead of graph-driven.
- The graph view and timeline projection need stable extraction state per node, not frontend inference.

The benchmark results also showed that each tool family has a clear lane:

- CPU entity models are good for broad coverage.
- HeidelTime is good for normal historical dates, but not deep-time expressions,
  and is declined for now because the Java/Windows subprocess dependency is too
  fragile for the current solo-workstation build.
- GPU LLMs are useful for temporal reasoning and explanations, but raw JSON is too brittle.
- Relation extraction is not production-ready yet.

Therefore the architecture should split work into explicit lanes instead of treating "agent enrichment" as one generic job.

## 3. Design Principles

1. Render first, enrich second.
   The core article body must display before L1/L2 enrichment begins.

2. Graph first, UI second.
   L1/L2 filtering, expansion state, and link traversal should come from Neo4j relationships and node state, not ad hoc frontend parsing.

3. CPU before GPU.
   Use CPU extractors widely because CPU is underutilized and GPU is the bottleneck.

4. LLMs reason over selected context, not raw article soup.
   The LLM should receive graph-shaped context, source snippets, extracted entities, dates, and relevance candidates.

5. Every extracted fact needs provenance.
   Store source title, section, character span where available, extractor version, model version, run id, confidence, and created timestamp.

6. Cache is versioned, not magical.
   Refresh should invalidate related derived rows by extractor/model/schema version.

7. No hidden work.
   Every node and section should expose extraction state: pending, running, completed, failed, skipped, stale.

8. Relation extraction is deferred.
   Current relation benchmark quality is not strong enough to write authoritative graph edges.

## 4. Benchmark-Informed Tool Decisions

### 4.1 CPU Entity Lane

Primary candidate: GLiNER2 base
Fallback candidate: GLiNER decoder large
Secondary experiments: GLiNER relex large, GLiNER PII large, NuNER zero

Benchmark conclusion:

- GLiNER2 base was the fastest successful CPU extractor at roughly 288 ms per passage
  (run `results/knowledge_benchmark_20260628_094412.json`; note it is not in the
  CPU summary table, which predates this run).
- GLiNER decoder large had the best cached CPU entity quality, but was slower at roughly 2086 ms per passage.
- NuNER zero was fast but produced **token-fragmented** spans ("Abraham"/"Lincoln"
  split, "City"/"of"/"Joy" as separate entities). Fragmentation actively poisons
  entity-overlap scoring, so it is **dropped from the shortlist** unless a proven
  adjacent-span merge is added first - not kept as a "fast recall" option.
- GLiNER relex is interesting for future relation experiments, but not graph truth.

**Reality check (important).** On the broad-ontology benchmark, the best CPU
extractor averages ~0.59 F1 (decoder-large) and GLiNER2 base ~0.52 - i.e. roughly
40% of gold entities are missed or mistyped. Notably, the served GPU LLM is **not
better** at entities (~0.52 F1) and is less reliable. So: CPU entities are a
**noisy first-pass signal**, which is exactly why (a) they are stored as
un-normalized mentions, (b) relevance scoring must not depend on entity overlap
alone (specificity-weighted overlap + embeddings + temporal/topical agreement -
Sec. 4.6), and (c) authoritative typing is deferred to normalization.

Architecture decision:

- Use GLiNER2 base as the broad first-pass entity extractor for L0, L1 introductions, and selected L2 introductions.
- Use GLiNER decoder large as a slower quality pass only when the section or article has high relevance (top-K gated; ~7x slower than base).
- Store entity mentions as extracted facts, not as authoritative ontology objects until normalized.
- Promote a CPU entity/temporal fact into graph/timeline only above a confidence
  band (per the scoring framework); below it, keep as a mention, not an authority.

### 4.2 Temporal Expression Lane

Primary conventional-date tool: project deterministic regex/date normalizer
Supplemental tools: deep-time parser, gated LLM fallback

HeidelTime probe results:

- Extracted normal historical dates well:
  - `February 12, 1809` -> `1809-02-12`
  - `1861` -> `1861`
  - `January 1, 1863` -> `1863-01-01`
  - `April 1865` -> `1865-04`
  - `July 16, 1945` -> `1945-07-16`
  - `August 1945` -> `1945-08`
  - `1985` -> `1985`
- Did not extract deep-time expressions:
  - `541 million years ago`
  - `66 million years ago`

**Deep time is the systematic weak spot across lanes.** On the deep-time fixture,
HeidelTime extracts nothing, and the best CPU *entity* extractor also bottoms out
(decoder-large drops to its worst, ~0.43 F1). So deep-time content is weak for both
the temporal and entity CPU lanes - the deterministic deep-time parser is
**load-bearing, not optional**, and deep-time sections should fall back to the GPU
lane more readily than modern-history sections.

**HeidelTime decision.** HeidelTime is **declined for now**. It needs a Java
subprocess per call, has a Windows path bug, and currently rides on the Neo4j
Desktop bundled JRE. The project normalizer already handles the important current
formats: full dates, month-year, seasons, eras, BC/BCE/AD/CE years, date ranges,
and deep-time expressions. Extend that deterministic normalizer first; route
residual ambiguous/high-value temporal cases to the gated GPU lane later. This
closes the Java dependency as a deliberate non-goal, not an unresolved gap.

Architecture decision:

- Use the deterministic project normalizer for conventional historical temporal expressions.
- Keep a deterministic project normalizer for:
  - centuries;
  - BCE/CE ranges;
  - dynasties/eras where rules exist;
  - approximate spans;
  - decade ranges;
  - date ranges with en dash/hyphen variants.
- Add a dedicated deep-time parser for geological expressions:
  - million years ago;
  - billion years ago;
  - geological periods;
  - fossil/extinction time anchors.
- Use the temporal LLM only for ambiguous or high-value sections after CPU/date lanes finish.

Implementation note:

- Do not integrate `py-heideltime` in the core path.
- Keep the old probe results as benchmark evidence only.
- If HeidelTime is revisited, it must be behind an optional adapter and must not
  become a startup/runtime requirement.

### 4.3 Embedding Similarity Lane

Primary tool: local CPU embedding pipeline

**Caveat: this lane is currently asserted, not benchmarked.** It is the only lane
with no numbers behind it, yet it is central to relevance. The fixtures already
carry 4-5 relevance checks each; run an **embedding-only relevance benchmark**
against them - especially the New Delhi relevance trap - before trusting embeddings
as a primary prioritization signal. Embedding similarity is expected to *fail* the
trap on its own (same place -> high cosine, low true relevance), which is precisely
why it is one signal among several, never the sole scorer (Sec. 4.6).

Architecture decision:

- Embed all L0 sections.
- Embed L1 introductions as soon as L1 links are parsed.
- Embed L2 introductions lazily and progressively.
- Use embeddings as a prioritization and relevance signal, not the only scorer.
- Store embeddings permanently with model/version metadata.

Embedding features should contribute to:

- L1 prioritization;
- L2 prioritization;
- timeline context promotion;
- "explain this" candidate selection;
- graph panel ordering;
- cache reuse across articles.

### 4.4 GPU Temporal and Relevance Lane

Primary served GPU model: Gemma E4B
Deferred temporal speed candidate: Gemma E2B with LangExtract
Experimental speed baseline: Qwen 3.5 2B
Deferred/experimental: NuExtract 1.5, Llama 3.2 3B, Ministral 3B

Benchmark conclusion:

- Gemma E2B with LangExtract was the best fast GPU temporal candidate in benchmarks.
- Gemma E4B remains the stronger relevance/temporal baseline and is the only
  default served model on 16GB VRAM.
- Qwen 3.5 2B is fast enough to revisit, but current relevance prompting is not usable.
- NuExtract 1.5 is serveable on vLLM but not a default lane under current prompts.
- Raw GPU JSON output remains fragile.

Architecture decision:

- Do not use GPU LLMs for broad extraction.
- Use GPU only after deterministic and CPU lanes create candidate context.
- Serve one model by default: Gemma E4B on `http://127.0.0.1:8101/v1`.
- Do not run E2B and E4B concurrently on 16GB VRAM; dual vLLM instances starve KV
  cache and make queue behavior unpredictable.
- Revisit E2B only if measured E4B latency blocks the queue after guided JSON and
  partial-accept are implemented.
- Use guided JSON decoding or equivalent schema-constrained extraction before productionizing new LLM lanes.
- Add partial-accept and repair so one malformed field does not discard the whole extraction.

### 4.5 Reasoning and Explanation Lane

Primary tool: local LLM over graph-shaped context

Architecture decision:

- The LLM should not scan entire articles blindly.
- It should receive:
  - selected L0 section summary;
  - candidate L1/L2 article intro;
  - extracted entities;
  - extracted temporal anchors;
  - embedding similarity;
  - link path;
  - existing graph relationships;
  - source snippets.
- Its output should be an explanation or WHY object, not raw source-of-truth fact storage unless grounded and validated.

### 4.6 Relevance and the Relevance Trap (defense)

This is the make-or-break of the product, and the benchmark has a fixture built to
test it: the **New Delhi relevance trap** - a chess tournament and a pollution
emergency that both mention "New Delhi" but are not topically related to each
other. The data shows why this is hard: entity F1 is **lowest** on this fixture
(0.27-0.56), so the entity signal is weakest exactly where precision matters most,
and embedding similarity is *inflated* by the shared place. No single signal can be
trusted here.

**The defense is multi-signal agreement, with no signal trusted alone:**

1. **Specificity-weight shared entities.** A shared low-IDF entity ("New Delhi",
   "United States") contributes far less than a shared high-IDF entity ("Manhattan
   Project"). A connection resting only on a common place is near-zero evidence.
2. **Require agreement across signals.** Relevance needs more than one of: a shared
   *high-specificity* entity, a real link path, temporal proximity, **and**
   topical/domain agreement. A shared place with **disagreeing domain or time** is
   a trap, not a connection.
3. **Penalize domain/topic mismatch explicitly.** Pollution-policy vs chess are
   different domains; the score is suppressed, not boosted, when domains diverge
   despite surface overlap.
4. **Embeddings are one signal, never the scorer** (Sec. 4.3) - they are most
   misleading exactly in the trap.
5. **Hard floor.** An admitted relation must be explainable by a non-trivial shared
   signal (high-specificity entity, link path, or temporal+topical co-occurrence) -
   not "same place." This is the scoring framework's floor `F`: zero-grounded-signal
   candidates are dropped, even under a no-blanks fallback.
6. **GPU adjudication for ambiguous high-stakes candidates.** The Gemma E4B
   relevance pass receives the *disagreement* signals (shared entity but divergent
   domain/time) as context and makes the final call - it is the adjudicator, not
   the first-line scorer.

**Acceptance test.** On the New Delhi fixture, the chess-tournament content and the
pollution content must **not** be scored as mutually relevant despite sharing "New
Delhi." Validate the multi-signal blend against the fixtures' relevance checks, and
demonstrate that an embedding-only baseline fails the trap while the blend passes.
(This maps directly to the Scoring/Gating/Insights framework: S_rel components +
specificity weighting + the hard floor.)

## 5. End-to-End Processing Flow

### 5.1 Article Load Core Path

This path always runs, even when insights are disabled.

1. Resolve article title and redirects.
2. Load article sections from Postgres.
3. Clean rendering artifacts, image captions, infobox spillover, and reference section behavior.
4. Render article body immediately.
5. Insert or update article and section nodes in Neo4j.
6. Mark section nodes as `content_loaded`.
7. Parse L0 section links.
8. Insert L1 article nodes and `(:Section)-[:LINKS_TO]->(:Article)` relationships
   (canonical vocabulary, Sec. 6.1).
9. Update L1 link extraction state.
10. Populate Graph tab panels from Neo4j.

No GPU should be touched in this path.

### 5.2 L1 Link Expansion Path

This path starts after L0 article render and L1 link parse.

1. For each L1 node, load introduction or first available section.
2. Store L1 intro text/provenance.
3. Parse links from L1 intro/selected sections.
4. Insert discovered L2 article nodes.
5. Insert `(:Article)-[:LINKS_TO]->(:Article)` relationships (the L1->L2 link;
   "L2" is a viewpoint, not a stored property - Sec. 6.1).
6. Mark node-level link extraction state:
   - not_started;
   - running;
   - complete;
   - failed;
   - stale.

This path remains deterministic and CPU-only.

### 5.3 CPU Enrichment Path

This path can run in core mode and should avoid GPU.

1. Run embeddings for L0 sections.
2. Run GLiNER2/spaCy entity extraction for L0 sections.
3. Run deterministic temporal normalizers for L0 sections.
4. Run embeddings for L1 introductions.
5. Run GLiNER2/spaCy entity extraction for L1 introductions.
6. Run deterministic temporal normalizers for L1 introductions.
7. Score L1 candidates using:
   - link path;
   - section source;
   - embedding similarity;
   - entity overlap;
   - temporal overlap;
   - topical/domain overlap;
   - number of supporting links from the core article.
8. Progressively run the same for L2 introductions.

### 5.4 GPU Enrichment Path

This path should be bounded, prioritized, and cancelable.

1. Select high-value candidates based on CPU scores.
2. Run Gemma E4B temporal extraction where CPU temporal extraction is incomplete or ambiguous.
3. Run Gemma E4B relevance/WHY reasoning with a different prompt/schema.
4. Store WHY objects and timeline context candidates with provenance.
5. Promote only validated, source-grounded temporal items into the timeline projection cache.

### 5.5 Refresh Path

Full refresh must clear more than article text.

It must invalidate or delete derived rows for:

- selected article sections;
- L1/L2 graph-derived relationships when parser version changes;
- entity extraction rows;
- temporal extraction rows;
- embedding rows when embedding model/version changes;
- related insights;
- timeline context projection;
- status rows;
- stale agent jobs.

Refresh should not kill vLLM. It should cancel application jobs and reset app-level state.

## 6. Storage Architecture

### 6.1 Neo4j Responsibilities

Neo4j should store graph shape and traversal state:

- Article nodes;
- Section nodes;
- link relationships;
- article-to-article relationships;
- extraction state per node;
- extraction state per relationship if useful;
- graph traversal provenance;
- UI graph panel source data.

Canonical graph vocabulary (shared with `Graph-View-and-API-Spec`):

- `(:Article)-[:HAS_SECTION]->(:Section)`
- `(:Section)-[:LINKS_TO]->(:Article)` - structural wikilink from a section.
- `(:Article)-[:LINKS_TO]->(:Article)` - structural article-to-article link.
- `(:Section|:Article)-[:RELATED_TO {method, components, ...}]->(:Article)` - a
  *scored* relatedness edge (shared entity / temporal / embedding / composite),
  written only when signals justify it.

This replaces the earlier `SECTION_LINKS_TO_ARTICLE` / `ARTICLE_LINKS_TO_ARTICLE` /
`LINKS_TO_L2` / `wikilink` names. The API `relation` attribute keeps its descriptive
sub-types (e.g. `shared_entity`, `temporal_overlap`) which map to `RELATED_TO`'s
`method`.

Important principle:

No node is inherently L1 or L2, and **`level` is not stored on the node**. L1/L2 is
a query viewpoint computed from graph distance to the currently open source article.

Recommended node identity:

- Article node key should use canonical Postgres title id.
- Section node key should use canonical Postgres title id plus section/heading id.
- Relationship keys should include source node id, target node id, link source section, parser version, and run id.

### 6.2 Postgres Responsibilities

Postgres should store canonical extracted/cached facts:

- cleaned sections;
- embeddings;
- entity mentions;
- temporal expressions;
- normalized time dimensions;
- scoring rows;
- related cache;
- timeline context cache;
- agent outputs;
- provenance;
- model versions;
- run ids;
- timestamps.

Neo4j may mirror high-value facts for traversal, but Postgres remains the canonical versioned cache.

## 7. Suggested New Tables Or Cache Shapes

Exact SQL can be finalized in implementation, but the architecture needs these logical stores.

### 7.1 `entity_mention`

Fields:

- article_title_id;
- section_id;
- mention_text;
- normalized_text;
- ontology_type;
- extractor;
- extractor_version;
- confidence;
- char_start;
- char_end;
- source_level_view;
- run_id;
- created_at;

### 7.2 `temporal_mention`

Fields:

- article_title_id;
- section_id;
- surface_text;
- normalized_value;
- normalized_start;
- normalized_end;
- temporal_type;
- granularity;
- extractor;
- extractor_version;
- confidence;
- char_start;
- char_end;
- run_id;
- created_at;

### 7.3 `candidate_relevance_score`

Fields:

- source_article_title_id;
- source_section_id;
- candidate_article_title_id;
- candidate_intro_section_id;
- graph_distance;
- embedding_score;
- entity_overlap_score;
- temporal_overlap_score;
- topic_score;
- link_support_score;
- final_score;
- confidence_band;
- score_version;
- run_id;
- created_at;

### 7.4 `extractor_run_state`

Fields:

- scope_type;
- scope_id;
- lane;
- status;
- started_at;
- completed_at;
- failed_at;
- error_message;
- extractor_version;
- model_version;
- run_id;
- stale_after_version;

This state must drive UI status instead of frontend guesses.

## 8. Orchestration Lanes

### 8.1 Core Lane

Purpose:

- article load;
- section render;
- link parse;
- graph insert.

Resource:

- DB + CPU only.

Must run:

- always.

### 8.2 CPU Broad Lane

Purpose:

- embeddings;
- GLiNER2 entity extraction;
- spaCy fallback/seed extraction;
- HeidelTime;
- deterministic temporal normalization.

Resource:

- CPU.

Must run:

- only when enrichment/insights are enabled, except embeddings can be separately enabled later.

### 8.3 GPU Reasoning Lane

Purpose:

- ambiguous temporal extraction;
- relevance explanation;
- WHY object generation;
- high-value timeline context.

Resource:

- vLLM/GPU.

Must run:

- only when insights are enabled and candidates have passed CPU prioritization.

### 8.4 Maintenance Lane

Purpose:

- stale job cleanup;
- refresh invalidation;
- cache version migration;
- failed job retry;
- VLLM health checks.

Resource:

- CPU/DB.

Must run:

- on app start and refresh.

## 9. UI Implications

### 9.1 Reader Tab

- Render article immediately.
- Show compact article-level processing status.
- Show section-level status in a dense scroller, not a tall stack.
- Full refresh should clear derived insights and timeline context, not just article body.

### 9.2 Graph Tab

- Source of truth should be Neo4j.
- Default graph should show L0 sections and only scored/relevant L1/L2 nodes.
- Panels should expose:
  - L0 sections;
  - L1 articles filtered by selected L0 section;
  - L2 articles filtered by selected L1 article or selected L0 path.
- Cards should show extraction state:
  - links parsed;
  - intro loaded;
  - embedded;
  - entities extracted;
  - temporal extracted;
  - relevance scored;
  - GPU insight available.

### 9.3 Timeline Tab

- Timeline remains one continuous chronological flow for now.
- Every item keeps:
  - source;
  - section;
  - domain/topic;
  - graph level view;
  - score;
  - confidence;
  - extraction source;
  - explanation availability.
- Reference sections should remain visible in the reader but excluded from enrichment and timeline promotion unless explicitly enabled.

## 10. Failure Handling

### 10.1 LLM JSON Failure

Required behavior:

- validate schema;
- accept valid partial facts;
- repair invalid fragments when possible;
- mark failed fields, not the whole article;
- never block core article rendering.

### 10.2 HeidelTime Failure

Required behavior:

- fallback to regex/deterministic normalizer;
- mark HeidelTime lane failed for that scope;
- keep other temporal extractors running;
- expose failure in state without stopping enrichment.

### 10.3 VLLM Contention

Required behavior:

- bounded worker pool;
- cancel stale app jobs;
- do not kill vLLM on app stop unless explicitly requested;
- expose active GPU job count;
- support insights disabled mode with no GPU calls.

## 11. Integration Plan

### Phase 1: Core graph-first baseline

- Ensure article render is independent of enrichment.
- Ensure L1 parse starts after article render.
- Ensure L2 parse starts after all L1 parse state reaches complete/failed.
- Store graph extraction state in Neo4j.
- Add one-time graph cleanup for old shape/state.

### Phase 2: CPU extraction lanes

- Integrate GLiNER2 base as broad entity extractor.
- Extend the deterministic temporal normalizer; keep HeidelTime declined/deferred.
- Add deterministic deep-time parser.
- Store entity and temporal mentions with provenance.
- Add extraction state rows.

### Phase 3: Relevance scoring

- Run embeddings for L0, L1 intro, and L2 intro.
- Compute candidate scores from graph, embedding, entity, temporal, topic, and link-support signals.
- Sort graph panels and timeline candidates by score.

### Phase 4: GPU gated enrichment

- Add candidate queue for high-score items.
- Add Gemma E4B temporal, relevance, and WHY lanes using separate prompts/schemas.
- Add guided JSON decoding or schema repair before making this default.

### Phase 5: UI status and controls

- Add global insights enable/disable.
- Add per-lane status display.
- Add rerun/refresh controls that invalidate derived facts.
- Ensure no GPU is touched when insights are disabled.

## 12. Open Questions

1. Should HeidelTime be revisited later?
   Recommendation: no for the current V4 build. Only revisit behind an optional adapter if deterministic + E4B fallback misses important temporal cases.

2. Should GLiNER2 entities be promoted directly into Neo4j?
   Recommendation: store mentions in Postgres first; mirror normalized high-confidence entities into Neo4j later.

3. Should deep-time parsing be one deterministic lane or part of temporal LLM fallback?
   Recommendation: deterministic lane first, LLM fallback second.

4. Should Qwen 3.5 2B be used for fast extraction?
   Recommendation: not yet. Revisit only after guided decoding and prompt redesign.

5. Should relation extraction be included in the first integration?
   Recommendation: no. Current benchmark quality does not justify it.

## 13. Final Recommendation

Adopt the multi-path architecture.

Immediate default stack:

- Core graph construction: deterministic parser + Neo4j.
- Broad entities: GLiNER2 base on CPU.
- Entity quality fallback: GLiNER decoder large on CPU.
- Conventional temporal extraction: deterministic regex normalizer.
- Deep time: custom deterministic parser.
- Similarity: stored embeddings.
- GPU temporal/reasoning: one served Gemma E4B model on port `8101`, with lane-specific prompts/schemas, gated by CPU scores.

Do not use raw GPU LLM extraction as the default source of truth until guided decoding, partial accept, and repair are implemented.

This gives Timeline a stable core, cheap broad coverage, controlled GPU usage, and a clean path to better agent reasoning without making article loading fragile.
