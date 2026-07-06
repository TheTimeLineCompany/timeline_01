from types import SimpleNamespace

from app.ontology.entity_mentions import (
    domain_for_type,
    gliner_label_to_type,
    gliner2_label_to_type,
    mention_salience,
    normalize_surface,
    spacy_label_to_type,
    surface_entity_id,
    wiki_entity_id,
)
from app.seeds.gliner_decoder_seed import _parse_entities as parse_decoder_entities
from app.seeds.gliner2_seed import _parse_entities
from app.seeds.ontology_entity_labels import DEFAULT_ENTITY_LABELS


def test_wiki_and_surface_entity_ids_are_deterministic():
    assert wiki_entity_id(12345) == "ent:wiki:12345"
    assert surface_entity_id("PLACE", "new_delhi") == "ent:surf:PLACE:new_delhi"


def test_normalize_surface_collapses_case_spaces_and_punctuation():
    assert normalize_surface(" New   Delhi! ") == "new_delhi"
    assert normalize_surface("City_of_Joy") == "city_of_joy"
    assert normalize_surface("  ") == ""


def test_spacy_labels_map_into_ontology_types():
    assert spacy_label_to_type("PERSON") == "PERSON"
    assert spacy_label_to_type("ORG") == "ORG"
    assert spacy_label_to_type("GPE") == "PLACE"
    assert spacy_label_to_type("LOC") == "PLACE"
    assert spacy_label_to_type("WORK_OF_ART") == "WORK"
    assert spacy_label_to_type("DATE") == "TIME"
    assert spacy_label_to_type("NORP") == "CONCEPT"


def test_gliner2_labels_map_into_ontology_types():
    assert gliner2_label_to_type("person") == "PERSON"
    assert gliner2_label_to_type("geopolitical place") == "PLACE"
    assert gliner2_label_to_type("caste or social group") == "GROUP"
    assert gliner2_label_to_type("religion") == "GROUP"
    assert gliner2_label_to_type("date or time period") == "TIME"
    assert gliner2_label_to_type("book") == "WORK"
    assert gliner2_label_to_type("law or policy") == "CONCEPT"
    assert gliner_label_to_type("political group") == "GROUP"
    assert gliner_label_to_type("work of art") == "WORK"


def test_cpu_entity_labels_include_timeline_ontology_labels():
    assert "law or policy" in DEFAULT_ENTITY_LABELS
    assert "caste or social group" in DEFAULT_ENTITY_LABELS
    assert "date or time period" in DEFAULT_ENTITY_LABELS


def test_gliner2_parser_accepts_structured_entities_without_model_download():
    text = "Mother Teresa founded the Missionaries of Charity in Calcutta."
    raw = {
        "entities": {
            "person": ["Mother Teresa"],
            "organization": [
                {
                    "text": "Missionaries of Charity",
                    "score": 0.91,
                    "start": 28,
                    "end": 51,
                }
            ],
            "location": [{"text": "Calcutta", "confidence": 0.88}],
        }
    }

    entities = _parse_entities(raw, text, 0.45)

    assert [(entity.text, entity.label) for entity in entities] == [
        ("Mother Teresa", "person"),
        ("Missionaries of Charity", "organization"),
        ("Calcutta", "location"),
    ]
    assert entities[0].char_start == 0
    assert entities[1].confidence == 0.91
    assert entities[2].char_start == text.index("Calcutta")


def test_gliner_decoder_parser_accepts_list_entities_without_model_download():
    text = "Lincoln issued the Emancipation Proclamation in 1863."
    raw = [
        {
            "text": "Lincoln",
            "label": "person",
            "score": 0.99,
            "start": 0,
            "end": 7,
            "generated_labels": ["Person"],
        },
        {
            "text": "Emancipation Proclamation",
            "label": "law or policy",
            "score": 0.91,
            "start": 19,
            "end": 44,
        },
        {
            "text": "weak",
            "label": "concept",
            "score": 0.1,
            "start": 0,
            "end": 4,
        },
    ]

    entities = parse_decoder_entities(raw, text, 0.30)

    assert [(entity.text, entity.label) for entity in entities] == [
        ("Lincoln", "person"),
        ("Emancipation Proclamation", "law or policy"),
    ]
    assert entities[0].confidence == 0.99


def test_domain_defaults_are_conservative_and_stable():
    assert domain_for_type("WORK") == "Arts & Culture"
    assert domain_for_type("PLACE") == "Exploration & Geography"
    assert domain_for_type("PERSON") == "Society & People"
    assert domain_for_type("ORG") == "Politics & Government"
    assert domain_for_type("CONCEPT") == "Society & People"


def test_mention_salience_prefers_title_heading_and_lead_mentions():
    section = SimpleNamespace(title="City of Joy", heading="Plot")
    title_candidate = SimpleNamespace(surface="City of Joy", char_start=900, source="spacy_seed")
    lead_candidate = SimpleNamespace(surface="Mother Teresa", char_start=80, source="spacy_seed")
    link_candidate = SimpleNamespace(surface="Kolkata", char_start=900, source="wiki_link")
    deep_candidate = SimpleNamespace(surface="Calcutta", char_start=900, source="spacy_seed")

    assert mention_salience(section, title_candidate) == 0.95
    assert mention_salience(section, lead_candidate) == 0.75
    assert mention_salience(section, link_candidate) == 0.65
    assert mention_salience(section, deep_candidate) == 0.50
