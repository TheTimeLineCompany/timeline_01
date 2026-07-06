"""Reader orchestration services."""

from __future__ import annotations

from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    AgentJob,
    ArticleCore,
    ContentRelatednessCache,
    EntityPassageScore,
    FactCache,
    FactTime,
    MentionCache,
    RelatedCache,
    SectionClean,
    SectionTag,
    SectionTime,
    TimelineContextCache,
)
from app.ingestion.redirects import RedirectResolver
from app.ingestion.section_cache import SectionCacheService
from app.ingestion.wiki_adapter import WikiAdapter
from app.graph.driver import execute_write
from app.ontology.entity_mentions import EntityMentionService
from app.ontology.passage_scores import EntityPassageScoreService
from app.ontology.temporal_projection import TemporalProjectionService
from app.seeds.service import SeedService

settings = get_settings()


class ReaderService:
    """High-level reader operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.adapter = WikiAdapter(session)
        self.redirects = RedirectResolver(session)
        self.cache = SectionCacheService(session)
        self.entity_mentions = EntityMentionService(session)
        self.entity_scores = EntityPassageScoreService(session)
        self.temporal_projection = TemporalProjectionService(session)
        self.seeds = SeedService(session)

    async def search(self, query: str, limit: int = 15) -> list[dict[str, object]]:
        """Search article titles."""

        return await self.adapter.search_titles(query, limit)

    async def get_article(
        self,
        title: str,
        *,
        seed: bool = False,
        enrich_ontology: bool = False,
    ) -> tuple[str, int, list[SectionClean]]:
        """Resolve, load, cache, and optionally seed an article.

        Request paths should normally leave ``enrich_ontology`` false and let
        the durable orchestration queue perform extraction. This keeps article
        rendering decoupled from CPU/GPU enrichment work.
        """

        resolved = await self.redirects.resolve_title(title)
        if resolved is None:
            raise ValueError(f"Article not found: {title}")
        canonical, title_id = resolved
        article = await self.adapter.get_article_by_title_id(canonical, title_id)
        if article is None:
            raise ValueError(f"Article not found: {canonical}")
        sections = await self.cache.cache_article(article)
        if enrich_ontology:
            await self.entity_mentions.enrich_article(sections, precision=False)
            await self.entity_scores.score_article(sections)
            await self.temporal_projection.project_sections(sections)
        if seed:
            for section in sections:
                await self.seeds.enrich_section(section)
        return canonical, title_id, sections

    async def refresh_article(
        self,
        title: str,
        *,
        seed: bool = False,
        enrich_ontology: bool = False,
    ) -> tuple[str, int, list[SectionClean]]:
        """Clear V4-derived state for an article, then recache it from source tables."""

        resolved = await self.redirects.resolve_title(title)
        if resolved is None:
            raise ValueError(f"Article not found: {title}")
        canonical, title_id = resolved
        article = await self.adapter.get_article_by_title_id(canonical, title_id)
        if article is None:
            raise ValueError(f"Article not found: {canonical}")

        await self._clear_article_derived_state(title_id)
        sections = await self.cache.cache_article(article)
        if enrich_ontology:
            await self.entity_mentions.enrich_article(sections, precision=False)
            await self.entity_scores.score_article(sections)
            await self.temporal_projection.project_sections(sections)
        if seed:
            for section in sections:
                await self.seeds.enrich_section(section)
        return canonical, title_id, sections

    async def get_section(self, section_key: str) -> SectionClean | None:
        """Return cached section by key."""

        result = await self.session.execute(
            select(SectionClean).where(SectionClean.section_key == section_key)
        )
        return result.scalar_one_or_none()

    async def _clear_article_derived_state(self, title_id: int) -> None:
        """Remove cached enrichment rows that depend on parser/app behavior."""

        result = await self.session.execute(
            select(SectionClean.section_key).where(SectionClean.title_id == title_id)
        )
        section_keys = [str(row[0]) for row in result.fetchall()]
        section_key_prefix = f"{int(title_id)}:%"
        section_scope = or_(
            RelatedCache.from_section_key.like(section_key_prefix),
            RelatedCache.from_section_key.in_(section_keys),
        )
        relatedness_scope = or_(
            ContentRelatednessCache.focus_section_key.like(section_key_prefix),
            ContentRelatednessCache.candidate_section_key.like(section_key_prefix),
            ContentRelatednessCache.focus_section_key.in_(section_keys),
            ContentRelatednessCache.candidate_section_key.in_(section_keys),
        )

        await self.session.execute(delete(SectionTag).where(SectionTag.title_id == title_id))
        await self.session.execute(delete(SectionTime).where(SectionTime.title_id == title_id))
        await self.session.execute(delete(MentionCache).where(MentionCache.title_id == title_id))
        await self.session.execute(delete(EntityPassageScore).where(EntityPassageScore.title_id == title_id))
        await self.session.execute(delete(FactTime).where(FactTime.title_id == title_id))
        await self.session.execute(delete(FactCache).where(FactCache.title_id == title_id))
        await self.session.execute(delete(ArticleCore).where(ArticleCore.title_id == title_id))
        await self.session.execute(delete(AgentJob).where(AgentJob.title_id == title_id))
        await self.session.execute(
            delete(TimelineContextCache).where(
                (TimelineContextCache.from_title_id == title_id)
                | (TimelineContextCache.source_title_id == title_id)
            )
        )
        await self.session.execute(
            text(f'DELETE FROM "{settings.pg_schema}".section_embedding WHERE title_id = :title_id'),
            {"title_id": title_id},
        )
        await self.session.execute(delete(RelatedCache).where(section_scope))
        await self.session.execute(delete(ContentRelatednessCache).where(relatedness_scope))
        await self._clear_article_graph_state(title_id, section_keys)
        await self.session.execute(delete(SectionClean).where(SectionClean.title_id == title_id))
        await self.session.commit()

    async def _clear_article_graph_state(self, title_id: int, section_keys: list[str]) -> None:
        """Best-effort Neo4j cleanup for a full article refresh."""

        try:
            await execute_write(
                """
                MATCH (a:V4Article {title_id: $title_id})
                OPTIONAL MATCH (a)-[has:HAS_SECTION]->(s:V4Section)
                OPTIONAL MATCH (s)-[rel:RELATED_TO]->(:V4Article)
                OPTIONAL MATCH (s)-[link:LINKS_TO]->(:V4Article)
                OPTIONAL MATCH (a)-[article_link:LINKS_TO]->(:V4Article)
                DELETE rel, link, article_link, has
                """,
                {"title_id": int(title_id)},
            )
            if section_keys:
                await execute_write(
                    """
                    UNWIND $section_keys AS section_key
                    MATCH (s:V4Section {section_key: section_key})
                    DETACH DELETE s
                    """,
                    {"section_keys": section_keys},
                )
        except Exception:
            # Refresh should still succeed when Neo4j is temporarily unavailable;
            # the next graph-frontier run will repair the graph projection.
            return
