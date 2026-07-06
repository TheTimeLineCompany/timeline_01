from app.related.service import RelatedCandidate, RelatedInfoService, _best_similarity, _embedding_signal_used, _normalize_article_title
import pytest
from types import SimpleNamespace


class RelatedRow:
    def __init__(self, scoring_version: str, gate_version: str, embedding_ready: dict | None = None):
        self.signals_json = {
            "scoring_version": scoring_version,
            "gates": {"version": gate_version},
        }
        if embedding_ready is not None:
            self.signals_json["embedding_ready"] = embedding_ready


def test_related_source_article_detection_uses_title_id():
    assert RelatedInfoService._is_source_article(
        "Other title",
        42,
        42,
        _normalize_article_title("Main Article"),
    )


def test_related_source_article_detection_uses_normalized_title():
    assert RelatedInfoService._is_source_article(
        "Main_Article",
        99,
        42,
        _normalize_article_title("Main Article"),
    )


def test_related_source_article_detection_allows_other_articles():
    assert not RelatedInfoService._is_source_article(
        "Other Article",
        99,
        42,
        _normalize_article_title("Main Article"),
    )


def test_related_cache_freshness_requires_current_gate_version():
    assert not RelatedInfoService._cache_has_current_scoring([
        RelatedRow("ontology-components-v1", "ontology-gates-v1")
    ])


def test_related_cache_freshness_accepts_current_gate_version():
    assert RelatedInfoService._cache_has_current_scoring([
        RelatedRow("component-relatedness-v2", "ontology-gates-v2")
    ])


@pytest.mark.asyncio
async def test_related_cache_signal_state_rejects_rows_scored_before_embeddings(monkeypatch):
    service = RelatedInfoService(session=None)

    async def has_embeddings(_keys):
        return True

    monkeypatch.setattr(service, "_has_embeddings", has_embeddings)

    fresh = await service._cache_has_current_signal_state(
        SimpleNamespace(section_key="section:1"),
        [RelatedRow("component-relatedness-v2", "ontology-gates-v2", {"source": False, "used": "none"})],
    )

    assert fresh is False


@pytest.mark.asyncio
async def test_wait_for_embeddings_returns_false_for_empty_keys():
    service = RelatedInfoService(session=None)

    assert await service._wait_for_embeddings([], timeout_seconds=0.01) is False


def test_embedding_signal_prefers_stronger_available_similarity():
    assert _best_similarity(None, None) is None
    assert _best_similarity(0.4, None) == 0.4
    assert _best_similarity(0.4, 0.7) == 0.7
    assert _embedding_signal_used(0.8, 0.4) == "intro"
    assert _embedding_signal_used(0.2, 0.6) == "broad"
    assert _embedding_signal_used(None, 0.6) == "broad"


def test_related_candidate_slice_keeps_all_l1_and_bounds_l2(monkeypatch):
    monkeypatch.setattr("app.related.service.settings.related_rank_candidate_limit", 3)
    candidates = [
        RelatedCandidate("L2 early", 5, 2, link_rank=0, source_article_link_count=10),
        RelatedCandidate("L1 weak", 1, 1, link_rank=10, source_article_link_count=1),
        RelatedCandidate("L1 strong", 2, 1, link_rank=2, source_article_link_count=3),
        RelatedCandidate("L1 repeated", 3, 1, link_rank=8, source_article_link_count=4),
        RelatedCandidate("L2 other", 4, 2, link_rank=1, source_article_link_count=5),
    ]

    selected = RelatedInfoService._prioritized_candidate_slice(candidates)

    assert [item.title for item in selected] == ["L1 repeated", "L1 strong", "L1 weak", "L2 early", "L2 other"]


def test_l1_candidates_for_l2_expansion_stays_bounded(monkeypatch):
    monkeypatch.setattr("app.related.service.settings.related_l1_limit", 2)
    candidates = [
        RelatedCandidate("L1 weak", 1, 1, link_rank=10, source_article_link_count=1),
        RelatedCandidate("L1 strong", 2, 1, link_rank=2, source_article_link_count=3),
        RelatedCandidate("L1 repeated", 3, 1, link_rank=8, source_article_link_count=4),
        RelatedCandidate("L2 early", 5, 2, link_rank=0, source_article_link_count=10),
    ]

    selected = RelatedInfoService._l1_candidates_for_l2_expansion(candidates)

    assert [item.title for item in selected] == ["L1 repeated", "L1 strong"]


def test_scored_l1_candidates_for_l2_expansion_uses_weighted_score(monkeypatch):
    monkeypatch.setattr("app.related.service.settings.related_l1_limit", 2)
    weak_repeated = RelatedCandidate("L1 repeated", 1, 1, link_rank=1, source_article_link_count=10)
    strong = RelatedCandidate("L1 strong", 2, 1, link_rank=5, source_article_link_count=1)
    medium = RelatedCandidate("L1 medium", 3, 1, link_rank=2, source_article_link_count=2)

    selected = RelatedInfoService._scored_l1_candidates_for_l2_expansion([
        (weak_repeated, 0.25, {}, ""),
        (strong, 0.81, {}, ""),
        (medium, 0.52, {}, ""),
    ])

    assert [(item.title, score) for item, score in selected] == [
        ("L1 strong", 0.81),
        ("L1 medium", 0.52),
    ]
