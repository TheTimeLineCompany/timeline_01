"""Optional GLiNER decoder CPU entity extraction adapter."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.seeds.ontology_entity_labels import DEFAULT_ENTITY_LABELS


MODEL_VERSION = "gliner-decoder-large-v1.0"


@dataclass(frozen=True)
class GlinerDecoderEntity:
    """One GLiNER decoder entity mention."""

    text: str
    label: str
    char_start: int
    char_end: int
    confidence: float


def extract_gliner_decoder_entities(
    text: str,
    *,
    model_name: str,
    labels: list[str] | None = None,
    threshold: float = 0.30,
    max_chars: int = 2500,
) -> tuple[list[GlinerDecoderEntity], list[str]]:
    """Extract ontology-driven entities with a GLiNER decoder model."""

    clean_text = text or ""
    if not clean_text.strip():
        return [], []
    try:
        extractor = _load_model(model_name)
    except Exception as exc:  # noqa: BLE001 - optional lane must not break enrichment.
        return [], [f"gliner_decoder_unavailable:{type(exc).__name__}:{exc}"]

    excerpt = clean_text[: max(200, max_chars)]
    try:
        raw = extractor.predict_entities(
            excerpt,
            labels or DEFAULT_ENTITY_LABELS,
            threshold=threshold,
            num_gen_sequences=1,
        )
    except TypeError:
        raw = extractor.predict_entities(excerpt, labels or DEFAULT_ENTITY_LABELS, threshold=threshold)
    except Exception as exc:  # noqa: BLE001
        return [], [f"gliner_decoder_extract_failed:{type(exc).__name__}:{exc}"]

    return _parse_entities(raw, excerpt, threshold), []


@lru_cache(maxsize=1)
def _load_model(model_name: str) -> Any:
    _ensure_unicode_console()
    _configure_torch_threads()
    from gliner import GLiNER  # type: ignore[import-not-found]

    return GLiNER.from_pretrained(model_name)


def _ensure_unicode_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            continue


def _configure_torch_threads() -> None:
    try:
        from app.core.config import get_settings

        settings = get_settings()
        import torch

        torch.set_num_threads(settings.cpu_entity_torch_threads)
        torch.set_num_interop_threads(settings.cpu_entity_torch_interop_threads)
    except Exception:
        return


def _parse_entities(raw: Any, text: str, threshold: float) -> list[GlinerDecoderEntity]:
    if not isinstance(raw, list):
        return []
    entities = [_entity_from_item(item, text, threshold) for item in raw if isinstance(item, dict)]
    return _dedupe_entities([entity for entity in entities if entity is not None])


def _entity_from_item(item: dict[str, Any], text: str, threshold: float) -> GlinerDecoderEntity | None:
    mention = str(item.get("text") or item.get("entity") or item.get("span") or "").strip()
    label = str(item.get("label") or item.get("type") or "concept").strip()
    confidence = _coerce_float(item.get("score") or item.get("confidence"), default=max(threshold, 0.6))
    if not mention or not label or confidence < threshold:
        return None
    start = _coerce_int(item.get("start") or item.get("char_start"))
    end = _coerce_int(item.get("end") or item.get("char_end"))
    if start is None or end is None or start < 0 or end < start:
        start = text.casefold().find(mention.casefold())
        if start < 0:
            return None
        end = start + len(mention)
    return GlinerDecoderEntity(
        text=mention,
        label=label,
        char_start=start,
        char_end=end,
        confidence=confidence,
    )


def _dedupe_entities(entities: list[GlinerDecoderEntity]) -> list[GlinerDecoderEntity]:
    by_key: dict[tuple[int, int, str], GlinerDecoderEntity] = {}
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
