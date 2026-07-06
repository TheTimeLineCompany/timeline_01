from app.related.gates import relatedness_gates


def components(
    *,
    embed: float = 0.0,
    entity: float = 0.0,
    domain: float = 0.0,
    graph: float = 0.0,
    backlink: float = 0.0,
    temporal: float = 0.0,
    content_score: float = 0.0,
    temporal_score: float = 0.0,
    bridge: float = 0.0,
):
    return {
        "content": {
            "S_embed": embed,
            "S_entity": entity,
            "S_domain": domain,
            "S_graph": graph,
            "S_backlink": backlink,
        },
        "temporal": {"overlap": temporal, "adjacency": 0.0, "containment": 0.0},
        "content_score": content_score,
        "temporal_score": temporal_score,
        "l2_bridge_signal": bridge,
    }


def test_low_score_l1_connection_is_agent_eligible_but_not_timeline_eligible():
    gates = relatedness_gates(
        level=1,
        score=0.42,
        why_source="template",
        components=components(graph=0.9, content_score=0.4),
        source_entity_count=2,
        candidate_entity_count=2,
        source_time_count=0,
        candidate_time_count=0,
    )

    assert gates["accepted"]
    assert gates["agent_eligible"]
    assert not gates["timeline_eligible"]
    assert "below_timeline_threshold" in gates["reasons"]


def test_strong_l1_connection_is_timeline_eligible():
    gates = relatedness_gates(
        level=1,
        score=0.62,
        why_source="template",
        components=components(graph=0.9, content_score=0.4),
        source_entity_count=2,
        candidate_entity_count=2,
        source_time_count=0,
        candidate_time_count=0,
    )

    assert gates["accepted"]
    assert gates["timeline_eligible"]


def test_weak_l2_is_rejected_as_connection():
    gates = relatedness_gates(
        level=2,
        score=0.25,
        why_source="template",
        components=components(graph=0.3, content_score=0.2),
        source_entity_count=2,
        candidate_entity_count=2,
        source_time_count=0,
        candidate_time_count=0,
    )

    assert not gates["accepted"]
    assert not gates["timeline_eligible"]
    assert "weak_content_signal" in gates["reasons"]


def test_agent_backed_l2_below_threshold_is_not_timeline_eligible():
    gates = relatedness_gates(
        level=2,
        score=0.35,
        why_source="agent_related_v1",
        components=components(entity=0.2, graph=0.5, content_score=0.35),
        source_entity_count=2,
        candidate_entity_count=2,
        source_time_count=0,
        candidate_time_count=0,
    )

    assert gates["accepted"]
    assert not gates["timeline_eligible"]


def test_l2_domain_only_signal_is_rejected():
    gates = relatedness_gates(
        level=2,
        score=0.45,
        why_source="template",
        components=components(domain=0.9, graph=0.2, content_score=0.2),
        source_entity_count=2,
        candidate_entity_count=2,
        source_time_count=0,
        candidate_time_count=0,
    )

    assert not gates["accepted"]
    assert "insufficient_l2_evidence" in gates["reasons"]


def test_l2_strong_bridge_plus_embedding_is_accepted():
    gates = relatedness_gates(
        level=2,
        score=0.46,
        why_source="template",
        components=components(embed=0.36, bridge=0.82, graph=0.28, content_score=0.28),
        source_entity_count=1,
        candidate_entity_count=1,
        source_time_count=0,
        candidate_time_count=0,
    )

    assert gates["accepted"]
    assert gates["signals"]["supporting_signal_count"] >= 2


def test_agent_backed_l2_at_threshold_is_timeline_eligible():
    gates = relatedness_gates(
        level=2,
        score=0.48,
        why_source="agent_related_v1",
        components=components(entity=0.2, graph=0.5, content_score=0.35),
        source_entity_count=2,
        candidate_entity_count=2,
        source_time_count=0,
        candidate_time_count=0,
        agent_signal={"confidence": 0.8, "reasoning_tags": ["direct_link"]},
    )

    assert gates["accepted"]
    assert gates["timeline_eligible"]


def test_weak_agent_signal_does_not_promote_l2_to_timeline():
    gates = relatedness_gates(
        level=2,
        score=0.55,
        why_source="agent_related_v1",
        components=components(entity=0.06, domain=0.9, graph=0.59, content_score=0.25, temporal_score=0.67),
        source_entity_count=2,
        candidate_entity_count=2,
        source_time_count=1,
        candidate_time_count=1,
        agent_signal={"confidence": 0.2, "reasoning_tags": ["weak_link"]},
    )

    assert gates["accepted"]
    assert not gates["timeline_eligible"]
    assert "weak_agent_signal" in gates["reasons"]


def test_self_reference_is_rejected():
    gates = relatedness_gates(
        level=1,
        score=0.9,
        why_source="template",
        components=components(graph=0.9, content_score=0.9),
        source_entity_count=2,
        candidate_entity_count=2,
        source_time_count=1,
        candidate_time_count=1,
        is_self_reference=True,
    )

    assert not gates["accepted"]
    assert "self_reference" in gates["reasons"]
