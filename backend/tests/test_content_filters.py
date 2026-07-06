from types import SimpleNamespace

from app.content_filters import is_content_section


def section(heading: str, text: str = "content", links: list[dict[str, object]] | None = None):
    return SimpleNamespace(heading=heading, clean_text=text, links_json=links or [])


def test_references_are_reader_only_non_content_sections():
    assert not is_content_section(section("References", "", [{"target": "Archive"}]))
    assert not is_content_section(section("External links", "official site"))
    assert not is_content_section(section("Further reading", "book list"))
    assert not is_content_section(section("See also", "", [{"target": "Related"}]))


def test_regular_sections_are_content_sections():
    assert is_content_section(section("Early life", "Born in 1809."))
    assert is_content_section(section("Lead", "Article introduction."))
