from app.ontology.temporal_projection import (
    _assertion_text,
    parse_isoish_date,
    precision_score,
    section_fact_id,
    time_scalar,
)


def test_section_fact_id_is_deterministic():
    assert section_fact_id("123:456", "year:1992") == "section-time:123:456:year:1992"


def test_parse_isoish_date_handles_year_month_and_day_precision():
    assert parse_isoish_date("1992") == (1992, 1, 1)
    assert parse_isoish_date("1992-04") == (1992, 4, 1)
    assert parse_isoish_date("1992-04-17") == (1992, 4, 17)
    assert parse_isoish_date("not-a-date") is None


def test_time_scalar_uses_date_first_then_fallback_year():
    assert time_scalar("1992", None) == 1992.0
    assert round(time_scalar("1992-04", None), 4) == 1992.25
    assert round(time_scalar("1992-04-17", None), 4) == 1992.2937
    assert time_scalar(None, 1993) == 1993.0
    assert time_scalar("not-a-date", 1994) == 1994.0
    assert time_scalar(None, None) is None


def test_precision_score_prefers_exact_dates_over_fuzzy_ranges():
    assert precision_score("day") > precision_score("year")
    assert precision_score("year") > precision_score("era")
    assert precision_score("unknown") == precision_score("fuzzy")


def test_assertion_text_uses_provenance_span_when_available():
    section = type(
        "Section",
        (),
        {
            "clean_text": "Lincoln was born in Kentucky on February 12, 1809.",
            "title": "Abraham Lincoln",
            "heading": "Early life",
        },
    )()
    time_dimension = type("TimeDimension", (), {"label": "February 12, 1809", "time_ref_id": "day:1809-02-12"})()

    assert _assertion_text(section, time_dimension, {"char_start": 32, "char_end": 49}) == "February 12, 1809"


def test_assertion_text_falls_back_to_section_heading():
    section = type(
        "Section",
        (),
        {"clean_text": "No direct span here.", "title": "Abraham Lincoln", "heading": "Early life"},
    )()
    time_dimension = type("TimeDimension", (), {"label": "1809", "time_ref_id": "year:1809"})()

    assert _assertion_text(section, time_dimension, {}) == "1809 is referenced in Abraham Lincoln / Early life."
