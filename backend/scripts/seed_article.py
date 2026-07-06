"""Run CPU seed enrichment for every cached section of one article.

Usage:
    python scripts/seed_article.py "Abraham Lincoln"
    python scripts/seed_article.py "Abraham Lincoln" --seed-first
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db.database import async_session_factory
from app.db.models import SectionClean
from app.seeds.service import SeedService
from app.services import ReaderService


async def run(title: str, seed_first: bool) -> None:
    async with async_session_factory() as session:
        reader = ReaderService(session)

        canonical, title_id, sections = await reader.get_article(title, seed=seed_first)
        print(f"Article: {canonical!r}  title_id={title_id}  sections={len(sections)}")

        seeder = SeedService(session)
        for i, section in enumerate(sections, 1):
            result = await seeder.enrich_section(section)
            entities = result["spacy_entities"]
            times = result["temporal_matches"]
            print(f"  [{i}/{len(sections)}] {section.heading!r:40s}  entities={entities}  times={times}")

    print("Seed run complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed all sections of an article.")
    parser.add_argument("title", nargs="?", default="Abraham Lincoln")
    parser.add_argument("--seed-first", action="store_true", help="Force re-cache sections before seeding.")
    args = parser.parse_args()
    asyncio.run(run(args.title, args.seed_first))


if __name__ == "__main__":
    main()
