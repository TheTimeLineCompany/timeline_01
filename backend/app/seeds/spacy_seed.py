"""spaCy seed NER for fast first-paint tags."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class SeedEntity:
    """One seed entity mention."""

    text: str
    label: str
    char_start: int
    char_end: int
    confidence: float = 0.6


@lru_cache(maxsize=1)
def _load_model():
    import spacy

    return spacy.load("en_core_web_sm")


def extract_seed_entities(text: str) -> tuple[list[SeedEntity], list[str]]:
    """Extract spaCy seed entities.

    Missing spaCy/model is reported as a warning so Phase 0 can still run.
    """

    try:
        nlp = _load_model()
    except Exception as exc:
        return [], [f"spaCy seed NER unavailable: {exc}"]

    doc = nlp(text or "")
    out: list[SeedEntity] = []
    for ent in doc.ents:
        if ent.label_ not in {"PERSON", "ORG", "GPE", "LOC", "EVENT", "WORK_OF_ART", "DATE"}:
            continue
        out.append(
            SeedEntity(
                text=ent.text,
                label=ent.label_,
                char_start=ent.start_char,
                char_end=ent.end_char,
            )
        )
    return out, []
