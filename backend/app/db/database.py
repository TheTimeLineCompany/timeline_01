"""Async SQLAlchemy database utilities."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    """Base class for ORM models."""


engine: AsyncEngine = create_async_engine(
    settings.pg_dsn,
    echo=False,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async DB session."""

    async with async_session_factory() as session:
        yield session


async def close_engine() -> None:
    """Dispose the global engine."""

    await engine.dispose()
