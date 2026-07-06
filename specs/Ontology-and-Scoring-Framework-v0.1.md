# Ontology & Scoring Framework

**Version:** 0.1 (draft)
**Status:** Core architecture. App-independent.
**Date:** 2026-06-25
**Encoding:** ASCII only.

> **What this document is.** A standalone description of the *knowledge substrate*:
> how things are identified, categorized, scored, cached, and compared. It is
> deliberately **independent of the application shape** (reader, timeline, graph
> explorer, API). Any application is a *consumer* of this substrate; none of its
> rules depend on a UI. The application spec (`Timeline-Spec-v0.3.md`) references
> this document; this document references no UI.
>
> **Two layers.** (1) a **Content-Topic layer** -- what a thing *is* and what it
> relates to; (2) a **Temporal layer** -- when a thing *is anchored* in time. A
> small **relatedness engine** sits over both and produces explainable
> comparisons.

---

## 1. Design principles

1. **Grounded identity.** Every thing resolves to a stable identity. The primary
   identity of a content entity is the **canonical Wikipedia article** it denotes;
   the primary identity of a time is a **canonical temporal anchor**. Nothing is
   identified by free text alone.
2. **Classification is layered onto identity, not fused with it.** An entity's
   identity (which article) is separate from its *types* (what kind of thing) and
   *domains* (what field). Identity is near-immutable; classification can be
   refined.
3. **Fixed top-level, reviewed growth.** Top-level categories, domains, temporal
   roles, and precision levels are a fixed, stable set. New subtypes/roles may be
   *proposed* (by the LLM) but enter a **review queue** before becoming canonical.
   This keeps scoring/caching stable while staying extensible.
4. **Scores are component vectors with a default blend.** No score is a single
   opaque number. Each score stores its **named components**; a default weight
   vector blends them; consumers (lenses/filters) may re-weight at query time
   without recomputation.
5. **Deep time, intervals and points.** Temporal modeling spans BCE through the
   present (and geological/cosmological magnitudes), as both instants and bounded
   intervals, with explicit precision, fuzziness, and calendar awareness.
6. **Deterministic, versioned caching.** Every derived fact has a deterministic
   key, a provenance pointer, and version stamps. Caches grow monotonically and
   recompute lazily on version bumps. The cache *is* the materialized knowledge.
7. **Explainability is mandatory.** Any comparison can emit the exact grounded
   reasons behind it (shared entities, overlapping interval, link paths), because
   the component breakdown is always retained.

---

## 2. Overview

```
   CONTENT-TOPIC LAYER                         TEMPORAL LAYER
   -------------------                         --------------
   Entity (=canonical article)                 TimeAnchor (point | interval)
     +-- Types   (structural facet)              +-- precision (day..eon)
     +-- Domains (thematic facet)                +-- kind (point/interval/era/fuzzy/relative)
     +-- aliases, specificity (corpus df)        +-- calendar, deep-time scalar
   Mention (occurrence w/ provenance) --resolves--> Entity
   Fact = (Entity, assertion, provenance) --dated-by--> TimeAnchor (with a role)

                     \                          /
                      \                        /
                       v                      v
                 +------------------------------------+
                 |        RELATEDNESS ENGINE          |
                 |  content_relatedness (components)  |
                 |  temporal_proximity  (components)  |
                 |  -> component vector + default blend|
                 |  -> gates + threshold + WHY         |
                 +------------------------------------+
```

The unit that flows through the system is a **Fact**: an assertion grounded in a
passage, about one or more Entities, optionally anchored to a TimeAnchor via a
**temporal role**. Facts are what get scored, cached, and compared.

---

## 3. Content-Topic Layer

### 3.1 Identity

- **Entity** -- a thing. Primary identity = the canonical Wikipedia article it
  denotes.
  - `entity_id = "ent:wiki:{title_id}"` where `title_id` is resolved through
    `redirect_map` to the canonical article (aliases/redirects collapse here).
  - **Fallback** when a mention does not resolve to an article:
    `entity_id = "ent:surf:{primary_type}:{normalized_surface}"`
    (normalized = casefold, trim, collapse whitespace/punctuation). A fallback
    entity is *promotable*: if later resolved to an article, it is merged into the
    `ent:wiki:` id with an audit record (Sec. 7.4).
- **Mention** -- one occurrence of an entity in a passage:
  `{section_key, char_start, char_end, surface, resolved_entity_id, confidence}`.
  Many mentions -> one Entity. Coreference groups mentions within a passage.
- **Fact** -- `{fact_id, primary_entity_id, other_entity_ids[], assertion_kind,
  provenance, temporal_ref?}`. The grounded unit of knowledge.

### 3.2 Classification: two orthogonal facets

Identity answers *which thing*. Classification answers *what kind* and *what
field*, as **two independent facets** (an entity carries both):

**Facet A -- Type (structural: what kind of thing).** Fixed top-level CATEGORY,
with reviewed SUBTYPE growth. `type = "CATEGORY:SUBTYPE"`.

Top-level categories (fixed):

| CATEGORY | Covers |
|---|---|
| PERSON | individual people (real or fictional-as-subject) |
| GROUP | peoples, nationalities, ethnic/religious/political groups (NORP) |
| ORG | institutions, companies, governments, bodies |
| PLACE | countries, cities, regions, structures, geographic features |
| EVENT | bounded happenings (wars, battles, elections, disasters, ceremonies) |
| CONCEPT | ideas, theories, ideologies, fields, methods, doctrines |
| WORK | created intellectual/artistic artifacts (books, art, treaties, laws, software, speeches) |
| OBJECT | physical artifacts, products, technologies-as-objects, materials-in-use |
| NATURAL_KIND | species, substances/elements, celestial/geological bodies |
| TIME | temporal references (bridge to the Temporal Layer) |

(An entity may hold several types; a **primary_type** is chosen by type weight.)

**Facet B -- Domain (thematic: what field).** Fixed top-level, reviewed growth.
Domains are the basis for lenses/filters. Proposed v1 set:

```
Politics & Government | War & Military | Science & Discovery |
Technology & Engineering | Economy & Finance | Arts & Culture |
Religion & Philosophy | Society & People | Law & Treaties |
Nature, Environment & Climate | Health & Medicine | Exploration & Geography |
Media & Portrayals
```

Domain is assigned (a) directly from subtype semantics via a **subtype->domain
map** (e.g. `PERSON:SCIENTIFIC -> Science & Discovery`, `EVENT:ECONOMIC -> Economy
& Finance`), and/or (b) from the entity's own domain tags. An entity may belong to
multiple domains; a **primary_domain** is chosen by weight. Type and Domain are
orthogonal: `EVENT:ECONOMIC` is type EVENT, domain Economy.

**Reviewed growth.** The LLM may propose a new subtype or domain; it is written to
a `taxonomy_candidate` queue with mention counts and never used for canonical
scoring until promoted (version bump). Mirrors the existing era-candidate pattern.

### 3.3 Salience & attribution (per entity, per passage)

How central is the entity *to this passage* -- distinct from how important it is in
general. Aligns with entity-aware attribution:

- **attribution** in `{core, related_same_article, related_linked_article,
  meta_reference}` (is this fact about the topic, or about a neighbor, or a
  portrayal).
- **salience** in [0,1] from: subjecthood (grammatical subject vs incidental
  mention), mention frequency / coreference count, and position (title/lead vs
  deep body).

### 3.4 Specificity (corpus-level informativeness)

Rare entities carry more signal than ubiquitous ones. Define
`specificity(entity) = idf` from corpus document frequency:
`idf = log(N / (1 + df))`, normalized to [0,1]. "Germany" is low-specificity;
"Manhattan Project" is high. Cached in the entity registry, refreshed as the
corpus grows.

### 3.5 Content scoring

All components in [0,1]; all stored; default blends shown are tunable (Sec. 8).

**Per (entity, passage) -- `entity_passage_score`:**

| Component | Meaning | Default w |
|---|---|---|
| type_weight | prior importance of the entity's primary type | 0.20 |
| salience | centrality to this passage (Sec. 3.3) | 0.30 |
| specificity | corpus informativeness (Sec. 3.4) | 0.25 |
| confidence | extraction trust (seed < llm < reviewed) | 0.15 |
| centrality | mention count / lead position | 0.10 |

`entity_passage_score = sum(w_i * component_i)`. Stored with the component vector.

**Per (passage A, candidate B) -- `content_relatedness`:**

| Component | Meaning | Default w |
|---|---|---|
| entity_overlap | weighted shared-entity overlap (weighted Jaccard, each shared entity weighted by specificity*salience) | 0.40 |
| domain_overlap | shared domains/types | 0.15 |
| graph_path | link/concept-graph signal: f(path_count, min_hops) | 0.25 |
| embedding_sim | passage embedding cosine (optional) | 0.20 |

`content_relatedness = sum(w_i * component_i)`. Stored with components and the
list of shared entities (for the WHY).

### 3.6 Content comparison operators

- **identity** -- equal canonical `entity_id` (after redirect/alias collapse).
- **alias/near-identity** -- alias-table match or high embedding similarity for
  unresolved fallbacks (feeds entity resolution, Sec. 7.4).
- **weighted set overlap** -- weighted Jaccard over entity sets.
- **graph distance** -- shortest path / path count in the link or concept graph.

### 3.7 Content caches

- `entity_registry`: `entity_id -> {canonical_title_id|surface, types[],
  primary_type, domains[], primary_domain, aliases[], df, specificity,
  ontology_version}`.
- `mention_cache`: `(section_key, span) -> {entity_id, attribution, salience,
  confidence, model_version, provenance}`.
- `entity_passage_score`: `(entity_id, section_key) -> {components{}, blend,
  ontology_version, model_version}`.
- `content_relatedness_cache` *(optional; else computed on demand from the per-item
  caches)*: `(section_key_A, candidate_B) -> {components{}, blend, shared[],
  provenance}`.

---

## 4. Temporal Layer

### 4.1 Representation: points, intervals, deep time

- **TimeAnchor** -- canonical temporal object, two kinds:
  - **TimePoint** -- an instant at some precision. `tp:{iso}` (e.g. `tp:1945-07-16`).
  - **TimeInterval** -- a bounded `[start, end]`. `ti:{kind}:{...}`
    (e.g. `ti:year:1945`, `ti:range:-0044-01-01:-0044-12-31`, `ti:era:renaissance`).
- **Deep-time scalar (comparison axis).** Every anchor stores a totally-ordered
  numeric pair `(t_start, t_end)` on a single signed axis so any two anchors are
  comparable regardless of era:
  - **Dated range** (day..year precision): signed proleptic-Gregorian day number.
  - **Deep time** (decade..eon, BCE, geological/cosmological): signed
    `year_scalar` (a float year; negative = BCE; magnitude may reach 1e9+ for
    geology/cosmology). Precision selects which resolution is meaningful.
  - BCE uses **astronomical year numbering** internally (1 BCE = year 0), with the
    human label preserved separately.
- **Calendar awareness.** Store the source `calendar` (gregorian | julian | other)
  and the original `label`; normalize to **proleptic Gregorian** for the scalar.
- **Open/unknown bounds.** Intervals may be half-open (ongoing, or unknown start/
  end) with an explicit `open_start`/`open_end` flag.

### 4.2 Precision levels (fixed)

`instant < day < month < season < year < decade < century < millennium < era <
geological < fuzzy`. Precision drives the comparison horizon (Sec. 4.5) and is
metadata on every anchor.

### 4.3 Kinds: exact, fuzzy, relative, era, recurring

- **exact** -- point or crisp interval.
- **fuzzy/approximate** -- "early 1980s", "circa 1500", "mid-19th century" ->
  interval `[lo, hi]` plus a `center` and `spread`, and reduced `confidence`.
- **relative** -- "three years after X" -> resolved against the referenced
  entity's anchor when available; else stored `relative_unresolved` with the
  reference id, for later resolution.
- **era** -- named periods -> canonical interval via an `era` table + aliases;
  unknown era phrases -> `era_candidate` review queue (existing pattern).
- **recurring** *(reserved)* -- periodic references; out of v1 scope, modeled
  later as a generator over intervals.

### 4.4 Temporal role (what a date means for a fact)

A date is meaningless without its relationship to the fact. Fixed top-level roles,
reviewed growth:

```
birth | death | start | end | founded | dissolved | reign | active |
created/published | discovered/proposed | verified/confirmed | occurred |
flourished | destroyed | enacted | observed
```

Crucial for precision: a scientific result can carry both `proposed` and
`verified` dates; a state carries `founded` and `dissolved`. The role is stored on
the `(fact -> anchor)` link, not on the anchor.

### 4.5 Temporal scoring

**Per anchor:** `precision_score` (sharper precision -> higher placement
confidence) and extraction `confidence`. Stored.

**Per (anchor A, anchor B) -- `temporal_proximity` (components, [0,1]):**

| Component | Meaning | Default w |
|---|---|---|
| overlap | interval intersection over union (IoU); 1.0 if one contains the other | 0.45 |
| adjacency | adaptive-decay closeness of the gap when they do not overlap | 0.35 |
| containment | degree one interval sits inside the other | 0.20 |

- **Adaptive decay.** Closeness uses `exp(-gap / horizon)`, where `horizon`
  **scales with the coarser precision/era** of the pair: years for modern dated
  events, centuries for antiquity, eons for geology. So "20 years apart" is near
  for two modern events but negligible for two geological ones. Horizons are
  named, versioned parameters (Sec. 8).
- Directional ordering (`before`/`after`) is also recorded for narrative use but
  is not part of the proximity magnitude.

### 4.6 Temporal comparison operators

- **Allen's interval algebra** as the relation primitive: `before, meets,
  overlaps, starts, during, finishes, equals` (+ inverses). Robust, standard,
  explainable.
- **IoU** for fuzzy overlap magnitude.
- **adaptive-decay distance** for gaps (Sec. 4.5).
- **scalar order** on `(t_start, t_end)` for sorting/zooming across all eras.

### 4.7 Temporal caches

- `time_anchor_registry`: `time_id -> {kind, precision, calendar, label,
  t_start, t_end, open_start, open_end, center, spread, confidence,
  ontology_version}`.
- `era_table` + `era_candidate` queue (reviewed growth).
- `fact_time`: `(fact_id|section_key, time_id, role, confidence, provenance,
  source)` where `source in {rule_based_seed, spacy_seed, agent_temporal_v1,
  agent_temporal_reviewed_v1}` (extractor-agnostic to consumers).

---

## 5. Relatedness engine (over both layers)

### 5.1 Component vector + default blend + lens reweighting

A candidate's overall relatedness to a focus fact/passage is a **vector** of the
content and temporal components above, plus a **default blend**:

```
relatedness.components = { ...content_relatedness.components,
                           ...temporal_proximity.components,
                           graph_path, embedding_sim }
relatedness.default    = Wc * content_relatedness + Wt * temporal_proximity
                         (default Wc=0.6, Wt=0.4)   # both tunable, versioned
```

Consumers (lenses/filters) supply an alternative weight vector at query time
(e.g. a "finance" lens upweights Economy-domain overlap; a "contemporaries" view
upweights temporal_proximity). **No recomputation** is needed -- only the blend
changes, because components are cached.

### 5.2 Gates (relationship + temporal + threshold)

Relatedness magnitude alone does not admit an item; it must pass **gates** the
application configures:

- **Relationship gate** -- at least one of: shared canonical entity, shared
  high-specificity tag, or a link/concept-graph path within K hops.
- **Temporal gate** -- the candidate's anchor overlaps (or is within an
  adaptive-decay horizon of) the focus envelope.
- **Threshold gate** -- blended (or lens-weighted) relatedness >= a configurable
  threshold (this is the knob a volume control maps to).

The ontology defines the gates and the score; the *application* sets K, horizons-
in-use, and thresholds. (This is exactly what the timeline's L1/L2 inclusion rule
consumes.)

### 5.3 Explainability output

Every admitted comparison emits a grounded WHY object:
`{shared_entities[], shared_domains[], allen_relation, overlap_or_gap,
graph_paths, top_components[]}` -> renders as "Shares Manhattan Project and
Germany; overlapping 1942-1945; 2 link paths." Always derivable because components
+ shared lists are retained.

### 5.4 Granular relevance scoring (anti-clumping)

**Problem statement.** A naive score -- a large constant prior plus a few small,
hard-capped additive bonuses -- collapses many candidates onto identical values
(score clumping). Differentiation must be *built in*, not hoped for. Five
construction rules make relevance granular by design:

**Rule 1 -- Continuous inputs only; no hard caps.** Replace every `min(count*w,
cap)` with a smooth saturating transform that keeps rising with diminishing
returns and never hits a flat ceiling:
```
sat(x; k) = 1 - exp(-k * x)        # x >= 0, in [0,1), strictly increasing
```
So "shares 10 strong entities" always outranks "shares 3", instead of both
pinning to the cap.

**Rule 2 -- Specificity-weight every overlap.** Overlap contribution is not a
count; it is a sum of per-item informativeness:
```
raw_entity = sum over shared e of  specificity(e) * min(salience_A(e), salience_B(e))
S_entity   = sat(raw_entity; k_e)
```
Sharing "Manhattan Project" (high IDF) moves the score far more than sharing
"United States" (low IDF). Because candidates differ in *which* entities they
share, scores separate naturally.

**Rule 3 -- Embedding cosine as the continuous backbone.** A real-valued cosine
almost never ties, so it is the primary separator and tie-breaker:
```
S_embed = (cosine(emb_A, emb_B) + 1) / 2     # map [-1,1] -> [0,1]
```
This requires the embedding store to actually be populated (see note below).

**Rule 4 -- Level/link-rank are weak priors, not the anchor.** Source level (L1/L2)
and link position become a small additive prior (e.g. <= 0.10 total), never the
dominant constant. Ordering is driven by the continuous signals above.

**Rule 5 -- Set-relative normalization for display/order.** Absolute weighted sums
bunch up; after scoring all candidates for a focus, spread them across the full
range before ordering/bucketing:
```
relevance_norm = (raw - min_raw) / (max_raw - min_raw)     # min-max per focus
# or rank-percentile, or softmax with a tunable temperature to control spread
```
Keep the **raw component vector** for gates/thresholds and the WHY; use
`relevance_norm` for ranking and visual bucketing. This guarantees separation
even when raw scores cluster.

**Composite (within-content) signal, continuous:**

| Signal | Source | Default w |
|---|---|---|
| S_embed | passage embedding cosine (Rule 3) | 0.35 |
| S_entity | specificity-weighted shared entities (Rule 2) | 0.30 |
| S_graph | `sat(path_count; a)` discounted by min_hops | 0.20 |
| S_domain | domain/type alignment | 0.10 |
| prior | level + link-rank prior (Rule 4) | 0.05 |

`content_relevance = sum(w_i * S_i)`; overall relevance blends this with
`temporal_proximity` per Sec. 5.1; then `relevance_norm` per Rule 5.

### 5.5 Tie-break cascade

For any residual exact ties, break deterministically in this order:
`S_entity (specificity-weighted) -> S_embed -> S_graph -> temporal_proximity ->
link_rank -> title`. Deterministic so caching/ordering is stable.

### 5.6 Hybrid ontology + vector (recommended)

Use both, for different jobs:

| Job | Use | Why |
|---|---|---|
| **Gate** (admit/reject) | ontology | grounded, explainable: must share an entity/tag or a graph path, and pass the temporal gate |
| **Rank** (order admitted) | ontology + vector blend | embedding gives continuous, semantic resolution; specificity-weighted overlap + graph give grounded structure |
| **Explain** (the WHY) | ontology | shared entities / interval / paths are concrete and auditable |
| **Recall** (catch missed kin) | vector | finds semantically related passages that share no surface tag or link |

Their errors are complementary: ontology misses paraphrase/semantic kinship with
no shared surface; embeddings assert topical nearness without grounding. Gating
with ontology constrains the embedding's failure mode; blending the embedding into
ranking constrains the ontology's. Net effect: **more granular ordering and fewer
ties**, with grounding and explainability preserved.

> **Implementation note (app status, not architecture).** A vector store and
> cosine query may exist while the **embedding column is unpopulated** -- in which
> case S_embed silently contributes 0 and the score loses its main separator. An
> embedder worker must actually write vectors (once per passage, cached) before
> the hybrid model delivers its granularity benefit. Treat "embeddings populated"
> as a precondition for Rule 3.

### 5.7 Measuring granularity

Make clumping observable so tuning is data-driven. Per focus, log:
- **score dispersion** -- stdev of the top-K relevance values;
- **tie rate** -- fraction of adjacent pairs with equal (rounded) score;
- **mean nearest-neighbor gap** -- average |score_i - score_{i+1}| in the ranked list.
Tuning (saturation constants `k`, weights, normalization temperature) aims to
*raise dispersion and lower tie rate while preserving correct ordering* (NDCG/MAP
against any human-labeled pairs). These feed the same instrumentation loop used
for agent-count tuning.

---

## 6. Identity management & caching

### 6.1 Keys & provenance

Deterministic keys everywhere: `entity_id`, `time_id`, `section_key =
{title_id}:{heading_id}`, `fact_id`. Every derived row carries a **provenance**
pointer `{title_id, heading_id, char_start, char_end}` and a `run_id` tying it to
an extraction trace.

### 6.2 Versioning stamps

Three independent version stamps on derived rows:
- `ontology_version` -- categories, domains, roles, precision set, default weights,
  horizons.
- `model_version` -- the extractor model/prompt.
- `parser_version` -- wikitext cleaning/link parsing.

### 6.3 Invalidation & growth

- Content/snapshot is static (no revision history), so invalidation is driven by
  **version bumps**, not source changes.
- Bumping **default weights or horizons** (`ontology_version`) does **not** require
  re-extraction -- blends recompute from cached components.
- Bumping **taxonomy membership** (new promoted subtype/domain) may trigger lazy
  re-classification of affected entities only.
- Caches grow **monotonically**; popular paths converge toward fully materialized.

### 6.4 Entity resolution (merge/split) with audit

- **Deterministic first.** Match by canonical id / redirect / alias before any
  fuzzy merge ("no blind merge").
- **Promotion.** A `ent:surf:` fallback that later resolves to an article is
  remapped to its `ent:wiki:` id; old references are forwarded via an
  `entity_alias_map`, with an audit row.
- **Possible-match.** Sub-threshold fuzzy candidates are stored as auditable
  `possible_match` links, never silently merged.

---

## 7. Governance & versioning

- A **review queue** governs all reviewed-growth sets (subtypes, domains, temporal
  roles, eras): proposals accrue with counts/examples; promotion is an explicit,
  version-bumping act.
- An `ontology_version` record pins: the category set, subtype set, domain set,
  subtype->domain map, type weights, temporal role set, precision levels, default
  score weights, decay horizons, and gate defaults.
- Changing scoring weights/horizons is cheap (recompute blends); changing
  membership is moderate (lazy re-classify); changing identity rules is the only
  expensive change and is rare.

---

## 8. Reference tables (v1 defaults -- all tunable, versioned)

**8.1 Type weights (priors).** Carried over and consolidated from the existing
hierarchical taxonomy; high for politically/historically pivotal types (e.g.
`EVENT:WAR`, `PERSON:POLITICAL` ~ 0.9-1.0 normalized), low for incidental
(`MISC`, `TIME:DURATION` ~ 0.1-0.3). Full table maintained in the taxonomy module;
this framework only requires that each canonical type has a weight in [0,1].

**8.2 Default score weights.** As tabulated in Sec. 3.5, 4.5, 5.1.

**8.3 Decay horizons (per coarser-precision bucket).**

| Bucket | Horizon (gap at which closeness ~ e^-1) |
|---|---|
| day/month | ~1 year |
| year | ~10 years |
| decade | ~50 years |
| century | ~300 years |
| millennium/era | ~2000 years |
| geological | ~1e6+ years |

**8.4 Gate defaults.** graph hops K=2; temporal gate = overlap OR within 1 horizon;
threshold = mid (consumer/volume-slider overrides).

---

## 9. Glossary

- **Entity** -- a thing, identified by its canonical Wikipedia article (fallback:
  normalized surface + type).
- **Type / Domain** -- orthogonal classification facets: structural kind vs
  thematic field. Both fixed-top-level, reviewed growth.
- **Fact** -- a grounded assertion about entities, optionally dated via a role.
- **TimeAnchor** -- canonical point or interval, comparable on a signed deep-time
  scalar, with precision/kind/calendar.
- **Temporal role** -- what a date means for a fact (birth, proposed, verified...).
- **Specificity** -- corpus IDF; rarer entity = more signal.
- **Component vector + default blend** -- scores store named components plus a
  tunable blend; lenses re-weight without recompute.
- **Gates** -- relationship + temporal + threshold conditions an item must pass to
  be admitted.
- **Reviewed growth** -- new categories/roles are proposed, queued, and promoted
  by an explicit version bump.
