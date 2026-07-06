"""Health endpoints."""

from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import get_settings
from app.db.database import async_session_factory
from app.graph.driver import verify_connectivity as verify_neo4j
from app.llm.openai_compatible import LocalLLMClient

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    """Return health for local dependencies."""

    settings = get_settings()
    postgres_ok = False
    vector_available = False
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
            postgres_ok = True
            result = await session.execute(
                text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
            )
            vector_available = result.scalar_one_or_none() == 1
    except Exception:
        postgres_ok = False

    neo4j_ok = await verify_neo4j()
    llm = LocalLLMClient(settings)
    try:
        llm_ok = await llm.health_check()
    finally:
        await llm.aclose()
    return {
        "status": "ok" if postgres_ok and neo4j_ok else "degraded",
        "postgres": postgres_ok,
        "pgvector_available": vector_available,
        "neo4j": neo4j_ok,
        "llm": llm_ok,
        "read_path_llm_required": False,
        "schema": settings.pg_schema,
        "neo4j_database": settings.neo4j_database,
    }
