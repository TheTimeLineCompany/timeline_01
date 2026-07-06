from app.db.database import Base
from app.ontology.constants import (
    DEFAULT_SCORE_WEIGHTS,
    DOMAINS,
    ENTITY_TYPES,
    ONTOLOGY_VERSION,
    PRECISION_LEVELS,
    TEMPORAL_ROLES,
)


def test_ontology_constants_pin_v0_1_defaults():
    assert ONTOLOGY_VERSION == "ontology-v0.1"
    assert "PERSON" in ENTITY_TYPES
    assert "Politics & Government" in DOMAINS
    assert "occurred" in TEMPORAL_ROLES
    assert "year" in PRECISION_LEVELS
    assert DEFAULT_SCORE_WEIGHTS["relatedness"] == {"content": 0.60, "temporal": 0.40}


def test_ontology_tables_are_registered_in_metadata():
    expected = {
        "timeline_v4.ontology_version",
        "timeline_v4.entity_registry",
        "timeline_v4.entity_alias_map",
        "timeline_v4.taxonomy_candidate",
        "timeline_v4.mention_cache",
        "timeline_v4.entity_passage_score",
        "timeline_v4.time_anchor_registry",
        "timeline_v4.fact_cache",
        "timeline_v4.fact_time",
        "timeline_v4.content_relatedness_cache",
        "timeline_v4.processing_state",
    }

    assert expected.issubset(set(Base.metadata.tables))
