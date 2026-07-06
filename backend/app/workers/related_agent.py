"""Agent enrichment for L1/L2 related information."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.core.config import Settings
from app.db.models import AgentJob, AgentTrace, RelatedCache, SectionClean
from app.llm.json_utils import extract_json_object
from app.llm.openai_compatible import LocalLLMClient
from app.orchestration.state import upsert_processing_state
from app.related.gates import relatedness_gates
from app.related.service import RelatedInfoService
from app.workers.related_cache import enqueue_related_cache_jobs

JOB_TYPE = "related_l1_l2_explain_v1"
FOCUS_JOB_TYPE = "section_insight_v1"
SWEEP_PACK_JOB_TYPE = "related_sweep_pack_v1"
SOURCE = "agent_related_v1"
MODEL_VERSION = "agent-related-v7"
RELATED_AGENT_GUIDED_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "to_title": {"type": "string"},
                    "why_text": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reasoning_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "evidence_char_start": {"type": ["integer", "null"]},
                    "evidence_char_end": {"type": ["integer", "null"]},
                },
                "required": ["to_title", "why_text", "confidence", "reasoning_tags"],
            },
        }
    },
    "required": ["insights"],
}


@dataclass(frozen=True)
class RelatedInsight:
    """Validated related-insight update from the LLM."""

    to_title: str
    why_text: str
    confidence: float
    reasoning_tags: list[str]
    evidence_char_start: int | None = None
    evidence_char_end: int | None = None


async def _mark_related_completed(
    session: AsyncSession,
    section: SectionClean,
    detail: str,
    updated_count: int,
    *,
    state: str = "completed",
) -> None:
    await upsert_processing_state(
        session,
        title_id=section.title_id,
        section_key=section.section_key,
        area="related_agent",
        state=state,
        expected_count=1,
        completed_count=1 if state == "completed" else 0,
        detail=detail,
        reason="Related-agent section job succeeded.",
        source="agent_worker",
        metadata={"updated_insights": updated_count},
        commit=False,
    )


async def enqueue_related_jobs(
    session: AsyncSession,
    sections: list[SectionClean],
    *,
    priority: int = 90,
    force: bool = False,
    target_title_id: int | None = None,
) -> int:
    """Create or refresh related-agent jobs for cached sections."""

    useful_sections = [section for section in sections if is_agent_related_section(section)]
    if not useful_sections:
        return 0

    values = []
    for section in useful_sections:
        value_score = _section_value_score(section)
        priority_delta = 0 if target_title_id is not None else int((1.0 - value_score) * 28)
        values.append(
            {
                "job_type": JOB_TYPE,
                "status": "pending",
                "priority": priority + priority_delta,
                "title_id": section.title_id,
                "section_key": section.section_key,
                "payload_json": {
                    "title": section.title,
                    "heading": section.heading,
                    "heading_id": section.heading_id,
                    "target_title_id": target_title_id,
                    "section_value_score": value_score,
                },
                "attempts": 0,
                "max_attempts": 3,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "completed_at": None,
            }
        )
    stmt = insert(AgentJob).values(values)
    if force:
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_job_type_section",
            set_={
                "status": "pending",
                "priority": stmt.excluded.priority,
                "title_id": stmt.excluded.title_id,
                "payload_json": stmt.excluded.payload_json,
                "attempts": 0,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "completed_at": None,
                "updated_at": func.now(),
            },
        )
    else:
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_job_type_section",
            set_={
                "status": "pending",
                "priority": stmt.excluded.priority,
                "title_id": stmt.excluded.title_id,
                "payload_json": stmt.excluded.payload_json,
                "attempts": 0,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "completed_at": None,
                "updated_at": func.now(),
            },
            where=AgentJob.status.in_(["pending", "retry", "failed"]),
        )

    result = await session.execute(stmt)
    status_result = await session.execute(
        select(AgentJob.section_key, AgentJob.status, AgentJob.last_error)
        .where(AgentJob.job_type == JOB_TYPE)
        .where(AgentJob.section_key.in_([section.section_key for section in useful_sections]))
    )
    job_states = {str(section_key): (str(status), last_error) for section_key, status, last_error in status_result.all()}
    for section in useful_sections:
        status, last_error = job_states.get(section.section_key, ("pending", None))
        state = _job_status_to_processing_state(status)
        await upsert_processing_state(
            session,
            title_id=section.title_id,
            section_key=section.section_key,
            area="related_agent",
            state=state,
            expected_count=1,
            completed_count=1 if state == "completed" else 0,
            pending_count=1 if state == "pending" else 0,
            running_count=1 if state == "running" else 0,
            failed_count=1 if state == "attention" else 0,
            detail=f"Related-agent section job is {status}.",
            reason="Synchronized from durable job state after enqueue.",
            last_error=last_error,
            source="agent_enqueue",
            metadata={
                "target_title_id": target_title_id,
                "section_value_score": _section_value_score(section),
            },
            commit=False,
        )
    await session.commit()
    return result.rowcount or 0


async def enqueue_related_sweep_pack_jobs(
    session: AsyncSession,
    sections: list[SectionClean],
    *,
    priority: int = 62,
    force: bool = False,
    pack_size: int = 4,
) -> int:
    """Create low-priority packed sweep jobs for broad related insights."""

    useful_sections = sorted(
        [section for section in sections if is_agent_related_section(section)],
        key=lambda section: (-_section_value_score(section), int(section.heading_id or 0), section.section_key),
    )
    if not useful_sections:
        return 0

    pack_size = max(1, min(8, int(pack_size or 4)))
    values = []
    for pack_index, offset in enumerate(range(0, len(useful_sections), pack_size)):
        pack_sections = useful_sections[offset : offset + pack_size]
        pack_key = f"sweep-pack:{pack_sections[0].title_id}:{pack_index + 1}:{'-'.join(str(section.heading_id) for section in pack_sections)}"
        values.append(
            {
                "job_type": SWEEP_PACK_JOB_TYPE,
                "status": "pending",
                "priority": priority + min(12, pack_index),
                "title_id": pack_sections[0].title_id,
                "section_key": pack_key,
                "payload_json": {
                    "section_keys": [section.section_key for section in pack_sections],
                    "section_count": len(pack_sections),
                    "insight_tier": "sweep_pack",
                    "pack_index": pack_index + 1,
                },
                "attempts": 0,
                "max_attempts": 3,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "completed_at": None,
            }
        )

    stmt = insert(AgentJob).values(values)
    if force:
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_job_type_section",
            set_={
                "status": "pending",
                "priority": stmt.excluded.priority,
                "title_id": stmt.excluded.title_id,
                "payload_json": stmt.excluded.payload_json,
                "attempts": 0,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "completed_at": None,
                "updated_at": func.now(),
            },
        )
    else:
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_job_type_section",
            set_={
                "status": "pending",
                "priority": stmt.excluded.priority,
                "title_id": stmt.excluded.title_id,
                "payload_json": stmt.excluded.payload_json,
                "attempts": 0,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "completed_at": None,
                "updated_at": func.now(),
            },
            where=AgentJob.status.in_(["pending", "retry", "failed"]),
        )

    result = await session.execute(stmt)
    for section in useful_sections:
        await upsert_processing_state(
            session,
            title_id=section.title_id,
            section_key=section.section_key,
            area="related_agent",
            state="pending",
            expected_count=1,
            pending_count=1,
            detail="Packed related sweep job is pending.",
            reason="Background sweep section grouped into a packed related-insight job.",
            source="related_sweep_pack_enqueue",
            metadata={
                "job_type": SWEEP_PACK_JOB_TYPE,
                "section_value_score": _section_value_score(section),
                "insight_tier": "sweep_pack",
            },
            commit=False,
        )
    await session.commit()
    return result.rowcount or 0


async def enqueue_section_insight_jobs(
    session: AsyncSession,
    sections: list[SectionClean],
    *,
    priority: int = 24,
    force: bool = False,
    limit: int = 0,
) -> int:
    """Create high-priority focus-tier section-insight jobs.

    This lane uses the same parser and persistence as the related-agent worker,
    but it has a separate durable job type so selected/high-value sections can
    run before broader sweep work.
    """

    useful_sections = sorted(
        [section for section in sections if is_agent_related_section(section)],
        key=lambda section: (-_section_value_score(section), int(section.heading_id or 0), section.section_key),
    )
    if limit > 0:
        useful_sections = useful_sections[:limit]
    if not useful_sections:
        return 0

    values = []
    for rank, section in enumerate(useful_sections):
        value_score = _section_value_score(section)
        values.append(
            {
                "job_type": FOCUS_JOB_TYPE,
                "status": "pending",
                "priority": priority + min(8, rank),
                "title_id": section.title_id,
                "section_key": section.section_key,
                "payload_json": {
                    "title": section.title,
                    "heading": section.heading,
                    "heading_id": section.heading_id,
                    "target_title_id": None,
                    "section_value_score": value_score,
                    "insight_tier": "focus",
                    "focus_rank": rank + 1,
                },
                "attempts": 0,
                "max_attempts": 3,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "completed_at": None,
            }
        )

    stmt = insert(AgentJob).values(values)
    if force:
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_job_type_section",
            set_={
                "status": "pending",
                "priority": stmt.excluded.priority,
                "title_id": stmt.excluded.title_id,
                "payload_json": stmt.excluded.payload_json,
                "attempts": 0,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "completed_at": None,
                "updated_at": func.now(),
            },
        )
    else:
        stmt = stmt.on_conflict_do_update(
            constraint="uq_agent_job_type_section",
            set_={
                "status": "pending",
                "priority": stmt.excluded.priority,
                "title_id": stmt.excluded.title_id,
                "payload_json": stmt.excluded.payload_json,
                "attempts": 0,
                "locked_by": None,
                "locked_at": None,
                "last_error": None,
                "completed_at": None,
                "updated_at": func.now(),
            },
            where=AgentJob.status.in_(["pending", "retry", "failed"]),
        )

    result = await session.execute(stmt)
    for section in useful_sections:
        await upsert_processing_state(
            session,
            title_id=section.title_id,
            section_key=section.section_key,
            area="related_agent",
            state="pending",
            expected_count=1,
            pending_count=1,
            detail="Focus-tier section insight job is pending.",
            reason="High-value section selected for first-pass LLM insight before broad sweep work.",
            source="section_insight_enqueue",
            metadata={
                "job_type": FOCUS_JOB_TYPE,
                "section_value_score": _section_value_score(section),
                "insight_tier": "focus",
            },
            commit=False,
        )
    await session.commit()
    return result.rowcount or 0


def _job_status_to_processing_state(status: str) -> str:
    if status == "succeeded":
        return "completed"
    if status == "running":
        return "running"
    if status in {"pending", "retry"}:
        return "pending"
    if status == "failed":
        return "attention"
    return "idle"


def _section_value_score(section: SectionClean) -> float:
    """Estimate which sections are worth enriching first."""

    text = " ".join((section.clean_text or "").split())
    length_score = min(1.0, len(text) / 1400)
    link_score = min(1.0, len(section.links_json or []) / 14)
    date_score = 1.0 if re.search(r"\b(?:\d{3,4}|BCE|CE|century|war|king|queen|president|founded|born|died)\b", text, re.I) else 0.0
    heading = (section.heading or "").strip().lower()
    heading_score = 0.25 if heading in {"lead", "introduction", "history", "overview", "background"} else 0.0
    score = 0.38 * length_score + 0.28 * link_score + 0.24 * date_score + 0.10 * heading_score
    return round(max(0.05, min(1.0, score)), 4)


async def process_related_job(
    session: AsyncSession,
    settings: Settings,
    llm: LocalLLMClient,
    job: AgentJob,
) -> None:
    """Run related L1/L2 explanation enrichment for one section."""

    if job.job_type == SWEEP_PACK_JOB_TYPE:
        await process_related_sweep_pack_job(session, settings, llm, job)
        return

    section = await _load_section(session, job.section_key)
    if section is None:
        raise ValueError(f"Cached section not found: {job.section_key}")
    if not is_agent_related_section(section):
        job.status = "succeeded"
        job.completed_at = datetime.utcnow()
        job.last_error = None
        job.locked_by = None
        job.locked_at = None
        job.run_after = None
        job.updated_at = datetime.utcnow()
        await _mark_related_completed(session, section, "Section is not eligible for paragraph comparison.", 0)
        await session.commit()
        return

    related_service = RelatedInfoService(session)
    related_rows = await related_service.read_cached_related(section.section_key, 40)
    cache_has_rows = bool(related_rows)
    cache_has_current_scoring = RelatedInfoService._cache_has_current_scoring(related_rows)
    cache_has_current_signal_state = (
        await related_service._cache_has_current_signal_state(section, related_rows)
        if cache_has_rows and cache_has_current_scoring
        else False
    )
    cache_current = (
        cache_has_rows
        and cache_has_current_scoring
        and cache_has_current_signal_state
    )
    if not cache_current:
        await _defer_for_related_cache(
            session,
            job,
            section,
            rows=len(related_rows),
            force_refresh=not cache_has_current_scoring or not cache_has_current_signal_state,
        )
        return

    target_title_id = _target_title_id(job.payload_json)
    if target_title_id is not None:
        related_rows = [row for row in related_rows if row.to_title_id == target_title_id]
        if not related_rows:
            raise ValueError(f"Related target not found for section {section.section_key}: {target_title_id}")

    _clear_stale_agent_insights(related_rows)
    run_id = f"{job.job_type}:{job.id}:{uuid.uuid4()}"
    trace = AgentTrace(
        run_id=run_id,
        step_name=job.job_type,
        model_name=settings.llm_model,
        status="running",
        input_json={
            "section_key": section.section_key,
            "title": section.title,
            "heading": section.heading,
            "related_count": len(related_rows),
            "target_title_id": target_title_id,
            "candidate_titles": [row.to_title for row in related_rows],
            "insight_tier": (job.payload_json or {}).get("insight_tier") or "sweep",
        },
    )
    session.add(trace)
    await session.commit()

    started = time.perf_counter()
    raw_responses: list[str] = []
    try:
        all_insights: list[RelatedInsight] = []
        usage: list[dict[str, Any] | None] = []
        parse_errors: list[str] = []
        parse_modes: list[str] = []
        batches_attempted = 0
        batches_succeeded = 0
        guided_json_enabled = bool(getattr(settings, "llm_guided_json_enabled", False))
        for prompt_rows in _prompt_batches(section.clean_text, related_rows, target_title_id=target_title_id):
            batches_attempted += 1
            candidate_context = await _candidate_context(session, prompt_rows)
            _, excerpt_start, excerpt_end = _section_prompt_excerpt(section.clean_text, prompt_rows)
            messages = _build_related_prompt(section, prompt_rows, candidate_context)
            response = await llm.chat_completion(
                messages,
                temperature=0.1,
                max_tokens=900,
                response_format={"type": "json_object"},
                guided_json=RELATED_AGENT_GUIDED_JSON_SCHEMA if guided_json_enabled else None,
                timeout_seconds=max(settings.llm_timeout_seconds, 180),
            )
            raw_response = response["choices"][0]["message"]["content"]
            raw_responses.append(raw_response)
            usage.append(response.get("usage"))
            try:
                parsed_batch, parse_mode = parse_related_agent_response_with_mode(
                    raw_response,
                    evidence_char_start=excerpt_start,
                    evidence_char_end=excerpt_end,
                    guided_json_used=guided_json_enabled,
                )
                all_insights.extend(parsed_batch)
                parse_modes.append(parse_mode)
                batches_succeeded += 1
            except Exception as exc:  # noqa: BLE001 - a malformed batch should not poison the section.
                parse_errors.append(str(exc)[:260])

        if batches_attempted and not batches_succeeded:
            raise ValueError("; ".join(parse_errors) or "No related-agent batch parsed successfully")

        latency_ms = int((time.perf_counter() - started) * 1000)
        updated = await _persist_insights(session, related_rows, all_insights)
        context_result = await _promote_timeline_context(session, section)

        trace.status = "incomplete" if parse_errors else "succeeded"
        trace.output_json = {
            "updated": updated,
            "target_title_id": target_title_id,
            "job_type": job.job_type,
            "insight_tier": (job.payload_json or {}).get("insight_tier") or "sweep",
            "parse_outcome": {
                "batches_attempted": batches_attempted,
                "batches_succeeded": batches_succeeded,
                "batches_failed": len(parse_errors),
                "guided_json_requested": guided_json_enabled,
                "modes": parse_modes,
                "errors": parse_errors[:6],
            },
            "facts_kept": updated,
            "candidate_count": len(related_rows),
            "timeline_context": {
                "rows_upserted": context_result.get("rows_upserted", 0),
                "temporal_jobs_enqueued": context_result.get("temporal_jobs_enqueued", 0),
                "pending": context_result.get("pending", False),
            },
            "insights": [
                {
                    "to_title": insight.to_title,
                    "confidence": insight.confidence,
                    "reasoning_tags": insight.reasoning_tags,
                    "evidence_char_start": insight.evidence_char_start,
                    "evidence_char_end": insight.evidence_char_end,
                }
                for insight in all_insights
            ],
        }
        trace.raw_response = "\n\n--- batch ---\n\n".join(raw_responses)
        trace.latency_ms = latency_ms
        trace.usage_json = {"batches": usage}
        trace.completed_at = datetime.utcnow()
        job.status = "succeeded"
        job.completed_at = datetime.utcnow()
        job.last_error = None
        job.locked_by = None
        job.locked_at = None
        job.run_after = None
        job.updated_at = datetime.utcnow()
        await _mark_related_completed(
            session,
            section,
            (
                f"Related agent completed across {len(related_rows)} candidate(s)."
                if not parse_errors
                else f"Related agent completed with parse gaps: {batches_succeeded}/{batches_attempted} batch(es) parsed."
            ),
            updated,
            state="incomplete" if parse_errors else "completed",
        )
        await session.commit()
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        error_text = str(exc) or exc.__class__.__name__
        trace.status = "failed"
        trace.raw_response = "\n\n--- batch ---\n\n".join(raw_responses) or None
        trace.error_text = error_text
        trace.latency_ms = latency_ms
        trace.completed_at = datetime.utcnow()
        await session.commit()
        raise


async def process_related_sweep_pack_job(
    session: AsyncSession,
    settings: Settings,
    llm: LocalLLMClient,
    job: AgentJob,
) -> None:
    """Run a packed background sweep with per-section partial accept."""

    payload = job.payload_json or {}
    section_keys = [str(key) for key in (payload.get("section_keys") or []) if str(key).strip()]
    if not section_keys:
        raise ValueError("related_sweep_pack_v1 payload did not include section_keys")

    pack_run_id = f"{SWEEP_PACK_JOB_TYPE}:{job.id}:{uuid.uuid4()}"
    pack_trace = AgentTrace(
        run_id=pack_run_id,
        step_name=SWEEP_PACK_JOB_TYPE,
        model_name=settings.llm_model,
        status="running",
        input_json={
            "job_id": job.id,
            "section_keys": section_keys,
            "section_count": len(section_keys),
            "insight_tier": payload.get("insight_tier") or "sweep_pack",
        },
    )
    session.add(pack_trace)
    await session.commit()

    started = time.perf_counter()
    succeeded: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    usage: list[dict[str, Any] | None] = []
    raw_sections: list[str] = []

    for section_key in section_keys:
        section = await _load_section(session, section_key)
        if section is None:
            skipped.append({"section_key": section_key, "reason": "missing_section"})
            continue
        if not is_agent_related_section(section):
            await _mark_related_completed(session, section, "Section is not eligible for paragraph comparison.", 0)
            skipped.append({"section_key": section_key, "reason": "not_agent_related"})
            continue

        related_service = RelatedInfoService(session)
        related_rows = await related_service.read_cached_related(section.section_key, 40)
        cache_has_rows = bool(related_rows)
        cache_has_current_scoring = RelatedInfoService._cache_has_current_scoring(related_rows)
        cache_has_current_signal_state = (
            await related_service._cache_has_current_signal_state(section, related_rows)
            if cache_has_rows and cache_has_current_scoring
            else False
        )
        cache_current = (
            cache_has_rows
            and cache_has_current_scoring
            and cache_has_current_signal_state
        )
        if not cache_current:
            queued = await enqueue_related_cache_jobs(
                session,
                [section],
                priority=22,
                force=not cache_has_current_scoring or not cache_has_current_signal_state,
            )
            await enqueue_related_sweep_pack_jobs(
                session,
                [section],
                priority=int(job.priority or 62) + 6,
                force=True,
                pack_size=1,
            )
            await upsert_processing_state(
                session,
                title_id=section.title_id,
                section_key=section.section_key,
                area="related_agent",
                state="pending",
                expected_count=1,
                pending_count=1,
                detail=f"Packed sweep waiting for related-cache inputs. Cached rows currently available: {len(related_rows)}.",
                reason="Packed related sweep is cache-only; CPU related-cache work must finish first.",
                source="related_sweep_pack_waiting_inputs",
                metadata={"cached_rows": len(related_rows), "related_cache_jobs_enqueued": queued},
                commit=False,
            )
            await session.commit()
            deferred.append({"section_key": section.section_key, "cached_rows": len(related_rows)})
            continue

        try:
            section_result = await _run_related_section_from_pack(
                session,
                settings,
                llm,
                section,
                related_rows,
                pack_job_id=int(job.id),
            )
            succeeded.append(section_result["summary"])
            usage.extend(section_result["usage"])
            raw_sections.extend(section_result["raw_responses"])
        except Exception as exc:  # noqa: BLE001 - one section must not poison pack siblings.
            failed.append({"section_key": section.section_key, "error": str(exc)[:260]})
            await upsert_processing_state(
                session,
                title_id=section.title_id,
                section_key=section.section_key,
                area="related_agent",
                state="attention",
                expected_count=1,
                failed_count=1,
                detail="Packed related sweep failed for this section.",
                reason="This section failed inside a packed sweep; successful sibling sections were kept.",
                last_error=str(exc)[:260],
                source="related_sweep_pack_worker",
                metadata={"pack_job_id": job.id},
                commit=False,
            )
            await session.commit()

    latency_ms = int((time.perf_counter() - started) * 1000)
    pack_trace.status = "incomplete" if failed or deferred else "succeeded"
    pack_trace.output_json = {
        "sections_attempted": len(section_keys),
        "sections_succeeded": len(succeeded),
        "sections_deferred": len(deferred),
        "sections_skipped": len(skipped),
        "sections_failed": len(failed),
        "succeeded": succeeded,
        "deferred": deferred,
        "skipped": skipped,
        "failed": failed,
    }
    pack_trace.raw_response = "\n\n--- section ---\n\n".join(raw_sections) or None
    pack_trace.latency_ms = latency_ms
    pack_trace.usage_json = {"sections": usage}
    pack_trace.completed_at = datetime.utcnow()
    job.status = "succeeded"
    job.completed_at = datetime.utcnow()
    job.last_error = None if not failed else f"{len(failed)} section(s) failed inside packed sweep"
    job.locked_by = None
    job.locked_at = None
    job.run_after = None
    job.updated_at = datetime.utcnow()
    await session.commit()


async def _run_related_section_from_pack(
    session: AsyncSession,
    settings: Settings,
    llm: LocalLLMClient,
    section: SectionClean,
    related_rows: list[RelatedCache],
    *,
    pack_job_id: int,
) -> dict[str, Any]:
    """Run the existing section-scoped related prompt inside a sweep pack."""

    _clear_stale_agent_insights(related_rows)
    run_id = f"{SWEEP_PACK_JOB_TYPE}:{pack_job_id}:{section.section_key}:{uuid.uuid4()}"
    trace = AgentTrace(
        run_id=run_id,
        step_name=SWEEP_PACK_JOB_TYPE,
        model_name=settings.llm_model,
        status="running",
        input_json={
            "pack_job_id": pack_job_id,
            "section_key": section.section_key,
            "title": section.title,
            "heading": section.heading,
            "related_count": len(related_rows),
            "candidate_titles": [row.to_title for row in related_rows],
            "insight_tier": "sweep_pack",
        },
    )
    session.add(trace)
    await session.commit()

    started = time.perf_counter()
    raw_responses: list[str] = []
    usage: list[dict[str, Any] | None] = []
    parse_errors: list[str] = []
    parse_modes: list[str] = []
    all_insights: list[RelatedInsight] = []
    batches_attempted = 0
    batches_succeeded = 0
    guided_json_enabled = bool(getattr(settings, "llm_guided_json_enabled", False))

    try:
        for prompt_rows in _prompt_batches(section.clean_text, related_rows):
            batches_attempted += 1
            candidate_context = await _candidate_context(session, prompt_rows)
            _, excerpt_start, excerpt_end = _section_prompt_excerpt(section.clean_text, prompt_rows)
            messages = _build_related_prompt(section, prompt_rows, candidate_context)
            response = await llm.chat_completion(
                messages,
                temperature=0.1,
                max_tokens=900,
                response_format={"type": "json_object"},
                guided_json=RELATED_AGENT_GUIDED_JSON_SCHEMA if guided_json_enabled else None,
                timeout_seconds=max(settings.llm_timeout_seconds, 180),
            )
            raw_response = response["choices"][0]["message"]["content"]
            raw_responses.append(raw_response)
            usage.append(response.get("usage"))
            try:
                parsed_batch, parse_mode = parse_related_agent_response_with_mode(
                    raw_response,
                    evidence_char_start=excerpt_start,
                    evidence_char_end=excerpt_end,
                    guided_json_used=guided_json_enabled,
                )
                all_insights.extend(parsed_batch)
                parse_modes.append(parse_mode)
                batches_succeeded += 1
            except Exception as exc:  # noqa: BLE001 - malformed batch still allows partial accept.
                parse_errors.append(str(exc)[:260])

        if batches_attempted and not batches_succeeded:
            raise ValueError("; ".join(parse_errors) or "No packed related-agent batch parsed successfully")

        updated = await _persist_insights(session, related_rows, all_insights)
        context_result = await _promote_timeline_context(session, section)
        latency_ms = int((time.perf_counter() - started) * 1000)
        trace.status = "incomplete" if parse_errors else "succeeded"
        trace.output_json = {
            "updated": updated,
            "job_type": SWEEP_PACK_JOB_TYPE,
            "insight_tier": "sweep_pack",
            "parse_outcome": {
                "batches_attempted": batches_attempted,
                "batches_succeeded": batches_succeeded,
                "batches_failed": len(parse_errors),
                "guided_json_requested": guided_json_enabled,
                "modes": parse_modes,
                "errors": parse_errors[:6],
            },
            "candidate_count": len(related_rows),
            "timeline_context": {
                "rows_upserted": context_result.get("rows_upserted", 0),
                "temporal_jobs_enqueued": context_result.get("temporal_jobs_enqueued", 0),
                "pending": context_result.get("pending", False),
            },
        }
        trace.raw_response = "\n\n--- batch ---\n\n".join(raw_responses)
        trace.latency_ms = latency_ms
        trace.usage_json = {"batches": usage}
        trace.completed_at = datetime.utcnow()
        await _mark_related_completed(
            session,
            section,
            (
                f"Packed related sweep completed across {len(related_rows)} candidate(s)."
                if not parse_errors
                else f"Packed related sweep completed with parse gaps: {batches_succeeded}/{batches_attempted} batch(es) parsed."
            ),
            updated,
            state="incomplete" if parse_errors else "completed",
        )
        await session.commit()
        return {
            "summary": {
                "section_key": section.section_key,
                "updated": updated,
                "batches_attempted": batches_attempted,
                "batches_succeeded": batches_succeeded,
                "parse_errors": len(parse_errors),
            },
            "usage": usage,
            "raw_responses": raw_responses,
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        trace.status = "failed"
        trace.raw_response = "\n\n--- batch ---\n\n".join(raw_responses) or None
        trace.error_text = str(exc)
        trace.latency_ms = latency_ms
        trace.completed_at = datetime.utcnow()
        await session.commit()
        raise


async def _defer_for_related_cache(
    session: AsyncSession,
    job: AgentJob,
    section: SectionClean,
    *,
    rows: int,
    force_refresh: bool = False,
) -> None:
    """Requeue the LLM job until CPU related-cache inputs are available."""

    now = datetime.utcnow()
    queued = await enqueue_related_cache_jobs(session, [section], priority=22, force=force_refresh or rows == 0)
    job.status = "retry"
    job.attempts = max(0, int(job.attempts or 0) - 1)
    job.locked_by = None
    job.locked_at = None
    job.run_after = now + timedelta(seconds=60)
    job.last_error = "Waiting for current related-cache inputs before LLM related insight."
    job.updated_at = now
    await upsert_processing_state(
        session,
        title_id=section.title_id,
        section_key=section.section_key,
        area="related_agent",
        state="pending",
        expected_count=1,
        pending_count=1,
        detail=f"Waiting for related-cache inputs. Cached rows currently available: {rows}.",
        reason="Related LLM jobs are cache-only; CPU related-cache work must finish first.",
        last_error=job.last_error,
        source="agent_worker_waiting_inputs",
        metadata={"cached_rows": rows, "related_cache_jobs_enqueued": queued, "force_refresh": force_refresh},
        commit=False,
    )
    await session.commit()


def parse_related_agent_response(
    raw_response: str,
    *,
    evidence_char_start: int | None = None,
    evidence_char_end: int | None = None,
) -> list[RelatedInsight]:
    """Parse strict JSON related insight output."""

    insights, _ = parse_related_agent_response_with_mode(
        raw_response,
        evidence_char_start=evidence_char_start,
        evidence_char_end=evidence_char_end,
    )
    return insights


def parse_related_agent_response_with_mode(
    raw_response: str,
    *,
    evidence_char_start: int | None = None,
    evidence_char_end: int | None = None,
    guided_json_used: bool = False,
) -> tuple[list[RelatedInsight], str]:
    """Parse related insights and report whether JSON was clean or repaired."""

    if raw_response.strip().lower() in {"null", "none", ""}:
        return [], "empty"
    try:
        payload = _extract_json_object(raw_response)
        insights = payload.get("insights")
        if not isinstance(insights, list):
            raise ValueError("Related agent response must contain an insights list")
        parse_mode = "guided_json" if guided_json_used else "json_object"
    except Exception:
        insights = _extract_related_insight_objects(raw_response)
        if not insights:
            raise
        parse_mode = "repaired_objects"

    parsed: list[RelatedInsight] = []
    seen: set[str] = set()
    for item in insights:
        if not isinstance(item, dict):
            continue
        title = str(item.get("to_title") or "").strip()
        why = _clean_insight_text(" ".join(str(item.get("why_text") or "").split()), title)
        if not title or not why:
            continue
        if _is_rejectable_insight(why):
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        tags_raw = item.get("reasoning_tags") or []
        tags = [str(tag).strip()[:60] for tag in tags_raw if str(tag).strip()][:8]
        parsed.append(
            RelatedInsight(
                to_title=title,
                why_text=why[:900],
                confidence=_clamp_float(item.get("confidence"), 0.1, 1.0, default=0.7),
                reasoning_tags=tags,
                evidence_char_start=_int_or_default(item.get("evidence_char_start"), evidence_char_start),
                evidence_char_end=_int_or_default(item.get("evidence_char_end"), evidence_char_end),
            )
        )
    return parsed[:40], parse_mode


def _extract_related_insight_objects(raw_response: str) -> list[dict[str, Any]]:
    """Recover individual insight objects from malformed batched JSON."""

    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    seen_spans: set[tuple[int, int]] = set()
    for match in re.finditer(r"\{", raw_response):
        start = match.start()
        try:
            value, end_offset = decoder.raw_decode(raw_response[start:])
        except json.JSONDecodeError:
            continue
        end = start + end_offset
        if (start, end) in seen_spans:
            continue
        seen_spans.add((start, end))
        if not isinstance(value, dict):
            continue
        nested = value.get("insights")
        if isinstance(nested, list):
            objects.extend(item for item in nested if isinstance(item, dict))
        elif "to_title" in value and "why_text" in value:
            objects.append(value)
    return objects


def _target_title_id(payload: dict[str, Any] | None) -> int | None:
    if not payload:
        return None
    value = payload.get("target_title_id")
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_agent_related_section(section: SectionClean) -> bool:
    """Return whether a section has enough text for paragraph-comparison insight."""

    text = " ".join((section.clean_text or "").split())
    if not is_content_section(section) or len(text) < 120:
        return False
    if section.links_json:
        return True
    return _section_value_score(section) >= 0.22


async def _persist_insights(
    session: AsyncSession,
    related_rows: list[RelatedCache],
    insights: list[RelatedInsight],
) -> int:
    by_title = {row.to_title.lower(): row for row in related_rows}
    updated = 0
    for insight in insights:
        row = by_title.get(insight.to_title.lower())
        if row is None:
            continue
        signals = dict(row.signals_json or {})
        signals[SOURCE] = {
            "confidence": insight.confidence,
            "reasoning_tags": insight.reasoning_tags,
            "evidence_char_start": insight.evidence_char_start,
            "evidence_char_end": insight.evidence_char_end,
        }
        signals["gates"] = _agent_backed_gates(row, signals)
        row.why_text = insight.why_text
        row.why_source = SOURCE
        row.signals_json = signals
        row.model_version = MODEL_VERSION
        row.updated_at = datetime.utcnow()
        updated += 1
    return updated


def _agent_backed_gates(row: RelatedCache, signals: dict[str, Any]) -> dict[str, Any]:
    """Recompute gates after an agent insight promotes a candidate."""

    existing = signals.get("gates") or {}
    components = signals.get("components") or {}
    if components:
        return relatedness_gates(
            level=int(row.level),
            score=float(row.score),
            why_source=SOURCE,
            components=components,
            source_entity_count=int(signals.get("source_entity_count") or 0),
            candidate_entity_count=int(signals.get("candidate_entity_count") or 0),
            source_time_count=int(signals.get("source_time_count") or 0),
            candidate_time_count=int(signals.get("candidate_time_count") or 0),
            agent_signal=signals.get(SOURCE) or {},
        )

    promoted = dict(existing)
    promoted["accepted"] = bool(existing.get("accepted", row.level in {1, 2}))
    promoted["agent_eligible"] = True
    promoted["timeline_eligible"] = promoted["accepted"]
    promoted["level"] = int(row.level)
    promoted["score"] = round(float(row.score), 4)
    return promoted


def _clear_stale_agent_insights(related_rows: list[RelatedCache]) -> None:
    """Prevent old graph-only agent text from being displayed as current insight."""

    for row in related_rows:
        if row.why_source == SOURCE and row.model_version != MODEL_VERSION:
            row.why_source = "template"


async def _promote_timeline_context(session: AsyncSession, section: SectionClean) -> dict[str, Any]:
    """Advance timeline context after a related-agent section pass."""

    try:
        from app.timeline.context_service import TimelineContextService

        result = await TimelineContextService(session).build_for_article(
            [section],
            section_limit=1,
            related_limit=120,
        )
        return {
            "rows_upserted": result.rows_upserted,
            "temporal_jobs_enqueued": result.temporal_jobs_enqueued,
            "pending": result.pending,
        }
    except Exception as exc:  # noqa: BLE001 - related insight success should not be lost to context promotion.
        return {
            "rows_upserted": 0,
            "temporal_jobs_enqueued": 0,
            "pending": True,
            "error": str(exc)[:220],
        }


def _clean_insight_text(text: str, title: str | None = None) -> str:
    """Keep agent insight wording crisp and avoid boilerplate wrappers."""

    cleaned = re.sub(
        r"^(this\s+(article|candidate|item)\s+is\s+)?(relevant|useful)\s+because\s+",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    ).strip()
    if title and cleaned:
        if cleaned.lower().startswith(f"{title.lower()} is "):
            cleaned = f"{title}: {cleaned[len(title) + 4:]}"
        elif cleaned.lower().startswith(f"{title.lower()} are "):
            cleaned = f"{title}: {cleaned[len(title) + 5:]}"
        elif not cleaned.lower().startswith(title.lower()):
            cleaned = f"{title}: {cleaned[0].lower()}{cleaned[1:]}"
    return cleaned


def _is_rejectable_insight(text: str) -> bool:
    """Drop model outputs that describe non-relevance instead of insight."""

    lowered = text.lower()
    reject_phrases = (
        "unrelated topic",
        "is unrelated",
        "not related",
        "no direct relation",
        "no clear relation",
        "does not fit",
        "doesn't fit",
        "does not relate",
        "doesn't relate",
        "but does not",
        "mentioned alongside",
        "merely mentioned",
        "only mentioned",
        "appears only as",
        "lacks a concrete connection",
        "lacks concrete connection",
        "not a concept tied",
        "not tied to",
        "but the section discusses",
    )
    return any(phrase in lowered for phrase in reject_phrases)


async def _load_section(session: AsyncSession, section_key: str) -> SectionClean | None:
    result = await session.execute(select(SectionClean).where(SectionClean.section_key == section_key))
    return result.scalar_one_or_none()


async def _candidate_context(
    session: AsyncSession,
    related_rows: list[RelatedCache],
) -> dict[str, dict[str, str]]:
    """Load concise candidate snippets so the LLM has more than graph signals."""

    title_ids = [row.to_title_id for row in related_rows]
    if not title_ids:
        return {}

    result = await session.execute(
        select(SectionClean.title_id, SectionClean.title, SectionClean.heading, SectionClean.clean_text)
        .where(SectionClean.title_id.in_(title_ids))
        .order_by(SectionClean.title_id.asc(), SectionClean.heading_id.asc())
    )

    context: dict[str, dict[str, str]] = {}
    for title_id, title, heading, clean_text in result.all():
        if title in context:
            continue
        snippet = " ".join((clean_text or "").split())
        if len(snippet) > 150:
            snippet = snippet[:150].rsplit(" ", 1)[0]
        context[str(title_id)] = {
            "title": str(title),
            "heading": str(heading or ""),
            "snippet": snippet,
        }
    return context


def _select_prompt_rows(section_text: str, related_rows: list[RelatedCache]) -> list[RelatedCache]:
    """Prioritize candidates explicitly evidenced in the clicked section."""

    evidence_rows = [row for row in related_rows if _section_evidence(section_text, row.to_title)]
    if evidence_rows:
        selected = sorted(evidence_rows, key=lambda row: (-row.score, row.level, row.to_title.lower()))
        selected_ids = {row.to_title_id for row in selected}
        fallback = [
            row
            for row in sorted(related_rows, key=lambda row: (-row.score, row.level, row.to_title.lower()))
            if row.to_title_id not in selected_ids
        ]
        return (selected + fallback)[:6]

    return sorted(
        related_rows,
        key=lambda row: (
            0 if _section_evidence(section_text, row.to_title) else 1,
            -row.score,
            row.level,
            row.to_title.lower(),
        ),
    )[:6]


def _prompt_batches(
    section_text: str,
    related_rows: list[RelatedCache],
    *,
    target_title_id: int | None = None,
    batch_size: int | None = None,
) -> list[list[RelatedCache]]:
    """Split all candidates into prompt-sized batches, with strongest evidence first."""

    if target_title_id is not None:
        return [related_rows[:1]] if related_rows else []

    if batch_size is None:
        text_len = len(section_text or "")
        batch_size = 6 if text_len < 900 else 4 if text_len < 1800 else 3

    ordered = _select_prompt_rows(section_text, related_rows)
    selected_ids = {row.to_title_id for row in ordered}
    ordered.extend(
        row
        for row in sorted(related_rows, key=lambda row: (-row.score, row.level, row.to_title.lower()))
        if row.to_title_id not in selected_ids
    )
    return [ordered[index : index + batch_size] for index in range(0, len(ordered), batch_size)]


def _build_related_prompt(
    section: SectionClean,
    related_rows: list[RelatedCache],
    candidate_context: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    section_text, excerpt_start, excerpt_end = _section_prompt_excerpt(section.clean_text, related_rows)

    candidates = []
    mandatory_titles: list[str] = []
    for row in related_rows:
        section_evidence = _section_evidence(section_text, row.to_title)
        if section_evidence:
            mandatory_titles.append(row.to_title)
        candidates.append(
            {
                "to_title": row.to_title,
                "level": row.level,
                "score": row.score,
                "via_title": (row.signals_json or {}).get("via_title"),
                "direct_link": bool((row.signals_json or {}).get("direct_link")),
                "section_evidence": section_evidence,
                "mandatory": bool(section_evidence),
                "candidate_context": (candidate_context or {}).get(str(row.to_title_id), {}),
            }
        )

    return [
        {
            "role": "system",
            "content": (
                "You enrich encyclopedia related-information candidates. "
                "Return only valid JSON. Do not use markdown. "
                "Use only the selected section, candidate snippets, and candidate signals. "
                "Insights are not link labels. Produce concise comparative notes when the selected section "
                "and candidate snippet reveal a concrete historical, geographic, political, "
                "biographical, institutional, or temporal connection. Do not produce generic explanations "
                "such as 'it is linked', 'nearby in the graph', or 'this article discusses X'. "
                "Use section_evidence as the primary anchor when present. "
                "Candidates with section_evidence are usually more important than higher-scoring generic places. "
                "Include enough candidate context to say who or what the candidate is, but do not drift "
                "into generic biography or location unless the selected section makes that connection. "
                "Every why_text must start with the candidate topic, then explain the section relation."
            ),
        },
        {
            "role": "user",
            "content": (
                "Rewrite L1/L2 related insights for the selected section.\n"
                "Return this JSON shape exactly:\n"
                '{"insights":[{"to_title":"Article title","why_text":"One concise explanation.",'
                '"confidence":0.8,"reasoning_tags":["direct_link","shared_time"],'
                '"evidence_char_start":123,"evidence_char_end":456}]}\n'
                "Use the exact to_title values from candidates. Keep why_text under 45 words. "
                "Use evidence_char_start/evidence_char_end from the selected section excerpt span when a specific "
                "evidence sentence is used; otherwise repeat the provided section excerpt char_start/char_end. "
                "Return exactly one insight for every candidate in Candidates JSON, unless the candidate has no usable "
                "candidate_context and no section_evidence. Use lower confidence for weak connections. "
                "If a candidate is a publisher, software company, university, animal species, or incidental L2 branch "
                "without a concrete section-level bridge, omit it. Never write that it is unrelated. "
                "Write crisp insight fragments in this pattern: '<Candidate topic>: <what it is>, and <why it matters in this section>'. "
                "Start with the exact candidate title or a natural expansion of it. "
                "Do not start with a section character, setting, or event unless that is the candidate title. "
                "Avoid vague endings like 'key element of the plot', 'central element', 'related to the context', "
                "'connecting to the themes', 'part of the focus', or 'is mentioned'. "
                "Make the bridge concrete and thematic. "
                "Do not start with 'This is relevant because', "
                "'This article is useful because', or similar boilerplate. "
                "Do not include a candidate merely because it is L1, L2, directly linked, or high scoring. "
                "Each why_text should combine candidate identity/context with the section-specific reason. "
                "When the section explicitly names a person or organization, explain their role in the paragraph's "
                "theme or argument. Avoid weak statements like 'related through Calcutta' when the section gives "
                "a stronger reason. Example style: 'Mother Teresa and the Missionaries of Charity sit inside the "
                "paragraph's contrast between slum suffering, religion, and dignity, not just its Kolkata setting.'\n\n"
                "Bad: 'Hasari Pal, a rickshaw puller, is central to the narrative.'\n"
                "Good: 'Pulled rickshaw: the human-powered vehicle defines Hasari Pal's bone-breaking labor, turning the book's poverty theme into bodily exhaustion.'\n"
                "Bad: 'Father Stephan joins a religious order.'\n"
                "Good: 'Missionaries of Charity: Mother Teresa's Catholic order gives the paragraph's religious service theme an institutional form amid slum suffering.'\n\n"
                f"Article: {section.title}\n"
                f"Section heading: {section.heading}\n"
                f"Section excerpt char_start: {excerpt_start}\n"
                f"Section excerpt char_end: {excerpt_end}\n"
                f"Mandatory candidate titles: {json.dumps(mandatory_titles, ensure_ascii=False)}\n"
                f"Section excerpt: {section_text}\n\n"
                f"Candidates JSON:\n{json.dumps(candidates, ensure_ascii=False)}"
            ),
        },
    ]


def _section_prompt_excerpt(section_text: str, related_rows: list[RelatedCache], max_chars: int = 720) -> tuple[str, int, int]:
    """Choose a stable, bounded section excerpt for one candidate batch."""

    raw = section_text or ""
    if len(raw) <= max_chars:
        return " ".join(raw.split()), 0, len(raw)

    lowered = raw.lower()
    positions: list[int] = []
    for row in related_rows:
        title_tokens = [
            token.lower()
            for token in re.findall(r"[A-Za-z0-9]{3,}", row.to_title)
            if token.lower() not in {"the", "and"}
        ]
        for token in title_tokens[:3]:
            index = lowered.find(token)
            if index >= 0:
                positions.append(index)
                break

    anchor = min(positions) if positions else 0
    start = max(0, anchor - max_chars // 3)
    sentence_start = raw.rfind(".", 0, start)
    if sentence_start >= 0 and anchor - sentence_start <= max_chars:
        start = sentence_start + 1
    end = min(len(raw), start + max_chars)
    sentence_end = raw.rfind(".", start, end)
    if sentence_end > start + 220:
        end = sentence_end + 1
    excerpt = " ".join(raw[start:end].split())
    return excerpt, start, end


def _section_evidence(section_text: str, title: str) -> str:
    title_tokens = [token for token in re.findall(r"[A-Za-z0-9]{3,}", title) if token.lower() not in {"the", "and"}]
    if not title_tokens:
        return ""

    lowered = section_text.lower()
    positions = [lowered.find(token.lower()) for token in title_tokens]
    positions = [pos for pos in positions if pos >= 0]
    if not positions:
        return ""

    index = min(positions)
    start = max(0, section_text.rfind(".", 0, index) + 1)
    if start <= 0:
        start = max(0, index - 160)
    end_dot = section_text.find(".", index)
    end = min(len(section_text), end_dot + 1 if end_dot >= 0 else index + 220)
    evidence = " ".join(section_text[start:end].split())
    return evidence[:420]


def _extract_json_object(raw_response: str) -> dict[str, Any]:
    return extract_json_object(raw_response)


def _int_or_default(value: Any, default: int | None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_float(value: Any, minimum: float, maximum: float, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))
