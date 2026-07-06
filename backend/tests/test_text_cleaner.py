from app.ingestion.text_cleaner import (
    clean_wikitext,
    detect_redirect_target,
    extract_wikilinks,
    normalize_title_target,
    safe_html_from_wikitext,
)


def test_normalize_title_target_strips_anchor_and_namespace():
    assert normalize_title_target("abraham_lincoln#Early_life") == "Abraham lincoln"
    assert normalize_title_target("File:Portrait.jpg") is None


def test_detect_redirect_target():
    assert detect_redirect_target("#REDIRECT [[Abraham Lincoln]]") == "Abraham Lincoln"


def test_extract_wikilinks_filters_namespaces():
    links = extract_wikilinks("[[Abraham Lincoln|Lincoln]] [[File:Portrait.jpg]]")
    assert [link.target for link in links] == ["Abraham Lincoln"]
    assert links[0].label == "Lincoln"


def test_extract_wikilinks_ignores_media_caption_links():
    raw = (
        "[[File:Basor weaving baskets.jpg|thumb|The [[Basor]] weaving bamboo "
        "baskets in [[Uttar Pradesh]].]]\n\n"
        "[[Social stratification]] and [[Endogamy]] are body links."
    )

    links = extract_wikilinks(raw)

    assert [link.target for link in links] == ["Social stratification", "Endogamy"]


def test_safe_html_escapes_raw_text():
    html = safe_html_from_wikitext("<script>alert(1)</script>\n\nNormal")
    assert "<script>" not in html
    assert "Normal" in html


def test_clean_wikitext_removes_file_caption_with_nested_links():
    raw = (
        "[[File:Basor weaving baskets.jpg|thumb|The [[Basor]] weaving bamboo "
        "baskets in a 1916 book. The ''Basor'' are a [[Scheduled Caste]] found "
        "in [[Uttar Pradesh]] in India.]]\n\n"
        "A '''caste''' is a fixed social group."
    )

    clean = clean_wikitext(raw)

    assert "weaving bamboo baskets" not in clean
    assert "Scheduled Caste found" not in clean
    assert clean == "A caste is a fixed social group."


def test_clean_wikitext_removes_table_blocks():
    raw = (
        "{| class=\"infobox\"\n"
        "|+ Political and legal anthropology\n"
        "|-\n"
        "| Status and rank\n"
        "|}\n\n"
        "Article paragraph."
    )

    assert clean_wikitext(raw) == "Article paragraph."
