"""Shared content-section filters for reader enrichment."""

from __future__ import annotations

import re
from typing import Protocol


class SectionLike(Protocol):
    """Minimal section shape needed by enrichment filters."""

    heading: str
    clean_text: str
    links_json: list[dict[str, object]]


NON_CONTENT_HEADINGS = {
    "references",
    "notes",
    "footnotes",
    "citations",
    "sources",
    "bibliography",
    "further reading",
    "external links",
    "see also",
}


def normalized_heading(heading: str | None) -> str:
    """Normalize headings for section-policy checks."""

    return re.sub(r"\s+", " ", (heading or "").strip().lower())


def is_content_section(section: SectionLike) -> bool:
    """Return whether a section should participate in enrichment/timeline logic."""

    heading = normalized_heading(section.heading)
    if heading in NON_CONTENT_HEADINGS:
        return False
    if heading.startswith("references ") or heading.startswith("external links "):
        return False
    return bool((section.clean_text or "").strip() or section.links_json)
