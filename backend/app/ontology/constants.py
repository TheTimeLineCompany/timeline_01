"""Ontology v0.1 fixed defaults."""

ONTOLOGY_VERSION = "ontology-v0.1"

ENTITY_TYPES = [
    "PERSON",
    "GROUP",
    "ORG",
    "PLACE",
    "EVENT",
    "CONCEPT",
    "WORK",
    "OBJECT",
    "NATURAL_KIND",
    "TIME",
]

DOMAINS = [
    "Politics & Government",
    "War & Military",
    "Science & Discovery",
    "Technology & Engineering",
    "Economy & Finance",
    "Arts & Culture",
    "Religion & Philosophy",
    "Society & People",
    "Law & Treaties",
    "Nature, Environment & Climate",
    "Health & Medicine",
    "Exploration & Geography",
    "Media & Portrayals",
]

TEMPORAL_ROLES = [
    "birth",
    "death",
    "start",
    "end",
    "founded",
    "dissolved",
    "reign",
    "active",
    "created/published",
    "discovered/proposed",
    "verified/confirmed",
    "occurred",
    "flourished",
    "destroyed",
    "enacted",
    "observed",
]

PRECISION_LEVELS = [
    "instant",
    "day",
    "month",
    "season",
    "year",
    "decade",
    "century",
    "millennium",
    "era",
    "geological",
    "fuzzy",
]

DEFAULT_SCORE_WEIGHTS = {
    "entity_passage": {
        "type_weight": 0.20,
        "salience": 0.30,
        "specificity": 0.25,
        "confidence": 0.15,
        "centrality": 0.10,
    },
    "content_relevance": {
        "S_embed": 0.30,
        "S_entity": 0.27,
        "S_graph": 0.17,
        "S_backlink": 0.10,
        "S_domain": 0.10,
        "prior": 0.06,
    },
    "temporal_proximity": {
        "overlap": 0.45,
        "adjacency": 0.35,
        "containment": 0.20,
    },
    "relatedness": {
        "content": 0.60,
        "temporal": 0.40,
    },
}
