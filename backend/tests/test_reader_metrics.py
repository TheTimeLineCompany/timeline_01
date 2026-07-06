from types import SimpleNamespace

import pytest

from app.api.reader import _job_counts
from app.api.reader import _related_scoring_metrics, _timeline_scoring_metrics
from app.api.schemas import TimelineEventResponse


def test_timeline_scoring_metrics_reports_attribution_and_caps() -> None:
    events = [
        TimelineEventResponse(
            id="l0",
            title_id=1,
            source_title_id=1,
            source_title="Focus",
            heading_id=1,
            source_heading="Lead",
            section_key="1:1",
            heading="Lead",
            time_ref_id="year:1809",
            time_kind="point",
            label="1809",
            precision="year",
            start_date=None,
            end_date=None,
            year=1809,
            month=None,
            day=None,
            season=None,
            source="agent_temporal_v1",
            confidence=0.9,
            excerpt="Born in 1809.",
            provenance={},
            attribution={"status": "focus_core", "focus_topic_assertion": True, "reviewed": False},
        ),
        TimelineEventResponse(
            id="ctx",
            title_id=1,
            source_title_id=2,
            source_title="Context",
            heading_id=2,
            source_heading="History",
            section_key="1:1",
            heading="Lead",
            time_ref_id="year:1810",
            time_kind="point",
            label="1810",
            precision="year",
            start_date=None,
            end_date=None,
            year=1810,
            month=None,
            day=None,
            season=None,
            source="context_l1",
            confidence=0.68,
            excerpt="Context in 1810.",
            level=1,
            track="context",
            relevance_score=0.52,
            provenance={},
            attribution={
                "status": "section_attributed_unreviewed",
                "focus_topic_assertion": False,
                "reviewed": False,
            },
        ),
    ]

    metrics = _timeline_scoring_metrics(events)

    assert metrics["attribution_counts"]["focus_core"] == 1
    assert metrics["attribution_counts"]["section_attributed_unreviewed"] == 1
    assert metrics["section_attributed_context_count"] == 1
    assert metrics["confidence_capped_context_count"] == 1


def test_related_scoring_metrics_reports_priority_distribution() -> None:
    items = [
        SimpleNamespace(score=0.7, level=1, signals_json={"priority": {"S_prio": 0.8}}),
        SimpleNamespace(score=0.4, level=2, signals_json={"priority": {"S_prio": 0.3}}),
        SimpleNamespace(score=0.2, level=2, signals_json={"priority": {"S_prio": "bad"}}),
    ]

    metrics = _related_scoring_metrics(items)

    assert metrics["priority_distribution"]["count"] == 2
    assert metrics["priority_distribution"]["max"] == 0.8


@pytest.mark.anyio
async def test_job_counts_treats_exhausted_retry_as_failed(monkeypatch) -> None:
    class FakeResult:
        def all(self):
            return [("retry", 3, 3, 2), ("retry", 1, 3, 1), ("pending", 0, 3, 4)]

    class FakeSession:
        async def execute(self, _stmt):
            return FakeResult()

    counts = await _job_counts(FakeSession(), "job", 1, [])

    assert counts["failed"] == 2
    assert counts["retry"] == 1
    assert counts["pending"] == 4
