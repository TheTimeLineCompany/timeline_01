"""Inspect V4 agent jobs and recent traces."""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text

from app.db.database import async_session_factory, close_engine


def _print_row(row: object) -> None:
    print(json.dumps(dict(row), default=str, ensure_ascii=True))


async def inspect(job_type: str, section_key: str | None, trace_limit: int) -> None:
    async with async_session_factory() as session:
        where = "where job_type = :job_type"
        params: dict[str, object] = {"job_type": job_type, "trace_limit": trace_limit}
        if section_key:
            where += " and section_key = :section_key"
            params["section_key"] = section_key

        jobs = await session.execute(
            text(
                f"""
                select id, job_type, status, section_key, attempts, max_attempts,
                       locked_by, locked_at, run_after, updated_at, completed_at, last_error
                from timeline_v4.agent_job
                {where}
                order by updated_at desc nulls last, id desc
                limit 30
                """
            ),
            params,
        )
        print("Jobs")
        for row in jobs.mappings():
            _print_row(row)

        traces = await session.execute(
            text(
                """
                select id, step_name, status, latency_ms, error_text,
                       left(raw_response, 900) as raw_response,
                       output_json
                from timeline_v4.agent_trace
                where step_name = :job_type
                order by id desc
                limit :trace_limit
                """
            ),
            params,
        )
        print("\nRecent traces")
        for row in traces.mappings():
            _print_row(row)
    await close_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect V4 agent jobs.")
    parser.add_argument("--job-type", default="related_l1_l2_explain_v1")
    parser.add_argument("--section-key")
    parser.add_argument("--trace-limit", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(inspect(args.job_type, args.section_key, args.trace_limit))


if __name__ == "__main__":
    main()
