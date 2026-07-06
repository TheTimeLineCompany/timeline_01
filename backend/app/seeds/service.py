"""CPU seed enrichment service."""

from __future__ import annotations

import uuid

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.core.config import get_settings
from app.core.provenance import make_provenance
from app.db.models import FactTime, SectionClean, SectionTag, SectionTime, TimeDimension
from app.ontology.temporal_projection import TemporalProjectionService
from app.seeds.spacy_seed import extract_seed_entities
from app.seeds.temporal import TemporalMatch, normalize_temporal_mentions

settings = get_settings()


class SeedService:
    """Run and store CPU seed enrichments for one section."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enrich_section(self, section: SectionClean) -> dict[str, object]:
        """Run spaCy/date seeds and persist cache rows."""

        if not is_content_section(section):
            await self._clear_section_enrichment(section)
            await self.session.commit()
            return {
                "section_key": section.section_key,
                "spacy_entities": 0,
                "temporal_matches": 0,
                "warnings": ["non_content_section_skipped"],
            }

        entities, warnings = extract_seed_entities(section.clean_text)
        temporal_matches = normalize_temporal_mentions(section.clean_text)

        await self.session.execute(
            delete(SectionTag).where(
                SectionTag.section_key == section.section_key,
                SectionTag.source == "spacy_seed",
            )
        )

        for entity in entities:
            provenance = make_provenance(
                title_id=section.title_id,
                heading_id=section.heading_id,
                char_start=entity.char_start,
                char_end=entity.char_end,
                parser_version=settings.parser_version,
                model_version=settings.model_version,
                run_id=f"seed:{uuid.uuid4()}",
            )
            self.session.add(
                SectionTag(
                    section_key=section.section_key,
                    title_id=section.title_id,
                    heading_id=section.heading_id,
                    tag_text=entity.text,
                    tag_type=_map_spacy_label(entity.label),
                    tag_subtype=entity.label,
                    source="spacy_seed",
                    confidence=entity.confidence,
                    char_start=entity.char_start,
                    char_end=entity.char_end,
                    provenance_json=provenance,
                    model_version=settings.model_version,
                )
            )

        await self._upsert_times(section, temporal_matches)
        await TemporalProjectionService(self.session).project_sections([section])
        await self.session.commit()

        return {
            "section_key": section.section_key,
            "spacy_entities": len(entities),
            "temporal_matches": len(temporal_matches),
            "warnings": warnings,
        }

    async def enrich_temporal_only(self, section: SectionClean) -> int:
        """Run only rule-based temporal seeds for a cached section."""

        if not is_content_section(section):
            await self.session.execute(delete(SectionTime).where(SectionTime.section_key == section.section_key))
            await self.session.commit()
            return 0

        temporal_matches = normalize_temporal_mentions(section.clean_text)
        await self._upsert_times(section, temporal_matches)
        await TemporalProjectionService(self.session).project_sections([section])
        await self.session.commit()
        return len(temporal_matches)

    async def _clear_section_enrichment(self, section: SectionClean) -> None:
        await self.session.execute(delete(SectionTag).where(SectionTag.section_key == section.section_key))
        await self.session.execute(delete(SectionTime).where(SectionTime.section_key == section.section_key))
        await self.session.execute(delete(FactTime).where(FactTime.section_key == section.section_key))

    async def _upsert_times(self, section: SectionClean, matches: list[TemporalMatch]) -> None:
        await self.session.execute(delete(SectionTime).where(SectionTime.section_key == section.section_key))
        if not matches:
            return

        payload = []
        for match in matches:
            meta = dict(match.metadata_json or {})
            meta["source_section_key"] = section.section_key
            meta["normalization_method"] = "rule_based_seed"
            payload.append(
                {
                    "time_ref_id": match.time_ref_id,
                    "time_kind": match.time_kind,
                    "label": match.label,
                    "precision": match.precision,
                    "start_date": match.start_date,
                    "end_date": match.end_date,
                    "year": match.year,
                    "month": match.month,
                    "day": match.day,
                    "season": match.season,
                    "era_name": match.era_name,
                    "region_scope": match.region_scope,
                    "metadata_json": meta,
                    "active": True,
                }
            )
        stmt = insert(TimeDimension).values(payload)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_v4_time_ref_id",
            set_={
                "label": stmt.excluded.label,
                "precision": stmt.excluded.precision,
                "start_date": stmt.excluded.start_date,
                "end_date": stmt.excluded.end_date,
                "year": stmt.excluded.year,
                "month": stmt.excluded.month,
                "day": stmt.excluded.day,
                "season": stmt.excluded.season,
                "era_name": stmt.excluded.era_name,
                "region_scope": stmt.excluded.region_scope,
                "metadata_json": stmt.excluded.metadata_json,
                "active": True,
            },
        )
        await self.session.execute(stmt)

        for match in matches:
            provenance = make_provenance(
                title_id=section.title_id,
                heading_id=section.heading_id,
                char_start=0,
                char_end=len(section.clean_text),
                parser_version=settings.parser_version,
                model_version=settings.model_version,
                run_id=f"seed-time:{uuid.uuid4()}",
            )
            self.session.add(
                SectionTime(
                    section_key=section.section_key,
                    title_id=section.title_id,
                    heading_id=section.heading_id,
                    time_ref_id=match.time_ref_id,
                    source="rule_based_seed",
                    confidence=0.8,
                    provenance_json=provenance,
                )
            )


def _map_spacy_label(label: str) -> str:
    if label == "GPE" or label == "LOC":
        return "PLACE"
    if label == "WORK_OF_ART":
        return "WORK"
    if label == "DATE":
        return "TIME"
    if label in {"PERSON", "ORG", "EVENT"}:
        return label
    return "CONCEPT"
