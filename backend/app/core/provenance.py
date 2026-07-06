"""Shared provenance helpers."""

from typing import TypedDict


class Provenance(TypedDict):
    """Uniform source span provenance object."""

    title_id: int
    heading_id: int
    char_start: int
    char_end: int
    parser_version: str
    model_version: str
    run_id: str


def make_provenance(
    *,
    title_id: int,
    heading_id: int,
    char_start: int,
    char_end: int,
    parser_version: str,
    model_version: str,
    run_id: str,
) -> Provenance:
    """Build a provenance dictionary."""

    return {
        "title_id": title_id,
        "heading_id": heading_id,
        "char_start": char_start,
        "char_end": char_end,
        "parser_version": parser_version,
        "model_version": model_version,
        "run_id": run_id,
    }
