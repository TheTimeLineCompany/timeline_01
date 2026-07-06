# Enrichment Pipeline Spec -- Layered Passes & Optimization

**Version:** 0.1 (draft)
**Status:** Architecture for V4 enrichment. Optimizes first-pass usability and
second-pass richness/robustness.
**Date:** 2026-06-25
**Encoding:** ASCII only.
**Companions:** `Timeline-Spec-v0.3.md` (application), `Ontology-and-Scoring-Framework-v0.1.md` (scoring/identity).

---

## 1. Problem (grounded in current V4)

The read path is correctly cache-first and fast, but the enrichment behind it has
two faults the user observed:

**A. First pass is too thin.** On article open, enrichment is **pure CPU**:
spaCy `en_core_web_sm` seed tags + regex explicit-date temporal + the deterministic
related scorer. No LLM, so there is no real topic identity, no attribution, no
implied dates, and weak entities. "Usable core" is missing.

**B. Second pass is slow and breaks.**
- **Granularity:** `enqueue_temporal_jobs` creates **one job per section**
  (Abraham Lincoln = 42 sections -> 42 vLLM calls). Related-explain and
  timeline-context jobs add more per article.
- **Concurrency = 1:** `run_agent_worker.py` claims `... FOR UPDATE SKIP LOCKED
  LIMIT 1` and processes serially with a 2s idle poll. At ~6s/call that is
  minutes per article.
- **Fragile output:** `chat_completion` sends plain messages with `max_tokens=900`
  and **no guided decoding**; strict `parse_temporal_agent_response` fails on
  fenced/partial JSON -> 3 retries -> job `failed` ("attention").
- **Transport:** a **new `httpx.AsyncClient` per call** (no pooling) + fixed 60s
  timeout; large sections truncate at 900 output tokens; no partial-accept.

**Conclusion.** The fix is not "more workers" alone. It is a **layered pipeline**
that (1) adds a *small, bounded* LLM call to make the first pass usable, and
(2) makes the deep pass batched, prioritized, guided, and fault-tolerant so it
gets *richer over time without stalling or breaking*.

---

## 2. Design goals

1. **Fast usable core, with LLM.** First usable result includes LLM understanding,
   not just CPU seeds -- but from a *bounded* amount of LLM work so it stays fast.
2. **Rich, incremental, resumable.** Deeper passes keep adding detail in priority
   order; partial progress is always usable; nothing blocks on the whole article.
3. **Robust by construction.** Constrained decoding + tolerant parsing + partial-
   accept + retries; a malformed item never fails a whole job.
4. **Throughput-aware.** Exploit vLLM continuous batching with bounded concurrency
   sized to 16 GB; never one-request-at-a-time.
5. **Measured.** Per-layer latency, tokens, parse-fail rate, and queue depth are
   logged so tuning is data-driven.
6. **Read path stays GPU-free.** All LLM work is background; reads serve cache.

---

## 3. The layered model

More than two passes. Each layer has a distinct trigger, granularity, LLM budget,
and priority. Lower layers are faster and higher-priority; higher layers add
richness.

| Layer | Name | Runs on | LLM? | Granularity | Output | Target latency | Priority |
|---|---|---|---|---|---|---|---|
| **L0** | Instant base | CPU, read path | no | section | parsed text, cached seeds, cached related | ms (cache) | n/a |
| **L1** | **Usable core** | GPU, background | **1 bounded call / article** | whole article (condensed) | topic identity, salient entities/topics, key dated spine, primary domains, short summary | seconds (1 call) | highest |
| **L2** | Rich section enrichment | GPU, background | batched, prioritized | section (chunked) | entity-aware mentions + attribution, full temporal w/ roles, relationships | progressive | high (by section value) |
| **L3** | Relational / context | GPU sparingly | template-first, LLM rare | candidate pairs | related "why", L1/L2 timeline context promotion, cross-article contemporaneity | progressive | medium |
| **L4** | Review / quality | GPU sparingly | low-volume | fact/cluster | attribution-quality review, grounding validation, entity dedup/merge | lazy | low |

Key reframing: **L0 paints instantly; L1 makes it *usable* with one LLM call; L2+
make it *rich* over time.** The current system jumps from L0 (thin) straight to a
per-section L2-style flood with no L1, which is why the first result is poor and
the deep pass is heavy.

---

## 4. Layer 1 -- the Usable Core (one bounded LLM call)

The single most important addition. Instead of 42 per-section calls, **one
article-level call on a condensed input** yields the core a reader/timeline needs
immediately.

**Input construction (CPU, cheap):** build a compact digest of the article:
- the lead/intro section (cleaned),
- all section **headings** (the article's skeleton),
- a few **high-signal sentences** per major section (those containing seed dates,
  capitalized entity runs, or high-IDF seed tags),
- the infobox key/values if present.
Cap the digest to a fixed token budget (e.g. ~2-3k tokens) so the call is fast and
fits context.

**One guided-JSON call ->** the core object:
```
{
  topic: { canonical_name, entity_type, aliases[], primary_domains[] },
  key_entities: [ {name, type, why_central} ],          # the real entities, not spaCy noise
  spine: [ {label, date_text, precision, role, section_hint} ],  # the key dated facts
  summary: "2-3 sentence grounded synopsis"
}
```
- **Grounded:** every spine item and entity must cite a section hint; the digest is
  the only source. No outside facts.
- **Bounded:** one call, capped output. ~one GPU pass (single-digit seconds).
- **Immediately usable:** reader shows real topic + key entities; timeline shows
  the spine right away, before L2 deepens it.

L1 is enqueued at **highest priority** on article open (durable job, off read
path). It supersedes the CPU seeds for display while L2 enriches underneath.

---

## 5. Layer 2 -- Rich section enrichment (batched, prioritized, incremental)

This is where the entity-aware chain (Ontology framework Sec. 8 of the app spec)
does authoritative per-section work -- but made fast and robust.

**Triage & priority (do not blindly enqueue all sections).**
- Skip non-content sections via the existing `content_filters.is_content_section`
  (References, See also, External links, etc.).
- **Score section value** cheaply (seed-tag density, date presence, length,
  position) and enqueue **high-value sections first**. Richness then accretes in
  the order that matters.
- L1's `key_entities`/`spine` can further bias which sections deepen first.

**Batching & chunking.**
- **Batch small sections** into one call where context allows (multiple short
  sections -> one prompt with per-section delimiters and IDs), reducing call count.
- **Chunk large sections** to fit context and a sane output budget; chunks carry
  stable provenance offsets.
- Aim to turn "42 calls" into a much smaller number of **batched** calls.

**Per-section/chunk output (guided JSON):** entity mentions + attribution
(core/context/related/meta), temporal facts with roles, and intra-section
relationships -- written to the ontology caches with provenance and source
`agent_*_v1`. Idempotent upserts keyed by `(section_key, model_version)`.

L2 keeps running in the background; each completed section makes the article
richer without blocking anything.

---

## 6. Layer 3 -- Relational / context (template-first)

- **Related "why":** default to deterministic templates from matched signals
  (Ontology framework Sec. 5.4); enqueue an LLM phrasing job only for non-obvious
  pairs. Batched, low priority.
- **Timeline context promotion:** the existing `timeline_context_promote_v1`
  path, gated by the inclusion rule (shared-tag + time-overlap + relevance
  threshold).
- **Cross-article contemporaneity:** tag+temporal index joins (CPU); LLM only to
  phrase, rarely.

---

## 7. Layer 4 -- Review / quality (lazy)

Attribution-quality review, grounding validation (entity-overlap check), and
entity dedup/merge ("no blind merge"). Lowest priority; improves precision after
the article is already usable and rich.

---

## 8. Optimization: throughput

The current bottleneck is orchestration, not the GPU (the probe showed one request
saturates the GPU at ~6s; the worker just never sends more than one at a time).

1. **Concurrency pool.** Replace the single serial claimer with a worker pool that
   claims and processes **N jobs concurrently** (N = a tunable semaphore sized for
   16 GB; start small, e.g. 3-6, and measure). vLLM **continuous-batches** the
   concurrent requests, multiplying throughput for the same GPU.
2. **Pooled HTTP client.** Reuse one `httpx.AsyncClient` (keep-alive) across calls
   instead of constructing one per request.
3. **vLLM batch capacity.** Tune `--max-num-seqs` / `--max-num-batched-tokens` so
   the server admits the concurrent requests into one batch.
4. **Priority queue.** `agent_job.priority` already exists -- use it: L1 > L2-high
   > L2-low > L3 > L4. Process the usable core and high-value sections first.
5. **Batch packing.** Pack multiple small sections per call (Sec. 5) to cut call
   count and per-call fixed overhead.
6. **Right-size tokens.** Set `max_tokens` per job from input size; do not pay 900
   tokens for a one-date section, and do not truncate a dense one.
7. **Warm-up.** When idle, pre-enqueue L1 (and high-value L2) for the current
   article's L1 neighbors so the likely next click is already warm. Yields to live
   work.

---

## 9. Optimization: robustness (stop the breaking)

| Failure (current) | Fix |
|---|---|
| Model emits fenced/partial JSON -> strict parse fails | **Guided decoding**: send vLLM `guided_json` (JSON Schema) or `response_format={"type":"json_object"}` so output is valid JSON by construction |
| One malformed item fails the whole job | **Partial-accept**: parse item-by-item, keep valid facts, log the rest; never discard a whole section for one bad row |
| Residual bad JSON | **Tolerant parse + repair**: strip fences, extract first JSON object, run a json-repair fallback before failing |
| Large section truncates at 900 tokens | **Chunking** + per-job `max_tokens` sizing (Sec. 5, 8.6) |
| Fixed 60s timeout under load | **Adaptive timeout + circuit breaker**: scale timeout to batch latency; if vLLM saturates/health drops, back off and requeue rather than error-storm |
| New client per call | **Pooled keep-alive client** |
| Worker crash leaves running locks | Existing 15-min stale reclaim + a **heartbeat** to shorten recovery |
| Retry hammering | **Exponential backoff + jitter** on `run_after`; cap `max_attempts`; dead-letter to L4/attention with the raw response retained in `agent_trace` |
| Re-runs duplicate rows | **Idempotent upserts** keyed by `(section_key, model_version)`; version bumps recompute cleanly |

Net: a job degrades to *partial success* instead of failing, and transient server
issues back off instead of cascading into "attention."

---

## 10. Measurement (so tuning is data-driven)

Extend `agent_trace` / processing-state with per-layer metrics and surface them on
the dashboard:
- **latency_ms** and **tokens** (prompt/completion) per job;
- **parse outcome** (clean | repaired | partial | failed) and **% facts kept**;
- **queue depth** and **age** per layer; **GPU concurrency** in flight;
- **time-to-usable-core** (L0 open -> L1 done) and **time-to-rich** (L2 coverage %).
Targets to tune against: time-to-usable-core in single-digit seconds; L2 high-value
coverage well before a user would scroll there; parse-failure rate near zero after
guided decoding.

---

## 11. Scheduling summary

```
article open (read path, no GPU)
  -> L0 serve cached/seeds instantly
  -> enqueue L1 (priority=highest)               # usable core, 1 call
  -> enqueue L2 for high-value sections (priority by section value)
  worker pool (concurrency N, pooled client, guided JSON):
     L1 first  -> core visible in seconds
     L2 next   -> richness accretes, high-value first, batched/chunked
     L3/L4     -> relational + review fill in, low priority
  idle -> warm-up L1 of neighbors
```

---

## 12. Concrete deltas to current V4

1. **LLM client (`llm/openai_compatible.py`)**: add `response_format`/`guided_json`
   support and a pooled, reused `AsyncClient`; expose adaptive timeout.
2. **New L1 job type** `core_digest_v1`: digest builder (CPU) + one guided call +
   core-object persistence (topic/entities/spine/summary) + provenance.
3. **Worker (`workers/temporal_agent.py` / `run_agent_worker.py`)**: convert the
   serial loop into a **concurrency-bounded pool**; honor `priority`; backoff +
   partial-accept + repair in parsing.
4. **Enqueue policy (`reader.py` auto-enqueue)**: stop blanket per-section temporal
   enqueue; instead enqueue L1 + **triaged, prioritized** L2 via section-value
   scoring; reuse `is_content_section`.
5. **Batch packer**: group small sections per call; chunk large ones.
6. **Config**: new knobs (Sec. 13).
7. **Dashboard/trace**: add the Sec. 10 metrics.

---

## 13. New config knobs (defaults to tune)

```
worker_concurrency          = 4        # concurrent in-flight LLM jobs (size to 16 GB)
llm_guided_json             = true     # use schema-constrained decoding
llm_keepalive               = true     # pooled client
l1_digest_token_budget      = 2500     # condensed article digest cap
l2_batch_small_section_max  = 600      # tokens; pack sections under this together
l2_chunk_tokens             = 1200     # split larger sections into chunks
job_max_attempts            = 3
job_backoff_base_seconds    = 5        # * 2^attempt + jitter
section_value_min_to_enqueue= <tuned>  # skip near-empty sections from deep pass
```

---

## 14. Phased rollout

1. **Robustness first (unblocks breakage):** guided decoding + partial-accept +
   repair + pooled client + backoff. (Stops "attention" storms immediately.)
2. **Concurrency:** worker pool + vLLM batch tuning + priority. (Kills the
   serial-per-section slowness.)
3. **L1 usable core:** digest builder + `core_digest_v1`. (Fixes thin first pass.)
4. **L2 triage + batching:** section-value scoring, batch packing, chunking.
5. **L3/L4 + warm-up + measurement loop.**

---

## 15. What would sharpen this further (inputs welcome)

- Confirm the vLLM build supports **guided/JSON-schema decoding** (outlines/
  xgrammar) for Gemma-4-E4B-NVFP4; if not, we lean harder on repair + partial-accept.
- A target **time-to-usable-core** and an acceptable **background GPU duty cycle**
  (how hard you want the card run during enrichment).
- Whether L1's short **summary** is wanted in-product or internal-only (grounding
  rules still apply either way).
