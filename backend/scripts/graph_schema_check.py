"""Validate or apply the Timeline Neo4j graph schema cleanup."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.graph.driver import create_constraints, execute_query, execute_write
from app.graph.schema import (
    GRAPH_SCHEMA_CLEANUP_QUERIES,
    GRAPH_SCHEMA_PRE_CONSTRAINT_CLEANUP_QUERIES,
    GRAPH_SCHEMA_VALIDATION_QUERY,
)


async def _validate() -> dict[str, object]:
    rows = await execute_query(GRAPH_SCHEMA_VALIDATION_QUERY)
    return rows[0] if rows else {}


async def _cleanup() -> dict[str, object]:
    for query in GRAPH_SCHEMA_PRE_CONSTRAINT_CLEANUP_QUERIES:
        await execute_write(query)
    await create_constraints()
    for query in GRAPH_SCHEMA_CLEANUP_QUERIES:
        await execute_write(query)
    return await _validate()


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup", action="store_true", help="Apply schema cleanup before validating.")
    args = parser.parse_args()
    result = await (_cleanup() if args.cleanup else _validate())
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    failures = {
        key: value
        for key, value in result.items()
        if key != "articles" and isinstance(value, int) and value != 0
    }
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(_main())
