"""Entity passage component scoring."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.provenance import make_provenance
from app.db.models import EntityPassageScore, EntityRegistry, MentionCache, SectionClean
from app.ontology.constants import DEFAULT_SCORE_WEIGHTS, ONTOLOGY_VERSION

settings = get_settings()

TYPE_WEIGHTS = {
    "PERSON": 0.90,
    "GROUP": 0.82,
    "ORG": 0.78,
    "PLACE": 0.72,
    "EVENT": 0.88,
    "CONCEPT": 0.62,
    "WORK": 0.72,
    "OBJECT": 0.58,
    "NATURAL_KIND": 0.60,
    "TIME": 0.50,
}


@dataclass(frozen=True)
class EntityMentionGroup:
    """Mention rows and registry metadata for one entity within one section."""

    entity_id: str
    primary_type: str
    specificity: float
    mentions: list[MentionCache]


class EntityPassageScoreService:
    """Compute per-entity per-section component vectors."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def score_article(self, sections: list[SectionClean]) -> dict[str, int]:
        """Score all entities mentioned in cached article sections."""

        section_count = 0
        score_count = 0
        for section in sections:
            groups = await self._mention_groups(section)
            for group in groups:
                await self._upsert_score(section, group)
                score_count += 1
            if groups:
                section_count += 1
        await self.session.commit()
        return {"sections": section_count, "scores": score_count}

    async def _mention_groups(self, section: SectionClean) -> list[EntityMentionGroup]:
        result = await self.session.execute(
            select(MentionCache, EntityRegistry)
            .join(EntityRegistry, EntityRegistry.entity_id == MentionCache.entity_id)
            .where(
                MentionCache.section_key == section.section_key,
                MentionCache.ontology_version == ONTOLOGY_VERSION,
                EntityRegistry.ontology_version == ONTOLOGY_VERSION,
            )
            .order_by(MentionCache.entity_id, MentionCache.char_start)
        )

        grouped: dict[str, EntityMentionGroup] = {}
        for mention, entity in result.all():
            existing = grouped.get(mention.entity_id)
            if existing is None:
                grouped[mention.entity_id] = EntityMentionGroup(
                    entity_id=mention.entity_id,
                    primary_type=entity.primary_type,
                    specificity=entity.specificity,
                    mentions=[mention],
                )
            else:
                existing.mentions.append(mention)
        return list(grouped.values())

    async def _upsert_score(self, section: SectionClean, group: EntityMentionGroup) -> None:
        components = passage_components(section.clean_text, group)
        blend = blend_components(components)
        first_mention = min(group.mentions, key=lambda mention: mention.char_start)
        provenance = make_provenance(
            title_id=section.title_id,
            heading_id=section.heading_id,
            char_start=first_mention.char_start,
            char_end=first_mention.char_end,
            parser_version=settings.parser_version,
            model_version=settings.model_version,
            run_id=f"entity-score:{uuid.uuid4()}",
        )
        stmt = insert(EntityPassageScore).values(
            entity_id=group.entity_id,
            section_key=section.section_key,
            title_id=section.title_id,
            heading_id=section.heading_id,
            components_json=components,
            blend=blend,
            provenance_json=provenance,
            model_version=settings.model_version,
            ontology_version=ONTOLOGY_VERSION,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_entity_passage_score",
            set_={
                "title_id": stmt.excluded.title_id,
                "heading_id": stmt.excluded.heading_id,
                "components_json": stmt.excluded.components_json,
                "blend": stmt.excluded.blend,
                "provenance_json": stmt.excluded.provenance_json,
                "model_version": stmt.excluded.model_version,
            },
        )
        await self.session.execute(stmt)


def passage_components(text: str, group: EntityMentionGroup) -> dict[str, float]:
    """Build the named component vector for one entity in one section."""

    text_length = max(len(text or ""), 1)
    first_start = min(mention.char_start for mention in group.mentions)
    lead_score = max(0.0, 1.0 - (first_start / text_length))
    mention_count = len(group.mentions)
    frequency_score = min(1.0, mention_count / 4.0)
    centrality = clamp((lead_score * 0.55) + (frequency_score * 0.45))
    salience = clamp(max(mention.salience for mention in group.mentions))
    confidence = clamp(sum(mention.confidence for mention in group.mentions) / mention_count)

    return {
        "type_weight": type_weight(group.primary_type),
        "salience": salience,
        "specificity": clamp(group.specificity),
        "confidence": confidence,
        "centrality": centrality,
        "mention_count": float(mention_count),
        "first_char_start": float(first_start),
    }


def blend_components(components: dict[str, float]) -> float:
    """Blend component vector using the pinned ontology weights."""

    weights = DEFAULT_SCORE_WEIGHTS["entity_passage"]
    return clamp(
        sum(
            components.get(component, 0.0) * weight
            for component, weight in weights.items()
        )
    )


def type_weight(primary_type: str) -> float:
    """Return type prior for entity-passage importance."""

    return TYPE_WEIGHTS.get(primary_type, TYPE_WEIGHTS["CONCEPT"])


def clamp(value: float) -> float:
    """Clamp numeric score to the 0..1 interval."""

    return max(0.0, min(1.0, float(value)))
