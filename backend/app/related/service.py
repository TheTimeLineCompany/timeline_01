"""Cache-first Related Information generation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.content_filters import is_content_section
from app.db.models import (
    ContentRelatednessCache,
    EntityPassageScore,
    EntityRegistry,
    FactTime,
    RelatedCache,
    SectionClean,
    SectionTag,
    SectionTime,
    TimeAnchorRegistry,
)
from app.ingestion.redirects import RedirectResolver
from app.ingestion.section_cache import SectionCacheService
from app.ingestion.wiki_adapter import WikiAdapter
from app.ontology.constants import ONTOLOGY_VERSION
from app.ontology.entity_mentions import EntityMentionService
from app.ontology.passage_scores import EntityPassageScoreService
from app.orchestration.priorities import compute_candidate_priority, embedding_priority
from app.related.component_scoring import (
    SCORING_VERSION,
    EntityScore,
    TimeAnchorScore,
    content_components,
    graph_prior,
    graph_signal,
    normalize_raw_scores,
    relatedness_components,
    temporal_components,
)
from app.related.gates import relatedness_gates
from app.related.gates import GATE_VERSION
from app.seeds.service import SeedService
from app.workers.embeddings import enqueue_embedding_jobs

settings = get_settings()


@dataclass
class RelatedCandidate:
    """One related article candidate."""

    title: str
    title_id: int
    level: int
    via_title: str | None = None
    link_rank: int = 0
    source_article_link_count: int = 0
    bridge_route_score: float = 0.0


class RelatedInfoService:
    """Build and serve cached related information for a section."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.adapter = WikiAdapter(session)
        self.resolver = RedirectResolver(session)

    async def get_related(
        self,
        section: SectionClean,
        *,
        refresh: bool = False,
        limit: int | None = None,
    ) -> list[RelatedCache]:
        """Return cached related rows, building deterministic cache if needed."""

        result_limit = limit or settings.related_return_limit
        if not refresh:
            cached = await self._read_cache(section.section_key, result_limit)
            if cached and self._cache_has_current_scoring(cached) and await self._cache_has_current_signal_state(section, cached):
                return cached

        source_normalized_title = _normalize_article_title(section.title)
        source_link_counts = await self._source_article_link_counts(section.title_id)
        l1_candidates = await self._collect_l1_candidates(section, source_normalized_title, source_link_counts)
        l1_ranked = await self._rank_candidates(section, l1_candidates)
        l2_candidates = await self._collect_l2_candidates(
            section,
            l1_ranked,
            source_normalized_title,
            source_link_counts,
        )
        l2_ranked = await self._rank_candidates(section, l2_candidates) if l2_candidates else []
        ranked = sorted([*l1_ranked, *l2_ranked], key=lambda item: (-item[1], item[0].level, item[0].title))
        await self._write_cache(section, ranked)
        return await self._read_cache(section.section_key, result_limit)

    async def _read_cache(self, section_key: str, limit: int) -> list[RelatedCache]:
        result = await self.session.execute(
            select(RelatedCache)
            .where(RelatedCache.from_section_key == section_key)
            .order_by(RelatedCache.score.desc(), RelatedCache.level.asc(), RelatedCache.to_title.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def read_cached_related(self, section_key: str, limit: int) -> list[RelatedCache]:
        """Return cached related rows without building or refreshing them."""

        return await self._read_cache(section_key, limit)

    @staticmethod
    def _cache_has_current_scoring(rows: list[RelatedCache]) -> bool:
        return all(
            (row.signals_json or {}).get("scoring_version") == SCORING_VERSION
            and ((row.signals_json or {}).get("gates") or {}).get("version") == GATE_VERSION
            for row in rows
        )

    async def _cache_has_current_signal_state(self, section: SectionClean, rows: list[RelatedCache]) -> bool:
        """Return whether cached rows are current with available signal coverage.

        Version checks alone are not enough. A row can have the current scoring
        version but have been scored before embeddings landed. Once the source
        section embedding exists, rows that recorded no embedding signal should
        be treated as stale and rebuilt.
        """

        if not rows:
            return False
        embedding_exists = await self._has_embeddings([section.section_key])
        if not embedding_exists:
            return True
        for row in rows:
            signals = row.signals_json or {}
            embedding_ready = signals.get("embedding_ready") or {}
            if not isinstance(embedding_ready, dict):
                continue
            if embedding_ready.get("source") is False:
                return False
        return True

    async def _collect_candidates(self, section: SectionClean) -> list[RelatedCandidate]:
        source_normalized_title = _normalize_article_title(section.title)
        source_link_counts = await self._source_article_link_counts(section.title_id)
        l1_candidates = await self._collect_l1_candidates(section, source_normalized_title, source_link_counts)
        l1_ranked = [(candidate, 0.0, {}, "") for candidate in self._l1_candidates_for_l2_expansion(l1_candidates)]
        return [
            *l1_candidates,
            *await self._collect_l2_candidates(section, l1_ranked, source_normalized_title, source_link_counts),
        ]

    async def _collect_l1_candidates(
        self,
        section: SectionClean,
        source_normalized_title: str,
        source_link_counts: dict[str, int],
    ) -> list[RelatedCandidate]:
        seen: set[int] = set()
        candidates: list[RelatedCandidate] = []
        l1_links = [str(item.get("target")) for item in (section.links_json or []) if item.get("target")]
        for idx, link_title in enumerate(l1_links):
            resolved = await self.resolver.resolve_title(link_title)
            if resolved is None:
                continue
            title, title_id = resolved
            if self._is_source_article(title, title_id, section.title_id, source_normalized_title) or title_id in seen:
                continue
            seen.add(title_id)
            link_count = max(
                source_link_counts.get(_normalize_article_title(link_title), 0),
                source_link_counts.get(_normalize_article_title(title), 0),
            )
            candidates.append(
                RelatedCandidate(
                    title=title,
                    title_id=title_id,
                    level=1,
                    link_rank=idx,
                    source_article_link_count=link_count,
                )
            )
        return candidates

    async def _collect_l2_candidates(
        self,
        section: SectionClean,
        l1_ranked: list[tuple[RelatedCandidate, float, dict[str, Any], str]],
        source_normalized_title: str,
        source_link_counts: dict[str, int],
    ) -> list[RelatedCandidate]:
        seen = {section.title_id}
        for candidate, _score, _signals, _why in l1_ranked:
            seen.add(candidate.title_id)
        candidates: list[RelatedCandidate] = []
        l2_parent_candidates = self._scored_l1_candidates_for_l2_expansion(l1_ranked)
        for l1, bridge_score in l2_parent_candidates:
            article = await self.adapter.get_article_by_title_id(l1.title, l1.title_id)
            if article is None:
                continue
            for idx, link_title in enumerate(article.all_links[: settings.related_l2_per_l1_limit]):
                resolved = await self.resolver.resolve_title(link_title)
                if resolved is None:
                    continue
                title, title_id = resolved
                if self._is_source_article(title, title_id, section.title_id, source_normalized_title) or title_id in seen:
                    continue
                seen.add(title_id)
                link_count = max(
                    source_link_counts.get(_normalize_article_title(title), 0),
                    source_link_counts.get(_normalize_article_title(l1.title), 0),
                )
                candidates.append(
                    RelatedCandidate(
                        title=title,
                        title_id=title_id,
                        level=2,
                        via_title=l1.title,
                        link_rank=idx,
                        source_article_link_count=link_count,
                        bridge_route_score=bridge_score,
                    )
                )
        return candidates

    @staticmethod
    def _is_source_article(
        candidate_title: str,
        candidate_title_id: int,
        source_title_id: int,
        source_normalized_title: str,
    ) -> bool:
        return (
            candidate_title_id == source_title_id
            or _normalize_article_title(candidate_title) == source_normalized_title
        )

    async def _rank_candidates(
        self,
        section: SectionClean,
        candidates: list[RelatedCandidate],
    ) -> list[tuple[RelatedCandidate, float, dict[str, Any], str]]:
        candidates = self._prioritized_candidate_slice(candidates)
        source_entities = await self._entity_scores_for_sections([section.section_key])
        source_times = await self._time_anchors_for_sections([section.section_key])
        await enqueue_embedding_jobs(self.session, [section], priority=32, force=False)
        source_embedding_ready = await self._wait_for_embeddings(
            [section.section_key],
            timeout_seconds=2.5,
        )
        raw_items: list[tuple[RelatedCandidate, float, dict[str, Any], str]] = []
        for candidate in candidates:
            candidate_sections = await self._candidate_sections(candidate.title, candidate.title_id)
            intro_sections = [item for item in candidate_sections if is_content_section(item)][:1]
            candidate_content_sections = [item for item in candidate_sections if is_content_section(item)]
            intro_keys = [item.section_key for item in intro_sections]
            if intro_sections:
                await self._ensure_candidate_enrichment(candidate, intro_sections, candidate_content_sections)
                await self._wait_for_embeddings(intro_keys, timeout_seconds=0.35)
            candidate_keys = [item.section_key for item in candidate_content_sections]
            candidate_entities = await self._entity_scores_for_sections(candidate_keys)
            candidate_times = await self._time_anchors_for_sections(candidate_keys)
            intro_embedding_similarity = await self._embedding_similarity(
                section.section_key,
                intro_keys,
            )
            broad_embedding_similarity = await self._embedding_similarity(section.section_key, candidate_keys[:20])
            embedding_similarity = _best_similarity(intro_embedding_similarity, broad_embedding_similarity)
            backlink_count = self._candidate_backlink_count(candidate_content_sections, section.title)
            backlink_signal = min(1.0, backlink_count / 3.0)
            graph_component = graph_signal(candidate.level, candidate.link_rank)
            prior_component = graph_prior(candidate.level, candidate.link_rank)
            if candidate.level == 2 and candidate.bridge_route_score > 0:
                bridge_factor = 0.5 + (0.5 * min(1.0, candidate.bridge_route_score))
                graph_component = round(graph_component * bridge_factor, 4)
                prior_component = round(prior_component * bridge_factor, 4)
            content = content_components(
                source_entities,
                candidate_entities,
                graph_signal=graph_component,
                embedding_similarity=embedding_similarity,
                backlink_signal=backlink_signal,
                prior=prior_component,
            )
            temporal = temporal_components(source_times, candidate_times)
            component_object = relatedness_components(content, temporal)
            component_object["l2_bridge_signal"] = round(candidate.bridge_route_score, 4)
            raw_score = float(component_object["raw_score"])
            l2_path_score = raw_score
            if candidate.level == 2 and candidate.bridge_route_score > 0:
                l2_path_score = min(
                    1.0,
                    (raw_score * 0.62)
                    + (candidate.bridge_route_score * 0.28)
                    + (backlink_signal * 0.06)
                    + ((embedding_similarity or 0.0) * 0.04),
                )
            priority_components = compute_candidate_priority(
                level=candidate.level,
                link_rank=candidate.link_rank,
                source_article_link_count=candidate.source_article_link_count,
                intro_similarity=embedding_similarity,
                estimated_cost=0.35 if candidate.level == 1 else 0.55,
            )
            signals = {
                "scoring_version": SCORING_VERSION,
                "level": candidate.level,
                "via_title": candidate.via_title,
                "l2_bridge_signal": round(candidate.bridge_route_score, 4),
                "l2_path_score": round(l2_path_score, 4),
                "components": component_object,
                "source_entity_count": len(source_entities),
                "candidate_entity_count": len(candidate_entities),
                "source_time_count": len(source_times),
                "candidate_time_count": len(candidate_times),
                "shared_entities": shared_entity_ids(source_entities, candidate_entities)[:12],
                "shared_domains": shared_domains(source_entities, candidate_entities)[:8],
                "time_overlap": shared_time_ids(source_times, candidate_times)[:10],
                "embedding_similarity": embedding_similarity,
                "intro_embedding_similarity": intro_embedding_similarity,
                "broad_embedding_similarity": broad_embedding_similarity,
                "embedding_ready": {
                    "source": source_embedding_ready,
                    "candidate_intro": intro_embedding_similarity is not None,
                    "candidate_broad": broad_embedding_similarity is not None,
                    "used": (
                        _embedding_signal_used(intro_embedding_similarity, broad_embedding_similarity)
                    ),
                    "candidate_sections_requested": len(candidate_keys),
                },
                "graph_signal": content["S_graph"],
                "backlink_signal": content["S_backlink"],
                "candidate_backlink_count": backlink_count,
                "prior": content["prior"],
                "source_article_link_count": candidate.source_article_link_count,
                "priority": priority_components.as_dict(),
                "candidate_priority_score": priority_components.S_prio,
            }
            why = self._template_why(candidate, signals)
            raw_items.append((candidate, l2_path_score, signals, why))

        normalized = normalize_raw_scores([item[1] for item in raw_items])
        ranked: list[tuple[RelatedCandidate, float, dict[str, Any], str]] = []
        for (candidate, raw_score, signals, why), score in zip(raw_items, normalized, strict=False):
            signals["raw_score"] = round(raw_score, 4)
            signals["relevance_norm"] = score
            signals["gates"] = relatedness_gates(
                level=candidate.level,
                score=score,
                why_source="template",
                components=signals.get("components") or {},
                source_entity_count=int(signals.get("source_entity_count") or 0),
                candidate_entity_count=int(signals.get("candidate_entity_count") or 0),
                source_time_count=int(signals.get("source_time_count") or 0),
                candidate_time_count=int(signals.get("candidate_time_count") or 0),
            )
            ranked.append((candidate, score, signals, why))
        ranked.sort(key=lambda item: (-item[1], item[0].level, item[0].title))
        return ranked

    @staticmethod
    def _prioritized_candidate_slice(candidates: list[RelatedCandidate]) -> list[RelatedCandidate]:
        """Order expensive related scoring without dropping direct L1 links.

        Direct section links are the strongest deterministic evidence that the
        source section wants the candidate considered. L2 candidates are still
        bounded because they can explode quickly.
        """

        limit = max(1, int(settings.related_rank_candidate_limit))
        l1_candidates = sorted(
            [candidate for candidate in candidates if candidate.level == 1],
            key=lambda candidate: (
                -candidate.source_article_link_count,
                candidate.link_rank,
                candidate.title.casefold(),
            ),
        )
        l2_candidates = sorted(
            [candidate for candidate in candidates if candidate.level != 1],
            key=lambda candidate: (
                -candidate.source_article_link_count,
                candidate.link_rank,
                candidate.title.casefold(),
            ),
        )
        return l1_candidates + l2_candidates[:limit]

    @staticmethod
    def _l1_candidates_for_l2_expansion(candidates: list[RelatedCandidate]) -> list[RelatedCandidate]:
        """Choose bounded L1 parents for L2 expansion.

        This bound affects how far the frontier expands, not whether direct L1
        links are eligible for weighted scoring.
        """

        limit = max(1, int(settings.related_l1_limit))
        return sorted(
            [candidate for candidate in candidates if candidate.level == 1],
            key=lambda candidate: (
                -candidate.source_article_link_count,
                candidate.link_rank,
                candidate.title.casefold(),
            ),
        )[:limit]

    @staticmethod
    def _scored_l1_candidates_for_l2_expansion(
        ranked: list[tuple[RelatedCandidate, float, dict[str, Any], str]]
    ) -> list[tuple[RelatedCandidate, float]]:
        """Choose L2 bridge parents from weighted L1 relevance results."""

        limit = max(1, int(settings.related_l1_limit))
        l1_rows = [
            (candidate, float(score))
            for candidate, score, _signals, _why in ranked
            if candidate.level == 1
        ]
        return sorted(
            l1_rows,
            key=lambda item: (
                -item[1],
                -item[0].source_article_link_count,
                item[0].link_rank,
                item[0].title.casefold(),
            ),
        )[:limit]

    async def _write_cache(
        self,
        section: SectionClean,
        ranked: list[tuple[RelatedCandidate, float, dict[str, Any], str]],
    ) -> None:
        await self.session.execute(delete(RelatedCache).where(RelatedCache.from_section_key == section.section_key))
        for candidate, score, signals, why in ranked:
            await self._write_component_cache(section, candidate, score, signals)
            stmt = insert(RelatedCache).values(
                from_section_key=section.section_key,
                to_title_id=candidate.title_id,
                to_title=candidate.title,
                level=candidate.level,
                score=score,
                signals_json=signals,
                why_text=why,
                why_source="template",
                provenance_json=section.provenance_json,
                parser_version=settings.parser_version,
                model_version=settings.model_version,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_v4_related",
                set_={
                    "to_title": stmt.excluded.to_title,
                    "score": stmt.excluded.score,
                    "signals_json": stmt.excluded.signals_json,
                    "why_text": stmt.excluded.why_text,
                    "why_source": stmt.excluded.why_source,
                    "provenance_json": stmt.excluded.provenance_json,
                    "parser_version": stmt.excluded.parser_version,
                    "model_version": stmt.excluded.model_version,
                },
            )
            await self.session.execute(stmt)
        await self.session.commit()

    async def _write_component_cache(
        self,
        section: SectionClean,
        candidate: RelatedCandidate,
        score: float,
        signals: dict[str, Any],
    ) -> None:
        component_object = signals.get("components") or {}
        stmt = insert(ContentRelatednessCache).values(
            focus_section_key=section.section_key,
            candidate_key=f"article:{candidate.title_id}",
            candidate_title_id=candidate.title_id,
            candidate_section_key=None,
            components_json=component_object,
            raw_score=float(signals.get("raw_score") or component_object.get("raw_score") or score),
            relevance_norm=score,
            why_json={
                "template": self._template_why(candidate, signals),
                "shared_entities": signals.get("shared_entities") or [],
                "shared_domains": signals.get("shared_domains") or [],
                "time_overlap": signals.get("time_overlap") or [],
            },
            gates_json={
                **(signals.get("gates") or {}),
                "scoring_version": SCORING_VERSION,
            },
            provenance_json=section.provenance_json,
            model_version=settings.model_version,
            ontology_version=ONTOLOGY_VERSION,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_content_relatedness_focus_candidate",
            set_={
                "candidate_title_id": stmt.excluded.candidate_title_id,
                "candidate_section_key": stmt.excluded.candidate_section_key,
                "components_json": stmt.excluded.components_json,
                "raw_score": stmt.excluded.raw_score,
                "relevance_norm": stmt.excluded.relevance_norm,
                "why_json": stmt.excluded.why_json,
                "gates_json": stmt.excluded.gates_json,
                "provenance_json": stmt.excluded.provenance_json,
                "model_version": stmt.excluded.model_version,
            },
        )
        await self.session.execute(stmt)

    async def _section_tag_set(self, section_key: str) -> set[str]:
        result = await self.session.execute(
            select(SectionTag.tag_text).where(SectionTag.section_key == section_key)
        )
        return {str(row[0]).strip().lower() for row in result.fetchall() if row[0]}

    async def _tag_set_for_sections(self, section_keys: list[str]) -> set[str]:
        if not section_keys:
            return set()
        result = await self.session.execute(
            select(SectionTag.tag_text).where(SectionTag.section_key.in_(section_keys))
        )
        return {str(row[0]).strip().lower() for row in result.fetchall() if row[0]}

    async def _section_time_set(self, section_key: str) -> set[str]:
        result = await self.session.execute(
            select(SectionTime.time_ref_id).where(SectionTime.section_key == section_key)
        )
        return {str(row[0]) for row in result.fetchall() if row[0]}

    async def _time_set_for_sections(self, section_keys: list[str]) -> set[str]:
        if not section_keys:
            return set()
        result = await self.session.execute(
            select(SectionTime.time_ref_id).where(SectionTime.section_key.in_(section_keys))
        )
        return {str(row[0]) for row in result.fetchall() if row[0]}

    async def _candidate_section_keys(self, title: str, title_id: int) -> list[str]:
        return [section.section_key for section in await self._candidate_sections(title, title_id)]

    async def _candidate_sections(self, title: str, title_id: int) -> list[SectionClean]:
        result = await self.session.execute(
            select(SectionClean)
            .where(SectionClean.title_id == title_id)
            .order_by(SectionClean.heading_id.asc())
            .limit(20)
        )
        sections = list(result.scalars().all())
        if not sections:
            article = await self.adapter.get_article_by_title_id(title, title_id)
            if article is None:
                return []
            sections = await SectionCacheService(self.session).cache_article(article)

        return sections[:20]

    @staticmethod
    def _candidate_backlink_count(candidate_sections: list[SectionClean], source_title: str) -> int:
        """Count candidate article links back to the focused source article."""

        source_normalized = _normalize_article_title(source_title)
        count = 0
        for section in candidate_sections:
            for link in section.links_json or []:
                target = str(link.get("target") or "").strip()
                if target and _normalize_article_title(target) == source_normalized:
                    count += 1
        return count

    async def _ensure_candidate_enrichment(
        self,
        candidate: RelatedCandidate,
        intro_sections: list[SectionClean],
        candidate_sections: list[SectionClean],
    ) -> None:
        """Ensure L1/L2 candidate sections have CPU/embedding work started."""

        entity_mentions = EntityMentionService(self.session)
        entity_scores = EntityPassageScoreService(self.session)
        await entity_mentions.enrich_article(intro_sections)
        await entity_scores.score_article(intro_sections)
        seed_service = SeedService(self.session)
        for section in intro_sections:
            await seed_service.enrich_temporal_only(section)
        scope = "l1_intro" if candidate.level == 1 else "l2_intro"
        priority = min(
            embedding_priority(section, scope=scope, link_count=candidate.source_article_link_count)
            for section in intro_sections
        )
        await enqueue_embedding_jobs(self.session, intro_sections, priority=priority, force=False)

    async def _source_article_link_counts(self, title_id: int) -> dict[str, int]:
        result = await self.session.execute(
            select(SectionClean.links_json).where(SectionClean.title_id == title_id)
        )
        counts: dict[str, int] = {}
        for (links_json,) in result.all():
            for link in links_json or []:
                target = str(link.get("target") or "").strip()
                if not target:
                    continue
                normalized = _normalize_article_title(target)
                counts[normalized] = counts.get(normalized, 0) + 1
        return counts

    async def _wait_for_embeddings(self, section_keys: list[str], *, timeout_seconds: float) -> bool:
        """Wait briefly for queued embeddings before scoring relevance."""

        keys = sorted({key for key in section_keys if key})
        if not keys:
            return False
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            if await self._has_embeddings(keys):
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def _has_embeddings(self, section_keys: list[str]) -> bool:
        if not section_keys:
            return False
        result = await self.session.execute(
            text(
                f"""
                SELECT count(*)
                FROM "{settings.pg_schema}".section_embedding
                WHERE section_key = ANY(:section_keys)
                """
            ),
            {"section_keys": section_keys},
        )
        return int(result.scalar_one() or 0) >= len(set(section_keys))

    async def _entity_scores_for_sections(self, section_keys: list[str]) -> list[EntityScore]:
        if not section_keys:
            return []
        result = await self.session.execute(
            select(
                EntityPassageScore.entity_id,
                EntityPassageScore.blend,
                EntityRegistry.primary_type,
                EntityRegistry.primary_domain,
            )
            .join(EntityRegistry, EntityRegistry.entity_id == EntityPassageScore.entity_id)
            .where(
                EntityPassageScore.section_key.in_(section_keys),
                EntityPassageScore.ontology_version == ONTOLOGY_VERSION,
                EntityRegistry.ontology_version == ONTOLOGY_VERSION,
            )
            .order_by(EntityPassageScore.blend.desc())
        )
        by_entity: dict[str, EntityScore] = {}
        for entity_id, blend, primary_type, primary_domain in result.all():
            existing = by_entity.get(str(entity_id))
            score = EntityScore(
                entity_id=str(entity_id),
                blend=float(blend),
                primary_type=str(primary_type),
                primary_domain=str(primary_domain),
            )
            if existing is None or score.blend > existing.blend:
                by_entity[score.entity_id] = score
        return list(by_entity.values())

    async def _time_anchors_for_sections(self, section_keys: list[str]) -> list[TimeAnchorScore]:
        if not section_keys:
            return []
        result = await self.session.execute(
            select(
                TimeAnchorRegistry.time_id,
                TimeAnchorRegistry.center,
                TimeAnchorRegistry.spread,
                TimeAnchorRegistry.precision_score,
                TimeAnchorRegistry.confidence,
            )
            .join(FactTime, FactTime.time_id == TimeAnchorRegistry.time_id)
            .where(
                FactTime.section_key.in_(section_keys),
                FactTime.ontology_version == ONTOLOGY_VERSION,
                TimeAnchorRegistry.ontology_version == ONTOLOGY_VERSION,
            )
        )
        by_time: dict[str, TimeAnchorScore] = {}
        for time_id, center, spread, precision, confidence in result.all():
            score = TimeAnchorScore(
                time_id=str(time_id),
                center=None if center is None else float(center),
                spread=None if spread is None else float(spread),
                precision_score=float(precision),
                confidence=float(confidence),
            )
            existing = by_time.get(score.time_id)
            if existing is None or score.confidence > existing.confidence:
                by_time[score.time_id] = score
        return list(by_time.values())

    async def _embedding_similarity(
        self,
        source_section_key: str,
        candidate_section_keys: list[str],
    ) -> float | None:
        if not candidate_section_keys:
            return None
        try:
            result = await self.session.execute(
                text(
                    f"""
                    SELECT max(1 - (src.embedding <=> dst.embedding)) AS similarity
                    FROM "{settings.pg_schema}".section_embedding src
                    JOIN "{settings.pg_schema}".section_embedding dst
                      ON dst.section_key = ANY(:candidate_keys)
                    WHERE src.section_key = :source_key
                    """
                ),
                {"source_key": source_section_key, "candidate_keys": candidate_section_keys},
            )
            value = result.scalar_one_or_none()
            return None if value is None else float(value)
        except Exception:
            return None

    @staticmethod
    def _template_why(candidate: RelatedCandidate, signals: dict[str, Any]) -> str:
        parts: list[str] = []
        entities = signals.get("shared_entities") or []
        domains = signals.get("shared_domains") or []
        times = signals.get("time_overlap") or []
        if entities:
            parts.append("shares specific entities")
        if domains:
            parts.append("overlaps in " + ", ".join(str(domain) for domain in domains[:2]))
        if times:
            parts.append("overlaps in time")
        if candidate.level == 1:
            parts.append("is directly linked from this section")
        elif candidate.via_title:
            parts.append(f"is linked through {candidate.via_title}")
        if not parts:
            parts.append("is nearby in the section link graph")
        return f"{candidate.title} is related because it " + "; ".join(parts) + "."

    @staticmethod
    def _rank_bonus(candidate: RelatedCandidate) -> float:
        if candidate.level == 1:
            return round(max(0.02, 0.18 - (candidate.link_rank * 0.014)), 4)
        return round(max(0.01, 0.10 - (candidate.link_rank * 0.012)), 4)

    @staticmethod
    def _title_overlap(section_text: str, title: str) -> float:
        section_tokens = set(re.findall(r"[a-z0-9]{3,}", section_text.lower()))
        title_tokens = set(re.findall(r"[a-z0-9]{3,}", title.lower()))
        if not section_tokens or not title_tokens:
            return 0.0
        overlap = len(section_tokens & title_tokens) / max(1, len(title_tokens))
        return round(min(0.12, overlap * 0.12), 4)


def _normalize_article_title(title: str) -> str:
    """Normalize article titles for self-reference checks."""

    return re.sub(r"\s+", " ", (title or "").replace("_", " ").strip().casefold())


def shared_entity_ids(source_entities: list[EntityScore], candidate_entities: list[EntityScore]) -> list[str]:
    candidate_ids = {entity.entity_id for entity in candidate_entities}
    return sorted(entity.entity_id for entity in source_entities if entity.entity_id in candidate_ids)


def shared_domains(source_entities: list[EntityScore], candidate_entities: list[EntityScore]) -> list[str]:
    candidate_domains = {entity.primary_domain for entity in candidate_entities}
    return sorted({entity.primary_domain for entity in source_entities if entity.primary_domain in candidate_domains})


def shared_time_ids(source_times: list[TimeAnchorScore], candidate_times: list[TimeAnchorScore]) -> list[str]:
    candidate_ids = {time.time_id for time in candidate_times}
    return sorted(time.time_id for time in source_times if time.time_id in candidate_ids)


def _best_similarity(*values: float | None) -> float | None:
    present = [float(value) for value in values if value is not None]
    return max(present) if present else None


def _embedding_signal_used(intro_similarity: float | None, broad_similarity: float | None) -> str:
    if intro_similarity is None and broad_similarity is None:
        return "none"
    if intro_similarity is None:
        return "broad"
    if broad_similarity is None:
        return "intro"
    return "intro" if intro_similarity >= broad_similarity else "broad"


def gate_counts(rows: list[RelatedCache]) -> dict[str, int]:
    """Return aggregate gate counters for related rows."""

    counts = {
        "total": len(rows),
        "accepted": 0,
        "agent_eligible": 0,
        "timeline_eligible": 0,
        "rejected": 0,
    }
    for row in rows:
        gates = (row.signals_json or {}).get("gates") or {}
        if gates.get("accepted"):
            counts["accepted"] += 1
        else:
            counts["rejected"] += 1
        if gates.get("agent_eligible"):
            counts["agent_eligible"] += 1
        if gates.get("timeline_eligible"):
            counts["timeline_eligible"] += 1
    return counts
