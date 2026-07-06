"""Run Timeline background agent jobs.

Examples:
    python scripts/run_agent_worker.py --once --limit 3
    python scripts/run_agent_worker.py --poll-seconds 2
"""

from __future__ import annotations

import argparse
import asyncio

from app.core.config import get_settings
from app.db.database import async_session_factory, close_engine
from app.workers.temporal_agent import TemporalAgentWorker


async def run_worker(
    limit: int,
    poll_seconds: float,
    idle_exit: bool,
    concurrency: int,
    *,
    core_only: bool = False,
    lane: str = "full",
) -> None:
    concurrency = max(1, concurrency)
    stats = {"reserved": 0, "processed": 0, "succeeded": 0, "failed": 0}
    stats_lock = asyncio.Lock()

    async def worker_lane(lane_index: int) -> None:
        async with async_session_factory() as session:
            worker = TemporalAgentWorker(session, core_only=core_only, lane=lane)
            mode = "core-only" if core_only else worker.lane
            print(f"Agent worker lane {lane_index} started: {worker.worker_id} ({mode})")
            try:
                while True:
                    async with stats_lock:
                        if limit > 0 and stats["reserved"] >= limit:
                            break
                        if limit > 0:
                            stats["reserved"] += 1

                    job = await worker.claim_next_job()
                    if job is None:
                        if limit > 0:
                            async with stats_lock:
                                stats["reserved"] -= 1
                        if idle_exit:
                            break
                        await asyncio.sleep(poll_seconds)
                        continue

                    async with stats_lock:
                        stats["processed"] += 1
                    try:
                        await worker.process_job(job)
                        async with stats_lock:
                            stats["succeeded"] += 1
                    except Exception as exc:  # noqa: BLE001 - worker must keep polling after failures.
                        await worker.mark_failed(job, exc)
                        async with stats_lock:
                            stats["failed"] += 1
            finally:
                await worker.llm.aclose()

    mode = "core-only" if core_only else (lane or "full")
    print(f"Agent worker pool starting: concurrency={concurrency} mode={mode}")
    await asyncio.gather(*(worker_lane(index + 1) for index in range(concurrency)))
    print(
        "Agent worker result: "
        f"processed={stats['processed']} succeeded={stats['succeeded']} failed={stats['failed']}"
    )
    await close_engine()


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run V4 temporal agent jobs.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum jobs to process; 0 means forever.")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="Polling interval when idle.")
    parser.add_argument("--once", action="store_true", help="Exit when no job is available.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=settings.worker_concurrency,
        help="Number of concurrent worker lanes in this process.",
    )
    parser.add_argument(
        "--core-only",
        action="store_true",
        help="Only claim graph-frontier jobs; do not run embeddings, entities, or LLM-backed jobs.",
    )
    parser.add_argument(
        "--lane",
        choices=["cpu", "llm", "full"],
        default="full",
        help="Worker lane to claim. cpu runs CPU/DB jobs; llm runs vLLM jobs; full runs both.",
    )
    args = parser.parse_args()
    asyncio.run(
        run_worker(
            args.limit,
            args.poll_seconds,
            args.once,
            args.concurrency,
            core_only=args.core_only,
            lane=args.lane,
        )
    )


if __name__ == "__main__":
    main()
