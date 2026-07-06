from app.related.component_scoring import (
    EntityScore,
    TimeAnchorScore,
    content_components,
    graph_signal,
    normalize_raw_scores,
    relatedness_components,
    temporal_components,
)


def test_content_components_reward_entity_and_domain_overlap():
    source = [
        EntityScore("ent:wiki:1", 0.8, "PERSON", "Politics & Government"),
        EntityScore("ent:wiki:2", 0.6, "PLACE", "Exploration & Geography"),
    ]
    candidate = [
        EntityScore("ent:wiki:1", 0.7, "PERSON", "Politics & Government"),
        EntityScore("ent:wiki:3", 0.4, "ORG", "Politics & Government"),
    ]

    components = content_components(
        source,
        candidate,
        graph_signal=0.9,
        embedding_similarity=0.5,
        prior=0.7,
    )

    assert components["S_entity"] > 0
    assert components["S_domain"] > components["S_entity"]
    assert components["S_graph"] == 0.9
    assert components["S_embed"] == 0.5


def test_temporal_components_reward_exact_and_nearby_times():
    source = [TimeAnchorScore("year:1992", 1992.0, 0.0, 0.72, 0.8)]
    exact = [TimeAnchorScore("year:1992", 1992.0, 0.0, 0.72, 0.9)]
    nearby = [TimeAnchorScore("year:1993", 1993.0, 0.0, 0.72, 0.9)]

    exact_components = temporal_components(source, exact)
    nearby_components = temporal_components(source, nearby)

    assert exact_components["overlap"] > 0
    assert exact_components["adjacency"] == 1.0
    assert nearby_components["overlap"] == 0
    assert nearby_components["adjacency"] == 0.9


def test_relatedness_components_and_normalization_are_bounded():
    content = {"S_embed": 0.5, "S_entity": 0.6, "S_graph": 0.9, "S_domain": 0.8, "prior": 0.7}
    temporal = {"overlap": 0.4, "adjacency": 0.9, "containment": 0.0}
    components = relatedness_components(content, temporal)
    normalized = normalize_raw_scores([0.2, components["raw_score"], 0.8])

    assert 0 < components["content_score"] <= 1
    assert 0 < components["temporal_score"] <= 1
    assert 0 < components["raw_score"] <= 1
    assert normalized == sorted(normalized)


def test_normalization_does_not_promote_weak_sections_by_rank_alone():
    normalized = normalize_raw_scores([0.05, 0.12, 0.29])

    assert normalized == [0.05, 0.12, 0.29]
    assert max(normalized) < 0.42


def test_graph_signal_keeps_l1_above_l2_for_same_rank():
    assert graph_signal(1, 0) > graph_signal(2, 0)
