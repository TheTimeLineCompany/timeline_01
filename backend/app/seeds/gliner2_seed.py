"""Optional GLiNER2 CPU entity extraction adapter."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.seeds.ontology_entity_labels import DEFAULT_ENTITY_LABELS

MODEL_VERSION = "gliner2-base-v1"


@dataclass(frozen=True)
class Gliner2Entity:
    """One GLiNER2 entity mention."""

    text: str
    label: str
    char_start: int
    char_end: int
    confidence: float


def extract_gliner2_entities(
    text: str,
    *,
    model_name: str,
    labels: list[str] | None = None,
    threshold: float = 0.45,
    max_chars: int = 2500,
) -> tuple[list[Gliner2Entity], list[str]]:
    """Extract schema-driven entities with GLiNER2 if the dependency is installed."""

    clean_text = text or ""
    if not clean_text.strip():
        return [], []
    try:
        extractor = _load_model(model_name)
    except Exception as exc:  # noqa: BLE001 - optional lane must not break enrichment.
        return [], [f"gliner2_unavailable:{type(exc).__name__}:{exc}"]

    excerpt = clean_text[: max(200, max_chars)]
    try:
        raw = extractor.extract_entities(excerpt, labels or DEFAULT_ENTITY_LABELS)
    except TypeError:
        raw = extractor.extract_entities(excerpt, labels or DEFAULT_ENTITY_LABELS, threshold=threshold)
    except Exception as exc:  # noqa: BLE001
        return [], [f"gliner2_extract_failed:{type(exc).__name__}:{exc}"]

    return _parse_entities(raw, excerpt, threshold), []


@lru_cache(maxsize=1)
def _load_model(model_name: str) -> Any:
    _ensure_unicode_console()
    from gliner2 import GLiNER2  # type: ignore[import-not-found]

    return GLiNER2.from_pretrained(model_name)


def _ensure_unicode_console() -> None:
    """Avoid optional model startup prints breaking Windows cp1252 consoles."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            continue


def _parse_entities(raw: Any, text: str, threshold: float) -> list[Gliner2Entity]:
    entities: list[Gliner2Entity] = []
    if isinstance(raw, dict) and isinstance(raw.get("entities"), dict):
        for label, values in raw["entities"].items():
            if not isinstance(values, list):
                continue
            for value in values:
                entities.extend(_entity_from_value(value, str(label), text, threshold))
    elif isinstance(raw, list):
        for item in raw:
            entities.extend(_entity_from_value(item, "", text, threshold))
    return _dedupe_entities(entities)


def _entity_from_value(value: Any, fallback_label: str, text: str, threshold: float) -> list[Gliner2Entity]:
    if isinstance(value, str):
        start = text.casefold().find(value.casefold())
        if start < 0:
            return []
        return [
            Gliner2Entity(
                text=value,
                label=fallback_label,
                char_start=start,
                char_end=start + len(value),
                confidence=max(threshold, 0.6),
            )
        ]
    if not isinstance(value, dict):
        return []

    mention = str(value.get("text") or value.get("entity") or value.get("span") or value.get("value") or "").strip()
    if not mention:
        return []
    label = str(value.get("label") or value.get("type") or fallback_label or "concept").strip()
    confidence = _coerce_float(value.get("score") or value.get("confidence"), default=max(threshold, 0.6))
    if confidence < threshold:
        return []
    start = _coerce_int(value.get("start") or value.get("char_start"))
    end = _coerce_int(value.get("end") or value.get("char_end"))
    if start is None or end is None or start < 0 or end < start:
        start = text.casefold().find(mention.casefold())
        if start < 0:
            return []
        end = start + len(mention)
    return [
        Gliner2Entity(
            text=mention,
            label=label,
            char_start=start,
            char_end=end,
            confidence=confidence,
        )
    ]


def _dedupe_entities(entities: list[Gliner2Entity]) -> list[Gliner2Entity]:
    by_key: dict[tuple[int, int, str], Gliner2Entity] = {}
    for entity in entities:
        key = (entity.char_start, entity.char_end, entity.label.casefold())
        existing = by_key.get(key)
        if existing is None or entity.confidence > existing.confidence:
            by_key[key] = entity
    return sorted(by_key.values(), key=lambda item: (item.char_start, item.char_end, item.label))


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
