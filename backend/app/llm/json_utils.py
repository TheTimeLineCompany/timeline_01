"""Tolerant JSON extraction helpers for local LLM outputs."""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json_object(raw_response: str) -> dict[str, Any]:
    """Extract a JSON object from common LLM response wrappers.

    This intentionally stays small and dependency-free. It handles the failure
    modes we see most often: markdown fences, prose around JSON, and trailing
    commas before object/list closes. Item-level salvage still belongs in the
    worker-specific parser because each job has a different schema.
    """

    cleaned = _strip_markdown_fence(raw_response.strip())
    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])

    errors: list[str] = []
    for candidate in candidates:
        for repaired in (candidate, _repair_common_json(candidate)):
            try:
                payload = json.loads(repaired)
            except json.JSONDecodeError as exc:
                errors.append(str(exc))
                continue
            if isinstance(payload, dict):
                return payload
            errors.append("parsed JSON was not an object")
    raise ValueError(f"LLM response did not contain a JSON object: {'; '.join(errors[-2:])}")


def _strip_markdown_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    return re.sub(r"```$", "", text).strip()


def _repair_common_json(text: str) -> str:
    repaired = text.strip()
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    return repaired
