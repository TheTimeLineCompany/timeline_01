"""Backfill related/cache gates to the current ontology gate version."""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select
from sqlalchemy import delete

from app.db.database import async_session_factory, close_engine
from app.db.models import ContentRelatednessCache, RelatedCache, TimelineContextCache
from app.related.gates import GATE_VERSION, relatedness_gates
from app.workers.related_agent import SOURCE, _agent_backed_gates


def _template_gates(row: RelatedCache, signals: dict) -> dict:
    return relatedness_gates(
        level=int(row.level),
        score=float(row.score),
        why_source=str(row.why_source or "template"),
        components=signals.get("components") or {},
        source_entity_count=int(signals.get("source_entity_count") or 0),
        candidate_entity_count=int(signals.get("candidate_entity_count") or 0),
        source_time_count=int(signals.get("source_time_count") or 0),
        candidate_time_count=int(signals.get("candidate_time_count") or 0),
    )


async def main(*, clear_timeline_context: bool) -> None:
    async with async_session_factory() as session:
        result = await session.execute(select(RelatedCache))
        rows = list(result.scalars().all())
        related_updated = 0
        stale_sections: set[str] = set()
        for row in rows:
            signals = dict(row.signals_json or {})
            gates = signals.get("gates") or {}
            new_gates = _agent_backed_gates(row, signals) if row.why_source == SOURCE else _template_gates(row, signals)
            if gates != new_gates:
                signals["gates"] = new_gates
                row.signals_json = signals
                stale_sections.add(row.from_section_key)
                related_updated += 1

        component_result = await session.execute(select(ContentRelatednessCache))
        component_rows = list(component_result.scalars().all())
        component_updated = 0
        for row in component_rows:
            gates = dict(row.gates_json or {})
            if gates.get("version") == GATE_VERSION:
                continue
            level = int(gates.get("level") or 2)
            score = float(gates.get("score") or row.relevance_norm or 0)
            new_gates = relatedness_gates(
                level=level,
                score=score,
                why_source="template",
                components=row.components_json or {},
                source_entity_count=0,
                candidate_entity_count=0,
                source_time_count=0,
                candidate_time_count=0,
            )
            row.gates_json = {**new_gates, "scoring_version": gates.get("scoring_version")}
            component_updated += 1

        timeline_deleted = 0
        if clear_timeline_context and stale_sections:
            delete_result = await session.execute(
                delete(TimelineContextCache).where(TimelineContextCache.from_section_key.in_(sorted(stale_sections)))
            )
            timeline_deleted = delete_result.rowcount or 0

        await session.commit()
        print(f"Backfilled {related_updated} related_cache gate row(s).")
        print(f"Backfilled {component_updated} content_relatedness_cache gate row(s).")
        if clear_timeline_context:
            print(f"Deleted {timeline_deleted} stale timeline_context_cache row(s).")
    await close_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill related gates to the current gate version.")
    parser.add_argument(
        "--clear-timeline-context",
        action="store_true",
        help="Delete timeline-context rows for sections whose related gates changed.",
    )
    args = parser.parse_args()
    asyncio.run(main(clear_timeline_context=args.clear_timeline_context))
