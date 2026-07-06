"""Entity registry and mention extraction from cached sections."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.core.config import get_settings
from app.core.provenance import make_provenance
from app.db.models import EntityRegistry, MentionCache, OntologyVersion, SectionClean
from app.ingestion.redirects import RedirectResolver
from app.ontology.constants import (
    DEFAULT_SCORE_WEIGHTS,
    DOMAINS,
    ENTITY_TYPES,
    ONTOLOGY_VERSION,
    PRECISION_LEVELS,
    TEMPORAL_ROLES,
)
from app.seeds.gliner_decoder_seed import extract_gliner_decoder_entities
from app.seeds.gliner2_seed import extract_gliner2_entities
from app.seeds.spacy_seed import SeedEntity, extract_seed_entities

settings = get_settings()


@dataclass(frozen=True)
class MentionCandidate:
    """One resolved mention before persistence."""

    entity_id: str
    canonical_title_id: int | None
    canonical_title: str | None
    surface: str
    primary_type: str
    primary_domain: str
    char_start: int
    char_end: int
    confidence: float
    source: str


class EntityMentionService:
    """Populate ontology entity and mention caches from section text/links."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.redirects = RedirectResolver(session)

    async def enrich_article(
        self,
        sections: list[SectionClean],
        *,
        precision: bool = False,
        precision_section_limit: int | None = None,
    ) -> dict[str, Any]:
        """Extract and persist ontology mentions for cached content sections."""

        await self.ensure_ontology_version()
        section_count = 0
        mention_count = 0
        entity_count = 0
        source_counts: dict[str, int] = {}
        precision_limit = (
            settings.gliner_decoder_section_limit
            if precision_section_limit is None
            else max(0, int(precision_section_limit))
        )
        for section in sections:
            if not is_content_section(section):
                continue
            candidates = await self._section_candidates(
                section,
                use_precision_extractor=precision and section_count < precision_limit,
            )
            await self._replace_section_mentions(section, candidates)
            section_count += 1
            mention_count += len(candidates)
            entity_count += len({candidate.entity_id for candidate in candidates})
            for candidate in candidates:
                source_counts[candidate.source] = source_counts.get(candidate.source, 0) + 1
        return {
            "sections": section_count,
            "mentions": mention_count,
            "entities": entity_count,
            "source_counts": source_counts,
        }

    async def ensure_ontology_version(self) -> None:
        """Upsert the active ontology version row."""

        stmt = insert(OntologyVersion).values(
            version_key=ONTOLOGY_VERSION,
            status="active",
            categories_json=ENTITY_TYPES,
            domains_json=DOMAINS,
            temporal_roles_json=TEMPORAL_ROLES,
            precision_levels_json=PRECISION_LEVELS,
            weights_json=DEFAULT_SCORE_WEIGHTS,
            horizons_json={
                "day_month": "1 year",
                "year": "10 years",
                "decade": "50 years",
                "century": "300 years",
                "millennium_era": "2000 years",
                "geological": "1e6 years",
            },
            gates_json={
                "graph_hops": 2,
                "temporal_gate": "overlap_or_one_horizon",
                "threshold": "mid",
            },
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_ontology_version_key",
            set_={
                "status": stmt.excluded.status,
                "categories_json": stmt.excluded.categories_json,
                "domains_json": stmt.excluded.domains_json,
                "temporal_roles_json": stmt.excluded.temporal_roles_json,
                "precision_levels_json": stmt.excluded.precision_levels_json,
                "weights_json": stmt.excluded.weights_json,
                "horizons_json": stmt.excluded.horizons_json,
                "gates_json": stmt.excluded.gates_json,
            },
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def _section_candidates(
        self,
        section: SectionClean,
        *,
        use_precision_extractor: bool = False,
    ) -> list[MentionCandidate]:
        candidates: list[MentionCandidate] = []
        seen: set[tuple[int, int, str]] = set()

        for link in section.links_json or []:
            target = str(link.get("target") or "").strip()
            if not target:
                continue
            resolved = await self.redirects.resolve_title(target)
            if resolved is None:
                continue
            title, title_id = resolved
            surface = str(link.get("label") or target or title).strip()
            char_start = _coerce_int(link.get("char_start"))
            char_end = _coerce_int(link.get("char_end"))
            char_start, char_end = _span_or_find(section.clean_text, surface, char_start, char_end)
            candidate = MentionCandidate(
                entity_id=wiki_entity_id(title_id),
                canonical_title_id=title_id,
                canonical_title=title,
                surface=surface or title,
                primary_type="CONCEPT",
                primary_domain="Society & People",
                char_start=char_start,
                char_end=char_end,
                confidence=0.9,
                source="wiki_link",
            )
            key = (candidate.char_start, candidate.char_end, candidate.entity_id)
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)

        seed_entities, _warnings = extract_seed_entities(section.clean_text)
        for entity in seed_entities:
            primary_type = spacy_label_to_type(entity.label)
            normalized = normalize_surface(entity.text)
            if not normalized:
                continue
            candidate = MentionCandidate(
                entity_id=surface_entity_id(primary_type, normalized),
                canonical_title_id=None,
                canonical_title=None,
                surface=entity.text,
                primary_type=primary_type,
                primary_domain=domain_for_type(primary_type),
                char_start=max(entity.char_start, 0),
                char_end=max(entity.char_end, entity.char_start),
                confidence=entity.confidence,
                source="spacy_seed",
            )
            key = (candidate.char_start, candidate.char_end, candidate.entity_id)
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)

        if settings.gliner2_enabled:
            gliner_entities, _warnings = extract_gliner2_entities(
                section.clean_text,
                model_name=settings.gliner2_model,
                threshold=settings.gliner2_threshold,
                max_chars=settings.gliner2_max_chars,
            )
            for entity in gliner_entities:
                primary_type = gliner2_label_to_type(entity.label)
                normalized = normalize_surface(entity.text)
                if not normalized:
                    continue
                candidate = MentionCandidate(
                    entity_id=surface_entity_id(primary_type, normalized),
                    canonical_title_id=None,
                    canonical_title=None,
                    surface=entity.text,
                    primary_type=primary_type,
                    primary_domain=domain_for_type(primary_type),
                    char_start=max(entity.char_start, 0),
                    char_end=max(entity.char_end, entity.char_start),
                    confidence=entity.confidence,
                    source="gliner2_seed",
                )
                key = (candidate.char_start, candidate.char_end, candidate.entity_id)
                if key not in seen:
                    seen.add(key)
                    candidates.append(candidate)

        if settings.gliner_decoder_enabled and use_precision_extractor:
            decoder_entities, _warnings = extract_gliner_decoder_entities(
                section.clean_text,
                model_name=settings.gliner_decoder_model,
                threshold=settings.gliner_decoder_threshold,
                max_chars=settings.gliner_decoder_max_chars,
            )
            for entity in decoder_entities:
                primary_type = gliner_label_to_type(entity.label)
                normalized = normalize_surface(entity.text)
                if not normalized:
                    continue
                candidate = MentionCandidate(
                    entity_id=surface_entity_id(primary_type, normalized),
                    canonical_title_id=None,
                    canonical_title=None,
                    surface=entity.text,
                    primary_type=primary_type,
                    primary_domain=domain_for_type(primary_type),
                    char_start=max(entity.char_start, 0),
                    char_end=max(entity.char_end, entity.char_start),
                    confidence=entity.confidence,
                    source="gliner_decoder_seed",
                )
                key = (candidate.char_start, candidate.char_end, candidate.entity_id)
                if key not in seen:
                    seen.add(key)
                    candidates.append(candidate)

        return candidates

    async def _replace_section_mentions(
        self,
        section: SectionClean,
        candidates: list[MentionCandidate],
    ) -> None:
        await self.session.execute(delete(MentionCache).where(MentionCache.section_key == section.section_key))
        ordered_candidates = sorted(
            candidates,
            key=lambda candidate: (candidate.entity_id, candidate.char_start, candidate.char_end, candidate.surface.casefold()),
        )
        for candidate in ordered_candidates:
            await self._upsert_entity(candidate)
            provenance = make_provenance(
                title_id=section.title_id,
                heading_id=section.heading_id,
                char_start=candidate.char_start,
                char_end=candidate.char_end,
                parser_version=settings.parser_version,
                model_version=settings.model_version,
                run_id=f"mention:{uuid.uuid4()}",
            )
            stmt = insert(MentionCache).values(
                section_key=section.section_key,
                title_id=section.title_id,
                heading_id=section.heading_id,
                entity_id=candidate.entity_id,
                surface=candidate.surface,
                char_start=candidate.char_start,
                char_end=candidate.char_end,
                attribution="core",
                salience=mention_salience(section, candidate),
                confidence=candidate.confidence,
                source=candidate.source,
                provenance_json=provenance,
                parser_version=settings.parser_version,
                model_version=settings.model_version,
                ontology_version=ONTOLOGY_VERSION,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_mention_span_entity",
                set_={
                    "surface": stmt.excluded.surface,
                    "attribution": stmt.excluded.attribution,
                    "salience": stmt.excluded.salience,
                    "confidence": stmt.excluded.confidence,
                    "source": stmt.excluded.source,
                    "provenance_json": stmt.excluded.provenance_json,
                    "parser_version": stmt.excluded.parser_version,
                    "model_version": stmt.excluded.model_version,
                    "ontology_version": stmt.excluded.ontology_version,
                },
            )
            await self.session.execute(stmt)
        await self.session.commit()

    async def _upsert_entity(self, candidate: MentionCandidate) -> None:
        aliases: list[str] = [candidate.surface]
        if candidate.canonical_title:
            aliases.append(candidate.canonical_title)
        stmt = insert(EntityRegistry).values(
            entity_id=candidate.entity_id,
            canonical_title_id=candidate.canonical_title_id,
            canonical_title=candidate.canonical_title,
            surface=None if candidate.canonical_title_id is not None else candidate.surface,
            primary_type=candidate.primary_type,
            types_json=[{"type": candidate.primary_type, "weight": 1.0, "source": candidate.source}],
            primary_domain=candidate.primary_domain,
            domains_json=[{"domain": candidate.primary_domain, "weight": 1.0, "source": candidate.source}],
            aliases_json=sorted(set(aliases)),
            document_frequency=0,
            specificity=0.5,
            ontology_version=ONTOLOGY_VERSION,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_entity_registry_entity_id",
            set_={
                "canonical_title_id": stmt.excluded.canonical_title_id,
                "canonical_title": stmt.excluded.canonical_title,
                "surface": stmt.excluded.surface,
                "primary_type": stmt.excluded.primary_type,
                "types_json": stmt.excluded.types_json,
                "primary_domain": stmt.excluded.primary_domain,
                "domains_json": stmt.excluded.domains_json,
                "aliases_json": stmt.excluded.aliases_json,
                "ontology_version": stmt.excluded.ontology_version,
            },
        )
        await self.session.execute(stmt)


def wiki_entity_id(title_id: int) -> str:
    """Return canonical wiki entity id."""

    return f"ent:wiki:{title_id}"


def surface_entity_id(primary_type: str, normalized_surface: str) -> str:
    """Return deterministic fallback surface entity id."""

    return f"ent:surf:{primary_type}:{normalized_surface}"


def normalize_surface(surface: str) -> str:
    """Normalize fallback surface text."""

    normalized = re.sub(r"[\s_]+", " ", (surface or "").strip().casefold())
    normalized = re.sub(r"[^\w\s:-]", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip().replace(" ", "_")


def spacy_label_to_type(label: str) -> str:
    """Map spaCy labels into ontology top-level types."""

    if label == "PERSON":
        return "PERSON"
    if label == "ORG":
        return "ORG"
    if label in {"GPE", "LOC"}:
        return "PLACE"
    if label == "EVENT":
        return "EVENT"
    if label == "WORK_OF_ART":
        return "WORK"
    if label == "DATE":
        return "TIME"
    return "CONCEPT"


def gliner2_label_to_type(label: str) -> str:
    """Map GLiNER2 schema labels into ontology top-level types."""

    return gliner_label_to_type(label)


def gliner_label_to_type(label: str) -> str:
    """Map GLiNER schema labels into ontology top-level types."""

    normalized = (label or "").casefold().strip()
    if "person" in normalized:
        return "PERSON"
    if "organization" in normalized:
        return "ORG"
    if "location" in normalized or "geopolitical" in normalized or "place" in normalized:
        return "PLACE"
    if "event" in normalized:
        return "EVENT"
    if "work" in normalized or "book" in normalized:
        return "WORK"
    if "date" in normalized or "time" in normalized or "period" in normalized:
        return "TIME"
    if "group" in normalized or "religion" in normalized or "caste" in normalized or "ethnic" in normalized:
        return "GROUP"
    if "law" in normalized or "policy" in normalized:
        return "CONCEPT"
    return "CONCEPT"


def domain_for_type(primary_type: str) -> str:
    """Return a conservative default domain for an ontology type."""

    if primary_type == "WORK":
        return "Arts & Culture"
    if primary_type == "TIME":
        return "Society & People"
    if primary_type == "PLACE":
        return "Exploration & Geography"
    if primary_type in {"PERSON", "GROUP"}:
        return "Society & People"
    if primary_type == "ORG":
        return "Politics & Government"
    if primary_type == "EVENT":
        return "Politics & Government"
    return "Society & People"


def mention_salience(section: SectionClean, candidate: MentionCandidate) -> float:
    """Compute a simple mention salience score for Phase 2."""

    surface = candidate.surface.casefold()
    title = (section.title or "").casefold()
    heading = (section.heading or "").casefold()
    if surface and (surface in title or surface in heading):
        return 0.95
    if candidate.char_start <= 300:
        return 0.75
    if candidate.source == "wiki_link":
        return 0.65
    return 0.50


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _span_or_find(
    text: str,
    surface: str,
    char_start: int | None,
    char_end: int | None,
) -> tuple[int, int]:
    if char_start is not None and char_end is not None and 0 <= char_start <= char_end:
        return char_start, char_end
    index = (text or "").casefold().find((surface or "").casefold())
    if index >= 0:
        return index, index + len(surface)
    return 0, max(len(surface), 0)
