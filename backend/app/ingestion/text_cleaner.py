"""Wikitext cleaning and link extraction for V4."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

NAMESPACE_PREFIXES = (
    "category:",
    "file:",
    "help:",
    "image:",
    "portal:",
    "template:",
    "wikipedia:",
    "wikt:",
)


@dataclass(frozen=True)
class WikiLink:
    """A parsed wiki link."""

    target: str
    label: str
    char_start: int
    char_end: int


def normalize_title_target(value: str) -> str | None:
    """Normalize a wiki link/title target for lookup."""

    text = " ".join((value or "").replace("_", " ").split()).strip()
    if not text:
        return None
    text = text.split("#", 1)[0].strip()
    if not text:
        return None
    lowered = text.lower()
    if any(lowered.startswith(prefix) for prefix in NAMESPACE_PREFIXES):
        return None
    if ":" in text:
        return None
    return text[:1].upper() + text[1:]


def extract_wikilinks(raw_text: str) -> list[WikiLink]:
    """Extract normalized wikilinks with approximate raw spans."""

    links: list[WikiLink] = []
    raw_text = remove_table_blocks(remove_file_blocks(raw_text or ""))
    pattern = re.compile(r"\[\[([^\[\]]+?)\]\]")
    for match in pattern.finditer(raw_text or ""):
        inner = match.group(1)
        target_raw, _, label_raw = inner.partition("|")
        target = normalize_title_target(target_raw)
        if target is None:
            continue
        label = (label_raw or target_raw).strip()
        links.append(
            WikiLink(
                target=target,
                label=label or target,
                char_start=match.start(),
                char_end=match.end(),
            )
        )
    return links


def remove_file_blocks(text: str) -> str:
    """Remove file/image wiki blocks, including captions with nested links."""

    source = text or ""
    output: list[str] = []
    index = 0
    while index < len(source):
        if _starts_media_link(source, index):
            end = _find_balanced_wikilink_end(source, index)
            if end < 0:
                break
            output.append("\n")
            index = end
            continue
        output.append(source[index])
        index += 1
    return "".join(output)


def _starts_media_link(text: str, index: int) -> bool:
    if text[index : index + 2] != "[[":
        return False
    inner = text[index + 2 : index + 24].lstrip()
    lowered = inner.lower()
    return lowered.startswith("file:") or lowered.startswith("image:")


def _find_balanced_wikilink_end(text: str, start: int) -> int:
    depth = 0
    index = start
    while index < len(text):
        if text[index : index + 2] == "[[":
            depth += 1
            index += 2
            continue
        if text[index : index + 2] == "]]" and depth:
            depth -= 1
            index += 2
            if depth == 0:
                return index
            continue
        index += 1
    return -1


def remove_table_blocks(text: str) -> str:
    """Remove wiki table blocks, including infobox-like table markup."""

    source = text or ""
    output: list[str] = []
    depth = 0
    index = 0
    while index < len(source):
        if source[index : index + 2] == "{|":
            depth += 1
            index += 2
            continue
        if source[index : index + 2] == "|}" and depth:
            depth -= 1
            index += 2
            if depth == 0:
                output.append("\n")
            continue
        if depth == 0:
            output.append(source[index])
        index += 1
    return "".join(output)


def remove_templates(text: str) -> str:
    """Remove nested template blocks with a small stack parser."""

    output: list[str] = []
    depth = 0
    index = 0
    while index < len(text):
        if text[index : index + 2] == "{{":
            depth += 1
            index += 2
        elif text[index : index + 2] == "}}" and depth:
            depth -= 1
            index += 2
        else:
            if depth == 0:
                output.append(text[index])
            index += 1
    return "".join(output)


def clean_wikitext(raw_text: str) -> str:
    """Convert wikitext into plain readable text."""

    text = raw_text or ""
    text = remove_file_blocks(text)
    text = remove_table_blocks(text)
    text = remove_templates(text)
    text = re.sub(r"\[\[([^\[\]|]+)\|([^\[\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\[\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[https?://[^\]]+\]", "", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.DOTALL)
    text = re.sub(r"<ref[^/]*/>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"'''([^']+)'''", r"\1", text)
    text = re.sub(r"''([^']+)''", r"\1", text)
    text = re.sub(r"\n\s*\n", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def safe_html_from_wikitext(raw_text: str) -> str:
    """Render a deliberately small safe HTML subset."""

    clean = clean_wikitext(raw_text)
    parts: list[str] = []
    for paragraph in clean.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        heading_match = re.match(r"^(=+)\s*(.*?)\s*=+$", paragraph)
        if heading_match:
            level = min(6, max(2, len(heading_match.group(1))))
            parts.append(f"<h{level}>{html.escape(heading_match.group(2))}</h{level}>")
        else:
            parts.append(f"<p>{html.escape(paragraph)}</p>")
    return "\n".join(parts)


def detect_redirect_target(raw_text: str) -> str | None:
    """Return redirect target if raw wikitext is a redirect stub."""

    match = re.search(
        r"^\s*#\s*REDIRECT(?:ION)?\s*:?\s*\[\[\s*([^\]|#]+)",
        raw_text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return normalize_title_target(match.group(1))
