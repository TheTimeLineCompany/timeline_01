"""FastAPI entry point for Timeline."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.graph import router as graph_router
from app.api.reader import router as reader_router
from app.api.setup import router as setup_router
from app.core.config import get_settings
from app.db.database import close_engine

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage process-level resources."""

    yield
    await close_engine()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Cache-first Timeline backend.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, tags=["Health"])
app.include_router(setup_router, prefix="/api/setup", tags=["Setup"])
app.include_router(reader_router, prefix="/api/reader", tags=["Reader"])
app.include_router(graph_router, prefix="/api/graph", tags=["Graph"])


@app.get("/")
async def root() -> dict[str, str]:
    """Return API metadata."""

    return {
        "name": settings.app_name,
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
    }
