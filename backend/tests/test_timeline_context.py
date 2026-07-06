from types import SimpleNamespace
from datetime import datetime, timedelta

from app.api.reader import _timeline_display_confidence
from app.timeline.context_service import TimelineContextService
from app.workers.temporal_agent import SOURCE as AGENT_TEMPORAL_SOURCE
from app.workers.timeline_context import _apply_timeline_context_job_result


def test_timeline_context_score_rewards_agent_temporal_and_l1():
    related = SimpleNamespace(score=0.7, level=1, why_source="template")
    section_time = SimpleNamespace(confidence=0.8, source=AGENT_TEMPORAL_SOURCE)

    score = TimelineContextService._timeline_relevance_score(related, section_time)

    assert score == 0.842


def test_timeline_context_score_caps_at_one():
    related = SimpleNamespace(score=1.0, level=1, why_source="template")
    section_time = SimpleNamespace(confidence=1.0, source=AGENT_TEMPORAL_SOURCE)

    score = TimelineContextService._timeline_relevance_score(related, section_time)

    assert score == 1.0


def test_timeline_context_score_rewards_agent_related_rows():
    related = SimpleNamespace(score=0.12, level=2, why_source="agent_related_v1")
    section_time = SimpleNamespace(confidence=0.9, source=AGENT_TEMPORAL_SOURCE)

    score = TimelineContextService._timeline_relevance_score(related, section_time)

    assert score == 0.5316


def test_timeline_context_identifies_self_context():
    assert TimelineContextService._is_self_context(from_title_id=10, source_title_id=10)
    assert not TimelineContextService._is_self_context(from_title_id=10, source_title_id=11)


def test_timeline_context_promotes_l2_only_when_agent_or_high_score():
    agent_l2 = SimpleNamespace(level=2, score=0.55, why_source="agent_related_v1")
    high_l2 = SimpleNamespace(level=2, score=0.73, why_source="template")
    weak_l2 = SimpleNamespace(level=2, score=0.63, why_source="template")
    l1 = SimpleNamespace(level=1, score=0.45, why_source="template")

    assert TimelineContextService._is_promotable_related_row(agent_l2)
    assert TimelineContextService._is_promotable_related_row(high_l2)
    assert not TimelineContextService._is_promotable_related_row(weak_l2)
    assert TimelineContextService._is_promotable_related_row(l1)


def test_timeline_context_marks_related_rows_as_section_attributed() -> None:
    attribution = TimelineContextService._section_attribution()

    assert attribution["level"] == "section"
    assert attribution["status"] == "section_attributed_unreviewed"
    assert attribution["focus_topic_assertion"] is False
    assert attribution["reviewed"] is False


def test_unreviewed_context_confidence_is_capped() -> None:
    assert _timeline_display_confidence(
        0.95,
        {
            "level": "section",
            "status": "section_attributed_unreviewed",
            "focus_topic_assertion": False,
            "reviewed": False,
        },
    ) == 0.68

    assert _timeline_display_confidence(
        0.95,
        {"status": "focus_core", "focus_topic_assertion": True, "reviewed": False},
    ) == 0.95


def test_timeline_context_prefers_stored_gate_decision():
    gated_l2 = SimpleNamespace(
        level=2,
        score=0.2,
        why_source="template",
        signals_json={"gates": {"timeline_eligible": True}},
    )
    rejected_l1 = SimpleNamespace(
        level=1,
        score=0.9,
        why_source="template",
        signals_json={"gates": {"timeline_eligible": False}},
    )

    assert TimelineContextService._is_promotable_related_row(gated_l2)
    assert not TimelineContextService._is_promotable_related_row(rejected_l1)


def test_temporal_context_gate_rejects_far_direct_link_dates():
    source_time = SimpleNamespace(year=-1800, start_date=None, end_date=None)
    candidate_time = SimpleNamespace(year=1922, start_date=None, end_date=None)
    related = SimpleNamespace(level=1, score=0.52, why_source="template")

    gate = TimelineContextService._temporal_context_gate([source_time], candidate_time, related)

    assert gate["accepted"] is False
    assert gate["reason"] == "temporal_distance_too_large"


def test_temporal_context_gate_accepts_near_direct_link_dates():
    source_time = SimpleNamespace(year=1861, start_date=None, end_date=None)
    candidate_time = SimpleNamespace(year=1865, start_date=None, end_date=None)
    related = SimpleNamespace(level=1, score=0.52, why_source="template")

    gate = TimelineContextService._temporal_context_gate([source_time], candidate_time, related)

    assert gate["accepted"] is True
    assert gate["reason"] == "near_same_period"


def test_passage_context_gate_accepts_candidate_that_mentions_source_title():
    source = SimpleNamespace(title="Abraham Lincoln", heading="Early life", clean_text="Lincoln was born in Kentucky.")
    candidate = SimpleNamespace(
        title="Samuel Lincoln",
        heading="Samuel Lincoln",
        clean_text="Samuel Lincoln was an ancestor of President Abraham Lincoln.",
    )
    related = SimpleNamespace(level=1, score=0.26, why_source="agent_related_v1", to_title="Samuel Lincoln", signals_json={})

    gate = TimelineContextService._passage_context_gate(source, candidate, related)

    assert gate["accepted"] is True
    assert gate["reason"] == "candidate_mentions_source_title"


def test_passage_context_gate_rejects_generic_linked_history_passage():
    source = SimpleNamespace(title="Abraham Lincoln", heading="Early life", clean_text="Lincoln's ancestors lived in Virginia.")
    candidate = SimpleNamespace(
        title="Colony of Virginia",
        heading="History",
        clean_text="The Spanish made earlier attempts along the Mid-Atlantic coastline in the 16th century.",
    )
    related = SimpleNamespace(
        level=1,
        score=0.16,
        why_source="agent_related_v1",
        to_title="Colony of Virginia",
        signals_json={"agent_related_v1": {"confidence": 0.8, "reasoning_tags": ["direct_link"]}},
    )

    gate = TimelineContextService._passage_context_gate(source, candidate, related)

    assert gate["accepted"] is False
    assert gate["reason"] == "candidate_passage_not_source_specific"


def test_passage_context_gate_rejects_l2_shared_time_without_passage_overlap():
    source = SimpleNamespace(
        title="Caste",
        heading="United States",
        clean_text="Discrimination in the Southern United States in the 1930s was compared to Indian castes.",
    )
    candidate = SimpleNamespace(
        title="2020 United States census",
        heading="Background",
        clean_text="The U.S. census has been conducted every 10 years since 1790. Census day was April 1, 2020.",
    )
    related = SimpleNamespace(
        level=2,
        score=0.63,
        why_source="agent_related_v1",
        to_title="2020 United States census",
        signals_json={"agent_related_v1": {"confidence": 0.8, "reasoning_tags": ["direct_link", "shared_time"]}},
    )

    gate = TimelineContextService._passage_context_gate(source, candidate, related)

    assert gate["accepted"] is False
    assert gate["reason"] == "candidate_passage_not_source_specific"


def test_timeline_context_pending_result_requeues_without_burning_attempt() -> None:
    now = datetime(2026, 6, 25, 12, 0, 0)
    job = SimpleNamespace(
        status="running",
        attempts=2,
        completed_at=None,
        last_error="old error",
        locked_by="worker-1",
        locked_at=now - timedelta(seconds=10),
        run_after=None,
        updated_at=None,
    )

    _apply_timeline_context_job_result(job, pending=True, now=now, retry_delay_seconds=45)

    assert job.status == "pending"
    assert job.attempts == 1
    assert job.completed_at is None
    assert job.last_error is None
    assert job.locked_by is None
    assert job.locked_at is None
    assert job.run_after == now + timedelta(seconds=45)
    assert job.updated_at == now


def test_timeline_context_ready_result_marks_job_succeeded() -> None:
    now = datetime(2026, 6, 25, 12, 0, 0)
    job = SimpleNamespace(
        status="running",
        attempts=1,
        completed_at=None,
        last_error=None,
        locked_by="worker-1",
        locked_at=now - timedelta(seconds=10),
        run_after=now + timedelta(seconds=30),
        updated_at=None,
    )

    _apply_timeline_context_job_result(job, pending=False, now=now)

    assert job.status == "succeeded"
    assert job.attempts == 1
    assert job.completed_at == now
    assert job.locked_by is None
    assert job.locked_at is None
    assert job.run_after is None
    assert job.updated_at == now
