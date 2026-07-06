# Weighted Section-Route Scoring Whitepaper v0.1

## Purpose

This document explains how V4 scores related article context for a loaded
article, why the older scoring behavior caused bad graph promotions, and what
the current scoring architecture is meant to do.

The key product goal is simple:

> When a reader is looking at one section of an article, show the most relevant
> surrounding context for that section, not merely the articles that are nearby
> in the raw link graph.

That means scoring must be section-specific, weighted, inspectable, and
separate from neutral graph storage.

## Background

The V4 reader has three related but different graph concepts:

1. The neutral graph framework.
2. The weighted relevance layer.
3. The promoted display layer.

These must not be collapsed into one field called `score`.

The neutral graph tells us what is connected:

- Article has sections.
- Section links to article.
- Article links to other articles.

The weighted relevance layer tells us why something is relevant:

- Semantic similarity.
- Entity overlap.
- Direct graph path strength.
- Reciprocal/backlink strength.
- Domain overlap.
- Temporal overlap or proximity.
- Prioritization signals.

The promoted display layer tells us what should be visible first:

- Top section-scoped weighted routes.
- Nodes connected through those promoted routes.
- Context edges attached to promoted nodes.

## The Main Mistake We Hit

The graph view was promoting articles using structural/default graph scores.

Example:

- In the Abraham article, several promoted nodes showed score `0.62`.
- `Fear and Trembling` appeared promoted.
- `Islam` and other religious-tradition topics were underrepresented.

That `0.62` was not a weighted relevance score. It came from the neutral
Neo4j graph frontier. It meant roughly:

> This is a nearby direct graph link.

It did not mean:

> This candidate has been scored as highly relevant to this source section.

This caused a false promotion path:

1. Neo4j returned direct L1 links.
2. Direct links received default/structural scores.
3. Graphology backfilled those edge scores onto article nodes.
4. Article-level promotion ranked the nodes.
5. Some unscored links became visible as promoted nodes.

The result looked like relevance but was actually graph proximity.

## Why Article-Level Promotion Was Wrong

Article-level promotion asks:

> Is this candidate article globally important to the source article?

But the reader experience needs:

> Is this candidate relevant to this specific L0 section?

Those are not the same question.

For Abraham:

- `Fear and Trembling` may be relevant to the `Literature` section.
- `Islam` is relevant to the lead, `Religious traditions`, and `Islam`
  sections.
- `Hagar` is relevant to the `Hagar` section.
- `Cave of the Patriarchs` is relevant to `Later years`.

If the system promotes at article level, a candidate with one strong or
structurally lucky connection can appear as globally important. This creates
clustering around whichever sections were processed first or whichever links
had high default graph proximity.

The corrected model promotes section routes:

> Source section -> candidate article

The candidate article node becomes visible because one or more section routes
are promoted. The node is not the source of truth for promotion.

## Current Data Roles

### Neo4j

Neo4j stores the neutral durable graph:

- `(:Article)`
- `(:Section)`
- `(:Article)-[:HAS_SECTION]->(:Section)`
- `(:Section)-[:LINKS_TO]->(:Article)`
- `(:Article)-[:LINKS_TO]->(:Article)`

Neo4j should not store `L1` or `L2` as durable node properties.

L1 and L2 are query viewpoints:

- L1: one hop from the current article/section.
- L2: two hops from the current article/section.

### Postgres `related_cache`

`related_cache` stores weighted relevance routes:

- `from_section_key`
- `to_title_id`
- `to_title`
- `level`
- `score`
- `signals_json`
- `why_text`

This table is the current source of truth for weighted section-route relevance.

### Graph API Projection

The graph API overlays `related_cache` onto the neutral graph as scored
section routes:

- source: L0 section node
- target: candidate article node
- relation: `related_to`
- score source: `related_cache`
- level: computed view level

Raw wikilinks remain present, but they are structural context. They do not
drive promotion.

## Current Weighted Scoring Components

The scoring layer builds a component vector for each candidate route.

There are three score-like numbers in the system. They must not be confused:

| Name | Where it appears | Meaning |
| --- | --- | --- |
| `raw_score` | `related_cache.signals_json.raw_score`, `content_relatedness_cache.raw_score` | Direct weighted blend of component signals. This is the closest value to the mathematical relevance model. |
| `relevance_norm` / `related_cache.score` | `related_cache.score`, graph edge `score` | Display/ranking score after local normalization inside the candidate set for that source section. This is the score used by route ranking and graph promotion. |
| `best_route_score` | graph article node attribute | Display aggregate on an article node: the best promoted route score pointing to that node. It is not the source of truth for promotion. |

The route is authoritative. Article-node scores are summaries of one or more
section routes.

### Content Components

The content score currently uses:

| Component | Meaning |
| --- | --- |
| `S_embed` | Embedding similarity between source section and candidate sections. |
| `S_entity` | Weighted overlap of extracted entities. |
| `S_graph` | Direct graph-path signal based on level and link rank. |
| `S_backlink` | Candidate article links back to the source article. |
| `S_domain` | Ontology domain/type overlap. |
| `prior` | Small tie-breaking prior from graph level/rank. |

Current weights:

```text
S_embed    0.30
S_entity   0.27
S_graph    0.17
S_backlink 0.10
S_domain   0.10
prior      0.06
```

The important change is `S_backlink`.

A candidate article that links back to the source article is usually more
meaningfully related than a candidate that only appears because the source links
out to it. This is especially useful for broad topics where outbound links can
be noisy.

Backlinks are not allowed to promote by themselves. They are one weighted
signal inside the scoring formula.

### Temporal Components

The temporal score currently uses:

| Component | Meaning |
| --- | --- |
| `overlap` | Exact time-anchor overlap. |
| `adjacency` | Near-time relationship between anchors. |
| `containment` | Coarse interval containment or overlap. |

Current weights:

```text
overlap     0.45
adjacency   0.35
containment 0.20
```

### Overall Relatedness

Current blend:

```text
content  0.60
temporal 0.40
```

This means a candidate with strong topical relevance but no temporal evidence
can still score, but it will not receive the full score possible for candidates
that are both topical and temporal.

## Known Scoring Tension

Many Abraham rows have temporal score `0.0`.

That suppresses otherwise meaningful religious/topic candidates. For example:

- `Abrahamic religions` from the lead scored around `0.59`.
- `Prophets and messengers in Islam` from the lead scored around `0.51`.
- Some highly relevant broad religion topics were lower because temporal
  anchors were absent or not yet extracted.

This is not automatically wrong, but it means the UI should avoid interpreting
`0.50` as weak in all contexts. In a mostly non-temporal section, `0.50` may be
a useful content match.

### Normalization

After raw component scoring, candidate scores are normalized within the current
source section's candidate set:

```text
local_rank = (raw_score - min_raw_score) / (max_raw_score - min_raw_score)

if max_raw_score <= 0.30:
  related_cache.score = raw_score
else:
  rank_weight = 0.30
  if max_raw_score < 0.42:
    rank_weight is damped between 0.00 and 0.30
  related_cache.score = raw_score * (1.0 - rank_weight) + local_rank * rank_weight
```

This keeps absolute signal meaningful while still creating local separation
between candidates that would otherwise sit in a narrow score band.

The weak-section guard is important: a section where every candidate is poor
must not promote its best candidate merely because it ranked first locally.

Interpretation:

- `raw_score` answers: "How strong are the measured signals?"
- `related_cache.score` answers: "How strong is this route among the candidate
  routes for this source section?"

This is why scores should be compared most confidently within a section. Cross-
section comparison is still useful for display, but it is slightly less pure
because each section has its own candidate distribution.

### Current Score Bands

The graph projection currently labels strength as:

```text
high   >= 0.72
medium >= 0.52 and < 0.72
low    < 0.52
```

Promotion is not simply "all high rows." Promotion uses section budgets and a
fallback floor:

```text
strong_threshold = 0.62
fallback_floor   = 0.42
min_promoted     = 12
max_promoted     = 72
```

That means a medium-score row can be promoted if it is one of the best weighted
routes for its source section. This is intentional. Otherwise broad but useful
sections with no temporal anchors would appear empty.

Observed Abraham diagnostic ranges after weighted scoring:

```text
L1 score range: about 0.09 to 0.83
L2 score range: about 0.05 to 0.83
```

Those ranges overlap. Therefore, L2 is not automatically lower-quality than
L1. The system treats L2 as higher-risk because it is one hop farther, not
because its numeric score must always be lower.

## L1 Versus L2 Scoring

L1 and L2 use the same component-vector formula. The difference is not a
different relevance model; the difference is path semantics and graph priors.

### L1 Route

An L1 route is:

```text
L0 section -> directly linked article
```

L1 has strong deterministic evidence because the source section explicitly
linked the candidate article.

Current L1 graph components:

```text
S_graph = max(0.50, 0.95 - link_rank * 0.035)
prior   = max(0.35, 0.76 - link_rank * 0.025)
estimated_cost = 0.35
```

L1 acceptance is intentionally permissive:

```text
accepted if graph_ok and content_candidate and not self_reference
```

Reason: a direct section link is already meaningful reader-facing context. It
may not deserve promotion, but it should generally remain eligible as a scored
connection.

### L2 Route

An L2 route is:

```text
L0 section -> L1 bridge article -> L2 article
```

L2 has weaker direct evidence and more topic-drift risk. It therefore receives
lower graph priors and must pass at least one additional relevance signal.

Current L2 graph components:

```text
S_graph = max(0.22, 0.62 - link_rank * 0.025)
prior   = max(0.18, 0.45 - link_rank * 0.018)
estimated_cost = 0.55
```

When the L2 candidate comes through a scored L1 bridge, the L2 graph/prior
components are scaled by bridge strength:

```text
bridge_factor = 0.5 + 0.5 * bridge_route_score
S_graph_l2 = S_graph_l2_base * bridge_factor
prior_l2   = prior_l2_base * bridge_factor
```

This makes an L2 through a strong L1 route rank differently from an L2 through
a weak or incidental L1 route.

Current L2 acceptance requires the row to be graph-valid, content-like,
non-self-referential, and have at least two independent supporting signals, or
one supporting signal with a strong score:

```text
supporting_signal_count >= 2
or score >= 0.62 with at least 1 supporting signal
```

Supporting signals currently include:

```text
S_entity >= 0.12
S_embed >= 0.32
S_backlink >= 0.34
S_domain >= 0.36
S_graph >= 0.50
temporal_signal >= 0.45 or temporal_score >= 0.32
content_score >= 0.42
```

Because L2 `S_graph` usually starts below `0.50`, L2 generally needs embedding,
entity, domain, temporal, backlink, or normalized-score support. This is
intentional: L2 should explain or extend the section, not merely be reachable.

### Practical Difference

| Area | L1 | L2 |
| --- | --- | --- |
| Meaning | Direct section link | Two-hop context through a bridge article |
| Candidate eligibility | All direct section links are scored | Bounded by selected L1 parents and per-parent frontier limits |
| Graph prior | Strong | Moderate/weak |
| Acceptance | Permissive because direct link exists | Requires extra signal or score floor |
| Promotion budget | Separate per-section L1 budget | Separate per-section L2 budget |
| Display | "L1" | "L2 via `<bridge>`" |
| Main risk | Over-promoting generic direct links | Topic drift / noisy two-hop expansion |

### Candidate Limits

Current settings still contain:

```text
related_l1_limit = 12
related_l2_per_l1_limit = 6
related_rank_candidate_limit = 24
```

Their current intended meaning:

- `related_l1_limit` limits which L1 parent articles are expanded into L2. It
  must not slice direct L1 scoring eligibility. Parent selection is made after
  weighted L1 scoring, not from raw link order.
- `related_l2_per_l1_limit` limits the number of outbound links read from each
  selected L1 parent.
- `related_rank_candidate_limit` limits L2 candidates passed through the
  expensive ranking path after all direct L1 links are kept.

In other words:

```text
all direct L1 links remain score-eligible
L2 expansion is bounded to the top scored L1 bridges
compute order is prioritized
eligibility is not arbitrary link-order slicing
```

Future scoring should make the display language sensitive to missing temporal
coverage:

- `content-strong`
- `temporal-strong`
- `mixed`
- `content-only`
- `low-confidence`

## Candidate Collection Problems We Hit

The old collection path had hard caps:

```text
related_l1_limit = 12
related_l2_per_l1_limit = 6
related_rank_candidate_limit = 24
```

The most harmful behavior was slicing direct L1 links before scoring:

```text
l1_links[:related_l1_limit]
```

For sections with many links, such as Abraham's lead, important direct links
could appear after the first 12 links. Those candidates were never scored.

This is wrong because direct L1 links are deterministic section evidence. They
should be eligible for weighted scoring.

The corrected policy:

- collect all direct L1 links for the section
- score all direct L1 links
- bound L2 expansion separately
- use priority ordering to decide compute order, not eligibility

## Promotion Rules

Promotion now operates on scored section routes.

Eligible route:

```text
edge.relation == related_to
edge.score_source == related_cache
```

Non-eligible route:

```text
edge.relation == wikilink
edge.score_source == structural_frontier
```

Promotion flow:

1. Group scored routes by source L0 section.
2. Rank routes inside each section by weighted score.
3. Promote top routes above the fallback floor.
4. Use a global top-up only if the graph has too few promoted routes.
5. Mark target article nodes visible because they have promoted routes.
6. Keep other weighted routes as context.
7. Keep raw structural links unpromoted.

This gives section distribution and prevents the first processed cluster from
owning the graph.

## L2 Promotion Problem

After moving promotion to section-scoped weighted routes, L1 candidates still
tend to dominate promoted graph space.

This is expected from the current scoring shape:

- L1 has direct source-section evidence.
- L1 receives stronger graph priors.
- L1 usually has cleaner attribution.
- L2 routes are one hop farther and have more opportunities for topic drift.

However, this can starve genuinely useful L2 context.

Example pattern:

```text
L0 section: Islam
L1 bridge: Prophets and messengers in Islam
L2 candidate: Muhammad
```

The L2 article may explain the section better than many direct L1 links, but it
must compete against all L1 routes in a shared promotion pool. That is not the
right product behavior.

L2 is not a weaker version of L1. L2 has a different job:

> Add explanatory context that is not directly named or fully explained by the
> source article's immediate links.

## L2 Promotion Policy

L2 promotion should be bridge-aware and budgeted separately.

### Separate Budgets

Each source section should have separate promotion capacity for L1 and L2.

Initial policy:

```text
per source section:
  promote up to 4 L1 routes
  promote up to 3 L2 routes

article-wide:
  promote at least 12 routes when available
  keep a global max route cap of 72
```

This prevents L1 routes from crowding out every L2 route.

### Bridge-Aware Scoring

An L2 route should be understood as:

```text
L0 section -> L1 bridge -> L2 article
```

The L2 candidate should carry:

- `via_title`
- bridge route strength
- bridge backlink signal where available
- L2 target score
- final path score

The displayed explanation should preserve the bridge:

> via Prophets and messengers in Islam

This makes L2 intelligible instead of looking like a random distant article.

### L2 Evidence Floor

An L2 route should not promote just because it is two hops away.

It should have at least two supporting signals, such as:

- strong embedding similarity
- shared specific entity
- same or adjacent temporal anchor
- ontology domain overlap
- backlink to the source article
- backlink to the L1 bridge
- strong bridge route

This is a guard against noisy two-hop expansion.

### L2 Novelty Bonus

L2 should get a small bonus when it adds useful new context not already covered
by promoted L1 routes.

Useful novelty includes:

- new specific entities
- new dates or eras
- new but compatible ontology domain
- reciprocal links that explain why the path matters

Novelty must remain bounded. It is a tie-breaker and context-expansion signal,
not a replacement for relevance.

### L2 Display Rule

The graph and panels should show L2 with its bridge:

```text
Muhammad
L2 via Prophets and messengers in Islam
score: 0.59
```

If the user selects the bridge L1 node, L2 routes through that bridge should
float up first.

## Current L2 Promotion Adjustment

The base weighted score remains the same. L2 currently receives a small
promotion-order adjustment, not a persisted replacement score.

```text
route_promotion_score =
  related_cache.score
  + backlink * 0.04
  + 0.025 if via_title exists
```

The adjustment is deliberately small:

- `related_cache.score` remains the visible and stored score.
- `via_title` helps L2 routes with an explicit bridge sort slightly higher.
- backlink helps reciprocal L2 relationships sort slightly higher.
- this affects promotion ordering only; it does not make an unscored structural
  L2 route promotable.

Future work may add a persisted `l2_path_score`, `l2_bridge_signal`, and
`l2_novelty_signal`, but those fields should not be assumed to exist yet.

## What Promoted Means Now

A promoted article node means:

> At least one source section has a promoted weighted route to this article.

It does not mean:

> This article globally scored high against the whole source article.

The article node may display:

- best route score
- number of promoted routes
- source sections that promoted it
- strongest domains
- entity/time previews

But the route remains the source of truth.

## Backlink Signal

Backlinks are now part of the weighted formula.

Definition:

> `S_backlink` counts whether candidate sections link back to the source article.

For example, while scoring Abraham:

- If a candidate article has links back to `Abraham`, that is useful evidence.
- If an L2 candidate links back to Abraham through its lead/history sections,
  it may deserve a relevance boost.

Why it matters:

- It catches reciprocal conceptual relationships.
- It helps distinguish generic outgoing links from mutually meaningful links.
- It is still cheap and CPU-friendly.

Why it must be bounded:

- Popular articles may link back to many broad concepts.
- A backlink alone does not explain relevance.
- It must work with embeddings, entities, graph path, domains, and temporal
  evidence.

## Relevance Trap Defense

The New Delhi/chess/pollution style trap remains the key benchmark:

> Shared place is not enough.

The scoring layer must avoid promoting candidates merely because they share:

- a broad location
- a broad domain
- a popular article backlink
- a high embedding similarity from generic text

Defense:

- embeddings are one signal, not the authority
- entities are specificity-weighted
- domain overlap has limited weight
- graph proximity has limited weight
- backlinks have limited weight
- temporal disagreement should reduce confidence
- ambiguous cases should be queued for GPU adjudication later

## What Is Still Missing

### Per-section coverage completion

The Abraham diagnosis showed `related_cache` existed for only a subset of
sections. That creates biased promotion even with good scoring.

Needed:

- every content L0 section should get a related-cache pass
- status should show which sections have scored routes
- graph panels should distinguish:
  - unscored structural links
  - scored but not promoted routes
  - promoted weighted routes

### Better temporal-aware score interpretation

The current `content 0.60 / temporal 0.40` blend is good for timeline context,
but the graph view may need an additional label:

- high topical relevance, no temporal evidence
- high temporal relevance, weak topical evidence
- balanced relevance

This can prevent content-strong items from looking weak simply because no dates
are present.

### Score explanations

Cards should eventually expose:

- embedding similarity
- entity overlap
- graph path level
- backlink score
- domain overlap
- temporal score
- final weighted score

This should be compact by default and expandable for inspection.

## Acceptance Tests

### Abraham

Expected:

- `Fear and Trembling` is promoted only if the `Literature` section has a
  weighted route score for it.
- `Fear and Trembling` is not promoted from structural/default graph score.
- `Islam`, `Abraham in Islam`, and `Prophets and messengers in Islam` are
  eligible wherever they are direct L1 section links.
- Promotions distribute across sections after all sections are scored.

### New Delhi Trap

Expected:

- Chess and pollution should not strongly promote each other just because both
  mention New Delhi.
- Shared generic place/domain signal should not rescue a weak candidate.
- The system should require multi-signal agreement or mark the result
  low-confidence.

### Backlink

Expected:

- A candidate that links back to the source article gets a bounded boost.
- Backlink-only candidates do not promote unless other weighted signals support
  them.

## Current Decision

The project should now treat weighted section-route scoring as the only valid
promotion path.

Structural graph proximity is still valuable, but only as:

- graph navigation
- candidate discovery
- weak graph-path component inside weighted scoring
- diagnostic context

It must not directly promote graph nodes.
