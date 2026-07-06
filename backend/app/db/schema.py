"""Schema bootstrap for Timeline."""

from sqlalchemy import text

from app.core.config import get_settings
from app.db.database import Base, engine
from app.db import models  # noqa: F401

settings = get_settings()


async def create_schema(*, include_vector: bool = True) -> dict[str, object]:
    """Create Timeline Postgres schema and tables.

    The pgvector-backed table is created with raw DDL because SQLAlchemy does not
    ship a built-in vector type. If pgvector is unavailable, callers can set
    `include_vector=False` to create the rest of the schema.
    """

    status: dict[str, object] = {
        "schema": settings.pg_schema,
        "orm_tables_created": False,
        "vector_table_created": False,
        "vector_error": None,
    }

    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{settings.pg_schema}"'))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                f'''
                ALTER TABLE IF EXISTS "{settings.pg_schema}".redirect_map
                DROP CONSTRAINT IF EXISTS uq_redirect_from_title_id
                '''
            )
        )
        status["orm_tables_created"] = True

        if include_vector:
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await conn.execute(
                    text(
                        f'''
                        CREATE TABLE IF NOT EXISTS "{settings.pg_schema}".section_embedding (
                            id bigserial PRIMARY KEY,
                            section_key text NOT NULL UNIQUE,
                            title_id bigint NOT NULL,
                            heading_id bigint NOT NULL,
                            embedding vector({settings.embedding_dimensions}) NOT NULL,
                            embedding_model text NOT NULL,
                            provenance_json jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                            created_at timestamptz NOT NULL DEFAULT now(),
                            updated_at timestamptz NULL
                        )
                        '''
                    )
                )
                await conn.execute(
                    text(
                        f'''
                        CREATE INDEX IF NOT EXISTS idx_v4_section_embedding_title
                        ON "{settings.pg_schema}".section_embedding (title_id)
                        '''
                    )
                )
                status["vector_table_created"] = True
            except Exception as exc:
                status["vector_error"] = str(exc)

    return status
