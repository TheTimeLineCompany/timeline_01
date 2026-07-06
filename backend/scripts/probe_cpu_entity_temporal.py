"""Probe CPU entity and temporal extraction without DB writes or LLM calls."""

from __future__ import annotations

import json
from dataclasses import asdict

from app.core.config import get_settings
from app.seeds.gliner2_seed import extract_gliner2_entities
from app.seeds.gliner_decoder_seed import extract_gliner_decoder_entities
from app.seeds.spacy_seed import extract_seed_entities
from app.seeds.temporal import normalize_temporal_mentions


CASES = [
    {
        "id": "lincoln",
        "text": (
            "Abraham Lincoln was born on February 12, 1809 in Kentucky. "
            "He became president of the United States in 1861, issued the "
            "Emancipation Proclamation on January 1, 1863, and was assassinated "
            "in April 1865."
        ),
    },
    {
        "id": "city_of_joy",
        "text": (
            "City of Joy was published in 1985 by Dominique Lapierre. The book "
            "describes poverty, caste divisions, Kolkata, Mother Teresa, and the "
            "Missionaries of Charity."
        ),
    },
    {
        "id": "new_delhi_trap",
        "text": (
            "A chess tournament in New Delhi in 2010 involved international players. "
            "A separate pollution emergency in New Delhi in 2016 led officials to close "
            "schools and restrict traffic. The two events share a city but not a topic."
        ),
    },
    {
        "id": "deep_time",
        "text": (
            "The Cambrian explosion occurred about 541 million years ago. The "
            "Cretaceous-Paleogene extinction happened around 66 million years ago."
        ),
    },
]


def serialize_entity(entity: object) -> dict[str, object]:
    data = asdict(entity)
    return {
        "text": data.get("text"),
        "label": data.get("label"),
        "char_start": data.get("char_start"),
        "char_end": data.get("char_end"),
        "confidence": data.get("confidence"),
    }


def main() -> None:
    settings = get_settings()
    output = []
    for case in CASES:
        text = case["text"]
        spacy_entities, spacy_warnings = extract_seed_entities(text)
        gliner2_entities, gliner2_warnings = extract_gliner2_entities(
            text,
            model_name=settings.gliner2_model,
            threshold=settings.gliner2_threshold,
            max_chars=settings.gliner2_max_chars,
        )
        decoder_entities, decoder_warnings = extract_gliner_decoder_entities(
            text,
            model_name=settings.gliner_decoder_model,
            threshold=settings.gliner_decoder_threshold,
            max_chars=settings.gliner_decoder_max_chars,
        )
        temporal = normalize_temporal_mentions(text)
        output.append(
            {
                "id": case["id"],
                "text": text,
                "spacy": {
                    "count": len(spacy_entities),
                    "warnings": spacy_warnings,
                    "entities": [serialize_entity(entity) for entity in spacy_entities],
                },
                "gliner2": {
                    "count": len(gliner2_entities),
                    "warnings": gliner2_warnings,
                    "entities": [serialize_entity(entity) for entity in gliner2_entities],
                },
                "gliner_decoder": {
                    "count": len(decoder_entities),
                    "warnings": decoder_warnings,
                    "entities": [serialize_entity(entity) for entity in decoder_entities],
                },
                "temporal": {
                    "count": len(temporal),
                    "matches": [asdict(match) for match in temporal],
                },
            }
        )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
