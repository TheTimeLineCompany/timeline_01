# Orchestration Spec -- Graph-First Article Load (LangGraph)

**Version:** 0.1 (draft)
**Status:** Architecture for V4 orchestration. Formalizes tasks/027.
**Date:** 2026-06-25
**Encoding:** ASCII only.
**Companions:** `Timeline-Spec-v0.4.md` (app), `Enrichment-Pipeline-Spec-v0.1.md`
(layers/optimization), `Ontology-and-Scoring-Framework-v0.1.md` (scoring),
`Graph-View-and-API-Spec-v0.1.md` (graph contract).

> Grounding: V4 already has `langgraph==1.2.6`, `orchestration/article_pipeline.py`
> (a compiled `StateGraph`: `article_shell_ready -> graph_framework ->
> l0_enrichment_fanout`), `orchestrate_article_load` driving the read route, the
> `ProcessingState` ledger, and the durable `agent_job` queue. This spec
> formalizes that into the full graph-first flow and fixes the boundaries.

---

## 1. Principles

1. **Graph first, enrichment streaming.** The link graph becomes available fast
   (CPU/DB); embeddings/entities/temporal/scoring stream in behind it.
2. **Render never waits.** Reader paints from `section_clean` the moment L0 is
   cached; graph discovery and enrichment are all behind the first frame.
3. **Broad discovery, gated enrichment.** Resolving canonical IDs + inserting
   Neo4j edges can be broad (cheap). Caching intros + embedding + extracting is
   **gated** to a prioritized subset; the rest expand on demand.
4. **Scoring progressive, deepening relevance-gated.** Intro-level relevance
   decides how deep to enrich each article (Sec. 6).
5. **Durable + resumable; no "partial forever."** Every area reports
   pending/running/completed/attention with concrete counts and reasons, and
   survives restarts (Sec. 7).
6. **Control plane vs state store (the key boundary).** LangGraph orchestrates
   *flow*; the durable *truth* lives in Postgres + Neo4j (Sec. 2).
7. **Store ownership.** Neo4j owns relationship traversal + per-node coverage;
   Postgres owns durable text/artifacts/scores/queue/provenance. The UI reads
   backend projections, never infers graph structure from flat rows.
8. **GPU is the scarce resource.** All fan-out runs under a global priority order
   and a bounded GPU budget (Sec. 5).

---

## 2. Control plane vs state store (Hybrid boundary)

**Decision: Hybrid.** LangGraph is the control plane *and* keeps a checkpointer for
in-flight runs; the ledger remains authoritative for completion.

- **LangGraph (control plane).** Compiles the stage flow and the agent chains as
  `StateGraph`s; decides *what runs next*. A **Postgres-backed checkpointer**
  (`AsyncPostgresSaver`, same DB) persists **in-flight chain/run position** so a
  long multi-step chain can resume mid-way instead of restarting.
- **Postgres (durable truth).** `agent_job` (the execution queue, atomic
  `FOR UPDATE SKIP LOCKED` claiming, retries/backoff), `ProcessingState` (per-area
  ledger), and all caches/artifacts/scores.
- **Neo4j (graph + coverage ledger).** Relationships and per-node `coverage`
  (per-layer state + version), per the graph backbone spec.

**Authority rule (prevents two-sources-of-truth):**
- The **ledger/coverage is authoritative for "is it done."**
- The **checkpointer is authoritative only for "where is this in-flight run."** It
  is a *resume optimization*, never the record of completion.
- **On restart / reconcile:** trust the ledger for what is done; resume interrupted
  runs from checkpoints; **if a checkpoint conflicts with the ledger (ledger says
  done), the ledger wins** and the checkpoint is discarded. Then enqueue whatever
  the ledger shows missing/stale. A crash therefore costs at most a partial chain
  re-run, never lost or duplicated completion state.

LangGraph runs are otherwise **ephemeral and re-derivable**: the orchestrator can
always be re-invoked, read current coverage, and dispatch only the gaps.

---

## 3. The stage flow (LangGraph StateGraph)

Stages map to `ProcessingState` areas. Each stage is a node (or subgraph); edges
encode ordering and fan-out. Stages 3-6 fan out and run concurrently behind the
first render.

### Stage 1 -- Topic load & first render  (`area: article_load`, CPU/DB)
Resolve title (redirect-aware) -> cache/refresh clean L0 sections -> **render
Reader from `section_clean`** -> upsert Neo4j `(:V4Article)-[:HAS_SECTION]->
(:V4Section)` -> mark `article_load = completed`. No GPU.

### Stage 2 -- Link framework discovery  (`area: graph_framework`, CPU, BROAD)
Read `section_clean.links_json` -> resolve canonical neighbor IDs -> insert
`(:V4Section)-[:LINKS_TO]->(:V4Article)`. Cache L1 intros (first valid content
section) **for the retained set** (Sec. 6). Read retained neighbor intro links ->
resolve downstream article IDs -> insert `(:V4Article)-[:LINKS_TO]->(:V4Article)`
with source-section provenance. **Populate the Graph tab panels** from the graph
projection.
*Discovery (IDs + edges) is broad and cheap; it does not wait on the LLM.*

`L1` and `L2` are reader/query viewpoints computed from traversal distance to the
focused article. They are not stored as node labels, node properties, or special
relationship names in Neo4j.

### Stage 3 -- L0 enrichment fanout  (`area: l0_enrichment`, GPU+CPU, parallel)
As soon as L0 sections exist, queue CPU-safe work in parallel: per-section
**embeddings**, spaCy/GLiNER CPU entities, deterministic temporal extraction, and
graph-frontier expansion. LLM work such as core digest, temporal upgrades, and
related insight text is opt-in and must remain disabled in core mode. Store
artifacts in Postgres; mirror high-value facts + coverage into Neo4j. (Skip
non-content sections; clean text only -- Sec. 4 guard.)

### Stage 4 -- L1 intro enrichment  (`area: l1_intro_enrichment`, gated top-K)
For the retained top-K L1 (Sec. 6): **parse/clean intro first**, then embed ->
extract entities/time -> **compare L0 section vs L1 intro** using the component
vector (embedding + entity overlap + graph + temporal) -> **score/categorize L1**.

### Stage 5 -- L2 intro enrichment  (`area: l2_intro_enrichment`, gated by L1 strength)
L2 enrichment **cascades from the parent L1's strength -- it is not run per-L2.**
Only L2 under a **strong L1** is enriched, and the per-L1 L2 budget **scales with
that L1's relevance**: a high-relevance L1 enriches its top-M L2; a weak L1 enriches
few or none (its L2 stay link/graph-only until the user expands them). For the
selected L2, run the same loop and compare against the source L0 section and the
parent L1 (via graph adjacency) -> score/categorize. This spends embeddings/GPU only
along strong paths.

### Stage 6 -- Vertical deepening  (`area: deepening`, relevance-gated)
Only after intro relevance exists:
- **High relevance:** embed/enrich more content sections; promote temporal/context
  rows into the unified timeline.
- **Medium:** enrich a few top content sections.
- **Low:** keep link/graph-only until the user opens/asks.

### Timeline (cross-cutting)
Temporal extraction is **part of enrichment per node** (Stages 3-5), not a separate
late stage. `timeline_ready` is a **milestone** raised when enough L0 core temporal
facts exist to render the timeline projection; it keeps accreting as L1/L2 enrich.

---

## 4. Ordering guard (avoid poisoned scores)

Strict per-article order, enforced by stage edges:
`resolve -> cache/clean intro -> embed -> extract entities/time -> score`.
Never embed or extract on raw/unparsed text or mixed infobox/reference text; always
operate on `section_clean` content sections (`is_content_section`). Same for L2.

---

## 5. Concurrency, priority & backpressure

The fan-out (L0 sections + L1 intros + L2 intros + deepening + timeline + topic)
oversubscribes a single 16 GB GPU; the orchestrator enforces a budget.

- **CPU lane** (render, link discovery, seed tagging, graph writes) never waits on
  the GPU.
- **GPU lane**: a bounded **concurrency semaphore** drains `agent_job` by
  **priority** so vLLM continuous-batches (per the enrichment spec). Priority order:

  `L0 core+embeddings  >  L1 intro enrich  >  L1 scoring  >  L2 intro enrich  >
   L2 scoring  >  deepening  >  crawler backfill`

- **Backpressure:** if the GPU saturates, lower-priority stages stay queued; reads
  still serve cache + seeds. Robustness (guided decoding, partial-accept, backoff,
  pooled client) is mandatory per the enrichment spec.

---

## 6. Broad discovery vs gated enrichment

- **Discovery (broad, cheap):** resolve canonical L1/L2 IDs and insert Neo4j edges
  for the whole linked frontier; the Graph panels can show all link-only rows.
- **Enrichment (gated, cascading):** cache intros + embed + extract for **top-K L1**
  (by link prominence + early relevance), then enrich L2 **only under strong L1s**,
  with each L1's L2 budget **M scaled by that L1's relevance** (`M = f(L1 strength)`;
  weak L1 -> M=0, its L2 stay link-only). Relevance **propagates down the path** --
  path strength ~ the L0->L1(->L2) edge relevances -- so GPU is spent along strong
  paths, not sprayed across every L2. K and the M-scaling curve are tunable config.
- **On demand:** anything outside the gate (weak-path L2, low-tier sections)
  enriches when the user opens/expands it (lazy `/neighbors`) or when the idle
  coverage crawler reaches it.
- **Deepening tiers** (Stage 6) are likewise gated by intro relevance, so GPU goes
  where relevance is highest.

---

## 7. Durable state model & resumability

- **Areas (`ProcessingState`):** `article_load`, `graph_framework`,
  `l0_enrichment`, `l1_intro_enrichment`, `l2_intro_enrichment`, `deepening`,
  `timeline`. Each carries expected/completed/pending/running/failed counts +
  detail/reason.
- **Per-node coverage (Neo4j):** authoritative per-(article/section, layer,
  version) state, per the graph backbone spec.
- **Queue (`agent_job`):** the execution units; idempotent, version-keyed.
- **Global stages are a derived projection** (milestones) over per-node/per-area
  truth -- not a single linear enum.
- **Resumability:** on startup, **reclaim stale running locks**, reconcile the
  ledger, resume in-flight LangGraph runs from checkpoints, and enqueue gaps. "No
  partial forever": the coverage crawler (graph backbone) closes any remaining
  `missing`/`stale` at idle, version-aware.

---

## 8. Agent chains as LangGraph subgraphs (fold-in)

**Decision: one framework.** The entity-aware chain becomes a LangGraph subgraph,
invoked by Stages 3-5:

`topic_identifier -> entity_mention_extractor -> fact_attribution_classifier ->
[core pipeline | context pipeline] -> attribution_quality_reviewer`

- Each capability is a LangGraph **node**; the core/context split is a conditional
  **branch**; the reviewer is the gate before facts become renderable.
- The bespoke `AgentSpec`/registry/runner is **retired/thinned to a single-LLM-call
  helper** used by leaf nodes. But the **contracts, traces, and per-step
  instrumentation** (latency/tokens/parse/quality -- app spec Sec. 10.1) are kept,
  wired into LangGraph node callbacks, so the **agent-count optimization loop still
  works**. Capability composition (merge/split steps) is a graph-edit, not a
  rewrite -- which LangGraph makes cleaner.
- Robustness lives at the node level: guided JSON decoding, partial-accept, repair,
  backoff (enrichment spec Sec. 9).

---

## 9. Integration map

| Concern | Owned by | This spec's role |
|---|---|---|
| Stage flow + chains | LangGraph (`article_pipeline.py` + subgraphs) | defines the stages |
| Execution queue | Postgres `agent_job` | stages enqueue; pool drains |
| In-flight run state | LangGraph Postgres checkpointer | resume only |
| Completion ledger | `ProcessingState` + Neo4j coverage | authoritative |
| Work layers (L0..L4) | Enrichment spec | the jobs stages dispatch |
| Scoring/compare | Ontology framework | Stage 4-5 comparisons |
| Graph traversal/panels | Neo4j + Graph-View spec | Stage 2 output |

---

## 10. Mapping to current code & next steps

- **Done (027 Phase 1):** LangGraph installed; `article_pipeline.py` StateGraph
  (`article_shell_ready/graph_framework/l0_enrichment_fanout`); route enqueue moved
  into `orchestrate_article_load`; `graph_framework` + `l0_enrichment` areas shown.
- **Deltas from this spec:**
  1. Add the **Postgres checkpointer** + the reconcile/authority rule (Sec. 2).
  2. Split out a **graph-frontier worker** (broad discovery, no LLM) -- 027 Phase 2.
  3. Implement **gated enrichment fanout** with K/M caps + deepening tiers (Sec. 6).
  4. Add `l1_intro_enrichment`, `l2_intro_enrichment`, `deepening` areas and the
     **priority GPU pool** (Sec. 5) -- 027 Phases 3-4.
  5. **Fold the entity-aware chain into a LangGraph subgraph**; thin the bespoke
     runner; keep traces/instrumentation (Sec. 8).
  6. Move related-cache generation off the first frame (it currently mixes
     discovery + intro enrichment + embeddings + scoring -- too heavy for frame 1).

---

## 11. Open questions

1. K (top-L1) and M (top-L2) default caps, and the deepening relevance thresholds.
2. Checkpointer retention/GC policy (how long to keep completed run checkpoints).
3. Whether the graph-frontier worker also runs as the **idle crawler** (Sec. 7) or
   stays a separate job type.
4. Backpressure policy specifics: hard cap vs adaptive concurrency from telemetry.
