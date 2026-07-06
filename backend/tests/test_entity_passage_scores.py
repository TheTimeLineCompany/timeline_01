from types import SimpleNamespace

from app.ontology.passage_scores import (
    EntityMentionGroup,
    blend_components,
    passage_components,
    type_weight,
)


def mention(char_start: int, salience: float, confidence: float):
    return SimpleNamespace(char_start=char_start, char_end=char_start + 5, salience=salience, confidence=confidence)


def test_passage_components_capture_frequency_position_and_confidence():
    text = "x" * 1000
    strong = EntityMentionGroup(
        entity_id="ent:wiki:1",
        primary_type="PERSON",
        specificity=0.8,
        mentions=[
            mention(20, 0.95, 0.9),
            mention(180, 0.75, 0.8),
            mention(400, 0.65, 0.8),
        ],
    )
    weak = EntityMentionGroup(
        entity_id="ent:wiki:2",
        primary_type="TIME",
        specificity=0.2,
        mentions=[mention(900, 0.5, 0.5)],
    )

    strong_components = passage_components(text, strong)
    weak_components = passage_components(text, weak)

    assert strong_components["mention_count"] == 3.0
    assert strong_components["first_char_start"] == 20.0
    assert strong_components["centrality"] > weak_components["centrality"]
    assert strong_components["salience"] > weak_components["salience"]
    assert strong_components["confidence"] > weak_components["confidence"]
    assert blend_components(strong_components) > blend_components(weak_components)


def test_type_weight_defaults_to_concept_for_unknown_types():
    assert type_weight("PERSON") > type_weight("TIME")
    assert type_weight("UNKNOWN") == type_weight("CONCEPT")


def test_blend_components_is_bounded():
    score = blend_components(
        {
            "type_weight": 2.0,
            "salience": 2.0,
            "specificity": 2.0,
            "confidence": 2.0,
            "centrality": 2.0,
        }
    )

    assert score == 1.0
