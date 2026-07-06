# Graph View -- Data Contract, Connections & Color Spec

**Version:** 0.1 (draft)
**Status:** Contract for the `/graph` view. Renderer = Cytoscape.js, model =
graphology.
**Date:** 2026-06-25
**Encoding:** ASCII only.
**Companions:** `Timeline-Spec-v0.4.md` (Sec. 6.3, 12.1), `Ontology-and-Scoring-Framework-v0.1.md` (scores), `Enrichment-Pipeline-Spec-v0.1.md` (coverage layers).

> Purpose: define the exact JSON the backend serves into **graphology**, the
> **connection model** (including back-edges to the L0 article), and the **color /
> visual encoding** Cytoscape binds to. Weights/values/relevances live in the data;
> the renderer only maps data -> visual channel.

---

## 1. What the graph shows

For a focused article, the view shows a **neighborhood graph**:
- **L0 spine:** the focus article as a hub node + its **section** nodes.
- **L1:** articles directly linked from the focus article's sections.
- **L2:** articles one hop further.
- **All meaningful connections among them**, including **back-edges to L0** (an
  L1/L2 node that also relates to the focus article/sections beyond its tree
  parent) and cross-edges (L1<->L1, L2->other L1). The view is a **graph, not a
  tree** -- back-references are first-class.

Bounded by the focus neighborhood (dozens to low hundreds of nodes), filtered by
lens / level / relevance.

---

## 2. Node model

Node `key` formats (stable, deterministic):
- Focus/L1/L2 article: `art:{title_id}`
- Section (spine): `sec:{title_id}:{heading_id}`
- (optional later layers) Entity: `ent:wiki:{title_id}` / `ent:surf:...`;
  Time: `tp:...` / `ti:...`

**Node attributes:**

| Field | Type | Meaning / use |
|---|---|---|
| `kind` | `article` \| `section` \| `entity` \| `time` | node class |
| `level` | `0` \| `1` \| `2` | ring (0 = spine) -> concentric layout. **Computed at query time from graph distance to the focus article; NOT stored on the node** (a node is L1/L2 only relative to what you opened) |
| `label` | string | display name (section heading or article title) |
| `title_id` | int | source article id |
| `heading_id` | int? | for section nodes |
| `node_type` | enum | ontology type (PERSON/PLACE/ORG/EVENT/CONCEPT/...) |
| `primary_domain` | enum | one of the 13 domains (Sec. 6) -> node color |
| `specificity` | float [0,1] | corpus IDF -> node size |
| `centrality` | float [0,1] | in-neighborhood importance (alt size signal) |
| `relevance_to_focus` | float [0,1] | overall relevance to the spine -> filter/size |
| `coverage` | object | per-layer status (Sec. 3) -> the ledger ring |
| `embedding_state` | `present`\|`missing`\|`stale` | vector availability |
| `provenance` | object | `{title_id, heading_id?}` back-reference |
| `versions` | object | `{ontology, model, parser}` for staleness |

### 2.1 Coverage object (the ledger)

```json
"coverage": {
  "seed":     {"state": "done",    "version": "seed-0.1"},
  "core":     {"state": "done",    "version": "core-1"},
  "entities": {"state": "running", "version": "agent-ent-1"},
  "temporal": {"state": "stale",   "version": "agent-temporal-1"},
  "related":  {"state": "missing", "version": null},
  "review":   {"state": "missing", "version": null}
}
```
`state in {missing, running, done, stale, failed}`. This drives the per-layer
**coverage donut** (Sec. 6.4). `stale` = present but for an older version.

---

## 3. Edge model (connections)

Edge `key`: `{relation}:{source}->{target}` (deterministic; undirected relations
sort endpoints so the key is stable).

**Relation types:**

| `relation` | Meaning | Directed? |
|---|---|---|
| `has_section` | focus article hub -> its section (spine structure) | yes |
| `wikilink` | a `[[ ]]` link from a section to an article | yes |
| `shared_entity` | endpoints share >=1 canonical entity | no |
| `shared_tag` | endpoints share tags (lower-specificity) | no |
| `temporal_overlap` | endpoints' time bounds overlap | no |
| `embedding_sim` | high passage-embedding similarity | no |
| `composite` | multiple of the above combined into one edge | no |

**Neo4j storage mapping (canonical vocabulary, shared with the Multi-Path
Architecture spec).** The API `relation` values map to a small set of Neo4j
relationship types: `has_section` -> `HAS_SECTION`; `wikilink` (section->article
and article->article structural links) -> `LINKS_TO`; the scored relatedness types
(`shared_entity`, `shared_tag`, `temporal_overlap`, `embedding_sim`, `composite`)
-> `RELATED_TO` with a `method` property. There is no `LINKS_TO_L2` type - L1/L2 is
a viewpoint, not a stored level.

**Back-edges to L0 are not a separate relation type** -- they are any of the above
whose other endpoint is an L0 section/article. They carry `back_to_l0: true` so the
renderer can emphasize them (Sec. 6.5). This is how "connecting back to the L0
article" is represented: an L2 node sharing an entity with the focus article gets a
`shared_entity` edge straight to the relevant L0 section, flagged `back_to_l0`.

**Edge attributes:**

| Field | Type | Meaning / use |
|---|---|---|
| `relation` | enum | see table -> edge color/style |
| `weight` | float [0,1] | **blended relevance** -> edge thickness; inverse -> force length |
| `components` | object | `{embedding_cosine, graph_distance:{hops,path_count}, temporal_proximity, entity_overlap}` (from the ontology engine) |
| `confidence` | float [0,1] | -> edge opacity |
| `directed` | bool | structural/link edges true; relatedness false |
| `back_to_l0` | bool | endpoint is L0 -> emphasis |
| `shared` | string[] | the shared entities/tags (for hover/WHY) |
| `provenance` | object | section(s)/spans that justify the edge |
| `versions` | object | components version (for staleness) |

> Weights are **never bucketed into high/medium/low**; `weight` is the continuous
> relevance (consistent with the anti-clumping scoring). Bucket only at render time
> if a legend needs bands.

---

## 4. Response shape (graphology-serializable)

Directly loadable via `graph.import(payload)`:

```json
{
  "attributes": {
    "focus_title_id": 307,
    "focus_title": "Abraham Lincoln",
    "levels": 2,
    "lens": null,
    "min_relevance": 0.2,
    "generated_at": "2026-06-25T10:00:00Z",
    "versions": {"ontology": "v1", "model": "agent-1", "parser": "v4-0.2"}
  },
  "nodes": [
    {"key": "art:307", "attributes": {
      "kind": "article", "level": 0, "label": "Abraham Lincoln",
      "title_id": 307, "node_type": "PERSON", "primary_domain": "Politics & Government",
      "specificity": 0.7, "relevance_to_focus": 1.0,
      "coverage": { "...": "..." }, "embedding_state": "present"}},
    {"key": "sec:307:0", "attributes": {
      "kind": "section", "level": 0, "label": "Introduction",
      "title_id": 307, "heading_id": 0, "primary_domain": "Politics & Government",
      "coverage": { "...": "..." }}},
    {"key": "art:1184", "attributes": {
      "kind": "article", "level": 1, "label": "American Civil War",
      "title_id": 1184, "node_type": "EVENT", "primary_domain": "War & Military",
      "specificity": 0.55, "relevance_to_focus": 0.82,
      "coverage": { "...": "..." }, "embedding_state": "present"}}
  ],
  "edges": [
    {"key": "has_section:art:307->sec:307:0",
     "source": "art:307", "target": "sec:307:0",
     "attributes": {"relation": "has_section", "weight": 1.0, "directed": true}},
    {"key": "wikilink:sec:307:3->art:1184",
     "source": "sec:307:3", "target": "art:1184",
     "attributes": {"relation": "wikilink", "weight": 0.82, "directed": true,
       "components": {"embedding_cosine": 0.71, "graph_distance": {"hops":1,"path_count":4},
         "temporal_proximity": 0.6, "entity_overlap": 0.5}, "confidence": 0.9}},
    {"key": "shared_entity:sec:307:1->art:9920",
     "source": "sec:307:1", "target": "art:9920",
     "attributes": {"relation": "shared_entity", "weight": 0.66, "directed": false,
       "back_to_l0": true, "shared": ["Emancipation Proclamation"],
       "components": {"embedding_cosine": 0.64, "temporal_proximity": 0.8,
         "entity_overlap": 0.7}, "confidence": 0.85}}
  ]
}
```

The third edge is a **back-edge to L0**: an L2-reachable article tied directly to
an L0 section by a shared entity.

---

## 5. Endpoints

- **Neighborhood (primary):**
  `GET /api/graph/article/{title_or_id}`
  query: `levels=2` `lens=<domain|null>` `min_relevance=0.2`
  `include_back_edges=true` `include_cross_edges=true` `max_nodes=200`
  -> the serialized graph (Sec. 4).
- **Expand a node (lazy):**
  `GET /api/graph/node/{key}/neighbors?levels=1&min_relevance=...`
  -> nodes/edges to merge into the existing graphology graph (progressive
  disclosure; supports "expand on click").
- **Coverage refresh (the live ledger):**
  `GET /api/graph/coverage?title_id=307&since=<ts>`
  -> only changed `coverage` objects per node key, so the frontend patches node
  styles as the crawler/workers progress. (Option: SSE `/api/graph/events` for
  push instead of poll.)

All read-path-safe: served from cache/graph; never blocks on the GPU.

---

## 6. Color & visual encoding

Two independent channels carry the most meaning: **node fill = domain**, **node
ring = coverage status**. They must not clash, so status never uses the fill.

### 6.1 Coverage status colors (the ring/donut)

| State | Hex | Note |
|---|---|---|
| missing | `#9AA0A6` | neutral gray |
| running | `#3B82F6` | blue, animated pulse |
| done | `#22C55E` | green |
| stale | `#F59E0B` | amber (present, old version) |
| failed | `#EF4444` | red |

### 6.2 Domain palette (node fill) -- v1, tunable for accessibility

| Domain | Hex |
|---|---|
| Politics & Government | `#C0392B` |
| War & Military | `#7F8C8D` |
| Science & Discovery | `#2980B9` |
| Technology & Engineering | `#16A085` |
| Economy & Finance | `#27AE60` |
| Arts & Culture | `#8E44AD` |
| Religion & Philosophy | `#D4AC0D` |
| Society & People | `#E67E22` |
| Law & Treaties | `#2C3E50` |
| Nature, Environment & Climate | `#1ABC9C` |
| Health & Medicine | `#E74C3C` |
| Exploration & Geography | `#3498DB` |
| Media & Portrayals | `#95A5A6` |

_(open: run a contrast/colorblind check; some reds/greens are close.)_

### 6.3 Edge colors by relation

| Relation | Hex | Style |
|---|---|---|
| has_section | `#D0D5DD` | thin solid (structural) |
| wikilink | `#5B8DEF` | solid |
| shared_entity | `#8E5BEF` | solid |
| shared_tag | `#A66BBF` | solid (thinner) |
| temporal_overlap | `#2BB6A3` | solid |
| embedding_sim | `#9AA0A6` | dashed |
| composite | dominant component's color | solid |
| **back_to_l0 (flag)** | `#F2A100` | **emphasized: brighter + slightly thicker glow** |

### 6.4 The coverage donut (per-node status)

Each node renders an **outer ring split into arcs, one arc per enrichment layer**
(seed, core, entities, temporal, related, review), each arc colored by that layer's
status (Sec. 6.1). The **inner fill = primary_domain** (6.2). This shows, at a
glance, both *what kind of thing* and *how far processed* -- the graph-as-ledger.
A simpler fallback: a single ring colored by the **least-complete** layer's status.

### 6.5 Other channels

| Data | Channel |
|---|---|
| `level` (0/1/2) | concentric ring position + border weight (L0 thickest) |
| `specificity`/`centrality` | node diameter |
| `relevance_to_focus` | also filterable; can fade low-relevance nodes |
| edge `weight` | edge thickness |
| edge `weight` (inverse) | `fcose` ideal edge length (more relevant = closer) |
| edge `confidence` | edge opacity |
| `back_to_l0` | gold emphasis (6.3) so back-connections are obvious |
| selection / hover | highlight node + incident edges; dim the rest |

---

## 7. Connection rules (what to include)

1. **Always:** `has_section` (spine), `wikilink` L0->L1, L1->L2.
2. **Back-to-L0:** for every L1/L2 node, also emit relatedness edges to the L0
   section(s)/article it genuinely relates to (shared_entity/tag/temporal/embedding
   above `min_relevance`), flagged `back_to_l0`. (User requirement.)
3. **Cross-edges:** L1<->L1 and L2->other-L1 when related and `include_cross_edges`.
4. **Dedup nodes:** an L2 reachable via multiple L1s is **one node** with multiple
   edges (or a merged edge carrying `path_count`).
5. **Gates/volume:** drop edges below `min_relevance` (the volume slider); apply
   the `lens` filter by reweighting/keeping domain-relevant nodes/edges.
6. **Self/loop guard:** no self-edges; redirect-collapsed ids prevent alias dupes.

---

## 8. Cytoscape mapping (notes)

- Load: `graphology` graph <- `graph.import(payload)`; feed elements to Cytoscape
  (or use a graphology<->cytoscape adapter).
- Layout: `concentric` keyed on `level` for the spine+rings; or `fcose` with
  `idealEdgeLength = f(1 - weight)` and `edgeElasticity = f(weight)` for an organic
  weighted layout. Offer both (toggle).
- Style: bind `data(primary_domain)` -> fill, `data(coverage)` -> ring/donut,
  `data(weight)` -> edge width, `data(relation)` -> line-color, `data(back_to_l0)`
  -> emphasis class.
- Interaction: click node -> open in Reader/Timeline (shared session state);
  double-click -> lazy-expand via `/neighbors`; hover edge -> show `shared`/WHY.

---

## 9. Versioning & staleness

Nodes/edges carry `versions`. When `ontology`/`model`/`parser` bumps, affected
items are `stale` (amber), not silently current; the coverage crawler re-enriches
and the `/coverage` poll updates the rings live. Everything is provenance-stamped
back to source sections.
