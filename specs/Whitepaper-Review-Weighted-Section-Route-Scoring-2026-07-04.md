# Review: Weighted Section-Route Scoring Whitepaper v0.1

Reviewed: 2026-07-04
Scope: whitepaper assessment, code-vs-paper conformance, live Abraham runtime evidence, and recommendations (L2 focus).

---

## 1. Verdict

The whitepaper is correct in its core claim and the code implements most of it faithfully. Route-first promotion (section -> candidate, never article-global) is the right model, and the three-layer separation (neutral graph / weighted relevance / promoted display) plus the three-score taxonomy (`raw_score` / `relevance_norm` / `best_route_score`) is the discipline that prevents the `0.62` structural-score false-promotion bug from recurring.

The paper is, however, **ahead of the code** in several places it presents as current behavior, and the live Abraham session exposes two operational problems the paper does not cover: a status-polling storm and a refresh-path deadlock.

---

## 2. Whitepaper critique (the document itself)

**Strong:**

- The false-promotion diagnosis (structural frontier score backfilled onto nodes, then ranked) is precise and the fix is the right one.
- "Article-level promotion asks the wrong question" is the single best paragraph in the specs folder. Section routes are the correct unit of promotion.
- The candidate-collection correction (all direct L1 links score-eligible; limits bound expansion and compute order, not eligibility) fixes a real recall bug.
- Honest about the temporal 0.60/0.40 tension and the overlapping L1/L2 score ranges.

**Weak / to fix in v0.2:**

1. **The normalization formula has its own trap.** `score = raw*0.70 + local_rank*0.30` gives the best candidate of a *weak* section an unconditional +0.30. In a section where every candidate is poor, the top row can clear the 0.42 fallback floor on rank alone. Recommendation: zero (or dampen) the rank term when `max_raw_score` for the section is below an absolute floor (e.g. 0.30), so local separation never manufactures promotability.
2. **Aspirational sections read as implemented.** The L2 evidence floor ("at least two supporting signals"), specificity-weighted entities, and the novelty bonus are not in the code (see conformance matrix). Mark them explicitly as target-state.
3. **Temporal-absence penalty needs a rule, not just a caveat.** The paper notes `0.50` may be a strong content match in a non-temporal section, then leaves it to "display language." Better: when `source_time_count == 0 or candidate_time_count == 0`, renormalize the content weight to 1.0 and stamp the row `content-only`. An unmeasured signal should not read as a measured zero. The proposed label set (`content-strong` / `temporal-strong` / `mixed` / `content-only` / `low-confidence`) should become a persisted `confidence_band` field, not just display language.
4. **Cross-section comparability is understated.** Because normalization is per-section, `best_route_score` on an article node mixes numbers from different distributions. The paper should state that node-level aggregates are ordinal within a section and only indicative across sections.

---

## 3. Code vs whitepaper conformance

| Whitepaper claim | Code reality | Status |
| --- | --- | --- |
| Route-first promotion, structural links never promote | `_apply_relevance_promotion_floor` filters to `relation=related_to` + `score_source=related_cache` | Implemented |
| Per-section L1/L2 budgets (4/3), floor 0.42, min 12 / max 72 | Same constants in `graphology.py` | Implemented |
| Small promotion-order adjustment (backlink + via) | `_route_promotion_score` | Implemented |
| All direct L1 links score-eligible | `_prioritized_candidate_slice` keeps all L1, bounds L2 | Implemented |
| `S_backlink` bounded signal (0.10 weight) | `_candidate_backlink_count` / `content_components` | Implemented |
| L2 evidence floor: **at least two** supporting signals | `gates.py` accepts L2 with **one** of content / temporal / score>=0.42; `S_domain >= 0.28` alone satisfies "content signal" | **Gap â€” and domain-only is exactly the trap the paper says must not rescue a candidate** |
| Specificity-weighted entity overlap | `entity_overlap` weights by passage `blend` (salience), not rarity. "New Delhi" with high blend contributes fully | **Gap â€” the hard floor F does not exist** |
| New Delhi trap acceptance test | No such test exists (033 Phase 3 unchecked) | **Gap** |
| L2 bridge-aware scoring | `via_title` carried for display + 0.025 promotion nudge only; bridge route strength never enters the L2 score | **Gap (biggest conceptual one â€” Sec. 5)** |
| Novelty bonus | Not implemented | Gap (paper marks it future â€” fine) |
| Per-section coverage completion | Not enforced; see stale-score note below | Gap |

**Stale-score mechanism the paper's "coverage" section should name:** rows scored before embeddings landed carry `S_embed = 0` and `embedding_ready.used == "none"` in signals. `_cache_has_current_scoring` checks only scoring/gate versions, so these rows keep artificially low scores until a manual refresh. Fix: treat `used == "none"` rows as stale once the embedding exists, and rescore. This is the concrete form of the "biased promotion even with good scoring" problem.

---

## 4. Live evidence â€” Abraham session (runtime logs)

Sampled `runtime_logs/backend-runtime.log` (Postgres/Neo4j not reachable from this sandbox, so this is the log-level view):

1. **Status polling storm.** 2,501 of 2,524 Abraham requests are `/status` polls (99%), from ~46 client sockets, with two pollers (article-level and section-level) running concurrently â€” plus separate status polling for L1 articles (`Jerusalem Bible`, `Fear and Trembling`). Every poll aggregates job/state tables on the remote Postgres. Recommendation: one consolidated status endpoint; adaptive interval (fast only while any lane is `running`, back off to 15â€“30s when idle/disabled/completed); or SSE push. This is also a UX honesty issue â€” the polling exists because the UI cannot trust state to arrive.
2. **`/related` called 164 times for one section** (the lead, `1436:1300009790`). Even cache-served, the frontend is re-fetching in a loop, likely coupled to the status poller. On any cache miss this re-enters the expensive ranking path.
3. **Refresh 500 â€” deadlock, and it confirms the read-path coupling.** `POST /refresh` runs `entity_mentions.enrich_article` synchronously inside the HTTP request (`services.py:91`) and deadlocked (`DeadlockDetectedError`) against the CPU-entity worker upserting the same `entity_registry` rows. Task 034 hardened the *worker* side (sorted writes, retries) but the *request* side still does synchronous enrichment. Fixes: (a) refresh should invalidate + enqueue, never extract inline â€” same render-first rule as first load; (b) entity upserts need one canonical strategy across all writers: sorted batch upsert in a single `INSERT ... ON CONFLICT` statement, or a per-entity advisory lock.
4. **vLLM instability is a standing tax.** ~150 restart/unhealthy mentions in the vLLM watchdog log. Reinforces: single served model (E4B), guided decoding before any LLM lane is re-enabled, and never letting app refresh touch the vLLM process.

---

## 5. L2: recommendations (priority order)

L2's job â€” "explanatory context not directly named by the source article's own links" â€” is stated well in the paper. The implementation does not yet give L2 a fair or safe fight:

1. **Rank L1 first, then expand L2 from top *scored* L1s.** `_l1_candidates_for_l2_expansion` currently picks L2 parents by `source_article_link_count` + link rank â€” structural signals chosen *before* weighted scoring exists. Two-phase it: score all L1 routes, then expand the top-K L1 by weighted score. Highest quality-per-effort change available.
2. **Propagate bridge strength into the L2 score.** An L2 via the best L1 currently gets the same `S_graph`/`prior` as an L2 via a barely-relevant L1. E.g. `S_graph_l2 = base * (0.5 + 0.5 * bridge_route_score)`. Persist `l2_bridge_signal` (paper already names the field).
3. **Aggregate multiple bridges.** The `seen` set keeps only the first bridge per L2 candidate and discards later paths â€” losing `path_count`, one of the strongest cheap trap defenses. Keep a parents set; use path count as a signal, best bridge for display.
4. **Enforce the two-signal evidence floor, and stop counting domain-only.** Require two independent signals for L2 acceptance; `S_domain` alone must not qualify.
5. **Add IDF specificity weighting.** One query over `entity_passage_score` (distinct sections per entity) yields a corpus document frequency; weight overlap terms by `min(blend_s, blend_c) * idf`. This is the hard floor F, and the prerequisite for the New Delhi acceptance test â€” which should be written *first*, red, then made green.
6. **Unify candidate sourcing on the graph frontier.** `graph/frontier.py` (L2 from L1 *intro* links) and `related/service.py` (L2 from `all_links[:6]`) derive different L2 worlds; the graph view and the scored routes will disagree. Read candidates from Neo4j (or one shared candidate table); `path_count` then comes for free.

## 6. Non-L2 engineering items (from the code read + logs)

- **De-sync `_rank_candidates`.** It currently does per-candidate article loads, entity extraction, temporal seeding, and embedding-wait sleeps (2.5s + 0.35s/candidate) inside the scoring pass while holding the DB session. Two-pass it: score from cached data only (never block), enqueue gaps, rescore on completion.
- **Kill the N+1s.** Per candidate: sections select, entity select, time select, two pgvector queries, backlink scan â€” ~500 round-trips per section to a remote Postgres. Batch by candidate set (single entity query grouped by title_id; single pgvector query with GROUP BY).
- **Batch Neo4j state writes.** `_mark_section_link_extraction` and per-section node/edge syncs fire individually inside loops â€” ~200 round-trips per 40-section article. Batch per pass.
- **Batch redirect resolution.** `resolve_title` is awaited link-by-link in both frontier and candidate collection.

## 7. Suggested sequencing

1. Refresh-path fix (invalidate + enqueue; unified upsert strategy) â€” it 500s today.
2. Status endpoint consolidation + adaptive polling â€” 99% of current traffic.
3. Batch N+1s and de-sync `_rank_candidates` â€” user-visible latency.
4. L2 items 1â€“3 (two-phase expansion, bridge propagation, multi-bridge).
5. IDF + two-signal L2 gate + New Delhi acceptance test (033 Phase 3).
6. Stale-embedding rescore + per-section coverage status.
7. Whitepaper v0.2: normalization guard, content-only reweighting rule, mark aspirational sections.

---

## 8. Implementation Response 2026-07-04

Implemented in task `044_whitepaper_review_scoring_hardening.md`:

- Normalization guard:
  - no local-rank lift when section `max_raw_score <= 0.30`;
  - damped rank lift until `max_raw_score >= 0.42`;
  - prevents weak sections from manufacturing promotion by local rank alone.
- L2 gate tightening:
  - L2 now requires two independent supporting signals, or one signal plus a
    strong score;
  - domain-only L2 acceptance is rejected.
- Stale embedding rescore:
  - related-cache rows scored with `embedding_ready.used == "none"` are stale
    once the source section embedding exists.
- Render-first article load/refresh:
  - reader request path no longer runs ontology extraction inline;
  - refresh invalidates/recaches/enqueues through orchestration.
- Scored-L1 L2 expansion:
  - L2 bridge parents are selected from weighted L1 route scores;
  - L2 graph/prior components are scaled by bridge route strength.

Verification:

- focused related scoring/gating/service tests passed: `24 passed`;
- broader related/graph/timeline test slice passed: `50 passed, 60 deselected`.

Still open:

- status endpoint consolidation and adaptive polling;
- batching/N+1 cleanup in `_rank_candidates`;
- multi-bridge aggregation and path count;
- persisted `l2_path_score`, `l2_bridge_signal`, and `l2_novelty_signal`;
- IDF/specificity weighting and New Delhi trap acceptance test;
- Neo4j-backed unified candidate source.
