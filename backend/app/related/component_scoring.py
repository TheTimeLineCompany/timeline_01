"""Component-vector relatedness scoring helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ontology.constants import DEFAULT_SCORE_WEIGHTS

SCORING_VERSION = "component-relatedness-v2"
NORMALIZATION_RANK_WEIGHT = 0.30
NORMALIZATION_WEAK_SECTION_FLOOR = 0.30
NORMALIZATION_FULL_RANK_FLOOR = 0.42


@dataclass(frozen=True)
class EntityScore:
    """Entity score projection used for relatedness math."""

    entity_id: str
    blend: float
    primary_type: str = "CONCEPT"
    primary_domain: str = "Society & People"


@dataclass(frozen=True)
class TimeAnchorScore:
    """Time-anchor projection used for relatedness math."""

    time_id: str
    center: float | None
    spread: float | None
    precision_score: float
    confidence: float


def content_components(
    source_entities: list[EntityScore],
    candidate_entities: list[EntityScore],
    *,
    graph_signal: float,
    embedding_similarity: float | None,
    backlink_signal: float = 0.0,
    prior: float,
) -> dict[str, float]:
    """Build the content component vector."""

    return {
        "S_embed": clamp(embedding_similarity or 0.0),
        "S_entity": entity_overlap(source_entities, candidate_entities),
        "S_graph": clamp(graph_signal),
        "S_backlink": clamp(backlink_signal),
        "S_domain": domain_overlap(source_entities, candidate_entities),
        "prior": clamp(prior),
    }


def temporal_components(
    source_times: list[TimeAnchorScore],
    candidate_times: list[TimeAnchorScore],
) -> dict[str, float]:
    """Build the temporal component vector."""

    return {
        "overlap": temporal_overlap(source_times, candidate_times),
        "adjacency": temporal_adjacency(source_times, candidate_times),
        "containment": temporal_containment(source_times, candidate_times),
    }


def relatedness_components(
    content: dict[str, float],
    temporal: dict[str, float],
) -> dict[str, Any]:
    """Build full relatedness component object with blended subtotals."""

    content_score = weighted_blend(content, DEFAULT_SCORE_WEIGHTS["content_relevance"])
    temporal_score = weighted_blend(temporal, DEFAULT_SCORE_WEIGHTS["temporal_proximity"])
    overall = weighted_blend(
        {"content": content_score, "temporal": temporal_score},
        DEFAULT_SCORE_WEIGHTS["relatedness"],
    )
    return {
        "version": SCORING_VERSION,
        "content": content,
        "temporal": temporal,
        "content_score": content_score,
        "temporal_score": temporal_score,
        "raw_score": overall,
    }


def normalize_raw_scores(scores: list[float]) -> list[float]:
    """Normalize raw scores while preserving meaningful absolute signal."""

    if not scores:
        return []
    min_score = min(scores)
    max_score = max(scores)
    if max_score == min_score:
        return [round(clamp(score), 4) for score in scores]
    if max_score <= NORMALIZATION_WEAK_SECTION_FLOOR:
        return [round(clamp(score), 4) for score in scores]
    rank_weight = NORMALIZATION_RANK_WEIGHT
    if max_score < NORMALIZATION_FULL_RANK_FLOOR:
        span = NORMALIZATION_FULL_RANK_FLOOR - NORMALIZATION_WEAK_SECTION_FLOOR
        rank_weight *= max(0.0, (max_score - NORMALIZATION_WEAK_SECTION_FLOOR) / span)
    raw_weight = 1.0 - rank_weight
    normalized: list[float] = []
    for score in scores:
        local_rank = (score - min_score) / (max_score - min_score)
        normalized.append(round(clamp((score * raw_weight) + (local_rank * rank_weight)), 4))
    return normalized


def entity_overlap(source_entities: list[EntityScore], candidate_entities: list[EntityScore]) -> float:
    """Weighted entity overlap based on entity-passage blends."""

    if not source_entities or not candidate_entities:
        return 0.0
    candidate_by_id = {entity.entity_id: entity for entity in candidate_entities}
    source_total = sum(entity.blend for entity in source_entities)
    if source_total <= 0:
        return 0.0
    matched = 0.0
    for source_entity in source_entities:
        candidate_entity = candidate_by_id.get(source_entity.entity_id)
        if candidate_entity is not None:
            matched += min(source_entity.blend, candidate_entity.blend)
    return round(clamp(matched / source_total), 4)


def domain_overlap(source_entities: list[EntityScore], candidate_entities: list[EntityScore]) -> float:
    """Weighted overlap between domains and entity types."""

    if not source_entities or not candidate_entities:
        return 0.0
    source_domains = _weighted_bucket(source_entities, "domain")
    candidate_domains = _weighted_bucket(candidate_entities, "domain")
    source_types = _weighted_bucket(source_entities, "type")
    candidate_types = _weighted_bucket(candidate_entities, "type")
    domain_score = _bucket_overlap(source_domains, candidate_domains)
    type_score = _bucket_overlap(source_types, candidate_types)
    return round(clamp((domain_score * 0.65) + (type_score * 0.35)), 4)


def temporal_overlap(source_times: list[TimeAnchorScore], candidate_times: list[TimeAnchorScore]) -> float:
    """Exact anchor overlap weighted by confidence and precision."""

    if not source_times or not candidate_times:
        return 0.0
    candidate_by_id = {time.time_id: time for time in candidate_times}
    source_total = sum(_time_weight(time) for time in source_times)
    if source_total <= 0:
        return 0.0
    matched = 0.0
    for source_time in source_times:
        candidate_time = candidate_by_id.get(source_time.time_id)
        if candidate_time is not None:
            matched += min(_time_weight(source_time), _time_weight(candidate_time))
    return round(clamp(matched / source_total), 4)


def temporal_adjacency(source_times: list[TimeAnchorScore], candidate_times: list[TimeAnchorScore]) -> float:
    """Near-time score using scalar anchor centers."""

    distances = [
        abs(source.center - candidate.center)
        for source in source_times
        for candidate in candidate_times
        if source.center is not None and candidate.center is not None
    ]
    if not distances:
        return 0.0
    min_distance = min(distances)
    if min_distance <= 0:
        return 1.0
    if min_distance <= 1:
        return 0.90
    if min_distance <= 10:
        return round(0.70 - ((min_distance - 1) / 9 * 0.25), 4)
    if min_distance <= 50:
        return round(0.42 - ((min_distance - 10) / 40 * 0.22), 4)
    if min_distance <= 300:
        return round(0.18 - ((min_distance - 50) / 250 * 0.12), 4)
    return 0.03


def temporal_containment(source_times: list[TimeAnchorScore], candidate_times: list[TimeAnchorScore]) -> float:
    """Coarse interval containment score."""

    best = 0.0
    for source in source_times:
        for candidate in candidate_times:
            if None in {source.center, candidate.center}:
                continue
            source_spread = source.spread or 0.0
            candidate_spread = candidate.spread or 0.0
            if source_spread <= 0 and candidate_spread <= 0:
                continue
            source_start = source.center - source_spread
            source_end = source.center + source_spread
            candidate_start = candidate.center - candidate_spread
            candidate_end = candidate.center + candidate_spread
            if source_start <= candidate_start and source_end >= candidate_end:
                best = max(best, 0.85)
            elif candidate_start <= source_start and candidate_end >= source_end:
                best = max(best, 0.75)
            elif source_start <= candidate_end and candidate_start <= source_end:
                best = max(best, 0.55)
    return best


def graph_signal(level: int, link_rank: int) -> float:
    """Convert graph path depth/rank into a bounded component."""

    if level == 1:
        return round(max(0.50, 0.95 - (link_rank * 0.035)), 4)
    return round(max(0.22, 0.62 - (link_rank * 0.025)), 4)


def graph_prior(level: int, link_rank: int) -> float:
    """Small prior that keeps direct links ahead when other signals tie."""

    if level == 1:
        return round(max(0.35, 0.76 - (link_rank * 0.025)), 4)
    return round(max(0.18, 0.45 - (link_rank * 0.018)), 4)


def weighted_blend(components: dict[str, float], weights: dict[str, float]) -> float:
    """Blend named components."""

    return round(clamp(sum(components.get(key, 0.0) * weight for key, weight in weights.items())), 4)


def clamp(value: float) -> float:
    """Clamp numeric score to the 0..1 interval."""

    return max(0.0, min(1.0, float(value)))


def _weighted_bucket(entities: list[EntityScore], bucket: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for entity in entities:
        key = entity.primary_domain if bucket == "domain" else entity.primary_type
        result[key] = result.get(key, 0.0) + entity.blend
    return result


def _bucket_overlap(source: dict[str, float], candidate: dict[str, float]) -> float:
    if not source or not candidate:
        return 0.0
    total = sum(source.values())
    if total <= 0:
        return 0.0
    matched = sum(min(weight, candidate.get(key, 0.0)) for key, weight in source.items())
    return clamp(matched / total)


def _time_weight(time: TimeAnchorScore) -> float:
    return clamp(time.confidence * ((time.precision_score * 0.65) + 0.35))
