# Scoring, Gating & Insights Framework

**Version:** 0.1 (draft)
**Status:** Operational scoring framework. Runs along the orchestration flow.
**Date:** 2026-06-25
**Encoding:** ASCII only.
**Companions:** `Ontology-and-Scoring-Framework-v0.1.md` (relatedness math + identity
-- referenced, not duplicated), `Orchestration-Spec-v0.1.md` (the stages this runs
on), `Enrichment-Pipeline-Spec-v0.1.md` (the work that gates dispatch),
`Graph-View-and-API-Spec-v0.1.md` (edge/score surfacing).

> One sentence: this turns scattered scoring heuristics into a **layered pipeline --
> Signals -> Scores -> Gates -> Insights -- that runs progressively across the
> orchestration stages**, so every gating decision is a threshold on a *named score*
> with a *confidence*, not a hand-wave.

---

## 1. The four layers

```
SIGNALS  (raw measurements, cheap->rich, stream in by stage)
   |   grouped: structural | lexical | semantic | entity | temporal | quality | cost | user
   v
SCORES   (named, component-based, each carries a CONFIDENCE that rises with signals)
   |   relatedness (pair) | connection-strength (graph) | importance (node) | priority (value/cost)
   v
GATES    (decisions = thresholds/policies on scores; drive the orchestrator)
   |   discovery | enrichment | cascade | deepening | timeline | insight | refresh
   v
INSIGHTS (grounded outputs)
       related+why | timeline accretion | graph edges+weights | queue priority | coverage
```

**Two score families, do not conflate them:**
- **Relatedness** = "how connected/relevant are A and B" (a property of a *pair*).
- **Priority** = "how worth-it is this unit of work right now" (a property of a
  *job*; value / cost). Gates for *what to enrich* use priority; gates for *what to
  show* use relatedness.

**Progressive scoring (the key runtime idea).** A score is computed from *whatever
signals exist so far*, and carries a **confidence/completeness** = (signals present
/ signals possible) x extraction confidence. Cheap structural signals let early
gates fire (discovery, queue order); the same scores are **recomputed with higher
confidence** as embeddings/entities/temporal stream in, unlocking the richer gates.

---

## 2. Signal inventory

| Group | Signal | Earliest stage | Cost |
|---|---|---|---|
| **Structural** | connection count L0<->L1/L2 | link framework | trivial |
| | back-link / **reciprocity** (does target link back to L0) | link framework | trivial |
| | **link prominence** (lead vs body vs navbox/refs) | link framework | trivial |
| | link order within section | link framework | trivial |
| | **distinct routes** (independent paths to L0) | link framework | cheap |
| | **min-hops** + path count | link framework | cheap |
| | **path strength** (min/product of edge relevances on L0->L1->L2) | after L1 score | cheap |
| | node degree / **centrality** in neighborhood | link framework | cheap |
| **Lexical** | title/section token overlap | L0 cache | trivial |
| **Semantic** | **embedding cosine** | after embed | mid (GPU once) |
| **Entity** | spaCy seed entities | L0/L1 seed | cheap (CPU) |
| | **ontology entities** (canonical id, type, domain) | after LLM extract | mid (GPU) |
| | **specificity / IDF** of shared entities | after extract | cheap |
| | **mention salience** (freq, coref, subjecthood; core vs context) | after attribution | mid |
| **Temporal** | temporal entities (anchors, precision) | after temporal extract | cheap-mid |
| | **temporal proximity** (interval IoU + adaptive decay) | after both anchored | cheap |
| | temporal role alignment (founded/born/occurred...) | after temporal extract | cheap |
| **Quality** | extraction **confidence** (seed < llm < reviewed) | per extract | free |
| | **coverage/freshness** (missing/running/done/stale) | always | free |
| **Cost** | **content density** (entities+dates per token) | after seeds | cheap |
| | length / **token-cost estimate** | L0 cache | trivial |
| | **novelty/redundancy** (new vs already-covered entities/time) | after extract | cheap |
| **User** | **focus** (currently viewed / likely-next), active lens, expansion | runtime | free |

---

## 3. Scores

All scores are component vectors with a default blend (per the ontology pattern);
all carry `confidence`. Weights are tunable/versioned.

### 3.1 Relatedness `S_rel(L0_section, candidate)` -- pair
**Reuses the Ontology framework Sec. 5.4 directly** (embedding cosine +
specificity-weighted entity overlap + graph signal + temporal proximity + domain
alignment, with saturation + set-relative normalization). Not re-derived here. This
is the "how related / how relevant" score.

### 3.2 Connection strength `S_conn(L0, candidate)` -- graph
The structural backbone of relatedness, usable **before** embeddings exist:
```
S_conn = blend( sat(distinct_routes), sat(connection_count),
                reciprocity_bonus, prominence_weight, 1/min_hops, path_strength )
```
Drives early discovery ordering and the L1->L2 cascade (path strength = the weakest
edge along L0->L1->L2). Available cheaply at the link-framework stage.

### 3.3 Importance `S_imp(node)` -- intrinsic
How significant the candidate is in itself: `blend(specificity, centrality,
content_density, type_weight)`. Independent of the focus; cached on the node.

### 3.4 Priority `S_prio(work_unit)` -- utility = value / cost
The score that orders the queue and gates enrichment depth:
```
value = blend( S_rel (or S_conn pre-embedding), S_imp, novelty, user_focus )
cost  = f( token_estimate, layer_base_cost )
S_prio = value / cost   x  staleness_factor   x  layer_priority
```
- `novelty` down-weights redundant neighbors (diminishing returns).
- `user_focus` boosts what the reader is looking at / likely to open next.
- `staleness_factor` favors `missing` > `stale` > `done`.
- `layer_priority` keeps L1-core ahead of L2 ahead of deepening.
This is the formalization of the enrichment spec's "section value" heuristic.

### 3.5 Confidence (on every score)
`confidence = (signals_present / signals_possible) x mean_extraction_confidence`.
Low early (structural only), rises as enrichment streams in. **Gates that affect
the user (show/hide, timeline, insight) require a minimum confidence; gates that
only spend cheap CPU (discovery, queue order) do not.**

---

## 4. Gates

Each gate is a documented threshold/policy on a score. Defaults are tunable.

| Gate | Decision | Score(s) | Policy (default) |
|---|---|---|---|
| **G1 Discovery** | include node/edge in the graph at all | exists-as-link | broad: any resolved link (cheap, no confidence req) |
| **G2 Enrichment** | cache intro + embed + extract a candidate | `S_prio` (pre-embed uses `S_conn`) | top-K L1 by `S_prio` |
| **G3 Cascade** | enrich L2 under an L1 | parent L1 `S_rel`/`S_conn` | `M = f(L1 strength)`; weak L1 -> M=0 (link-only) |
| **G4 Deepening** | how many sections of a candidate to enrich | `S_rel` tier | high->many, medium->few, low->intro-only |
| **G5 Timeline inclusion** | put a dated item on the unified timeline | shared-tag AND temporal overlap AND `S_rel` | **no-blanks** (Sec. 4.1): `>= tau_timeline` confident, else top-N% floor low-confidence, never below `F` |
| **G6 Insight (LLM why/synthesis)** | run the careful LLM vs a template | `S_prio` + "non-obvious" (low lexical, high embed) | LLM only if non-obvious AND `S_prio >= tau_insight`; else template |
| **G7 Refresh** | recompute a stale score/fact | version + `coverage` | recompute when `ontology/model/parser` bumps |

Gates that affect the user require `confidence >= tau_conf` to be shown as
**confident**; lower-confidence items may still appear, clearly marked, per the
no-blanks rule below.

### 4.1 No-blanks display gating (threshold-or-percentile, confidence-labeled)

A hard threshold can leave a panel empty. For **display gates** (Related panel, G5
timeline, and any "show the top related") use a **threshold-with-floor** rule
instead of a single cutoff, evaluated **once the relevant layer's `coverage =
done`** (so the candidate set is fully scored and a percentile is meaningful):

```
Given candidates scored for this focus+layer, with thresholds T (confident),
F (hard floor), and a floor count band [k_min .. k_max] / top-N percentile:

  CONFIDENT  = { c : score(c) >= T  AND  confidence(c) >= tau_conf }
  if |CONFIDENT| >= k_min:
      show CONFIDENT (normal styling)
  else:
      FLOOR = top-N-percentile(candidates), bounded to [k_min, k_max],
              restricted to score(c) >= F        # never rescue zero-signal items
      show CONFIDENT as confident
        + (FLOOR \ CONFIDENT) marked LOW CONFIDENCE
```

So the effective cutoff is the **looser** of "score >= T" and "in the top-N% (>= k_min)"
-- which is what prevents blanks -- but **never below the hard floor `F`** (an item
must have at least one grounded connection: a shared entity/link/time). Three bands:

- `score >= T` and `confidence >= tau_conf` -> **confident** (normal display).
- `F <= score < T` and within the top-N%/`k_min` floor -> **shown, low-confidence**
  (muted styling + a "low confidence" / "weak match" badge; the WHY notes which
  signal is thin).
- `score < F`, or zero grounded signal -> **not shown** (an honest "nothing strongly
  related yet" beats a confidently-wrong item).

**Timing.** While the layer is still enriching, stream the CONFIDENT set as it
fills; only apply the percentile floor after `coverage = done`, so low-confidence
backfill is computed against the final distribution, not a partial one.

This keeps panels populated without drawing blanks, while never dropping below an
honesty floor -- and the user always knows which items are weak.

---

## 5. Insights (outputs)

Everything a gate admits is a grounded, provenance-stamped insight:
- **Related items + "why"** (G2/G6) -> reader Related panel + `related_cache`.
- **Timeline accretion** (G5) -> unified timeline + `timeline_cache`.
- **Graph edges + weights/distance** (G1-G3) -> Neo4j + the graph view.
- **Queue priority + coverage decisions** (G2-G4, G7) -> `agent_job` + ledger.
No insight is emitted without provenance and a confidence.

---

## 6. How it runs across the orchestration stages

This is the layered runtime: at each stage, new signals arrive, scores are
(re)computed at higher confidence, and the gates that are now possible fire.

| Stage (orchestration) | Signals now available | Scores (re)computed | Gates that fire | Insights / effect |
|---|---|---|---|---|
| **1. L0 cached** | structural (own links), lexical, length | -- | -- | render; readiness |
| **2. Link framework** | counts, prominence, reciprocity, routes, hops, centrality | `S_conn`, `S_imp` (partial), `S_prio` (pre-embed) | **G1** (broad discovery), initial **G2** ordering | graph panels populate; queue ordered by `S_prio` |
| **3. L0 enrichment** | L0 embeddings, ontology+spaCy entities, temporal, density, confidence | L0 profile; `S_imp` full | -- (L0 is the focus) | L0 core ready; embeddings/entities cached |
| **4. L1 intro enrichment** | L1 intro embeddings/entities/temporal | **`S_rel`(L0,L1)** full, `S_prio` refined, path-ready | **G2** confirm, scoring/categorize, **G4** tier set | L1 ranked + categorized; "why" (template) |
| **5. L2 (under strong L1)** | L2 intro signals | `S_rel`(L2, L0/L1), path strength | **G3** cascade, **G4** tier | L2 ranked along strong paths only |
| **6. Deepening** | deeper-section signals | `S_rel` refresh at higher coverage | **G4** executes tiers | more sections enriched where relevant |
| **Timeline (cross-cut)** | temporal anchors + proximity | temporal proximity + `S_rel` | **G5** | items accrete onto the unified timeline |
| **Insight pass (bg)** | full pair signals | `S_prio`, non-obvious test | **G6** | LLM "why"/synthesis for the worthwhile minority |
| **Idle crawler** | coverage + versions | `confidence`, staleness | **G7** | gaps/stale refreshed, version-aware |

**Confidence rises left-to-right.** Early gates (G1, queue order) run on cheap
`S_conn` with low confidence -- fine, they only spend CPU / set ordering. Display
gates prefer confident `S_rel`, but once a layer's `coverage = done` they apply the
**no-blanks floor (Sec. 4.1)**: rather than show nothing, they backfill the
top-N%/`k_min` best (above the hard floor `F`) **clearly marked low-confidence**, so
the reader gets the best-available without being misled.

---

## 7. Config knobs (tunable, versioned)

```
# score weights
w_conn{routes,count,reciprocity,prominence,hops,path} ; w_prio{value,cost,...}
# gate thresholds
top_k_l1 ; l2_budget_curve M=f(L1_strength) ; deepening_tiers{high,med,low}
tau_timeline (volume slider) ; tau_insight ; tau_conf (min confidence for user gates)
# no-blanks display floor (Sec. 4.1)
T_display ; F_hard_floor ; floor_percentile_N ; k_min ; k_max
# cost model
token_cost_estimate ; layer_base_cost ; novelty_discount ; user_focus_boost
```

All recompute-cheap: changing weights/thresholds re-blends from cached signals
(per the ontology component model) -- no re-extraction.

---

## 8. Relationship to the other specs (no duplication)

- **Ontology framework** owns the *relatedness math* (`S_rel`) and identity. This
  doc references it and adds the **connection-strength**, **priority (value/cost)**,
  **confidence/progressive**, and the **gate + stage-runtime** layers.
- **Orchestration spec** owns the *flow*; it calls these gates at each stage.
- **Enrichment spec** owns the *work*; gates decide which work to dispatch.
- **Graph spec** surfaces the resulting scores/weights on edges and the ledger.

---

## 9. Open questions

1. Default weights for `S_conn` and `S_prio`, and the `tau_*` thresholds.
2. The `M = f(L1 strength)` curve shape (linear, step tiers, or relevance-proportional).
3. `tau_conf` per user-facing gate -- one global value or per-gate.
4. Novelty/redundancy measure: entity-set Jaccard vs embedding-coverage.
5. Whether `user_focus` is in v1 or deferred (needs UI focus events).
6. No-blanks floor (Sec. 4.1): values for `F` (hard floor), `floor_percentile_N`,
   and `[k_min,k_max]`; and whether `F` is global or per-gate.
