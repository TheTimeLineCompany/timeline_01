from app.seeds.temporal import normalize_temporal_mentions


def test_normalize_year_month_and_season():
    matches = normalize_temporal_mentions("In July 1983 and winter 1984, work continued.")
    refs = {match.time_ref_id for match in matches}
    assert "ti:month:1983-07" in refs
    assert "ti:season:1984:winter" in refs
    assert "ti:year:1983" in refs


def test_normalize_iso_date():
    matches = normalize_temporal_mentions("The event happened on 1983-07-16.")
    by_ref = {match.time_ref_id: match for match in matches}
    assert by_ref["tp:1983-07-16"].precision == "day"


def test_normalize_natural_full_date():
    matches = normalize_temporal_mentions("Lincoln was born on February 12, 1809 in Kentucky.")
    by_ref = {match.time_ref_id: match for match in matches}
    assert "tp:1809-02-12" in by_ref
    m = by_ref["tp:1809-02-12"]
    assert m.precision == "day"
    assert m.year == 1809
    assert m.month == 2
    assert m.day == 12


def test_normalize_natural_full_date_dmy():
    matches = normalize_temporal_mentions("He died 15 April 1865 from a gunshot wound.")
    by_ref = {match.time_ref_id: match for match in matches}
    assert "tp:1865-04-15" in by_ref
    assert by_ref["tp:1865-04-15"].precision == "day"


def test_ignores_image_map_coordinate_years():
    text = (
        "poly 905 418 941 328 987 295 995 284 982 244 990 206 "
        "1036 207 1046 247 1047 284 1066 312"
    )
    assert normalize_temporal_mentions(text) == []


def test_ignores_coordinate_runs_after_poly_marker():
    text = (
        "Gideon Welles poly 703 783 752 769 825 627 907 620 929 569 "
        "905 538 886 563 833 563 873 502 930 450 1043 407 1043 389 "
        "1036 382 1042 363 1058 335 1052 333 1052 324 1081 318"
    )
    assert normalize_temporal_mentions(text) == []


def test_normalize_explicit_era_years():
    matches = normalize_temporal_mentions("Rome changed after 476 CE and earlier crises around 300 BC.")
    by_ref = {match.time_ref_id: match for match in matches}
    assert "ti:year:0476" in by_ref
    assert by_ref["ti:year:0476"].year == 476
    assert by_ref["ti:year:0476"].era_name == "CE"
    assert "ti:year:-0300" in by_ref
    assert by_ref["ti:year:-0300"].year == -300
    assert by_ref["ti:year:-0300"].era_name == "BC"


def test_normalize_deep_time_years_ago():
    matches = normalize_temporal_mentions(
        "The Cambrian explosion occurred about 541 million years ago, long before the K-Pg event."
    )
    by_ref = {match.time_ref_id: match for match in matches}
    assert "ti:deep_time:541000000ya" in by_ref
    match = by_ref["ti:deep_time:541000000ya"]
    assert match.time_kind == "deep_time"
    assert match.precision == "deep_time"
    assert match.metadata_json["years_ago"] == 541_000_000
