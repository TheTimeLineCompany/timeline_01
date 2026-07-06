"""Create and populate the V4 indexed title search cache."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.db.database import async_session_factory


CREATE_TABLE = """
DROP TABLE IF EXISTS timeline_v4.title_search_cache;

CREATE TABLE timeline_v4.title_search_cache (
    cache_id bigserial PRIMARY KEY,
    heading text NOT NULL,
    title_id bigint NOT NULL,
    heading_lc text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE EXTENSION IF NOT EXISTS pg_trgm;
"""

CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_title_search_cache_heading_lc_pattern
    ON timeline_v4.title_search_cache (heading_lc text_pattern_ops);

CREATE INDEX IF NOT EXISTS ix_title_search_cache_heading_lc_trgm
    ON timeline_v4.title_search_cache USING gin (heading_lc gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ix_title_search_cache_title_id
    ON timeline_v4.title_search_cache (title_id);
"""


POPULATE = """
INSERT INTO timeline_v4.title_search_cache (title_id, heading, heading_lc, updated_at)
SELECT
    title_id::bigint,
    heading::text,
    lower(heading::text),
    now()
FROM public."wiki_content_lookup_V4"
WHERE heading IS NOT NULL
  AND title_id IS NOT NULL
  AND length(trim(heading::text)) > 0;
"""


async def main() -> None:
    async with async_session_factory() as session:
        for statement in CREATE_TABLE.strip().split(";"):
            sql = statement.strip()
            if sql:
                await session.execute(text(sql))
        await session.execute(text(POPULATE))
        for statement in CREATE_INDEXES.strip().split(";"):
            sql = statement.strip()
            if sql:
                await session.execute(text(sql))
        result = await session.execute(text("SELECT count(*) FROM timeline_v4.title_search_cache"))
        await session.commit()
        print(f"title_search_cache rows: {int(result.scalar_one())}")


if __name__ == "__main__":
    asyncio.run(main())
