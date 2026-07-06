"""DB-backed CPU entity/time smoke for one article, with no LLM calls."""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import func, select

from app.db.database import async_session_factory, close_engine
from app.db.models import EntityRegistry, MentionCache, SectionTime, TimeDimension
from app.services import ReaderService


async def run(title: str) -> dict[str, object]:
    async with async_session_factory() as session:
        service = ReaderService(session)
        canonical, title_id, sections = await service.refresh_article(
            title,
            seed=True,
            enrich_ontology=True,
        )
        mention_rows = await session.execute(
            select(MentionCache.source, func.count())
            .where(MentionCache.title_id == title_id)
            .group_by(MentionCache.source)
            .order_by(MentionCache.source)
        )
        time_rows = await session.execute(
            select(TimeDimension.time_ref_id, TimeDimension.label, TimeDimension.precision, SectionTime.source)
            .join(SectionTime, SectionTime.time_ref_id == TimeDimension.time_ref_id)
            .where(SectionTime.title_id == title_id)
            .order_by(TimeDimension.time_ref_id)
            .limit(40)
        )
        entity_rows = await session.execute(
            select(EntityRegistry.primary_type, EntityRegistry.primary_domain, func.count())
            .join(MentionCache, MentionCache.entity_id == EntityRegistry.entity_id)
            .where(MentionCache.title_id == title_id)
            .group_by(EntityRegistry.primary_type, EntityRegistry.primary_domain)
            .order_by(EntityRegistry.primary_type, EntityRegistry.primary_domain)
        )
        time_items = time_rows.all()
        return {
            "title": canonical,
            "title_id": title_id,
            "sections": len(sections),
            "mention_sources": {str(source): int(count) for source, count in mention_rows.all()},
            "entity_type_domain_counts": [
                {"type": str(row[0]), "domain": str(row[1]), "count": int(row[2])}
                for row in entity_rows.all()
            ],
            "time_count_sampled": len(time_items),
            "times": [
                {"time_ref_id": str(row[0]), "label": str(row[1]), "precision": str(row[2]), "source": str(row[3])}
                for row in time_items
            ],
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("title", nargs="?", default="City of Joy")
    args = parser.parse_args()
    async def _main() -> dict[str, object]:
        try:
            return await run(args.title)
        finally:
            await close_engine()

    print(json.dumps(asyncio.run(_main()), indent=2))


if __name__ == "__main__":
    main()
