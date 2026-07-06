from app.workers.related_agent import (
    SWEEP_PACK_JOB_TYPE,
    _section_prompt_excerpt,
    is_agent_related_section,
    parse_related_agent_response,
)


class RelatedRow:
    def __init__(self, title: str, score: float = 0.5, level: int = 1):
        self.to_title = title
        self.score = score
        self.level = level


class Section:
    title_id = 1
    title = "Example"
    heading = "Body"
    heading_id = 1
    level = 1
    section_key = "section:1"

    def __init__(
        self,
        text: str,
        links: list[dict] | None = None,
        *,
        section_key: str = "section:1",
        heading_id: int = 1,
    ):
        self.clean_text = text
        self.links_json = links or []
        self.section_key = section_key
        self.heading_id = heading_id


def test_parse_related_agent_response():
    insights = parse_related_agent_response(
        """
        {
          "insights": [
            {
              "to_title": "American Revolution",
              "why_text": "This gives political context for the section's wartime leadership.",
              "confidence": 0.82,
              "reasoning_tags": ["direct_link", "historical_context"]
            }
          ]
        }
        """
    )
    assert len(insights) == 1
    assert insights[0].to_title == "American Revolution"
    assert insights[0].confidence == 0.82
    assert insights[0].reasoning_tags == ["direct_link", "historical_context"]


def test_parse_related_agent_response_uses_fallback_offsets():
    insights = parse_related_agent_response(
        """
        {
          "insights": [
            {
              "to_title": "Missionaries of Charity",
              "why_text": "Missionaries of Charity: Catholic service frames the paragraph's religious response to poverty.",
              "confidence": 0.82,
              "reasoning_tags": ["direct_link"]
            }
          ]
        }
        """,
        evidence_char_start=120,
        evidence_char_end=340,
    )

    assert insights[0].evidence_char_start == 120
    assert insights[0].evidence_char_end == 340


def test_parse_related_agent_response_deduplicates_titles():
    insights = parse_related_agent_response(
        """
        ```json
        {
          "insights": [
            {
              "to_title": "Virginia",
              "why_text": "Connects the section to Washington's home colony.",
              "confidence": 0.7,
              "reasoning_tags": ["place"]
            },
            {
              "to_title": "virginia",
              "why_text": "Duplicate title with different case.",
              "confidence": 0.7,
              "reasoning_tags": []
            }
          ]
        }
        ```
        """
    )
    assert [insight.to_title for insight in insights] == ["Virginia"]


def test_parse_related_agent_response_treats_null_as_empty():
    assert parse_related_agent_response("null") == []


def test_parse_related_agent_response_recovers_objects_from_malformed_batch():
    insights = parse_related_agent_response(
        """
        {"insights":[{"to_title":"Coles County, Illinois","why_text":"Coles County, Illinois: Lincoln's father moved the family there before Abraham began independent life.","confidence":0.8,"reasoning_tags":["direct_link"],"evidence_char_start":103,"evidence_char_end":246}]
        ,
        {"to_title":"Lincoln's New Salem","why_text":"Lincoln's New Salem: the village anchors Lincoln's store work and early political ambitions.","confidence":0.8,"reasoning_tags":["direct_link"],"evidence_char_start":247,"evidence_char_end":348}]}
        """,
        evidence_char_start=10,
        evidence_char_end=20,
    )

    assert [insight.to_title for insight in insights] == ["Coles County, Illinois", "Lincoln's New Salem"]
    assert insights[0].evidence_char_start == 103


def test_parse_related_agent_response_rejects_negative_non_insights():
    insights = parse_related_agent_response(
        """
        {
          "insights": [
            {
              "to_title": "Talis Group",
              "why_text": "Talis Group, a software company, lacks a concrete connection to the social structure described.",
              "confidence": 0.4,
              "reasoning_tags": []
            },
            {
              "to_title": "Europe",
              "why_text": "Europe: a place that does not fit the definition of a caste.",
              "confidence": 0.4,
              "reasoning_tags": []
            },
            {
              "to_title": "Social group",
              "why_text": "Social group: caste is the inherited group used to define the caste system.",
              "confidence": 0.8,
              "reasoning_tags": ["direct_link"]
            }
          ]
        }
        """
    )

    assert [insight.to_title for insight in insights] == ["Social group"]


def test_section_prompt_excerpt_centers_candidate_evidence():
    text = (
        "Opening context that should not dominate the prompt. " * 20
        + "Mother Teresa and the Missionaries of Charity appear in this decisive sentence. "
        + "Trailing context that is less important. " * 20
    )

    excerpt, start, end = _section_prompt_excerpt(text, [RelatedRow("Missionaries of Charity")], max_chars=260)

    assert start > 0
    assert end > start
    assert "Missionaries of Charity" in excerpt
    assert excerpt.count("Opening context that should not dominate") < 4


def test_agent_related_section_requires_enough_signal():
    assert not is_agent_related_section(Section("Short text."))
    assert is_agent_related_section(
        Section(
            "This section is long enough to compare against linked articles and contains a concrete target. "
            "It has enough surrounding context for the agent to write a grounded comparison.",
            links=[{"target": "Target"}],
        )
    )


def test_related_sweep_pack_job_type_is_distinct():
    assert SWEEP_PACK_JOB_TYPE == "related_sweep_pack_v1"
    assert SWEEP_PACK_JOB_TYPE != "related_l1_l2_explain_v1"
