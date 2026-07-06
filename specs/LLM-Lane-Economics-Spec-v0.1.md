# LLM Lane Economics â€” Philosophy and Spec v0.1

Status: Draft for review
Date: 2026-07-05
Applies to: V4 GPU/LLM lanes (`temporal_extract_v1`, `related_l1_l2_explain_v1`, `core_digest_v1`, timeline-context promotion), worker runtime, and article-load fanout.
Companion: `Multi-Path-Knowledge-Extraction-Architecture-v0.1.md` (lane split), `Timeline-Spec-v0.4.md` Â§10.1 (capability composition), `Orchestration-Spec-v0.1.md`.

---

## Part 1 â€” Architectural Philosophy

### 1.1 The diagnosis this spec answers

V3 produced fast, plentiful insights. V4 is structurally better (durable queue, atomic claiming, retries, traces, honest state) but its LLM lanes are slow. The difference is not orchestration quality â€” it is **spend policy**:

- V3 treated the LLM as a **budgeted gap-filler**: deterministic extraction ran everywhere; the LLM was called only where determinism failed, capped at 8 calls per build, with tiny single-question prompts.
- V4 treats the LLM as a **per-section batch sweeper**: one temporal job per content section regardless of whether the deterministic lane already succeeded there, plus per-section related and context jobs, each carrying heavy fixed overhead (claim cycle, 3â€“4 commits, 6.5k-char prompt, inline CPU ranking), drained at concurrency 2.

Abraham arithmetic: V4 full-insight mode enqueues ~60â€“100 LLM jobs where V3 spent ~10 calls. Same GPU, same model class â€” hours versus minutes.

**The fix is not to abandon V4's structure. It is to run V3's economics on top of V4's rails.**

### 1.2 The principles

**P1 â€” The LLM is a scarce adjudicator, not a sweeper.**
Every LLM call must answer a question the deterministic/CPU lanes could not. If the rule-based normalizer already extracted a section's dates, a temporal LLM job on that section is waste. This is Multi-Path Â§5.4 ("select high-value candidates based on CPU scores") made mandatory at *enqueue time*, not at display time.

**P2 â€” Budgets are architecture, not tuning.**
Every LLM lane has an explicit per-article budget and a global in-flight bound. Unbudgeted lanes are bugs. Budgets are config, visible in status, and exhausting a budget is a recorded, honest state (`budget_exhausted`), never a silent stall. (V3's `timeline_agent_inference_limit = 8` is the ancestor; V4 generalizes it per lane.)

**P3 â€” Insight follows the reader's focus.**
The user experiences speed as *insight where they are looking*. LLM work is enqueued in two tiers:
- **Focus tier** (highest priority, small, immediate): the currently viewed section and its top-K promoted routes.
- **Sweep tier** (low priority, batched, interruptible): the rest of the article, drained only when the focus tier is empty â€” the V4 form of V3's "everything in one pass" and the spec's warm-up/crawler idea.
Loading a new article defers the previous article's sweep tier (already implemented in `_prioritize_active_article_jobs`; this spec extends it to tier semantics).

**P4 â€” LLM jobs are pure.**
An LLM job does exactly: read cached inputs â†’ one (or few) LLM calls â†’ parse â†’ persist â†’ one commit. It never computes CPU-lane work inline (ranking, entity extraction, embedding waits, article loading). If its inputs are missing, it reschedules itself behind the CPU job that produces them. Mixed jobs are why one "LLM job" costs 10â€“60s of non-GPU time today.

**P5 â€” Concurrency is sized to the server, not the worker.**
vLLM exists to continuous-batch. A drain loop that holds at most 2 requests in flight wastes the GPU. The worker fires N concurrent LLM jobs (asyncio semaphore), sized to E4B KV headroom on 16 GB. Throughput scales with in-flight requests until KV saturates; find N by measurement, start at 4.

**P6 â€” Schema-constrained output, partial accept.**
Guided JSON decoding (`guided_json`, already supported by `LocalLLMClient`) on every production lane. Parse failures burn concurrency slots and retries â€” the most expensive failure mode under P5. On partial validity, keep valid facts, mark bad fields; never fail a section for one bad row. (Restates enrichment-spec robustness as a precondition for raising concurrency.)

**P7 â€” One rich call beats a chain of thin calls.**
V3's insight density came from single calls returning rich objects. Where multiple lanes ask questions about the *same* context (focused section: dates + why-related + context flags), compose them into one call with one schema. Timeline-Spec Â§10.1 explicitly allows merging capabilities behind stable contracts; the default composition for the focus tier is **merged**, and for the sweep tier **packed** (multiple sections per call).

**P8 â€” Fixed overhead is part of the cost model.**
A call's cost = queue overhead + prompt tokens + decode + commits. When decode is small (900 tokens), overhead dominates â€” so reduce the number of calls (packing, merging) and commits per job (one success commit), before reducing per-call quality.

### 1.3 What stays from V4 (explicitly not changing)

- Durable `agent_job` queue, SKIP LOCKED claiming, retries/backoff, stale-lock recovery.
- Traces with latency/usage per run (this spec depends on them for budget tuning).
- Render-first: no LLM on the read path, core mode fully LLM-free.
- Honest status (`disabled`, not `queued`) â€” extended with `budget_exhausted` and `deferred_sweep`.
- Single served model (Gemma E4B) on one vLLM instance.

---

## Part 2 â€” Specification

### 2.1 Config additions (`app/core/config.py`)

```text
llm_focus_concurrency          = 3    # in-flight LLM calls for focus-tier jobs
llm_sweep_concurrency          = 2    # in-flight LLM calls for sweep-tier jobs
llm_max_inflight               = 4    # global semaphore (focus + sweep combined)
llm_temporal_budget_per_article = 10  # sweep-tier temporal LLM calls per article
llm_related_budget_per_article  = 12  # sweep-tier related-insight calls per article
llm_sections_per_temporal_call  = 5   # packing factor for sweep temporal
llm_focus_topk_routes           = 5   # promoted routes per focused section sent to LLM
llm_guided_json_enabled         = true
```

Priorities: focus tier 10â€“19; CPU lanes keep 30â€“55; sweep tier 60â€“90. (Numbers indicative; keep the existing ordering semantics.)

### 2.2 Temporal lane: gate at enqueue (P1, P2)

Change `enqueue_temporal_jobs` (called from `_node_l0_enrichment_fanout` in `article_pipeline.py`):

1. Before enqueueing, query deterministic coverage per content section: count of `section_time` rows from `rule_based_seed` / deterministic sources, plus an ambiguity flag (section contains temporal-ish regex hits â€” relative dates, deep time, era phrases â€” with zero normalized rows).
2. Enqueue `temporal_extract_v1` only for sections that are **uncovered** (zero deterministic anchors but temporal-ish text) or **ambiguous**. Fully covered sections get `processing_state` area `temporal` = `completed (deterministic)` â€” no job.
3. Apply `llm_temporal_budget_per_article` to the enqueued set, ordered by section value (`_section_value_score` already exists). Sections beyond budget get state `budget_exhausted` with the count, re-eligible on refresh or budget raise.

Acceptance: on a date-rich article (Abraham), temporal LLM jobs enqueued â‰¤ budget, and â‰¥60% of content sections resolve as `completed (deterministic)` with zero LLM calls.

### 2.3 Related lane: purity + cache-only inputs (P4)

Change `process_related_job` (`workers/related_agent.py`):

1. Replace `RelatedInfoService(session).get_related(section, refresh=False, ...)` with `read_cached_related(...)` **only**. No ranking, no candidate enrichment, no embedding waits inside the LLM job.
2. If no current-version `related_cache` rows exist: set `run_after = now + 60s`, ensure a `related_cache_v1` CPU job exists for the section, and return without consuming an attempt (or use a distinct `waiting_inputs` status). Never compute ranking inline.
3. Candidate context (`_candidate_context`) reads cached `section_clean` rows only; if a candidate has no cached sections, it is skipped in the prompt, not loaded inline.

Same rule applies to any future LLM lane: **inputs come from cache or the job reschedules.**

### 2.4 Worker concurrency (P5)

Change `TemporalAgentWorker.run` (or add a wrapping runner):

1. Claim up to `llm_max_inflight` jobs and process them as concurrent asyncio tasks, bounded by a semaphore; CPU-safe job types keep their own small bound (they contend for cores, not KV).
2. Each concurrent job uses its own DB session (sessions are not concurrency-safe); the LLM client stays shared (keep-alive pool).
3. On vLLM 5xx/timeout, apply backoff with jitter *per lane*, not per worker â€” one wedged request must not idle the other slots.
4. Record in-flight count in status (`gpu_inflight`) so the dashboard shows real GPU duty.

Acceptance: with 20 pending sweep jobs, vLLM receives â‰¥3 concurrent requests sustained; wall-clock for the batch improves â‰¥2.5x over serial baseline at unchanged failure rate.

### 2.5 Guided decoding everywhere (P6)

1. Temporal prompt (`_build_temporal_prompt`) sends `guided_json` with the events schema (client already accepts the param).
2. Related prompt sends `guided_json` with the insights schema.
3. Keep `extract_json_object` repair as fallback for backends without guided decoding; log which path parsed (`parse_mode: guided | repaired | failed`) into the trace for measurement.
4. Partial accept stays mandatory: valid events persist even when siblings fail validation.

Acceptance: parse-failure rate on 50 consecutive sweep jobs < 2%; zero whole-job failures caused by a single malformed field.

### 2.6 Focus tier: merged section-insight call (P3, P7)

New job type `section_insight_v1` (focus tier only):

- **Trigger:** frontend signals focused section (existing section-status/related fetch path can piggyback; explicit `POST /api/reader/section/{key}/focus` is cleaner). Enqueue at priority 10 if no fresh insight exists.
- **Inputs (all cached):** focused section text (â‰¤6.5k chars), its deterministic time anchors, entity preview, top `llm_focus_topk_routes` promoted routes with their intro snippets and component signals (including the *disagreement* signals per the relevance-trap defense).
- **One call, one schema:**

```json
{
  "temporal": [ { "label": "...", "start_date": "...", "end_date": null,
                  "precision": "year", "confidence": 0.9, "evidence": "..." } ],
  "related": [ { "title": "...", "verdict": "relevant|weak|unrelated",
                 "why": "...", "reasoning_tags": ["shared_entity"],
                 "confidence": 0.8 } ],
  "section_summary": "one grounded sentence"
}
```

- **Persistence:** temporal facts through the existing `_persist_facts` path (source `agent_temporal_v1`); related verdicts through the existing agent-signal gate path (`_agent_backed_gates`); summary to the section's insight cache. All provenance/versioning rules unchanged.
- **Contract note (Â§10.1 compliance):** this is a *composition* of the temporal + related capabilities, not a new capability. The output contract must be splittable back into separate calls without schema changes to consumers.

Acceptance: clicking a section with cached CPU data yields visible LLM insight in â‰¤10s at p50, â‰¤20s at p90, measured from focus event to persisted rows.

### 2.7 Sweep tier: packed calls (P7, P8)

Change sweep-tier temporal jobs to article-scoped packing:

1. Replace per-section `temporal_extract_v1` sweep jobs with `temporal_extract_batch_v1` carrying up to `llm_sections_per_temporal_call` uncovered/ambiguous section keys.
2. Prompt lists sections with keys; schema keys results by `section_key`; each section's results persist independently (partial accept per section).
3. Related sweep stays per-section (candidate context doesn't pack well) but is bounded by `llm_related_budget_per_article` and priority-ordered by section value and promoted-route count.
4. Sweep jobs are deprioritized (not canceled) when another article takes focus â€” extend `_prioritize_active_article_jobs` to push sweep-tier `run_after` rather than relying on priority alone.

### 2.8 Commit discipline (P8)

Per successful LLM job: one commit covering trace completion + fact rows + job status + processing state. The initial trace insert may remain a separate early commit for crash visibility, but intermediate `upsert_processing_state` commits inside a job are removed. Target: â‰¤2 commits per job.

### 2.9 Status and measurement

1. New status fields per article: `llm_focus_pending/running/done`, `llm_sweep_pending/deferred/budget_exhausted`, `gpu_inflight`, `deterministic_covered_sections`.
2. Traces already carry latency + usage; add `parse_mode` (2.5) and `tier` (focus|sweep). This is the data for the Â§10.1 merge/split optimization loop â€” after two weeks of use, revisit budgets, packing factor, and whether the merged focus call should split.
3. The dashboard's headline number becomes **time-to-first-focused-insight**, not queue depth.

### 2.10 Rollout order

| Step | Change | Files (primary) | Risk |
| --- | --- | --- | --- |
| 1 | Temporal enqueue gating + budget (2.2) | `workers/temporal_agent.py`, `article_pipeline.py` | Low â€” removes work |
| 2 | Related job purity (2.3) | `workers/related_agent.py`, `related/service.py` | Low |
| 3 | Guided JSON + parse_mode (2.5) | `workers/*`, `llm/openai_compatible.py` | Low â€” fallback retained |
| 4 | Worker concurrency (2.4) | `workers/temporal_agent.py`, worker launcher | Medium â€” session-per-task refactor |
| 5 | Commit discipline (2.8) | `workers/*`, `orchestration/state.py` | Low |
| 6 | Focus tier `section_insight_v1` (2.6) | new worker, reader API, frontend focus signal | Medium |
| 7 | Sweep packing `temporal_extract_batch_v1` (2.7) | new worker path | Medium |
| 8 | Status fields + dashboard metric (2.9) | status endpoint, frontend | Low |

Steps 1â€“3 are independent and individually shippable; each alone should produce a visible speedup. Step 4 requires step 3 (don't raise concurrency before parse failures are rare). Steps 6â€“7 change job shapes and should land after 1â€“5 stabilize.

### 2.11 Non-goals

- No second served model, no model swapping (single E4B; revisit only with measured evidence that E4B latency, not call count, is the bottleneck).
- No LLM on the read path, ever; core mode remains fully LLM-free.
- No relation extraction into the graph (unchanged from Multi-Path spec).
- No change to CPU lanes, scoring math, or promotion rules (covered by the whitepaper review).

### 2.12 Open questions

1. Focus signal: piggyback on existing section-status polling vs explicit focus endpoint? (Explicit is cleaner and cheaper once polling is consolidated.)
2. Does Gemma-4-E4B-NVFP4 on the current vLLM build support `guided_json` reliably? If not: `response_format` + repair, and cap concurrency at 3 until measured.
3. Should `section_insight_v1` results also feed `timeline_context_promote_v1` directly (skipping the separate promotion job for focused sections)?
4. Budget refill policy on explicit user refresh: full refill vs partial (suggest full refill â€” user intent is explicit).
