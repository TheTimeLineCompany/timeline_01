from types import SimpleNamespace

from app.workers.temporal_agent import (
    _find_evidence_start,
    _is_temporalish,
    _temporal_section_value_score,
    canonical_time_label,
    parse_temporal_agent_response,
)


def test_parse_temporal_agent_response_day_fact():
    facts = parse_temporal_agent_response(
        """
        {
          "events": [
            {
              "label": "February 12, 1809",
              "time_kind": "point",
              "precision": "day",
              "year": 1809,
              "month": 2,
              "day": 12,
              "season": null,
              "start_date": "1809-02-12",
              "end_date": "1809-02-12",
              "confidence": 0.93,
              "evidence": "Born in a one-room log cabin in Kentucky on February 12, 1809"
            }
          ]
        }
        """
    )
    assert len(facts) == 1
    assert facts[0].time_ref_id == "tp:1809-02-12"
    assert facts[0].precision == "day"
    assert facts[0].confidence == 0.93


def test_parse_temporal_agent_response_deduplicates_refs():
    facts = parse_temporal_agent_response(
        """
        ```json
        {
          "events": [
            {
              "label": "1809",
              "time_kind": "year",
              "precision": "year",
              "year": 1809,
              "month": null,
              "day": null,
              "season": null,
              "start_date": "1809-01-01",
              "end_date": "1809-12-31",
              "confidence": 0.7,
              "evidence": "February 12, 1809"
            },
            {
              "label": "1809 duplicate",
              "time_kind": "year",
              "precision": "year",
              "year": 1809,
              "month": null,
              "day": null,
              "season": null,
              "start_date": "1809-01-01",
              "end_date": "1809-12-31",
              "confidence": 0.7,
              "evidence": "February 12, 1809"
            }
          ]
        }
        ```
        """
    )
    assert [fact.time_ref_id for fact in facts] == ["ti:year:1809"]


def test_parse_temporal_agent_response_ignores_invalid_dates():
    facts = parse_temporal_agent_response(
        """
        {
          "events": [
            {
              "label": "coordinate noise",
              "time_kind": "year",
              "precision": "year",
              "year": 9999,
              "month": null,
              "day": null,
              "season": null,
              "start_date": "9999-01-01",
              "end_date": "9999-12-31",
              "confidence": 0.7,
              "evidence": "poly 1036 382 1042 363"
            }
          ]
        }
        """
    )
    assert facts == []


def test_parse_temporal_agent_response_salvages_extra_object_stream():
    raw = (
        '{"events":[{"label":"February 12, 1809","time_kind":"point","precision":"day",'
        '"year":1809,"month":2,"day":12,"season":null,"start_date":"1809-02-12",'
        '"end_date":"1809-02-12","confidence":0.9,"evidence":"February 12, 1809"}]},'
        '{"label":"April 15, 1865","time_kind":"point","precision":"day",'
        '"year":1865,"month":4,"day":15,"season":null,"start_date":"1865-04-15",'
        '"end_date":"1865-04-15","confidence":0.9,"evidence":"April 15, 1865"}'
    )
    refs = {fact.time_ref_id for fact in parse_temporal_agent_response(raw)}
    assert refs == {"tp:1809-02-12", "tp:1865-04-15"}


def test_parse_temporal_agent_response_treats_null_as_no_events():
    assert parse_temporal_agent_response("null") == []


def test_parse_temporal_agent_response_repairs_trailing_commas():
    facts = parse_temporal_agent_response(
        """
        ```json
        {
          "events": [
            {
              "label": "1865",
              "time_kind": "year",
              "precision": "year",
              "year": 1865,
              "month": null,
              "day": null,
              "season": null,
              "start_date": "1865-01-01",
              "end_date": "1865-12-31",
              "confidence": 0.8,
              "evidence": "1865",
            },
          ],
        }
        ```
        """
    )

    assert [fact.time_ref_id for fact in facts] == ["ti:year:1865"]


def test_find_evidence_start_requires_real_section_text():
    text = "Samuel Lincoln was an ancestor of President Abraham Lincoln."

    assert _find_evidence_start(text, "President Abraham Lincoln") >= 0
    assert _find_evidence_start(text, "Dedumose II died") == -1


def test_agent_fact_uses_canonical_time_label_not_event_phrase():
    facts = parse_temporal_agent_response(
        """
        {"events":[{"label":"Dedumose II's death","time_kind":"year","precision":"year",
        "year":1690,"month":null,"day":null,"season":null,
        "start_date":"1690-01-01","end_date":"1690-12-31","confidence":0.9,
        "evidence":"26 May 1690"}]}
        """
    )

    assert facts[0].label == "1690"
    assert facts[0].metadata_json["agent_label"] == "Dedumose II's death"


def test_canonical_time_label_formats_common_refs():
    assert canonical_time_label("tp:1865-04-15") == "1865-04-15"
    assert canonical_time_label("ti:year:1865") == "1865"
    assert canonical_time_label("ti:month:1865-04") == "1865-04"
    assert canonical_time_label("ti:season:1865:spring") == "Spring 1865"
    assert canonical_time_label("ti:interval:1861-01-01:1865-12-31") == "1861-01-01 to 1865-12-31"
    assert canonical_time_label("ti:year:0359") == "359"
    assert canonical_time_label("tp:0359-01-01") == "359-01-01"


def test_temporalish_gate_detects_dates_and_eras():
    assert _is_temporalish("The empire fell in 476 CE after a long crisis.")
    assert _is_temporalish("The deposits formed 4.5 billion years ago.")
    assert not _is_temporalish("This section describes names and places without chronology.")


def test_temporal_section_value_score_rewards_signal_density():
    high_signal = SimpleNamespace(
        clean_text="In 1809 he was born. In 1861 he became president. In 1865 he died after the war.",
        links_json=[{"target": "American Civil War"}],
        heading="History",
    )
    low_signal = SimpleNamespace(
        clean_text="This section describes a name and a place.",
        links_json=[],
        heading="See also",
    )

    assert _temporal_section_value_score(high_signal) > _temporal_section_value_score(low_signal)
