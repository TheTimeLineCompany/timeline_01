"""Priority helpers for V4 enrichment orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from app.content_filters import is_content_section
from app.db.models import SectionClean


@dataclass(frozen=True)
class PriorityComponents:
    """Named prioritization components for background enrichment work."""

    S_conn: float
    S_rel: float
    S_prio: float
    priority: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "S_conn": self.S_conn,
            "S_rel": self.S_rel,
            "S_prio": self.S_prio,
            "priority": self.priority,
        }


def section_value(section: SectionClean) -> float:
    """Return a rough section utility score for background work ordering."""

    if not is_content_section(section):
        return 0.0
    text = " ".join((section.clean_text or "").split())
    if not text:
        return 0.0
    score = 0.18
    heading = (section.heading or "").strip().casefold()
    if heading in {"lead", "introduction", "summary"} or int(section.level or 0) <= 1:
        score += 0.28
    if section.links_json:
        score += min(0.22, len(section.links_json) * 0.018)
    if len(text) >= 240:
        score += 0.14
    if any(char.isdigit() for char in text):
        score += 0.08
    return max(0.0, min(score, 1.0))


def embedding_priority(section: SectionClean, *, scope: str = "l0", link_count: int = 0) -> int:
    """Lower numeric values run earlier in the durable worker queue."""

    base = {
        "l0": 35,
        "l1_intro": 48,
        "l2_intro": 58,
        "candidate_deep": 72,
    }.get(scope, 70)
    value_bonus = int((1.0 - section_value(section)) * 10)
    link_bonus = min(8, max(0, link_count))
    return max(20, base + value_bonus - link_bonus)


def candidate_priority_score(
    *,
    level: int,
    link_rank: int,
    source_article_link_count: int,
    intro_similarity: float | None = None,
) -> float:
    """Score candidate work priority before expensive agent/temporal processing."""

    return compute_candidate_priority(
        level=level,
        link_rank=link_rank,
        source_article_link_count=source_article_link_count,
        intro_similarity=intro_similarity,
    ).S_prio


def compute_candidate_priority(
    *,
    level: int,
    link_rank: int,
    source_article_link_count: int,
    intro_similarity: float | None = None,
    distinct_routes: int = 1,
    source_section_count: int = 1,
    user_focus: float = 0.0,
    estimated_cost: float = 0.35,
) -> PriorityComponents:
    """Return connection, relevance, and compute-priority components.

    `S_conn` is cheap graph/link structure. `S_rel` is available semantic signal
    and can start as zero before embeddings/entities exist. `S_prio` is the
    queue utility after a small cost penalty and optional user-focus boost.
    """

    level_score = 1.0 if level == 1 else 0.62
    rank_score = max(0.0, 1.0 - (max(0, link_rank) * 0.08))
    frequency_score = min(1.0, source_article_link_count / 4.0)
    route_score = min(1.0, max(1, distinct_routes) / 3.0)
    source_section_score = min(1.0, max(1, source_section_count) / 4.0)
    s_conn = round(
        (level_score * 0.32)
        + (rank_score * 0.20)
        + (frequency_score * 0.24)
        + (route_score * 0.12)
        + (source_section_score * 0.12),
        4,
    )
    similarity_score = 0.0 if intro_similarity is None else max(0.0, min(1.0, intro_similarity))
    s_rel = round((similarity_score * 0.75) + (s_conn * 0.25), 4)
    cost_penalty = max(0.0, min(1.0, estimated_cost)) * 0.12
    s_prio = round(
        max(
            0.0,
            min(
                1.0,
                (s_conn * 0.48) + (s_rel * 0.36) + (max(0.0, min(1.0, user_focus)) * 0.16) - cost_penalty,
            ),
        ),
        4,
    )
    queue_priority = max(20, min(95, int(round(95 - (s_prio * 65)))))
    return PriorityComponents(S_conn=s_conn, S_rel=s_rel, S_prio=s_prio, priority=queue_priority)
