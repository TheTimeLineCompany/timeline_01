# Project Timeline -- System Specification

**Version:** 0.4 (draft) -- *supersedes v0.3; reconciled with the V4 build*
**Status:** Working draft for review. `(open)` marks an undecided point.
**Date:** 2026-06-25
**Scope:** Specification only.
**Companion specs:** `Ontology-and-Scoring-Framework-v0.1.md` (identity, relatedness
math), `Scoring-Gating-Insights-Framework-v0.1.md` (signals->scores->gates->insights,
stage runtime), `Enrichment-Pipeline-Spec-v0.1.md` (layered passes, optimization),
`Orchestration-Spec-v0.1.md` (graph-first LangGraph flow), and
`Graph-View-and-API-Spec-v0.1.md` (graph contract) -- all referenced, not duplicated.
`Multi-Path-Knowledge-Extraction-Architecture-v0.1.md` is the controlling
extraction/relevance architecture where it conflicts with older V3-style language
in this document.

> **What changed in v0.4.** (1) Folds in the **layered enrichment model (L0..L4)**
> from the enrichment spec -- notably a bounded-LLM **L1 "usable core"** so the
> first pass is no longer thin. (2) **Neo4j is confirmed as the graph backbone**
> (decision), and the doc tracks that V4 has not yet activated it. (3) Adds a
> **build-status / committed-gaps** reconciliation (Sec. 1.1). (4) Fixes the vLLM
> port and points scoring at the ontology framework.

> **Relationship to the prior projects (parts bin, not blueprint).**
> Three earlier builds exist. We take only what is genuinely good and fast and
> redesign what was slow or over-built. Do not get anchored on existing code.
>
> - **`01_Timeline_Project` (V1/V2):** good 3-column section **reader**, Neo4j
>   **graph explorer**, rule-based **temporal normalizer**, hierarchical **tag
>   taxonomy**, spaCy-on-CPU NER. **But its 4-pass discovery ran a synchronous
>   per-candidate LLM call at read time -- too slow.** Keep the parts; drop the
>   read-path LLM.
> - **`01_V3_Timeline_Project` (V3, newest):** the **agentic expansion** -- a
>   *supervised* multi-agent harness (bounded agents, JSON contracts, mediated
>   chains, auditable traces, live dashboard), the **entity-aware
>   attribution-before-extraction** flow, and the **one-topic-graph ->
>   reader + timeline projection** model. This is the harness + temporal design
>   we build on.
>
> **The unifying rule:** the **LLM does the real understanding** (entities,
> topics, relationships, time) as **background enrichment** into a shared graph;
> **reads serve that understanding from cache, instantly**, so no agent/LLM call
> sits on the read path. Seeds (spaCy, explicit-date regex) only fill the brief
> gap before the LLM result is cached. (This matches V3's philosophy: agents for
> judgment/review + validators before anything becomes renderable; "time-first
> source evidence, not generated storytelling.")

---

## 1. Purpose

A spec-driven-development reference: *what* the system does and *how the pieces
relate* -- not code. Every later change should trace to a statement here.

### 1.1 Build-status reconciliation (spec vs current V4)

The V4 code implements the read-path foundation well but deviates from this spec in
known ways. These are **committed gaps to close**, not spec changes:

| Area | Spec intent | V4 today | Action |
|---|---|---|---|
| **Graph store** | Neo4j is the shared topic-graph backbone (traversal, graph signal, explorer) | Active for the neutral article/section/link graph; L1/L2 are computed viewpoint distances, not stored node/edge labels | Continue using canonical `HAS_SECTION`, `LINKS_TO`, `RELATED_TO {method}`. Do not reintroduce stored L1/L2 properties. |
| **First pass** | Instant read + CPU core enrichment | Render-first core mode exists; CPU graph frontier, embeddings, and CPU entities run behind first paint | Keep GPU off the read path. Do not make LLM "usable core" mandatory for product feel. |
| **Entity-aware attribution** | Attribution-before-extraction guards contamination (Sec. 8) | deferred; current controlling extraction plan is Multi-Path CPU-first lanes + gated E4B later | Treat Sec. 8 as a later LLM phase. Do not enable before relevance Phase 3 and guided JSON/partial-accept exist. |
| **Timeline** | One unified, article-wide timeline (Sec. 7.3) | section-derived temporal rows exist; product timeline still needs article-wide projection/filter polish | Ship CPU-only unified timeline projection + level/domain/volume filters next. |
| **Relevance scoring** | Component-vector + granular anti-clumping (ontology Sec. 5.4) | migration started; embeddings now worker-backed, but embedding lane is not benchmarked against traps | Finish Phase 3: embedding-only benchmark, New Delhi trap acceptance test, hard floor `F`, common-place penalty. |
| **vLLM port** | `:8101` (Gemma-4-E4B) | config and `.env` are aligned to `:8101`; one served model is the default | Keep single E4B served model. Do not add E2B sidecar unless E4B latency is measured as blocking. |

The companion **enrichment spec** is the detailed plan for closing the first four.

---

## 2. Vision

Reading one Wikipedia topic becomes a **contextual, time-aware exploration**.
From a single topic, the system builds one understanding and projects it two ways:

- **Reader projection** -- the article(s) section-first, with a **Related
  Information** layer (L1/L2 linked context) per section: other articles that add
  real context, each with a source link and a short grounded "why."
- **Timeline projection** -- the topic's facts placed chronologically, with
  **contemporaneous events** from other articles (even ones that don't link to
  it) and place context.

Plus reader-controlled **lenses** (economics, science, climate, art...) to manage
volume and viewpoint. Everything local; nothing invented.

---

## 3. Core principles

1. **Strict grounding ("no invention").** Every fact, tag, date, edge, and
   insight traces to a source passage via provenance `{title_id, heading_id,
   char_start, char_end}`. The only generative latitude is a short *connective*
   "why this is relevant," which must paraphrase grounded content, never add
   claims. No provenance -> not stored, not shown. (Agents are instructed "do not
   invent facts; keep work bounded and auditable.")
2. **Instant read, progressive enrichment.** The article and all **cached**
   context render immediately. LLM-extracted understanding is computed in the
   background and cached on first encounter; until it lands, lightweight seeds
   fill the gap. **Nothing on the GPU blocks the first paint.**
3. **Fully local.** No cloud. Compute on the GPU rig; data on the local Postgres
   server reached over the direct Ethernet link. No external network dependency.
4. **Lazy materialisation.** Compute on first encounter, cache forever into the
   shared graph; the cache *becomes* the precompute. No long offline batch.
5. **Cache-first, LLM-grounded relevance.** The LLM produces the authoritative
   understanding (entities, topics, time); relevance is then ranked from signals
   computed over that **cached** understanding (tag overlap, graph paths,
   temporal overlap, embeddings) -- fast, with no LLM call on the read path. The
   ranking math is cheap; the *intelligence* it ranks over is the LLM's.
6. **CPU/GPU split, never sequential waiting.** Fast CPU work (parse, spaCy NER,
   rule-based dates, tag/path signals) runs independently of GPU work (agent
   chains, embeddings). No stage blocks on a slower one it doesn't depend on.
7. **Supervised agents, not loose chat.** Agents are bounded: fixed role, JSON
   output contract, mediated handoffs (each receives prior structured outputs),
   validators before anything becomes renderable, and an **auditable trace** of
   every run.
8. **Understand once, project twice.** Source text is understood a single time
   into a shared topic graph; the reader and timeline are two projections of it.
9. **Reader controls volume.** Lenses/filters reweight at read time over cached
   results.

---

## 4. Hardware & models (confirmed from current setup)

| Resource | Value |
|---|---|
| GPU rig | RTX 5070 Ti, 16 GB VRAM; 64 GB DDR5 |
| LLM | **Gemma-4-E4B-it-NVFP4**, served via **vLLM**, OpenAI-compatible at `http://127.0.0.1:8101/v1` |
| vLLM infra | `D:\02_VLLM_Infra` |
| Postgres | external Postgres 16+ instance with the local wiki source tables and Timeline cache schema |
| Wiki tables | `public."wiki_content_lookup_V4"`, `public."wiki_content_CSV_V4"` |
| Graph | Neo4j (timeline-v3 shared graph) |
| Seed NER (source only) | spaCy `en_core_web_sm` on CPU -- fast recall seed, **not** authoritative |
| Authoritative extraction | the **LLM entity-aware chain** (Sec. 8) -- real entities, topics, relationships, contextual/judged time |
| Explicit-date source | rule-based normalizer -- deterministic extraction of explicit date strings only |
| Embeddings | small sentence-embedder -> pgvector (optional recall) |

### 4.1 Model tiers ("careful model" explained)
Two job types, one or two models:
- **Workhorse (fast, high-volume):** entity/mention extraction, attribution
  classification, temporal extraction/parsing. Runs constantly.
- **Thinker / "careful" (low-volume):** the temporal **judge**, the **quality
  reviewer**, and the connective "why" phrasing -- judgment tasks that benefit
  from a more deliberate pass. On 16 GB this is the same Gemma-4-E4B by default;
  a larger model can be swapped in for **batched background** judging if quality
  needs it. Most "why" text should be **deterministic templates** (from matched
  signals), with the LLM reserved for non-obvious connections.
_(open: keep single Gemma-4-E4B for all tiers, or add a larger background thinker?)_

---

## 5. Architecture & data flow

```
            +----------------------------------------------+
            |  Topic Session  (one canonical topic id)       |
            |  -> Reader projection  /reader                  |
            |  -> Timeline projection /timeline               |
            |  -> Agent dashboard    /dashboard               |
            +---------------+------------------------------+
   read request             | (returns immediately)
                            v
            +----------------------------------------------+
            |  Read API (fast path, no GPU)                  |
            |  serve cache + CPU signals from shared graph    |
            +-------+-----------------------+---------------+
       cache hit <--+                       +--> miss/partial: enqueue
                    |                              |
                    v                              v
   +----------------------------+   +------------------------------------+
   | Postgres (remote, truth)   |   |  Enrichment Workers (async)         |
   |  wiki_*_V4, time_dimension,|   |  CPU pool: parse, spaCy, rule-dates,|
   |  cache tables, pgvector    |<->|   tag/path signals, redirect resolve|
   +----------------------------+   |  GPU pool: entity-aware agent chain,|
   +----------------------------+   |   "why" phrasing, embeddings (capped)|
   | Neo4j -- shared topic graph |<--|  Warm-up: prefetch L1 neighbors     |
   |  V3Topic/Article/Section/  |   +------------------------------------+
   |  Entity/TimeFact/Item/     |            writes graph + traces
   |  Context/FactAttribution   |
   +----------------------------+
```

**Read-time flow (target: sub-second first paint):**
1. Open a topic -> Read API returns Layer 0 (parsed article) and any **cached**
   tags/dates/related/timeline from the shared graph immediately.
2. For anything uncached, return what exists and **enqueue enrichment jobs**;
   the UI shows cached/seed results now, upgrades to LLM-grounded as it streams.
3. Workers run the entity-aware chain, write facts/edges/traces to the graph;
   next visit is instant.
4. A **warm-up worker** pre-enriches the current topic's L1 links when idle.

---

## 6. Data stores & data model

### 6.1 Postgres -- truth (existing, read-only, remote)
- `public."wiki_content_lookup_V4"` -- `heading`, `title_id`.
- `public."wiki_content_CSV_V4"` -- `title_id`, `heading_id`, `content` (wikitext).
Grain: **Article (`title_id`) -> Section (`heading_id`) -> wikitext**. Paragraph
granularity is derived by splitting clean section text; provenance spans still
point into section content.

### 6.2 Postgres -- cache / lookup + temporal (grows lazily)
All rows carry provenance + `model_version`/`parser_version`.
- `redirect_map` -- redirect stub -> canonical target (see Sec. 9).
- `section_clean` -- cached clean text + links per section (parse once).
- `section_tags` -- entities/places per section (spacy|llm, type, span, conf).
- `time_dimension` (+ `time_dimension_candidate`) -- **kept** from V1: canonical
  temporal rows with precision (day/month/season/year/era) and ISO bounds;
  unknown eras queued for review. A `section -> time_ref_id` join links facts.
- `section_embedding` -- pgvector vector per section (optional recall).
- `related_cache` -- `from_section_key`, `to_title_id`, `level`, `score`,
  `signals_json`, `why_text`, `why_source` (template|llm), `provenance`.
- `timeline_cache` -- per dated item on an article's unified timeline:
  `main_title_id` (the article whose timeline this item appears on),
  `source_title_id` + `source_heading_id` (provenance: where the item actually
  comes from), `time_ref_id`, `label`/`event_ref`, `lane` (domain layer),
  `level` (L0|L1|L2), `fact_type` (core|context|media), `place_ref`,
  `relevance_score`, `why_text` (context items), `provenance`. A context item's
  presence requires shared-tag + time-overlap with the main article and
  `relevance_score >= threshold` (Sec. 7.3).

### 6.3 Neo4j -- shared topic graph (V3 model, kept)
Nodes: `V3TopicSession`, `V3Article`, `V3Section`, `V3Entity`, `V3TopicCategory`,
`V3TimeFact`, `V3TimelineItem`, `V3ContextNode`, `V3FactAttribution`, plus
`TimePoint`/`TimeInterval`. Edges include `HAS_SECTION`, `MENTIONS`, `LINKS_TO`
(wikilink seed), `RELATED_TO`, `OCCURRED_AT/IN`, `CONTAINS`, `OVERLAPS`.
Keys: `Section.section_key = "{title_id}:{heading_id}"`; canonical time IDs
(`tp:1983-07-16`, `ti:year:1983`). Entities carry `pg_*` back-references.
Postgres stays truth + temporal + provenance; Neo4j is traversal + the shared
understanding all projections read from.

### 6.3.1 Graph as backbone + coverage ledger

The graph is not only what the views read -- it is the **backbone that tracks what
has been done and what remains**, and the map a crawler walks to finish the work.

**Node-level coverage state.** Every `Article`/`Section` node carries, per
enrichment layer, a `coverage` record:
`{ layer (seed|core|entities|temporal|related|review), state (missing|running|
done|stale|failed), model_version, ontology_version, updated_at }`. This is the
durable projection of V4's `orchestration/state.py` processing-state onto the
graph. A node is "done" only **for a given version**: bumping the embedding model
or taxonomy marks affected nodes `stale`, never silently done.

**Edges cache intrinsic relatedness (materialized frontier only).** A
`RELATED_TO` edge between two materialized nodes stores the **symmetric, intrinsic
components** from the ontology engine -- embedding cosine, graph distance
(`hops`,`path_count`), temporal proximity, entity overlap -- plus an
`embedding_state` and `components_version`. These are content-intrinsic: computed
once, **permanent and reused across every reading**. Distances are stored **only on
edges that exist** (the L1/L2 frontier actually visited), never all-pairs (O(N^2)
is infeasible); cosine is recomputable from the two stored vectors, so on-edge
distance is a speed cache, refreshed on a version bump.

**Division of labor (do NOT make Neo4j the job queue).** Postgres `agent_job`
remains the **execution queue** (atomic `FOR UPDATE SKIP LOCKED` claiming,
retries, backoff -- things a graph DB does poorly). The graph is the **ledger**:
the crawler reads node `coverage` to find gaps, enqueues Postgres jobs, and job
completion writes the resulting state back onto the node. One source of truth for
"is it done" (graph), one for "how it gets done" (Postgres).

### 6.3.2 Coverage crawler

A background crawler keeps the graph complete: it scans node `coverage` for
`missing`/`stale` layers and enqueues the matching jobs. It is **bounded by a
priority frontier** -- neighborhoods of recently-read articles first (the L1/L2
already materialized), not all of Wikipedia -- and yields to live reads. It is the
generalization of warm-up: over time, read paths converge to fully `done` (at the
current versions). Re-runs are idempotent and version-keyed.

### 6.3.3 Intrinsic-once vs insight-per-focus

What is computed when:
- **Intrinsic (compute once, permanent, reused):** parsing, embeddings, entity/
  temporal extraction, link structure, and the per-edge relatedness *components*
  and distances. These do not depend on which article you are reading.
- **Per-focus insight (on load, then cached):** picking a spine (the current
  article), then **ranking/gating/blending** the already-cached components
  relative to that spine, applying lenses, and accreting the timeline to the
  article's envelope. Deterministic re-ranking from cached components is instant;
  LLM insight (core digest, "why", attribution) stays **background + cached** and
  off the read path. So loading a new article re-uses all intrinsic data and only
  (re)computes focus-relative insight -- once -- exactly as intended.

### 6.4 Provenance object (uniform)
`{ title_id, heading_id, char_start, char_end, parser_version, model_version,
run_id }`. Nothing generated is stored/shown without one. `run_id` ties a fact to
its agent trace.

---

## 7. The reading experience

### 7.1 Layer 0 -- the article (CPU only, instant)
Render section-by-section from `section_clean`: headings, clean text, clickable
wiki links (redirect-resolved), infobox. No agents, no GPU.

### 7.1a Usable Core (enrichment Layer 1) -- one bounded LLM call
> Naming note: "L1/L2" elsewhere in Sec. 7 means link levels (1st/2nd hop). The
> *enrichment* layers (L0..L4, in the enrichment spec) are a different axis.

The first pass must be **usable and LLM-grounded, not just CPU seeds**. On article
open the system enqueues a single, bounded, highest-priority background call -- the
**core_digest** -- over a condensed article digest (lead + headings + high-signal
sentences + infobox). It returns the **core object**: canonical topic identity,
the real key entities/topics (superseding spaCy seeds), the key dated **spine**,
primary domains, and a short grounded summary. One GPU pass (single-digit seconds),
fully grounded in the digest, cached. Reads still serve instantly from cache; the
core simply replaces the thin seeds as soon as it lands. Deeper richness then
accretes via enrichment Layers L2+ (entity-aware extraction). See the **enrichment
spec** for the digest construction, layer table, and optimization.

### 7.2 Related Information (L1/L2) -- fast, cache-served
Selection/ranking is computed from cached LLM-extracted understanding (no LLM on
the read path) and returned instantly; phrasing is deferred:
1. **Candidates (CPU):** direct `[[ ]]` links = L1; links of L1 = L2; resolve
   titles via `redirect_map`.
2. **Rank (CPU):** score from cached signals only -- tag overlap, graph path
   signals (`path_count`, `max_hops`), temporal overlap, optional embedding
   similarity. Top-k (default 5). Returned immediately.
3. **"Why" (deferred):** prefer a **template** built from matched signals
   ("Shares *Germany* + *nuclear fission*; 2 link paths"). Only for non-obvious
   links (low lexical overlap, high embed sim) enqueue an LLM phrasing job; UI
   shows template now, upgrades when ready. Cached in `related_cache`.

### 7.3 Timeline projection -- ONE unified, article-wide timeline

**This replaces the current section-by-section timeline.** The reader is
organized section-by-section (correct for reading); the timeline must NOT be.
The timeline's organizing axis is **time**, and its scope is the **whole
article** at once, with related temporal items from L1/L2 accreting onto the same
axis over time. (Implementation note: the V4 endpoint
`GET /api/reader/article/{title}/timeline` currently returns events *grouped by
section* joining `timeline_v4.section_time -> time_dimension`; the change is to
return article-wide events ordered by time, each carrying provenance + lane +
level, and to keep appending L1/L2 context events. The cache-first contract and
source-agnostic rows (`rule_based_seed | spacy_seed | agent_temporal_v1 |
agent_temporal_reviewed_v1`) are unchanged.)

**What populates the timeline.**
- **Core (L0):** every dated fact from the *entire* main article (all sections),
  not one section at a time. Always shown (subject to filters).
- **Context (L1/L2):** dated facts from linked articles are added **only if BOTH**
  conditions hold (per the inclusion rule chosen):
  1. **Relationship + time overlap** -- the item shares >=1 entity/tag with the
     main article AND its normalized `time_dimension` bounds overlap the main
     article's temporal envelope (with a configurable margin); **and**
  2. **Relevance threshold** -- its relevance score >= a threshold tied to the
     **volume slider** (Sec. 7.3.3), so the user controls how much context appears.
  Context items keep **accreting** as more L1/L2 articles get enriched in the
  background; the timeline grows without re-asking.
- Source of the dates is irrelevant to rendering: regex seed, spaCy seed, or the
  agent temporal chain all write the same cached shape.

**Extraction quality.** The **rule-based normalizer** catches explicit dates as a
fast seed; the **entity-aware chain** (Sec. 8) does the authoritative temporal work
and, crucially, keeps **core** facts (about the topic) distinct from **context**
(L1/L2) and **media** (films/games -> an "In media" track), preventing the
Wellington-in-Napoleon's-timeline contamination.

#### 7.3.1 Layout -- continuous chronological (v1); swimlanes deferred
> **v1 render = one continuous, article-wide chronological flow. Swimlanes are
> deferred to a later visualization layer** (build the projection + scoring first).
> The data still carries a `lane` (domain) per item, so swimlanes later are a pure
> render change over the same lane-tagged projection -- no data rework.
- **Vertical** time axis (build on the existing core timeline UI; do not
  reinvent it). Zoom from millennia down to a day. **One continuous track**, items
  in chronological order (not split into lanes yet).
- **Per-item compact metadata** carried on each card (and used by filters now,
  swimlanes later): source `{article -> section}`, level (L0/L1/L2), domain/`lane`
  tag(s), relevance score, confidence, explain action.
- **Level and track via styling:** source level and fact track (core / context /
  media) shown by styling (L0 solid, L1 lighter, L2 outlined; media muted).
- **Provenance on every item:** the source badge `{article} -> {section}`; click
  opens that exact section in the Reader (shared state already wires this).
- **Multi-domain events:** keep a primary domain + secondary memberships in the
  data (for filtering now, and swimlane placement when that layer lands).

#### 7.3.2 Domain layers (filters now; swimlanes later)
Domains tag every item and drive **filters** in v1 (and **swimlanes** when that
render layer lands). Derived from the tag taxonomy (Sec. 16). Proposed set:
Politics & Government; War & Military; Science & Discovery; Technology &
Engineering; Economy & Finance; Arts & Culture (literature, music, visual, film);
Religion & Philosophy; Society & People (births, deaths, personal milestones);
Law & Treaties; Nature, Environment & Climate; Health & Medicine; Exploration &
Geography; Media & Portrayals (the "In media" track). A timeline event's lane is
its dominant tag category mapped to one of these. _(open: confirm/trim this set.)_

#### 7.3.3 Controls (how the user manages quantity/type)
All applied at read time over cached data, instant:
- **Domain layer toggles** -- filter items by domain (finance, art, technology...);
  these become swimlane show/hide when that render layer lands.
- **Level toggles** -- Main (L0) / L1 / L2. **Default = core-first: L0 only**, then
  the user expands to L1, then L2.
- **Volume / relevance slider** -- raises/lowers the context inclusion threshold
  (Sec. 7.3 condition 2), directly controlling how many L1/L2 items appear.
- **Place / region filter** -- restrict by where events occurred.
- **Time-range / zoom** -- focus a window on the vertical axis.
- _(optional)_ **Date-precision filter** -- exact dates vs approximate/era.

---

## 8. Entity-aware extraction (the quality core, kept from V3)

The most valuable idea from V3: **classify *who* a fact is about *before*
extracting time/relationships**, so facts don't get mis-attributed. Runs as
**background enrichment**, writing to the shared graph; never on the read path.

> **Deferred controlling note.** This V3-style chain is a later LLM phase, not the
> current default extractor. The controlling extraction plan is now the
> Multi-Path architecture: deterministic temporal extraction, GLiNER2 broad CPU
> entities, decoder-large gated CPU precision, embeddings, then one gated Gemma
> E4B lane after relevance Phase 3 and guided JSON/partial-accept are in place.
> Until this chain lands, timeline facts are section-attributed rather than fully
> entity-attributed, so contamination risk is mitigated by relevance gates but not
> eliminated.

**Phase 1 -- Entity resolution.** `topic_identifier` fixes the canonical topic
(id, name, type, aliases, distinguishing attrs). `entity_mention_extractor` tags
every mention per snippet (span, type, is_core_topic, coref, wikilink).

**Phase 2 -- Attribution (critical).** `fact_attribution_classifier` labels each
fact's relation to the core topic and routes it:
- `core` -- L0 source + core topic is the subject -> **core pipeline**.
- `related_same_article` -- L0 + different subject -> context pipeline.
- `related_linked_article` -- L1/L2 source -> context pipeline.
- `meta_reference` -- films/books/games/portrayals -> context pipeline (In media).

**Phase 3 -- Parallel pipelines.**
- *Core:* `core_fact_extractor` -> `temporal_parser` -> `core_temporal_judge`.
- *Context:* `context_extractor` -> `context_temporal_linker`.

**Phase 4 -- Quality.** `attribution_quality_reviewer` validates no
cross-contamination before facts become renderable timeline items / context lane
/ media section.

This directly enforces grounding quality and gives the timeline its precision.

> **These phases are *capabilities*, not a fixed agent count.** The spec does not
> commit to N agents. Each capability above (topic identity, mention tagging,
> attribution, core/context extraction, temporal parse/judge, quality review) may
> be one agent or several merged into a single call -- that boundary is a tuning
> decision, not an architectural one. The non-negotiable is **attribution before
> extraction** and **review before render**; how many LLM round-trips implement
> them is chosen later by measurement (Sec. 10.1). The composition is configurable so
> agents can be split or merged without changing the contracts around them.

---

## 9. Redirect resolution (the parser fix)

**Problem.** Wikipedia redirect pages are stubs (`#REDIRECT [[Target]]`). The
current parser and title resolver have **no redirect handling**, so following a
link to an alias lands on a stub (breaking L1/L2 expansion and intro retrieval),
`#REDIRECT` leaks into text, and anchored/cased/underscored variants mis-resolve.

**Design.**
1. **Detect stubs:** content matching `^\s*#\s*REDIRECT(?:ION)?\s*:?\s*\[\[\s*
   ([^\]|#]+)` (case-insensitive; tolerate leading colon, anchors, trailing rcat
   templates). Capture target.
2. **`redirect_map` table:** `from_title_id -> to_title_id` + normalized heading;
   populate lazily on encounter and/or via a one-pass scan over `wiki_content_CSV_V4`.
3. **Resolve on lookup:** after matching a heading, if it's a redirect, follow to
   target with **loop detection + max depth (~5)**; cache the result.
4. **Normalize link targets before lookup:** strip `#anchor`; `_`<->space; trim;
   uppercase first letter; drop interwiki/namespace prefixes (`File:`, `Image:`,
   `Category:`, `Wikipedia:`, `Help:`, `Portal:`, `Template:`, `wikt:` ...). Same
   anchor-stripping in link extraction.
5. **Display:** never render a redirect stub -- resolve and load the target
   (optionally note "redirected from X").

Robust traversal is a prerequisite for the whole L1/L2 layer.

---

## 10. Agent harness (supervised, kept & extended from V3)

Bounded, auditable, **mostly background**. Keep the V3 harness shape:
- **`AgentSpec`** -- fixed `name`, `role`, `output_contract` (JSON keys),
  `system_prompt` ("do not invent; keep bounded and auditable").
- **Registry** of agents; **Runner** executes one agent (JSON-only output) and
  records a **trace** (`run_id`, model, input, messages, parse status).
- **Mediated chains** -- each agent receives original input + prior structured
  outputs; **not** an open group conversation. (V3's hill-climb chain pattern.)
- **Trace store + live dashboard** for auditability.

**Workers & tiers:**

| Worker | Tier | Path | Role |
|---|---|---|---|
| Parser | CPU | read-safe | wikitext -> `section_clean` (once) |
| Redirect resolver | CPU | read-safe | maintain/consult `redirect_map` |
| Seed tagger (spaCy) | CPU | read-safe | fast **seed** entities/places -> `section_tags` (source only, superseded by chain) |
| Explicit-date normalizer | CPU | read-safe | rule-based **explicit** dates -> `time_dimension` (source only) |
| Embedder | CPU/GPU | background | section vector -> pgvector (a **worker**, not a manual script; populates `S_embed`) |
| Candidate ranker | CPU | read-safe | L1/L2 + signal scoring -> `related_cache` |
| **Core digest (L1)** | GPU | background, **highest priority** | one bounded call -> topic/entities/spine/summary (Sec. 7.1a) |
| Entity-aware chain (L2) | GPU | background, batched | Sec. 8 attribution + extraction + review |
| Why-phraser | GPU | background | non-obvious "why" text, capped |
| Timeline builder | CPU | background | article-wide aggregation + L1/L2 accretion (shared-tag + time-overlap + relevance threshold), lane/level tagging |
| Lens/filter | CPU | read-time | reweight cached results |

**Orchestration:** a durable job queue (`agent_job`) drained by a
**concurrency-bounded worker pool** (not one-at-a-time) so vLLM continuous-batches
concurrent requests; the GPU semaphore is sized for 16 GB. Jobs run by **priority
layer** (L1 core > high-value L2 > L3 > L4) and are **idempotent**, keyed
`(section_key, model_version)`. **Robustness is mandatory** (the enrichment spec
details it): guided/JSON-schema decoding, tolerant parse + repair, **partial-
accept** (keep valid facts, never fail a whole section for one bad row), backoff +
jitter, pooled keep-alive client. **Graceful degradation:** if the GPU saturates,
reads still return cache + seeds + template "why"; LLM enrichment backfills later.
**Warm-up:** when idle, pre-enrich (core + high-value sections of) the current
topic's neighbors, yielding to live reads. Full layer table, batching/chunking,
triage, and tuning knobs live in `Enrichment-Pipeline-Spec-v0.1.md`.

### 10.1 Configurable composition + measurement (tune agent count later)
The number of agents is **not fixed by this spec** -- it is an outcome of
measurement. Two requirements make that possible:

- **Capability composition.** The chain is defined as an ordered set of
  capabilities (Sec. 8) behind stable input/output contracts. A "step" may map to one
  agent or bundle several capabilities into a single LLM call. Splitting or
  merging steps must not change the contracts of neighbouring steps, so the
  composition can be reconfigured (ideally by config, not code rewrite).
- **Per-step instrumentation.** Every run records, in the trace: `agent/step
  name`, `model`, **latency_ms**, **prompt/`completion` tokens**, parse success,
  validation pass/fail, and a quality flag (e.g. reviewer rejections,
  contradicted spans). This extends the existing trace store.

This yields an **optimization loop**: run a topic set, read per-step latency +
token cost against output quality (attribution accuracy, reviewer rejection rate,
grounding-check failures), then **merge low-value steps or split overloaded ones**
to maximise *output per unit time* on the 16 GB rig. The agent count is whatever
that loop converges on -- revisited as models or data change.

---

## 11. Grounding validation

Cheap, so it's in. (1) **Structural:** every stored fact needs a resolvable
provenance span + `run_id`; else rejected at write. (2) **Attribution review:**
`attribution_quality_reviewer` already gates cross-contamination (Sec. 8). (3)
**Connective-text check** for LLM "why": validate that every named entity in the
sentence appears in the cited source/candidate intro; unsupported sentences drop
back to the template. _(open: add an optional second-model check later.)_

---

## 12. Local web app

**Three** synchronized projections off one **Topic Session** (shared browser topic
state keeps them aligned) -- understand once, project thrice:
- **Reader** `/reader` -- section-first; center = content, right = Related Info
  (instant from signals, "why" upgrades async), left reserved (TOC/provenance).
- **Timeline** `/timeline` -- **one unified, article-wide** timeline (Sec. 7.3),
  NOT section-by-section: vertical time axis, **continuous chronological flow
  (swimlanes deferred)**, core facts plus accreting L1/L2 context, every item
  badged with source article -> section, controlled by domain/level/volume
  filters. Default core-first.
- **Graph** `/graph` -- a first-class view over the Neo4j backbone (Sec. 6.3.1).
  **Central spine = the current article's sections**; **L1 and L2 article nodes**
  radiate outward. Each node renders its **coverage status** (per-layer:
  missing / running / done / stale / failed) so the graph doubles as a live map of
  what is enriched and what is pending; edges show the cached relatedness
  (embedding / graph distance / temporal). Clicking a node opens it in Reader/
  Timeline; the **coverage crawler** (Sec. 6.3.2) can be watched filling gaps here.
- **Agent dashboard** `/dashboard` -- live agents, chain steps, traces, queue depth.

Lens/filter controls are global, instant over cached data. Stack: React/Vite +
FastAPI. _(open: panel layout; current-section detection -- scroll vs click.)_

### 12.1 Graph view -- rendering & visual encoding

**Stack (decided).** In-memory graph **model = graphology** (decoupled from the
renderer; gives algorithms + lets the renderer change); **renderer = Cytoscape.js**
(`concentric` layout gives the spine->L1->L2 rings; `fcose` for organic weighted
layout; styles bind to data). The graph is hydrated from the Neo4j-backed API.
The existing hand-rolled `SectionGraphCanvas` (manual positioning, 3-bucket
strength) is superseded by this; do not keep hand-rolling layout.

**Principle.** Weights/values/relevances live in the **data** (the ontology
relatedness components on edges + node coverage/type/domain), and the renderer only
**maps data -> visual channel**. Drive edge weight from the **continuous relevance
score**, not 3 buckets (consistent with the anti-clumping work).

| Data (graph/ontology) | Visual channel |
|---|---|
| blended relevance / edge weight | edge thickness |
| relevance (inverse) | force ideal length (more relevant = closer) |
| relation type (link / shared-tag / temporal) | edge color/style |
| confidence | edge opacity |
| source level (spine / L1 / L2) | concentric ring |
| entity specificity / centrality | node size |
| primary domain (lens) | node color |
| **coverage status per layer** (missing/running/done/stale/failed) | **node ring/badge (the ledger)** |
| type (person/place/org/event) | node icon |
| importance + zoom | label visibility (declutter) |

Full node/edge JSON contract, connection rules (incl. back-edges to L0), endpoints,
and the color palette live in `Graph-View-and-API-Spec-v0.1.md`.

---

## 13. Non-goals (for now)

No cloud; no long offline batch (warm-up + lazy cache instead); no revision
history; no editing of source content; no invented/model-memory content on any
surface; **no LLM call on the read critical path**; no loose multi-agent chat.

---

## 14. Open questions

1. Freeze the **v1 tag taxonomy** (Sec. 16 + ontology framework) and add PLACE->geo id.
2. **Model tiers** (Sec. 4.1): single Gemma-4-E4B, or add a larger background thinker?
3. **Redirect coverage** -- are stubs present as rows in `wiki_content_CSV_V4`, or
   needed from another source? (Sec. 9)
4. Grounding **second-model check** needed, or is review + entity-overlap enough?
5. UI panel layout + current-section detection (Sec. 12).
6. vLLM **guided-decoding support** for Gemma-4-E4B-NVFP4 (enrichment spec Sec. 15)
   -- confirm, else lean on repair + partial-accept.
7. Target **time-to-usable-core** and acceptable background **GPU duty cycle**.

*Resolved since v0.3:* identity = canonical article + type layer; taxonomy =
fixed-top-level + reviewed growth; scoring = component-vector + default blend;
temporal = full deep-time intervals+points (all in the ontology framework).
Embeddings = **in v1** (a worker populates pgvector; embedding is the granularity
backbone). Neo4j = **activated as the graph backbone**. Agent count = measured,
not fixed (Sec. 10.1).

---

## 15. Phased roadmap

> **Status (2026-06-25):** Phases 0-2 are largely **built in V4** (foundations,
> CPU seeds, cache-served related, durable `agent_job` queue, a minimal temporal
> agent). The near-term priority order is now driven by the **enrichment spec**
> and the Sec. 1.1 gaps; the lettered items below are inserted accordingly.

- **Phase 0 -- Foundations & speed baseline.** [built] Cache tables,
  provenance+`run_id`, durable queue, **redirect resolution**, V4-table wiring.
- **Phase 1 -- Seed tags + explicit dates (CPU).** [built] spaCy seeds +
  rule-based explicit-date normalizer, cached (sources, not authoritative).
- **Phase 2 -- Related Information (cache-served).** [built/migrating] L1/L2 +
  signal ranking + template "why"; migrating to the component-vector scorer.
- **Phase 2.5 -- Enrichment robustness (unblock breakage).** Guided/JSON-schema
  decoding, tolerant parse + **partial-accept** + repair, pooled client, backoff.
  Deliverable: agent jobs stop failing in storms.
- **Phase 2.6 -- Concurrency & batching.** Worker **pool** + vLLM batch tuning +
  priority queue + section triage. Deliverable: deep pass no longer serial-slow.
- **Phase 2.7 -- L1 Usable Core.** `core_digest` job (Sec. 7.1a). Deliverable:
  first pass is LLM-grounded and usable in seconds.
- **Phase 2.8 -- Activate Neo4j backbone.** Write the topic graph (sections,
  entities, time, links) into Neo4j from the section cache + entity-aware chain;
  source `S_graph` and the explorer from it. Deliverable: the committed graph
  store is live (Sec. 1.1).
- **Phase 3 -- Entity-aware chain = authoritative extraction (background).** Sec. 8
  capabilities (attribution + entity/topic/time extraction + quality review)
  writing to the shared graph; supersedes the Phase-1 seeds. **Ship with per-step
  instrumentation (Sec. 10.1) from day one** -- latency, tokens, quality flags in the
  trace. Deliverable: clean, LLM-grounded core-vs-context facts, auditable and
  *measurable* in the dashboard.
- **Phase 4 -- Unified timeline (Sec. 7.3).** Switch the timeline endpoint from
  section-grouped to **article-wide, time-ordered**; render a **continuous
  chronological flow (swimlanes deferred to a later layer)**, core-first with
  L1/L2 expand-on-demand; accrete L1/L2 context under the shared-tag +
  time-overlap + relevance-threshold rule (with the no-blanks floor); per-item
  source-article -> section badges + score/confidence/domain metadata;
  domain/level/volume/place filters. Deliverable: the unified timeline tab.
- **Phase 4b (later) -- Swimlane render.** Layer domain swimlanes over the same
  lane-tagged projection once it is stable. Pure render; no data rework.
- **Phase 5 -- LLM "why" + grounding validation.** Background phrasing for
  non-obvious links + checks.
- **Phase 6 -- Embedder worker + granular scoring.** Populate pgvector via a
  worker and finish the **component-vector / anti-clumping** scorer (ontology
  Sec. 5.4): specificity-weighting, smooth saturation, embedding backbone,
  set-relative normalization. Deliverable: granular relevance, fewer ties.
- **Phase 7 -- Lenses & filters.** Viewpoint reweighting, interest + place filters.
- **Phase 8 -- Warm-up & agent-count optimization.** Idle prefetch, concurrency
  tuning, and the **output-vs-time loop (Sec. 10.1)**: use the Phase-3 instrumentation
  to merge/split capabilities and settle the agent count empirically.
- **Phase 9 -- Graph backbone, ledger & view.** Write node `coverage` + edge
  relatedness/distance into Neo4j (Sec. 6.3.1); build the **coverage crawler**
  (Sec. 6.3.2, bounded frontier, version-aware); ship the **`/graph` view** with
  per-node status. Couples with Phase 2.8 (Neo4j activation). Deliverable: the
  third view + a self-completing graph that reuses intrinsic data across reads.

---

## 16. v1 tag taxonomy (consolidated, to freeze)

Main categories + subtypes (each tag carries a ranking weight; PLACE tags also
normalise to a geo id for the timeline's spatial view):
- **PERSON** -- political, military, religious, scientific, artistic,
  philosophical, economic, family, other
- **PLACE** -- country, city, region, building, battlefield, institution, other
- **ORG** -- political, military, religious, economic, educational, secret, other
- **EVENT** -- war, battle, political, economic, cultural, natural, personal, other
- **CONCEPT** -- political, philosophical, scientific, technological, economic,
  religious, legal, other
- **WORK** -- book, art, document, speech, scientific, other
- **TIME** -- date, period, duration

**Lenses** are weighting profiles over these subtypes (economics -> ECONOMIC
subtypes + ORG:ECONOMIC + EVENT:ECONOMIC...), applied instantly at read time.

---

## 17. Glossary

- **Layer 0 / L1 / L2** -- focus article; directly linked context; one hop further.
- **Grounding** -- every fact traces to a source span; no model-memory facts.
- **Workhorse vs thinker ("careful")** -- fast model for high-volume extraction vs
  deliberate model for judging/reviewing/phrasing.
- **Instant read, progressive enrichment** -- cached understanding now (seeds fill
  the gap), LLM insight later; never block first paint on the GPU.
- **Entity-aware attribution** -- classify who a fact is about (core/context/meta)
  before extraction, preventing contamination.
- **Understand once, project twice** -- one shared topic graph -> reader + timeline.
- **Lazy materialisation** -- compute on first encounter, cache forever.
- **Lens** -- a tag-weighting viewpoint.
- **Connective "why"** -- the only generative output: a short grounded reason
  (template-first, LLM only when needed).
