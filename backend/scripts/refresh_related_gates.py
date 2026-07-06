"""Refresh cached relatedness gate decisions after scoring/gate changes."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.db.database import async_session_factory, close_engine
from app.db.models import RelatedCache, SectionClean, TimelineContextCache
from app.related.gates import relatedness_gates
from app.workers.related_agent import SOURCE as RELATED_AGENT_SOURCE


async def refresh_related_gates(title: str | None, dry_run: bool, purge_context: bool) -> dict[str, int]:
    async with async_session_factory() as session:
        stmt = select(RelatedCache)
        title_ids: list[int] = []
        if title:
            sections = (
                await session.execute(
                    select(SectionClean.section_key, SectionClean.title_id).where(SectionClean.title.ilike(title))
                )
            ).all()
            section_keys = [section_key for section_key, _title_id in sections]
            title_ids = sorted({int(title_id) for _section_key, title_id in sections})
            if not section_keys:
                return {"seen": 0, "updated": 0, "timeline_demoted": 0, "context_purged": 0}
            stmt = stmt.where(RelatedCache.from_section_key.in_(section_keys))

        rows = (await session.execute(stmt.order_by(RelatedCache.id.asc()))).scalars().all()
        updated = 0
        timeline_demoted = 0
        demoted_related_ids: set[int] = set()
        for row in rows:
            signals = dict(row.signals_json or {})
            components = signals.get("components") or {}
            if not components:
                continue
            old_gates = signals.get("gates") or {}
            new_gates = relatedness_gates(
                level=int(row.level),
                score=float(row.score),
                why_source=str(row.why_source),
                components=components,
                source_entity_count=int(signals.get("source_entity_count") or 0),
                candidate_entity_count=int(signals.get("candidate_entity_count") or 0),
                source_time_count=int(signals.get("source_time_count") or 0),
                candidate_time_count=int(signals.get("candidate_time_count") or 0),
                agent_signal=_agent_signal(row, signals),
            )
            if new_gates == old_gates:
                continue
            if old_gates.get("timeline_eligible") and not new_gates.get("timeline_eligible"):
                timeline_demoted += 1
                demoted_related_ids.add(int(row.id))
            signals["gates"] = new_gates
            if not dry_run:
                row.signals_json = signals
                row.updated_at = datetime.utcnow()
            updated += 1

        context_purged = 0
        if purge_context and demoted_related_ids:
            context_stmt = select(TimelineContextCache)
            if title_ids:
                context_stmt = context_stmt.where(TimelineContextCache.from_title_id.in_(title_ids))
            context_rows = (await session.execute(context_stmt)).scalars().all()
            for context_row in context_rows:
                related_cache_id = (context_row.provenance_json or {}).get("related_cache_id")
                if related_cache_id in demoted_related_ids:
                    if not dry_run:
                        await session.delete(context_row)
                    context_purged += 1

        if not dry_run:
            await session.commit()
        return {
            "seen": len(rows),
            "updated": updated,
            "timeline_demoted": timeline_demoted,
            "context_purged": context_purged,
        }


def _agent_signal(row: RelatedCache, signals: dict[str, Any]) -> dict[str, Any] | None:
    if row.why_source != RELATED_AGENT_SOURCE:
        return None
    return signals.get(RELATED_AGENT_SOURCE) or {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh V4 related-cache gate decisions.")
    parser.add_argument("--title", help="Optional source article title to refresh, e.g. Caste.")
    parser.add_argument("--dry-run", action="store_true", help="Compute changes without writing them.")
    parser.add_argument(
        "--purge-context",
        action="store_true",
        help="Delete timeline-context rows tied to related rows demoted by refreshed gates.",
    )
    args = parser.parse_args()
    result = asyncio.run(_run(args.title, args.dry_run, args.purge_context))
    print(result)


async def _run(title: str | None, dry_run: bool, purge_context: bool) -> dict[str, int]:
    try:
        return await refresh_related_gates(title, dry_run, purge_context)
    finally:
        await close_engine()


if __name__ == "__main__":
    main()
