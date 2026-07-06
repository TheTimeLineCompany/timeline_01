"""Agent-based temporal extraction worker."""

from __future__ import annotations

import asyncio
import json
import random
import re
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.content_filters import is_content_section
from app.core.config import Settings, get_settings
from app.core.provenance import make_provenance
from app.db.models import AgentJob, AgentTrace, SectionClean, SectionTime, TimeDimension
from app.llm.json_utils import extract_json_object
from app.llm.openai_compatible import LocalLLMClient
from app.orchestration.state import upsert_processing_state
from app.ontology.temporal_projection import TemporalProjectionService

JOB_TYPE = "temporal_extract_v1"
SOURCE = "agent_temporal_v1"
MODEL_VERSION = "agent-temporal-v1"
SUPPORTED_JOB_TYPES = {
    JOB_TYPE,
    "related_l1_l2_explain_v1",
    "section_insight_v1",
    "related_sweep_pack_v1",
    "timeline_context_promote_v1",
    "core_digest_v1",
    "embedding_generate_v1",
    "cpu_entity_precision_v1",
    "graph_frontier_discover_v1",
    "related_cache_build_v1",
}
LLM_JOB_TYPES = {
    JOB_TYPE,
    "related_l1_l2_explain_v1",
    "section_insight_v1",
    "related_sweep_pack_v1",
    "core_digest_v1",
}
CPU_SAFE_JOB_TYPES = {
    "graph_frontier_discover_v1",
    "embedding_generate_v1",
    "cpu_entity_precision_v1",
    "timeline_context_promote_v1",
    "related_cache_build_v1",
}
TEMPORAL_AGENT_GUIDED_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "time_kind": {"type": "string"},
                    "precision": {"type": "string"},
                    "year": {"type": ["integer", "null"]},
                    "month": {"type": ["integer", "null"]},
                    "day": {"type": ["integer", "null"]},
                    "season": {"type": ["string", "null"]},
                    "start_date": {"type": ["string", "null"]},
                    "end_date": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": ["label", "time_kind", "precision", "confidence", "evidence"],
            },
        }
    },
    "required": ["events"],
}


@dataclass(frozen=True)
class AgentTemporalFact:
    """Validated temporal fact returned by the LLM."""

    time_ref_id: str
    time_kind: str
    label: str
    precision: str
    start_date: str | None
    end_date: str | None
    year: int | None
    month: int | None
    day: int | None
    season: str | None
    confidence: float
    evidence: str
    metadata_json: dict[str, Any]


async def enqueue_temporal_jobs(
    session: AsyncSession,
    sections: list[SectionClean],
    *,
    priority: int = 100,
    force: bool = False,
) -> int:
    """Create or refresh temporal agent jobs for cached sections."""

    if not sections:
        return 0

    useful_sections = [section for section in sections if is_content_section(section)]
    if not force:
        useful_sections = await _llm_temporal_sections_with_budget(session, useful_sections)
    values = [
        {
            "job_type": JOB_TYPE,
            "status": "pending",
            "priority": priority,
            "title_id": section.title_id,
            "section_key": section.section_key,
            "payload_json": {
                "title": section.title,
                "heading": section.heading,
                "heading_id": section.heading_id,
            },
            "attempts": 0,
            "max_attempts": 3,
            "locked_by": None,
            "locked_at": None,
            "last_error": None,
            "completed_at": None,
        }
        for section in useful_sections
    ]
    if not values:
        await session.commit()
        return 0
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
            where=AgentJob.status == "failed",
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
            area="temporal",
            state=state,
            expected_count=1,
            completed_count=1 if state == "completed" else 0,
            pending_count=1 if state == "pending" else 0,
            running_count=1 if state == "running" else 0,
            failed_count=1 if state == "attention" else 0,
            detail=f"Temporal agent job is {status}.",
            reason="Synchronized from durable job state after enqueue.",
            last_error=last_error,
            source="agent_enqueue",
            commit=False,
        )
    await session.commit()
    return result.rowcount or 0


DETERMINISTIC_TEMPORAL_SOURCES = {"rule_based_seed"}

TEMPORALISH_PATTERN = re.compile(
    r"\b(?:"
    r"\d{3,4}\s*(?:BC|BCE|AD|CE)?|"
    r"(?:BC|BCE|AD|CE)|"
    r"centur(?:y|ies)|decade|era|period|dynasty|reign|"
    r"before|after|during|between|from|until|since|"
    r"spring|summer|fall|autumn|winter|"
    r"million years ago|billion years ago"
    r")\b",
    flags=re.IGNORECASE,
)


async def _llm_temporal_sections_with_budget(
    session: AsyncSession,
    sections: list[SectionClean],
) -> list[SectionClean]:
    """Return only sections worth spending temporal LLM budget on."""

    if not sections:
        return []
    section_keys = [section.section_key for section in sections]
    covered_result = await session.execute(
        select(SectionTime.section_key, func.count())
        .where(SectionTime.section_key.in_(section_keys))
        .where(SectionTime.source.in_(sorted(DETERMINISTIC_TEMPORAL_SOURCES)))
        .group_by(SectionTime.section_key)
    )
    deterministic_counts = {str(section_key): int(count or 0) for section_key, count in covered_result.all()}
    candidates: list[SectionClean] = []
    deterministic_covered = 0
    for section in sections:
        if deterministic_counts.get(section.section_key, 0) > 0:
            deterministic_covered += 1
            await _mark_temporal_deterministic(session, section, deterministic_counts[section.section_key])
            continue
        if _is_temporalish(section.clean_text or ""):
            candidates.append(section)
        else:
            await _mark_temporal_no_signal(session, section)

    ordered = sorted(candidates, key=_temporal_section_value_score, reverse=True)
    settings = get_settings()
    budget = max(0, int(settings.llm_temporal_budget_per_article))
    selected = ordered[:budget] if budget else []
    overflow = ordered[budget:] if budget else ordered
    for section in overflow:
        await _mark_temporal_budget_exhausted(session, section, budget=budget)
    if sections:
        title_id = sections[0].title_id
        await upsert_processing_state(
            session,
            title_id=title_id,
            section_key="",
            area="temporal_budget",
            state="completed" if not overflow else "budget_exhausted",
            expected_count=len(sections),
            completed_count=deterministic_covered + len(selected),
            pending_count=len(selected),
            detail=(
                f"Temporal LLM budget selected {len(selected)} section(s); "
                f"{deterministic_covered} deterministic-covered; {len(overflow)} over budget."
            ),
            reason="LLM temporal jobs are gated by deterministic coverage and per-article budget.",
            source="temporal_budget_gate",
            metadata={
                "budget": budget,
                "sections_considered": len(sections),
                "deterministic_covered": deterministic_covered,
                "llm_selected": len(selected),
                "budget_exhausted": len(overflow),
            },
            commit=False,
        )
    return selected


def _is_temporalish(text: str) -> bool:
    return bool(TEMPORALISH_PATTERN.search(text or ""))


def _temporal_section_value_score(section: SectionClean) -> float:
    text = " ".join((section.clean_text or "").split())
    length_score = min(1.0, len(text) / 1600)
    explicit_hits = len(TEMPORALISH_PATTERN.findall(text))
    temporal_score = min(1.0, explicit_hits / 8)
    link_score = min(1.0, len(section.links_json or []) / 12)
    heading = (section.heading or "").strip().lower()
    heading_score = 0.35 if heading in {"lead", "introduction", "overview", "history", "background"} else 0.0
    return round(0.42 * temporal_score + 0.25 * length_score + 0.20 * link_score + 0.13 * heading_score, 4)


async def _mark_temporal_deterministic(session: AsyncSession, section: SectionClean, count: int) -> None:
    await upsert_processing_state(
        session,
        title_id=section.title_id,
        section_key=section.section_key,
        area="temporal",
        state="completed",
        expected_count=1,
        completed_count=1,
        detail=f"Deterministic temporal coverage available: {count} row(s).",
        reason="Skipped temporal LLM spend because rule-based seed coverage already exists.",
        source="temporal_budget_gate",
        metadata={"deterministic_rows": count, "llm_skipped": True},
        commit=False,
    )


async def _mark_temporal_no_signal(session: AsyncSession, section: SectionClean) -> None:
    await upsert_processing_state(
        session,
        title_id=section.title_id,
        section_key=section.section_key,
        area="temporal",
        state="completed",
        expected_count=1,
        completed_count=1,
        detail="No temporal signal found for LLM adjudication.",
        reason="Skipped temporal LLM spend because the section has no temporal-ish text.",
        source="temporal_budget_gate",
        metadata={"llm_skipped": True, "skip_reason": "no_temporal_signal"},
        commit=False,
    )


async def _mark_temporal_budget_exhausted(session: AsyncSession, section: SectionClean, *, budget: int) -> None:
    await upsert_processing_state(
        session,
        title_id=section.title_id,
        section_key=section.section_key,
        area="temporal",
        state="budget_exhausted",
        expected_count=1,
        completed_count=0,
        detail=f"Temporal LLM budget exhausted for this article. Budget: {budget}.",
        reason="Section is eligible, but the per-article temporal LLM budget was spent on higher-value sections.",
        source="temporal_budget_gate",
        metadata={"budget": budget, "llm_skipped": True, "skip_reason": "budget_exhausted"},
        commit=False,
    )


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


def _job_type_area(job_type: str) -> str:
    if job_type == JOB_TYPE:
        return "temporal"
    if job_type == "timeline_context_promote_v1":
        return "timeline_context"
    if job_type == "core_digest_v1":
        return "core_digest"
    if job_type == "embedding_generate_v1":
        return "embeddings"
    if job_type == "cpu_entity_precision_v1":
        return "cpu_entities"
    if job_type == "graph_frontier_discover_v1":
        return "graph_frontier"
    if job_type == "related_cache_build_v1":
        return "related_cache"
    return "related_agent"


class TemporalAgentWorker:
    """Poll durable jobs and run temporal extraction through local vLLM."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        *,
        core_only: bool = False,
        lane: str = "full",
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.llm = LocalLLMClient(self.settings)
        self.worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        normalized_lane = "cpu" if core_only else (lane or "full").strip().lower()
        if normalized_lane == "cpu":
            self.supported_job_types = CPU_SAFE_JOB_TYPES
        elif normalized_lane == "llm":
            self.supported_job_types = LLM_JOB_TYPES
        elif normalized_lane == "full":
            self.supported_job_types = SUPPORTED_JOB_TYPES
        else:
            raise ValueError(f"Unsupported worker lane: {lane}")
        self.lane = normalized_lane

    async def run(
        self,
        *,
        limit: int = 0,
        poll_seconds: float = 2.0,
        idle_exit: bool = False,
    ) -> dict[str, int]:
        """Run jobs until limit is reached or forever."""

        processed = 0
        succeeded = 0
        failed = 0

        while limit <= 0 or processed < limit:
            job = await self.claim_next_job()
            if job is None:
                if idle_exit:
                    break
                await asyncio.sleep(poll_seconds)
                continue

            processed += 1
            try:
                await self.process_job(job)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001 - worker must keep polling after failures.
                failed += 1
                await self.mark_failed(job, exc)

        return {"processed": processed, "succeeded": succeeded, "failed": failed}

    async def claim_next_job(self) -> AgentJob | None:
        """Atomically claim the next pending/retryable agent job."""

        now = datetime.utcnow()
        stale_before = now - timedelta(minutes=15)
        async with self.session.begin():
            result = await self.session.execute(
                select(AgentJob)
                .where(
                    AgentJob.job_type.in_(self.supported_job_types),
                    or_(
                        AgentJob.status.in_(["pending", "retry"]),
                        (
                            (AgentJob.status == "running")
                            & (AgentJob.locked_at.is_not(None))
                            & (AgentJob.locked_at < stale_before)
                        ),
                    ),
                    AgentJob.attempts < AgentJob.max_attempts,
                    or_(AgentJob.run_after.is_(None), AgentJob.run_after <= now),
                )
                .order_by(AgentJob.priority.asc(), AgentJob.created_at.asc(), AgentJob.id.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            job = result.scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.attempts += 1
            job.locked_by = self.worker_id
            job.locked_at = now
            job.updated_at = now
            area = _job_type_area(job.job_type)
            await upsert_processing_state(
                self.session,
                title_id=job.title_id,
                section_key=job.section_key,
                area=area,
                state="running",
                expected_count=1,
                running_count=1,
                detail=f"{job.job_type} job claimed by {self.worker_id}.",
                reason="Worker is processing this section.",
                source="agent_worker",
                commit=False,
            )
            return job

    async def process_job(self, job: AgentJob) -> None:
        """Run one agent job and persist results."""

        if job.job_type != JOB_TYPE:
            from app.workers.core_digest import JOB_TYPE as CORE_DIGEST_JOB_TYPE
            from app.workers.core_digest import process_core_digest_job
            from app.workers.cpu_entities import JOB_TYPE as CPU_ENTITY_JOB_TYPE
            from app.workers.cpu_entities import process_cpu_entity_job
            from app.workers.embeddings import JOB_TYPE as EMBEDDING_JOB_TYPE
            from app.workers.embeddings import process_embedding_job
            from app.workers.graph_frontier import JOB_TYPE as GRAPH_FRONTIER_JOB_TYPE
            from app.workers.graph_frontier import process_graph_frontier_job
            from app.workers.related_cache import JOB_TYPE as RELATED_CACHE_JOB_TYPE
            from app.workers.related_cache import process_related_cache_job
            from app.workers.related_agent import FOCUS_JOB_TYPE as RELATED_FOCUS_JOB_TYPE
            from app.workers.related_agent import JOB_TYPE as RELATED_JOB_TYPE
            from app.workers.related_agent import SWEEP_PACK_JOB_TYPE as RELATED_SWEEP_PACK_JOB_TYPE
            from app.workers.related_agent import process_related_job
            from app.workers.timeline_context import JOB_TYPE as TIMELINE_CONTEXT_JOB_TYPE
            from app.workers.timeline_context import process_timeline_context_job

            if job.job_type == CPU_ENTITY_JOB_TYPE:
                await process_cpu_entity_job(self.session, job)
                return
            if job.job_type == GRAPH_FRONTIER_JOB_TYPE:
                await process_graph_frontier_job(self.session, job)
                return
            if job.job_type == RELATED_CACHE_JOB_TYPE:
                await process_related_cache_job(self.session, job)
                return
            if job.job_type == CORE_DIGEST_JOB_TYPE:
                await process_core_digest_job(self.session, self.settings, self.llm, job)
                return
            if job.job_type == EMBEDDING_JOB_TYPE:
                await process_embedding_job(self.session, job)
                return
            if job.job_type in {RELATED_JOB_TYPE, RELATED_FOCUS_JOB_TYPE, RELATED_SWEEP_PACK_JOB_TYPE}:
                await process_related_job(self.session, self.settings, self.llm, job)
                return
            if job.job_type == TIMELINE_CONTEXT_JOB_TYPE:
                await process_timeline_context_job(self.session, job)
                return
            raise ValueError(f"Unsupported agent job type: {job.job_type}")

        section = await self._load_section(job.section_key)
        if section is None:
            raise ValueError(f"Cached section not found: {job.section_key}")

        run_id = f"{JOB_TYPE}:{job.id}:{uuid.uuid4()}"
        messages = _build_temporal_prompt(section)
        trace = AgentTrace(
            run_id=run_id,
            step_name=JOB_TYPE,
            model_name=self.settings.llm_model,
            status="running",
            input_json={
                "section_key": section.section_key,
                "title": section.title,
                "heading": section.heading,
                "messages": messages,
            },
        )
        self.session.add(trace)
        await self.session.commit()

        started = time.perf_counter()
        raw_response = ""
        try:
            guided_json_enabled = bool(getattr(self.settings, "llm_guided_json_enabled", False))
            response = await self.llm.chat_completion(
                messages,
                temperature=0.0,
                max_tokens=900,
                response_format={"type": "json_object"},
                guided_json=TEMPORAL_AGENT_GUIDED_JSON_SCHEMA if guided_json_enabled else None,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            raw_response = response["choices"][0]["message"]["content"]
            facts, parse_mode = parse_temporal_agent_response_with_mode(
                raw_response,
                guided_json_used=guided_json_enabled,
            )
            await self._persist_facts(section, facts, run_id)
            await TemporalProjectionService(self.session).project_sections([section])

            trace.status = "succeeded"
            trace.output_json = {
                "parse_mode": parse_mode,
                "guided_json_requested": guided_json_enabled,
                "facts": [fact.metadata_json | {"time_ref_id": fact.time_ref_id} for fact in facts],
            }
            trace.raw_response = raw_response
            trace.latency_ms = latency_ms
            trace.usage_json = response.get("usage")
            trace.completed_at = datetime.utcnow()
            job.status = "succeeded"
            job.completed_at = datetime.utcnow()
            job.last_error = None
            job.locked_by = None
            job.locked_at = None
            job.run_after = None
            job.updated_at = datetime.utcnow()
            await upsert_processing_state(
                self.session,
                title_id=section.title_id,
                section_key=section.section_key,
                area="temporal",
                state="completed",
                expected_count=1,
                completed_count=1,
                detail=f"Temporal agent completed with {len(facts)} extracted fact(s).",
                reason="Temporal agent job succeeded.",
                source="agent_worker",
                commit=False,
            )
            await self.session.commit()
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            trace.status = "failed"
            trace.raw_response = raw_response or None
            trace.error_text = str(exc)
            trace.latency_ms = latency_ms
            trace.completed_at = datetime.utcnow()
            await self.session.commit()
            raise

    async def mark_failed(self, job: AgentJob, exc: Exception) -> None:
        """Mark a job as retryable or failed."""

        job_id = job.id
        await self.session.rollback()
        result = await self.session.execute(select(AgentJob).where(AgentJob.id == job_id))
        fresh_job = result.scalar_one_or_none()
        if fresh_job is None:
            return
        job = fresh_job
        now = datetime.utcnow()
        error_text = str(exc) or exc.__class__.__name__
        job.last_error = error_text
        job.locked_by = None
        job.locked_at = None
        job.updated_at = now
        if job.attempts >= job.max_attempts:
            job.status = "failed"
            job.completed_at = now
            state = "attention"
        else:
            job.status = "retry"
            backoff_seconds = min(
                300,
                self.settings.job_backoff_base_seconds * (2 ** max(0, job.attempts - 1))
                + random.uniform(0, self.settings.job_backoff_base_seconds),
            )
            job.run_after = now + timedelta(seconds=backoff_seconds)
            state = "pending"
        area = _job_type_area(job.job_type)
        await upsert_processing_state(
            self.session,
            title_id=job.title_id,
            section_key=job.section_key,
            area=area,
            state=state,
            expected_count=1,
            completed_count=0,
            pending_count=1 if state == "pending" else 0,
            failed_count=1 if state == "attention" else 0,
            detail=f"{job.job_type} job did not complete.",
            reason=error_text,
            last_error=error_text,
            source="agent_worker",
            commit=False,
        )
        await self.session.commit()

    async def _load_section(self, section_key: str) -> SectionClean | None:
        result = await self.session.execute(
            select(SectionClean).where(SectionClean.section_key == section_key)
        )
        return result.scalar_one_or_none()

    async def _persist_facts(
        self,
        section: SectionClean,
        facts: list[AgentTemporalFact],
        run_id: str,
    ) -> None:
        supported_facts = [fact for fact in facts if _find_evidence_start(section.clean_text, fact.evidence) >= 0]
        await self.session.execute(
            delete(SectionTime).where(
                SectionTime.section_key == section.section_key,
                SectionTime.source == SOURCE,
            )
        )
        if not supported_facts:
            return

        values = []
        for fact in supported_facts:
            values.append(
                {
                    "time_ref_id": fact.time_ref_id,
                    "time_kind": fact.time_kind,
                    "label": fact.label,
                    "precision": fact.precision,
                    "start_date": fact.start_date,
                    "end_date": fact.end_date,
                    "year": fact.year,
                    "month": fact.month,
                    "day": fact.day,
                    "season": fact.season,
                    "era_name": None,
                    "region_scope": None,
                    "metadata_json": fact.metadata_json,
                    "active": True,
                }
            )
        stmt = insert(TimeDimension).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_v4_time_ref_id",
            set_={
                "label": stmt.excluded.label,
                "precision": stmt.excluded.precision,
                "start_date": stmt.excluded.start_date,
                "end_date": stmt.excluded.end_date,
                "year": stmt.excluded.year,
                "month": stmt.excluded.month,
                "day": stmt.excluded.day,
                "season": stmt.excluded.season,
                "metadata_json": stmt.excluded.metadata_json,
                "active": True,
            },
        )
        await self.session.execute(stmt)

        for fact in supported_facts:
            char_start = _find_evidence_start(section.clean_text, fact.evidence)
            char_end = char_start + len(fact.evidence)
            provenance = make_provenance(
                title_id=section.title_id,
                heading_id=section.heading_id,
                char_start=char_start,
                char_end=char_end,
                parser_version=self.settings.parser_version,
                model_version=MODEL_VERSION,
                run_id=run_id,
            )
            stmt = insert(SectionTime).values(
                {
                    "section_key": section.section_key,
                    "title_id": section.title_id,
                    "heading_id": section.heading_id,
                    "time_ref_id": fact.time_ref_id,
                    "source": SOURCE,
                    "confidence": fact.confidence,
                    "provenance_json": provenance,
                }
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_v4_section_time",
                set_={
                    "source": SOURCE,
                    "confidence": fact.confidence,
                    "provenance_json": provenance,
                },
            )
            await self.session.execute(stmt)


def parse_temporal_agent_response(raw_response: str) -> list[AgentTemporalFact]:
    """Parse and validate strict JSON temporal facts."""

    facts, _ = parse_temporal_agent_response_with_mode(raw_response)
    return facts


def parse_temporal_agent_response_with_mode(
    raw_response: str,
    *,
    guided_json_used: bool = False,
) -> tuple[list[AgentTemporalFact], str]:
    """Parse temporal facts and report whether JSON was clean or repaired."""

    if raw_response.strip().lower() in {"null", "none", ""}:
        return [], "empty"

    try:
        payload = _extract_json_object(raw_response)
        events = payload.get("events")
        if not isinstance(events, list):
            raise ValueError("Temporal agent response must contain an events list")
        parse_mode = "guided_json" if guided_json_used else "json_object"
    except (json.JSONDecodeError, ValueError):
        events = _extract_event_objects(raw_response)
        if not events:
            raise
        parse_mode = "repaired_objects"

    facts: list[AgentTemporalFact] = []
    seen: set[str] = set()
    for item in events:
        if not isinstance(item, dict):
            continue
        fact = _fact_from_item(item)
        if fact is None or fact.time_ref_id in seen:
            continue
        seen.add(fact.time_ref_id)
        facts.append(fact)
    return facts[:24], parse_mode


def _extract_event_objects(raw_response: str) -> list[dict[str, Any]]:
    """Salvage event dictionaries from partially malformed model JSON."""

    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    decoder = json.JSONDecoder()
    events: list[dict[str, Any]] = []
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        obj_events = obj.get("events")
        if isinstance(obj_events, list):
            events.extend(item for item in obj_events if isinstance(item, dict))
        elif "label" in obj and "evidence" in obj:
            events.append(obj)
    return events


def _fact_from_item(item: dict[str, Any]) -> AgentTemporalFact | None:
    label = str(item.get("label") or "").strip()
    precision = str(item.get("precision") or "").strip().lower()
    time_kind = str(item.get("time_kind") or precision or "point").strip().lower()
    evidence = str(item.get("evidence") or "").strip()
    if not label or not evidence:
        return None

    year = _optional_int(item.get("year"))
    month = _optional_int(item.get("month"))
    day = _optional_int(item.get("day"))
    season = str(item.get("season") or "").strip().lower() or None
    start_date = _optional_str(item.get("start_date"))
    end_date = _optional_str(item.get("end_date"))

    normalized = _normalize_fact_dates(time_kind, precision, year, month, day, season, start_date, end_date)
    if normalized is None:
        return None
    time_kind, precision, start_date, end_date, time_ref_id = normalized
    canonical_label = canonical_time_label(time_ref_id, fallback=label)

    confidence = _clamp_float(item.get("confidence"), 0.1, 1.0, default=0.65)
    return AgentTemporalFact(
        time_ref_id=time_ref_id,
        time_kind=time_kind,
        label=canonical_label[:255],
        precision=precision,
        start_date=start_date,
        end_date=end_date,
        year=year,
        month=month,
        day=day,
        season=season,
        confidence=confidence,
        evidence=evidence[:500],
        metadata_json={
            "source_pattern": SOURCE,
            "agent_label": label,
            "evidence": evidence[:500],
            "raw_item": item,
        },
    )


def _normalize_fact_dates(
    time_kind: str,
    precision: str,
    year: int | None,
    month: int | None,
    day: int | None,
    season: str | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, str, str | None, str | None, str] | None:
    if start_date and not _valid_iso_date(start_date):
        start_date = None
    if end_date and not _valid_iso_date(end_date):
        end_date = None

    if year is not None and not 1 <= year <= 2100:
        return None
    if month is not None and not 1 <= month <= 12:
        return None
    if day is not None and not 1 <= day <= 31:
        return None

    if precision == "day" and year and month and day:
        date_s = f"{year:04d}-{month:02d}-{day:02d}"
        return "point", "day", date_s, date_s, f"tp:{date_s}"
    if precision == "month" and year and month:
        start = f"{year:04d}-{month:02d}-01"
        end = _month_end(year, month)
        return "month", "month", start, end, f"ti:month:{year:04d}-{month:02d}"
    if precision == "season" and year and season in {"spring", "summer", "fall", "autumn", "winter"}:
        season_norm = "fall" if season == "autumn" else season
        start, end = _season_bounds(year, season_norm)
        return "season", "season", start, end, f"ti:season:{year:04d}:{season_norm}"
    if precision in {"year", ""} and year:
        return "year", "year", f"{year:04d}-01-01", f"{year:04d}-12-31", f"ti:year:{year:04d}"
    if time_kind == "interval" and start_date and end_date:
        return "interval", precision or "range", start_date, end_date, f"ti:interval:{start_date}:{end_date}"
    return None


def canonical_time_label(time_ref_id: str, *, fallback: str | None = None) -> str:
    """Return a stable display label for a canonical time dimension row."""

    if time_ref_id.startswith("tp:"):
        return _human_iso_label(time_ref_id.removeprefix("tp:"))
    if time_ref_id.startswith("ti:year:"):
        return str(int(time_ref_id.removeprefix("ti:year:")))
    if time_ref_id.startswith("ti:month:"):
        return _human_iso_label(time_ref_id.removeprefix("ti:month:"))
    if time_ref_id.startswith("ti:season:"):
        _, _, year, season = time_ref_id.split(":", 3)
        return f"{season.title()} {year}"
    if time_ref_id.startswith("ti:interval:"):
        value = time_ref_id.removeprefix("ti:interval:")
        start, _, end = value.partition(":")
        return f"{start} to {end}" if end else start
    return str(fallback or time_ref_id)


def _human_iso_label(value: str) -> str:
    parts = value.split("-")
    if not parts:
        return value
    try:
        parts[0] = str(int(parts[0]))
    except ValueError:
        return value
    return "-".join(parts)


def _build_temporal_prompt(section: SectionClean) -> list[dict[str, str]]:
    text = section.clean_text
    if len(text) > 6500:
        text = text[:6500]
    return [
        {
            "role": "system",
            "content": (
                "You extract temporal facts from encyclopedia sections. "
                "Return only valid JSON. Do not include markdown. "
                "Prefer dates explicitly supported by the section text. "
                "Do not infer unrelated world-history dates. "
                "Ignore image-map coordinates, category numbers, citations, and table layout numbers."
            ),
        },
        {
            "role": "user",
            "content": (
                "Extract timeline-worthy dates from this section.\n"
                "If the section has no timeline-worthy dates, return {\"events\":[]}.\n"
                "Return this exact JSON shape:\n"
                '{"events":[{"label":"February 12, 1809","time_kind":"point","precision":"day",'
                '"year":1809,"month":2,"day":12,"season":null,"start_date":"1809-02-12",'
                '"end_date":"1809-02-12","confidence":0.9,"evidence":"short exact quote"}]}\n'
                "Use null for unknown month/day/season. Use precision year/month/day/season/range.\n\n"
                f"Title: {section.title}\n"
                f"Heading: {section.heading}\n"
                f"Section key: {section.section_key}\n\n"
                f"Text:\n{text}"
            ),
        },
    ]


def _extract_json_object(raw_response: str) -> dict[str, Any]:
    return extract_json_object(raw_response)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clamp_float(value: Any, minimum: float, maximum: float, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _valid_iso_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _month_end(year: int, month: int) -> str:
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    end = next_month - timedelta(days=1)
    return end.strftime("%Y-%m-%d")


def _season_bounds(year: int, season: str) -> tuple[str, str]:
    if season == "spring":
        return f"{year:04d}-03-01", f"{year:04d}-05-31"
    if season == "summer":
        return f"{year:04d}-06-01", f"{year:04d}-08-31"
    if season == "fall":
        return f"{year:04d}-09-01", f"{year:04d}-11-30"
    return f"{year:04d}-12-01", f"{year + 1:04d}-02-28"


def _find_evidence_start(text: str, evidence: str) -> int:
    if not evidence:
        return -1
    index = text.find(evidence)
    if index >= 0:
        return index
    return text.lower().find(evidence.lower())
