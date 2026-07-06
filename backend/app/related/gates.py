"""Ontology gate decisions for relatedness candidates."""

from __future__ import annotations

from typing import Any

GATE_VERSION = "ontology-gates-v2"


def relatedness_gates(
    *,
    level: int,
    score: float,
    why_source: str,
    components: dict[str, Any],
    source_entity_count: int,
    candidate_entity_count: int,
    source_time_count: int,
    candidate_time_count: int,
    is_self_reference: bool = False,
    is_content_candidate: bool = True,
    agent_signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return explicit gate decisions for a related candidate."""

    content = components.get("content") or {}
    temporal = components.get("temporal") or {}
    content_score = float(components.get("content_score") or 0.0)
    temporal_score = float(components.get("temporal_score") or 0.0)
    bridge_signal = float(components.get("l2_bridge_signal") or 0.0)

    reasons: list[str] = []
    if is_self_reference:
        reasons.append("self_reference")
    if not is_content_candidate:
        reasons.append("non_content_candidate")
    if level > 2:
        reasons.append("beyond_l2")

    graph_ok = level in {1, 2}
    entity_signal = float(content.get("S_entity") or 0.0)
    embedding_signal = float(content.get("S_embed") or 0.0)
    domain_signal = float(content.get("S_domain") or 0.0)
    graph_signal = float(content.get("S_graph") or 0.0)
    backlink_signal = float(content.get("S_backlink") or 0.0)
    temporal_signal = max(
        float(temporal.get("overlap") or 0.0),
        float(temporal.get("adjacency") or 0.0),
        float(temporal.get("containment") or 0.0),
    )
    has_entity_data = source_entity_count > 0 and candidate_entity_count > 0
    has_time_data = source_time_count > 0 and candidate_time_count > 0
    has_content_signal = (
        entity_signal >= 0.12
        or domain_signal >= 0.28
        or content_score >= 0.34
        or graph_signal >= 0.50
    )
    has_temporal_signal = temporal_signal >= 0.45 or temporal_score >= 0.32
    supporting_signal_count = sum(
        [
            entity_signal >= 0.12,
            embedding_signal >= 0.32,
            backlink_signal >= 0.34,
            bridge_signal >= 0.52,
            domain_signal >= 0.36,
            graph_signal >= 0.50,
            has_temporal_signal,
            content_score >= 0.42,
        ]
    )
    l2_has_enough_evidence = supporting_signal_count >= 2 or (
        score >= 0.62 and supporting_signal_count >= 1
    )
    agent_backed = why_source == "agent_related_v1"
    positive_agent_signal = _positive_agent_signal(agent_signal) if agent_signal is not None else agent_backed

    if level == 1:
        accepted = graph_ok and is_content_candidate and not is_self_reference
    else:
        accepted = graph_ok and is_content_candidate and not is_self_reference and l2_has_enough_evidence
    if not accepted and level == 2 and not has_content_signal:
        reasons.append("weak_content_signal")
    if not accepted and level == 2 and supporting_signal_count < 2:
        reasons.append("insufficient_l2_evidence")

    agent_eligible = accepted and (
        level == 1
        or score >= 0.38
        or has_content_signal
        or has_temporal_signal
    )
    if accepted and not agent_eligible:
        reasons.append("below_agent_threshold")

    weak_agent_signal = agent_backed and agent_signal is not None and not positive_agent_signal
    timeline_eligible = accepted and (
        (level == 1 and score >= 0.58 and (has_content_signal or has_temporal_signal))
        or (positive_agent_signal and score >= 0.45)
        or score >= 0.72
        or (score >= 0.45 and has_temporal_signal)
    )
    if weak_agent_signal:
        timeline_eligible = False
    if accepted and not timeline_eligible:
        reasons.append("below_timeline_threshold")
    if weak_agent_signal:
        reasons.append("weak_agent_signal")

    return {
        "version": GATE_VERSION,
        "accepted": accepted,
        "agent_eligible": agent_eligible,
        "timeline_eligible": timeline_eligible,
        "level": level,
        "score": round(float(score), 4),
        "signals": {
            "graph_ok": graph_ok,
            "graph_signal": round(graph_signal, 4),
            "embedding_signal": round(embedding_signal, 4),
            "entity_signal": round(entity_signal, 4),
            "domain_signal": round(domain_signal, 4),
            "backlink_signal": round(backlink_signal, 4),
            "bridge_signal": round(bridge_signal, 4),
            "temporal_signal": round(temporal_signal, 4),
            "content_score": round(content_score, 4),
            "temporal_score": round(temporal_score, 4),
            "supporting_signal_count": supporting_signal_count,
            "has_entity_data": has_entity_data,
            "has_time_data": has_time_data,
            "agent_backed": agent_backed,
            "positive_agent_signal": positive_agent_signal,
        },
        "reasons": sorted(set(reasons)),
    }


def _positive_agent_signal(agent_signal: dict[str, Any] | None) -> bool:
    """Return whether an agent result asserts a useful relationship."""

    if not agent_signal:
        return False
    confidence = float(agent_signal.get("confidence") or 0.0)
    tags = {str(tag).casefold().strip() for tag in agent_signal.get("reasoning_tags") or []}
    negative_tags = {
        "weak_link",
        "no_link",
        "no_connection",
        "generic_link",
        "not_relevant",
        "insufficient_evidence",
    }
    positive_tags = {
        "direct_link",
        "shared_time",
        "shared_entity",
        "historical_context",
        "temporal_context",
        "entity_overlap",
        "topic_overlap",
        "causal_context",
        "same_event",
        "same_place",
    }
    if tags and tags.issubset(negative_tags):
        return False
    if confidence < 0.55:
        return False
    if tags & negative_tags and not tags & positive_tags:
        return False
    return bool(tags & positive_tags) or confidence >= 0.7
