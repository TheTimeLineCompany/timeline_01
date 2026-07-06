"""Neo4j async driver helpers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession

from app.core.config import get_settings
from app.graph.schema import GRAPH_CONSTRAINTS

settings = get_settings()
_driver: AsyncDriver | None = None


async def get_driver() -> AsyncDriver:
    """Return a singleton Neo4j async driver."""

    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_pool_size=20,
        )
        await _driver.verify_connectivity()
    return _driver


@asynccontextmanager
async def get_session(database: str | None = None) -> AsyncGenerator[AsyncSession, None]:
    """Yield a Neo4j session."""

    driver = await get_driver()
    async with driver.session(database=database or settings.neo4j_database) as session:
        yield session


async def close_driver() -> None:
    """Close the singleton driver."""

    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def execute_query(
    query: str,
    parameters: dict[str, Any] | None = None,
    *,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Execute a Cypher query and return row dictionaries."""

    async with get_session(database) as session:
        result = await session.run(query, parameters or {})
        return [dict(record) async for record in result]


async def execute_write(
    query: str,
    parameters: dict[str, Any] | None = None,
    *,
    database: str | None = None,
) -> None:
    """Execute a Cypher write query."""

    async with get_session(database) as session:
        result = await session.run(query, parameters or {})
        await result.consume()


async def verify_connectivity() -> bool:
    """Return whether Neo4j is reachable."""

    try:
        await execute_query("RETURN 1 AS ok")
    except Exception:
        return False
    return True


async def create_constraints() -> None:
    """Create V4 graph constraints."""

    for constraint in GRAPH_CONSTRAINTS:
        await execute_write(constraint.cypher)
