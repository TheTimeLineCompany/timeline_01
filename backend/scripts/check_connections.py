"""Check V4 dependency connectivity."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.core.config import get_settings
from app.db.database import async_session_factory
from app.graph.driver import verify_connectivity
from app.llm.openai_compatible import LocalLLMClient


async def main() -> None:
    settings = get_settings()
    async with async_session_factory() as session:
        await session.execute(text("SELECT 1"))
        print("postgres: ok")
        vector = await session.execute(text("SELECT 1 FROM pg_available_extensions WHERE name='vector'"))
        print(f"pgvector_available: {vector.scalar_one_or_none() == 1}")
    print(f"neo4j: {await verify_connectivity()}")
    llm = LocalLLMClient(settings)
    try:
        print(f"llm: {await llm.health_check()}")
    finally:
        await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
