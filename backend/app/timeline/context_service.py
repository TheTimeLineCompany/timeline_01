"""Lazy L1/L2 timeline context orchestration."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.core.config import get_settings
from app.db.models import AgentJob, RelatedCache, SectionClean, SectionTime, TimeDimension, TimelineContextCache
from app.ingestion.section_cache import SectionCacheService
from app.related.service import RelatedInfoService
from app.seeds.service import SeedService
from app.workers.temporal_agent import JOB_TYPE as TEMPORAL_JOB_TYPE
from app.workers.temporal_agent import SOURCE as AGENT_TEMPORAL_SOURCE
from app.workers.temporal_agent import enqueue_temporal_jobs

settings = get_settings()
MODEL_VERSION = "timeline-context-v1"


@dataclass(frozen=True)
class TimelineContextBuildResult:
    """Summary for one lazy context build pass."""

    rows_upserted: int
    temporal_jobs_enqueued: int
    pending: bool


class TimelineContextService:
    """Build article timeline context rows from related candidates and temporal facts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.related = RelatedInfoService(session)

    async def build_for_article(
        self,
        sections: list[SectionClean],
        *,
        section_limit: int = 8,
        related_limit: int = 40,
        min_score: float = 0.38,
    ) -> TimelineContextBuildResult:
        """Populate context cache opportunistically without blocking on LLM calls."""

        all_useful_sections = [section for section in sections if is_content_section(section)]
        useful_sections = all_useful_sections[:section_limit] if section_limit else all_useful_sections
        rows_upserted = 0
        jobs_enqueued = 0
        pending = False

        for section in useful_sections:
            await self.session.execute(
                delete(TimelineContextCache).where(TimelineContextCache.from_section_key == section.section_key)
            )
            await self.session.commit()
            related_rows = await self.related.read_cached_related(section.section_key, related_limit)
            if not related_rows:
                pending = True
                continue
            strong_rows = [
                row
                for row in related_rows
                if row.score >= min_score or self._is_promotable_related_row(row)
            ]
            for related_row in strong_rows:
                if self._is_self_context(from_title_id=section.title_id, source_title_id=related_row.to_title_id):
                    continue
                if not self._is_promotable_related_row(related_row):
                    continue
                candidate_sections = await self._candidate_sections(related_row.to_title, related_row.to_title_id)
                if not candidate_sections:
                    pending = True
                    continue

                await self._ensure_cpu_temporal(candidate_sections[:6])

                temporal_states = {
                    candidate.section_key: await self._temporal_processing_state(candidate.section_key)
                    for candidate in candidate_sections[:6]
                }
                if any(state == "in_progress" for state in temporal_states.values()):
                    pending = True
                missing_temporal = [
                    candidate
                    for candidate in candidate_sections[:6]
                    if temporal_states.get(candidate.section_key) in {"missing", "failed"}
                ]
                if missing_temporal:
                    jobs_enqueued += await enqueue_temporal_jobs(
                        self.session,
                        missing_temporal,
                        priority=65 + min(20, related_row.level * 5),
                        force=True,
                    )
                    pending = True

                rows_upserted += await self._upsert_context_rows(section, related_row, candidate_sections[:8])

        return TimelineContextBuildResult(
            rows_upserted=rows_upserted,
            temporal_jobs_enqueued=jobs_enqueued,
            pending=pending,
        )

    async def _candidate_sections(self, title: str, title_id: int) -> list[SectionClean]:
        result = await self.session.execute(
            select(SectionClean)
            .where(SectionClean.title_id == title_id)
            .order_by(SectionClean.heading_id.asc())
            .limit(20)
        )
        sections = [section for section in result.scalars().all() if is_content_section(section)]
        if sections:
            return sections

        article = await self.related.adapter.get_article_by_title_id(title, title_id)
        if article is None:
            return []
        cached = await SectionCacheService(self.session).cache_article(article)
        return [section for section in cached[:20] if is_content_section(section)]

    async def _has_agent_temporal(self, section_key: str) -> bool:
        result = await self.session.execute(
            select(func.count())
            .select_from(SectionTime)
            .where(SectionTime.section_key == section_key)
            .where(SectionTime.source == AGENT_TEMPORAL_SOURCE)
        )
        return int(result.scalar_one() or 0) > 0

    async def _has_any_temporal(self, section_key: str) -> bool:
        result = await self.session.execute(
            select(func.count())
            .select_from(SectionTime)
            .where(SectionTime.section_key == section_key)
        )
        return int(result.scalar_one() or 0) > 0

    async def _ensure_cpu_temporal(self, sections: list[SectionClean]) -> None:
        seed_service = SeedService(self.session)
        for section in sections:
            if not await self._has_any_temporal(section.section_key):
                await seed_service.enrich_temporal_only(section)

    async def _temporal_processing_state(self, section_key: str) -> str:
        """Return temporal processing state for a candidate section."""

        if await self._has_any_temporal(section_key):
            return "processed"
        if await self._has_agent_temporal(section_key):
            return "processed"
        result = await self.session.execute(
            select(AgentJob.status)
            .where(AgentJob.job_type == TEMPORAL_JOB_TYPE)
            .where(AgentJob.section_key == section_key)
            .order_by(AgentJob.updated_at.desc().nullslast(), AgentJob.id.desc())
            .limit(1)
        )
        status = result.scalar_one_or_none()
        if status == "succeeded":
            return "processed"
        if status in {"pending", "running", "retry"}:
            return "in_progress"
        if status == "failed":
            return "failed"
        return "missing"

    async def _upsert_context_rows(
        self,
        from_section: SectionClean,
        related_row: RelatedCache,
        candidate_sections: list[SectionClean],
    ) -> int:
        if not candidate_sections:
            return 0

        section_by_key = {section.section_key: section for section in candidate_sections}
        source_times = await self._section_time_dimensions(from_section.section_key)
        result = await self.session.execute(
            select(SectionTime, TimeDimension)
            .join(TimeDimension, TimeDimension.time_ref_id == SectionTime.time_ref_id)
            .where(SectionTime.section_key.in_(section_by_key.keys()))
            .order_by(SectionTime.confidence.desc(), SectionTime.created_at.desc())
            .limit(18)
        )
        section_times = list(result.all())
        if not section_times:
            return 0

        rows = []
        for section_time, time_dim in section_times:
            source_section = section_by_key.get(section_time.section_key)
            if source_section is None or not is_content_section(source_section):
                continue
            if self._is_self_context(from_title_id=from_section.title_id, source_title_id=source_section.title_id):
                continue
            passage_gate = self._passage_context_gate(from_section, source_section, related_row)
            if not passage_gate["accepted"]:
                continue
            temporal_gate = self._temporal_context_gate(source_times, time_dim, related_row)
            if not temporal_gate["accepted"]:
                continue
            score = self._timeline_relevance_score(related_row, section_time)
            if score < 0.34:
                continue
            rows.append(
                {
                    "from_title_id": from_section.title_id,
                    "from_section_key": from_section.section_key,
                    "source_title_id": source_section.title_id,
                    "source_title": source_section.title,
                    "source_heading_id": source_section.heading_id,
                    "source_heading": source_section.heading,
                    "source_section_key": source_section.section_key,
                    "time_ref_id": section_time.time_ref_id,
                    "level": related_row.level,
                    "track": "context",
                    "relevance_score": score,
                    "signals_json": {
                        "related_score": related_row.score,
                        "temporal_confidence": section_time.confidence,
                        "temporal_context_gate": temporal_gate,
                        "passage_context_gate": passage_gate,
                        "related_signals": related_row.signals_json or {},
                        "why_source": related_row.why_source,
                        "attribution": self._section_attribution(),
                    },
                    "provenance_json": {
                        "from_section_key": from_section.section_key,
                        "related_cache_id": related_row.id,
                        "section_time_id": section_time.id,
                        "attribution_level": self._section_attribution()["level"],
                        "entity_attribution_status": self._section_attribution()["status"],
                    },
                    "model_version": MODEL_VERSION,
                }
            )

        if not rows:
            return 0

        stmt = insert(TimelineContextCache).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_v4_timeline_context",
            set_={
                "source_title": stmt.excluded.source_title,
                "source_heading_id": stmt.excluded.source_heading_id,
                "source_heading": stmt.excluded.source_heading,
                "track": stmt.excluded.track,
                "relevance_score": stmt.excluded.relevance_score,
                "signals_json": stmt.excluded.signals_json,
                "provenance_json": stmt.excluded.provenance_json,
                "model_version": stmt.excluded.model_version,
                "updated_at": func.now(),
            },
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount or 0

    async def _section_time_dimensions(self, section_key: str) -> list[TimeDimension]:
        result = await self.session.execute(
            select(TimeDimension)
            .join(SectionTime, SectionTime.time_ref_id == TimeDimension.time_ref_id)
            .where(SectionTime.section_key == section_key)
        )
        return list(result.scalars().all())

    @staticmethod
    def _timeline_relevance_score(related_row: RelatedCache, section_time: SectionTime) -> float:
        score = related_row.score * 0.78 + section_time.confidence * 0.22
        if section_time.source == AGENT_TEMPORAL_SOURCE:
            score += 0.08
        if related_row.why_source == "agent_related_v1":
            score += 0.16
        if related_row.level == 1:
            score += 0.04
        return round(min(score, 1.0), 4)

    @staticmethod
    def _section_attribution() -> dict[str, Any]:
        """Return the current fallback attribution object for L1/L2 context rows."""

        return {
            "level": "section",
            "status": "section_attributed_unreviewed",
            "focus_topic_assertion": False,
            "reviewed": False,
        }

    @classmethod
    def _temporal_context_gate(
        cls,
        source_times: list[TimeDimension],
        candidate_time: TimeDimension,
        related_row: RelatedCache,
    ) -> dict[str, Any]:
        """Return whether a candidate time belongs on this source section timeline."""

        candidate_center = cls._time_center(candidate_time)
        source_centers = [
            center for center in (cls._time_center(source_time) for source_time in source_times) if center is not None
        ]
        if not source_centers or candidate_center is None:
            row_score = float(related_row.score or 0.0)
            accepted = (
                (related_row.why_source == "agent_related_v1" and row_score >= 0.55)
                or (related_row.level <= 1 and row_score >= 0.54)
                or (related_row.level > 1 and row_score >= 0.72)
            )
            return {
                "accepted": accepted,
                "reason": "strong_context_without_source_time" if accepted else "missing_source_or_candidate_time",
                "min_year_distance": None,
            }

        min_distance = min(abs(candidate_center - source_center) for source_center in source_centers)
        if min_distance <= 50:
            return {"accepted": True, "reason": "near_same_period", "min_year_distance": round(min_distance, 2)}
        if min_distance <= 300 and (related_row.level == 1 or related_row.why_source == "agent_related_v1"):
            return {"accepted": True, "reason": "adjacent_period", "min_year_distance": round(min_distance, 2)}
        if min_distance <= 1000 and related_row.why_source == "agent_related_v1" and float(related_row.score) >= 0.62:
            return {"accepted": True, "reason": "agent_long_range_bridge", "min_year_distance": round(min_distance, 2)}
        return {"accepted": False, "reason": "temporal_distance_too_large", "min_year_distance": round(min_distance, 2)}

    @classmethod
    def _passage_context_gate(
        cls,
        from_section: SectionClean,
        candidate_section: SectionClean,
        related_row: RelatedCache,
    ) -> dict[str, Any]:
        """Return whether this candidate passage is specifically about the source context."""

        candidate_text = " ".join(
            [
                candidate_section.title or "",
                candidate_section.heading or "",
                (candidate_section.clean_text or "")[:1400],
            ]
        ).casefold()
        source_title_tokens = cls._important_tokens(from_section.title)
        source_heading_tokens = cls._important_tokens(from_section.heading)
        related_title_tokens = cls._important_tokens(related_row.to_title)
        source_mentions = sorted(token for token in source_title_tokens if token in candidate_text)
        heading_mentions = sorted(token for token in source_heading_tokens if token in candidate_text)

        if source_mentions:
            return {"accepted": True, "reason": "candidate_mentions_source_title", "matches": source_mentions[:6]}
        if len(heading_mentions) >= 2:
            return {"accepted": True, "reason": "candidate_mentions_source_heading", "matches": heading_mentions[:6]}

        signals = related_row.signals_json or {}
        agent_signal = signals.get("agent_related_v1") or {}
        reasoning_tags = {str(tag).casefold() for tag in agent_signal.get("reasoning_tags") or []}
        bridge_tags = {
            "historical_context",
            "temporal_context",
            "entity_overlap",
            "topic_overlap",
            "causal_context",
            "same_event",
            "same_place",
        }
        if (
            related_row.why_source == "agent_related_v1"
            and float(agent_signal.get("confidence") or 0.0) >= (0.82 if related_row.level <= 1 else 0.9)
            and bool(reasoning_tags & bridge_tags)
            and float(related_row.score) >= 0.34
        ):
            return {"accepted": True, "reason": "strong_agent_bridge", "matches": sorted(reasoning_tags)[:6]}

        if related_row.level == 1 and float(related_row.score) >= 0.62:
            related_mentions = sorted(token for token in related_title_tokens if token in (from_section.clean_text or "").casefold())
            if related_mentions:
                return {"accepted": True, "reason": "strong_direct_topic_bridge", "matches": related_mentions[:6]}

        return {"accepted": False, "reason": "candidate_passage_not_source_specific", "matches": []}

    @staticmethod
    def _time_center(time_dim: TimeDimension) -> float | None:
        if time_dim.year is not None:
            return float(time_dim.year)
        parsed = _year_from_date(time_dim.start_date)
        if parsed is not None:
            return float(parsed)
        parsed = _year_from_date(time_dim.end_date)
        if parsed is not None:
            return float(parsed)
        return None

    @staticmethod
    def _important_tokens(text: str | None) -> set[str]:
        stopwords = {
            "about",
            "after",
            "also",
            "and",
            "article",
            "before",
            "between",
            "from",
            "history",
            "into",
            "life",
            "the",
            "this",
            "united",
            "with",
            "states",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9]{4,}", (text or "").casefold())
            if token not in stopwords
        }

    @staticmethod
    def _is_self_context(*, from_title_id: int, source_title_id: int) -> bool:
        """Return whether a context row points back to the main article."""

        return source_title_id == from_title_id

    @staticmethod
    def _is_promotable_related_row(related_row: RelatedCache) -> bool:
        """Return whether a related row is strong enough to become timeline context."""

        gates = (getattr(related_row, "signals_json", None) or {}).get("gates") or {}
        if "timeline_eligible" in gates:
            return bool(gates.get("timeline_eligible"))
        if related_row.level <= 1:
            return True
        return related_row.why_source == "agent_related_v1" or related_row.score >= 0.72


def _year_from_date(value: str | None) -> int | None:
    if not value:
        return None
    match = re.match(r"^\s*(-?\d{1,6})", str(value))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None
