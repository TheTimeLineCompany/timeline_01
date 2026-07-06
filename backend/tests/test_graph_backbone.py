"""Graph backbone identity contract tests."""

from app.graph.backbone import article_node_id, section_node_id
from app.graph.coverage_crawler import _frontier_section_limit, _select_frontier_candidates
from app.graph.frontier import _frontier_target, _l1_articles_for_l2_expansion
from app.graph.graphology import _convert_edge, _coverage_state, _overall_coverage_state


def test_article_node_id_uses_postgres_title_id() -> None:
    assert article_node_id(7257) == "article:7257"


def test_section_node_id_uses_postgres_section_key() -> None:
    assert section_node_id("7257:1300069771") == "section:7257:1300069771"


def test_frontier_target_filters_non_article_namespaces_and_anchors() -> None:
    assert _frontier_target({"target": "File:Example.jpg"}) is None
    assert _frontier_target({"target": "Template:Infobox"}) is None
    assert _frontier_target({"target": "Category:History"}) is None
    assert _frontier_target({"target": "Abraham_Lincoln#Early_life"}) == "Abraham Lincoln"


def test_graph_frontier_l2_expands_intro_linked_l1_articles() -> None:
    ranked_l1 = [
        {"title_id": 1, "title": "Lead Link", "sections": {"a:lead"}},
        {"title_id": 2, "title": "Body Link", "sections": {"a:body"}},
        {"title_id": 3, "title": "Shared Link", "sections": {"a:lead", "a:body"}},
    ]

    selected = _l1_articles_for_l2_expansion(
        ranked_l1,
        intro_section_keys={"a:lead"},
        source_scope="intro_l1",
    )

    assert [row["title"] for row in selected] == ["Lead Link", "Shared Link"]


def test_graph_frontier_can_expand_all_l1_articles_when_requested() -> None:
    ranked_l1 = [
        {"title_id": 1, "title": "Lead Link", "sections": {"a:lead"}},
        {"title_id": 2, "title": "Body Link", "sections": {"a:body"}},
    ]

    selected = _l1_articles_for_l2_expansion(
        ranked_l1,
        intro_section_keys={"a:lead"},
        source_scope="all_l1",
    )

    assert [row["title"] for row in selected] == ["Lead Link", "Body Link"]


def test_coverage_crawler_reserves_frontier_slots_for_l2() -> None:
    rows = [
        {"to_title_id": 1, "to_title": "L1 A", "level": 1, "score": 0.91, "link_order": 1},
        {"to_title_id": 2, "to_title": "L1 B", "level": 1, "score": 0.89, "link_order": 2},
        {"to_title_id": 3, "to_title": "L1 C", "level": 1, "score": 0.87, "link_order": 3},
        {"to_title_id": 4, "to_title": "L2 A", "level": 2, "score": 0.65, "link_order": 1},
        {"to_title_id": 5, "to_title": "L2 B", "level": 2, "score": 0.63, "link_order": 2},
    ]

    selected = _select_frontier_candidates(rows, limit=2)

    assert [row["level"] for row in selected] == [1, 2]


def test_coverage_crawler_deepens_only_after_strong_priority() -> None:
    assert _frontier_section_limit(configured_limit=4, priority_score=0.4) == 1
    assert _frontier_section_limit(configured_limit=4, priority_score=0.55) == 2
    assert _frontier_section_limit(configured_limit=4, priority_score=0.7) == 4


def test_graph_coverage_state_distinguishes_incomplete_from_stale() -> None:
    assert _coverage_state("pending") == "incomplete"
    assert _coverage_state("partial") == "incomplete"
    assert _coverage_state("stale") == "stale"


def test_graph_overall_coverage_keeps_incomplete_visible() -> None:
    state = _overall_coverage_state(
        {
            "seed": {"state": "done"},
            "core": {"state": "incomplete"},
            "temporal": {"state": "missing"},
        }
    )

    assert state == "incomplete"


def test_graphology_edge_exposes_l2_via_title() -> None:
    edge = {
        "source": "section:1:10",
        "target": "article:3",
        "kind": "RELATED_TO",
        "level": 2,
        "score": 0.67,
        "signals": {"via_title": "Sarai and Pharaoh", "score": 0.67},
    }
    converted = _convert_edge(
        edge,
        node_key_by_old_id={"section:1:10": "sec:1:10", "article:3": "art:3"},
        nodes_by_old_id={
            "section:1:10": {"title_id": 1},
            "article:3": {"title_id": 3},
        },
        focus_title_id=1,
        min_relevance=0,
    )

    assert converted is not None
    assert converted["attributes"]["via_title"] == "Sarai and Pharaoh"


def test_graphology_l1_to_l2_edge_has_graph_relation() -> None:
    edge = {
        "source": "article:2",
        "target": "article:3",
        "kind": "L1_TO_L2",
        "level": 2,
        "score": 0.61,
        "signals": {"via_title": "Sarai and Pharaoh", "score": 0.61},
    }
    converted = _convert_edge(
        edge,
        node_key_by_old_id={"article:2": "art:2", "article:3": "art:3"},
        nodes_by_old_id={
            "article:2": {"title_id": 2},
            "article:3": {"title_id": 3},
        },
        focus_title_id=1,
        min_relevance=0,
    )

    assert converted is not None
    assert converted["attributes"]["relation"] == "l1_to_l2"
    assert converted["source"] == "art:2"
    assert converted["target"] == "art:3"
