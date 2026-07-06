"""Projection from current temporal caches into ontology temporal tables."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FactCache, FactTime, MentionCache, SectionClean, SectionTime, TimeAnchorRegistry, TimeDimension
from app.ontology.constants import ONTOLOGY_VERSION

PRECISION_SCORES = {
    "instant": 1.0,
    "day": 0.95,
    "month": 0.86,
    "season": 0.80,
    "year": 0.72,
    "decade": 0.60,
    "century": 0.48,
    "millennium": 0.35,
    "era": 0.25,
    "geological": 0.15,
    "fuzzy": 0.20,
}


class TemporalProjectionService:
    """Mirror existing V4 temporal rows into the ontology temporal substrate."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def project_sections(self, sections: list[SectionClean]) -> dict[str, int]:
        """Project time anchors and section-level fact-time roles for sections."""

        section_keys = [section.section_key for section in sections]
        if not section_keys:
            return {"time_anchors": 0, "fact_cache": 0, "fact_times": 0}

        section_by_key = {section.section_key: section for section in sections}
        primary_entities = await self._primary_entities(section_keys)
        result = await self.session.execute(
            select(SectionTime, TimeDimension)
            .join(TimeDimension, TimeDimension.time_ref_id == SectionTime.time_ref_id)
            .where(SectionTime.section_key.in_(section_keys))
        )
        rows = result.all()
        time_ids: set[str] = set()
        for section_time, time_dimension in rows:
            await self._upsert_time_anchor(time_dimension)
            await self._upsert_fact_cache(
                section_time,
                time_dimension,
                section_by_key.get(section_time.section_key),
                primary_entities.get(section_time.section_key),
            )
            await self._upsert_fact_time(section_time)
            time_ids.add(time_dimension.time_ref_id)
        await self.session.commit()
        return {"time_anchors": len(time_ids), "fact_cache": len(rows), "fact_times": len(rows)}

    async def _upsert_time_anchor(self, time_dimension: TimeDimension) -> None:
        t_start = time_scalar(time_dimension.start_date, time_dimension.year)
        t_end = time_scalar(time_dimension.end_date, time_dimension.year)
        if t_end is None:
            t_end = t_start
        center = None
        spread = None
        if t_start is not None and t_end is not None:
            center = (t_start + t_end) / 2.0
            spread = abs(t_end - t_start) / 2.0

        precision = time_dimension.precision or time_dimension.time_kind or "fuzzy"
        stmt = insert(TimeAnchorRegistry).values(
            time_id=time_dimension.time_ref_id,
            kind=time_dimension.time_kind,
            precision=precision,
            calendar="gregorian",
            label=time_dimension.label,
            t_start=t_start,
            t_end=t_end,
            open_start=False,
            open_end=False,
            center=center,
            spread=spread,
            confidence=0.8,
            precision_score=precision_score(precision),
            metadata_json=time_dimension.metadata_json or {},
            ontology_version=ONTOLOGY_VERSION,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_time_anchor_registry_time_id",
            set_={
                "kind": stmt.excluded.kind,
                "precision": stmt.excluded.precision,
                "calendar": stmt.excluded.calendar,
                "label": stmt.excluded.label,
                "t_start": stmt.excluded.t_start,
                "t_end": stmt.excluded.t_end,
                "open_start": stmt.excluded.open_start,
                "open_end": stmt.excluded.open_end,
                "center": stmt.excluded.center,
                "spread": stmt.excluded.spread,
                "confidence": stmt.excluded.confidence,
                "precision_score": stmt.excluded.precision_score,
                "metadata_json": stmt.excluded.metadata_json,
                "ontology_version": stmt.excluded.ontology_version,
            },
        )
        await self.session.execute(stmt)

    async def _upsert_fact_time(self, section_time: SectionTime) -> None:
        stmt = insert(FactTime).values(
            fact_id=section_fact_id(section_time.section_key, section_time.time_ref_id),
            section_key=section_time.section_key,
            title_id=section_time.title_id,
            heading_id=section_time.heading_id,
            time_id=section_time.time_ref_id,
            role="occurred",
            confidence=section_time.confidence,
            source=section_time.source,
            provenance_json=section_time.provenance_json or {},
            ontology_version=ONTOLOGY_VERSION,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_fact_time_role",
            set_={
                "section_key": stmt.excluded.section_key,
                "title_id": stmt.excluded.title_id,
                "heading_id": stmt.excluded.heading_id,
                "confidence": stmt.excluded.confidence,
                "source": stmt.excluded.source,
                "provenance_json": stmt.excluded.provenance_json,
                "ontology_version": stmt.excluded.ontology_version,
            },
        )
        await self.session.execute(stmt)

    async def _upsert_fact_cache(
        self,
        section_time: SectionTime,
        time_dimension: TimeDimension,
        section: SectionClean | None,
        primary_entity_id: str | None,
    ) -> None:
        fact_id = section_fact_id(section_time.section_key, section_time.time_ref_id)
        provenance = dict(section_time.provenance_json or {})
        attribution_status = "entity_linked_unreviewed" if primary_entity_id else "section_attributed_unreviewed"
        provenance["entity_attribution_status"] = attribution_status
        provenance["attribution_reviewed"] = False
        assertion_kind = (
            "entity_linked_temporal_unreviewed"
            if primary_entity_id
            else "section_temporal_unreviewed"
        )
        assertion_text = _assertion_text(section, time_dimension, provenance)
        stmt = insert(FactCache).values(
            fact_id=fact_id,
            section_key=section_time.section_key,
            title_id=section_time.title_id,
            heading_id=section_time.heading_id,
            primary_entity_id=primary_entity_id,
            other_entity_ids_json=[],
            assertion_kind=assertion_kind,
            assertion_text=assertion_text,
            confidence=min(float(section_time.confidence or 0.0), 0.68),
            provenance_json=provenance,
            parser_version=str(provenance.get("parser_version") or "v4"),
            model_version=str(provenance.get("model_version") or "temporal_projection_v1"),
            ontology_version=ONTOLOGY_VERSION,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_fact_cache_fact_id",
            set_={
                "primary_entity_id": stmt.excluded.primary_entity_id,
                "assertion_kind": stmt.excluded.assertion_kind,
                "assertion_text": stmt.excluded.assertion_text,
                "confidence": stmt.excluded.confidence,
                "provenance_json": stmt.excluded.provenance_json,
                "parser_version": stmt.excluded.parser_version,
                "model_version": stmt.excluded.model_version,
                "ontology_version": stmt.excluded.ontology_version,
            },
        )
        await self.session.execute(stmt)

    async def _primary_entities(self, section_keys: list[str]) -> dict[str, str]:
        result = await self.session.execute(
            select(MentionCache)
            .where(MentionCache.section_key.in_(section_keys))
            .order_by(
                MentionCache.section_key.asc(),
                MentionCache.salience.desc(),
                MentionCache.confidence.desc(),
                MentionCache.char_start.asc(),
            )
        )
        output: dict[str, str] = {}
        for mention in result.scalars().all():
            output.setdefault(mention.section_key, mention.entity_id)
        return output


def section_fact_id(section_key: str, time_ref_id: str) -> str:
    """Return deterministic section-level temporal fact id."""

    return f"section-time:{section_key}:{time_ref_id}"


def _assertion_text(section: SectionClean | None, time_dimension: TimeDimension, provenance: dict) -> str:
    label = time_dimension.label or time_dimension.time_ref_id
    if section is None:
        return f"{label} is referenced by a cached section."
    char_start = _safe_int(provenance.get("char_start"))
    char_end = _safe_int(provenance.get("char_end"))
    if char_start is not None and char_end is not None and char_end > char_start:
        excerpt = " ".join((section.clean_text or "")[char_start:char_end].split())
        if excerpt:
            return excerpt[:1000]
    heading = section.heading or "section"
    return f"{label} is referenced in {section.title} / {heading}."


def _safe_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def precision_score(precision: str | None) -> float:
    """Return a stable precision score for temporal anchors."""

    return PRECISION_SCORES.get((precision or "fuzzy").lower(), PRECISION_SCORES["fuzzy"])


def time_scalar(date_text: str | None, fallback_year: int | None) -> float | None:
    """Convert a stored date/year into a sortable scalar year value."""

    if date_text:
        parsed = parse_isoish_date(date_text)
        if parsed is not None:
            year, month, day = parsed
            return year + ((month - 1) / 12.0) + ((day - 1) / 366.0)
    if fallback_year is not None:
        return float(fallback_year)
    return None


def parse_isoish_date(date_text: str) -> tuple[int, int, int] | None:
    """Parse YYYY, YYYY-MM, or YYYY-MM-DD into numeric parts."""

    parts = [part for part in date_text.strip().split("-") if part]
    try:
        if len(parts) == 1:
            return int(parts[0]), 1, 1
        if len(parts) == 2:
            return int(parts[0]), int(parts[1]), 1
        if len(parts) >= 3:
            parsed = date.fromisoformat("-".join(parts[:3]))
            return parsed.year, parsed.month, parsed.day
    except ValueError:
        return None
    return None
