"""Setup/bootstrap endpoints."""

from fastapi import APIRouter

from app.db.schema import create_schema
from app.graph.driver import create_constraints

router = APIRouter()


@router.post("/postgres-schema")
async def bootstrap_postgres_schema(include_vector: bool = True) -> dict[str, object]:
    """Create V4 Postgres cache schema."""

    return await create_schema(include_vector=include_vector)


@router.post("/neo4j-constraints")
async def bootstrap_neo4j_constraints() -> dict[str, object]:
    """Create V4 Neo4j constraints."""

    await create_constraints()
    return {"status": "ok"}
