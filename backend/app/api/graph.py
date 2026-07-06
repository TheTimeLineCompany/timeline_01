"""Graphology/Cytoscape graph endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.database import get_session
from app.graph.frontier import expand_l1_graph_frontier, expand_section_graph_frontier
from app.graph.graphology import build_graphology_article_graph
from app.services import ReaderService

router = APIRouter()
settings = get_settings()


@router.get("/article/{title_or_id}")
async def get_article_graphology(
    title_or_id: str,
    levels: int = Query(default=2, ge=1, le=2),
    lens: str | None = None,
    min_relevance: float = Query(default=0.0, ge=0.0, le=1.0),
    include_back_edges: bool = True,
    include_cross_edges: bool = True,
    max_nodes: int = Query(default=1000, ge=20, le=2000),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Return a graphology-importable article neighborhood graph."""

    del lens  # Lens reweighting is intentionally deferred; keep the contract stable.
    service = ReaderService(session)
    try:
        canonical, title_id, sections = await service.get_article(
            title_or_id,
            seed=False,
            enrich_ontology=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await build_graphology_article_graph(
        session,
        title=canonical,
        title_id=title_id,
        sections=sections,
        levels=levels,
        min_relevance=min_relevance,
        max_nodes=max_nodes,
        include_back_edges=include_back_edges,
        include_cross_edges=include_cross_edges,
    )


@router.post("/article/{title_or_id}/expand")
async def expand_article_graphology(
    title_or_id: str,
    section_key: str | None = None,
    l1_title_id: int | None = Query(default=None, ge=1),
    l1_title: str | None = None,
    max_nodes: int = Query(default=1000, ge=20, le=2000),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Lazy-expand one selected graph browser node and return refreshed graph."""

    service = ReaderService(session)
    try:
        canonical, title_id, sections = await service.get_article(
            title_or_id,
            seed=False,
            enrich_ontology=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if section_key:
        section = next((item for item in sections if item.section_key == section_key), None)
        if section is None:
            raise HTTPException(status_code=404, detail=f"Section not found: {section_key}")
        await expand_section_graph_frontier(
            session,
            focus_title_id=title_id,
            section=section,
            max_l2_links_per_l1=settings.graph_frontier_l2_links_per_l1,
        )
    elif l1_title_id and l1_title:
        await expand_l1_graph_frontier(
            session,
            focus_title_id=title_id,
            l1_title_id=l1_title_id,
            l1_title=l1_title,
            max_l2_links_per_l1=settings.graph_frontier_l2_links_per_l1,
        )
    else:
        raise HTTPException(status_code=400, detail="Pass section_key or l1_title_id plus l1_title.")

    return await build_graphology_article_graph(
        session,
        title=canonical,
        title_id=title_id,
        sections=sections,
        levels=2,
        min_relevance=0.0,
        max_nodes=max_nodes,
        include_back_edges=True,
        include_cross_edges=True,
    )
