from app.orchestration.priorities import compute_candidate_priority


def test_candidate_priority_rewards_connection_strength() -> None:
    weak = compute_candidate_priority(
        level=2,
        link_rank=8,
        source_article_link_count=1,
        intro_similarity=0.1,
    )
    strong = compute_candidate_priority(
        level=1,
        link_rank=0,
        source_article_link_count=5,
        intro_similarity=0.1,
    )

    assert strong.S_conn > weak.S_conn
    assert strong.S_prio > weak.S_prio
    assert strong.priority < weak.priority


def test_candidate_priority_rewards_embedding_relevance() -> None:
    low = compute_candidate_priority(
        level=1,
        link_rank=1,
        source_article_link_count=2,
        intro_similarity=0.1,
    )
    high = compute_candidate_priority(
        level=1,
        link_rank=1,
        source_article_link_count=2,
        intro_similarity=0.8,
    )

    assert high.S_rel > low.S_rel
    assert high.S_prio > low.S_prio
