"""Article-level L1 core digest worker."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import AgentJob, AgentTrace, ArticleCore, SectionClean
from app.content_filters import is_content_section
from app.llm.json_utils import extract_json_object
from app.llm.openai_compatible import LocalLLMClient
from app.orchestration.state import upsert_processing_state

JOB_TYPE = "core_digest_v1"
MODEL_VERSION = "core-digest-v1"
CORE_DIGEST_GUIDED_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "topic": {"type": "object"},
        "key_entities": {
            "type": "array",
            "items": {"type": "object"},
        },
        "dated_spine": {
            "type": "array",
            "items": {"type": "object"},
        },
    },
    "required": ["summary", "topic", "key_entities", "dated_spine"],
}


@dataclass(frozen=True)
class ArticleCorePayload:
    summary: str
    topic: dict[str, Any]
    entities: list[dict[str, Any]]
    dated_spine: list[dict[str, Any]]


async def enqueue_core_digest_job(
    session: AsyncSession,
    *,
    title_id: int,
    title: str,
    sections: list[SectionClean],
    priority: int = 30,
    force: bool = False,
) -> int:
    """Create or refresh the article-level L1 digest job."""

    useful_sections = [section for section in sections if is_content_section(section)]
    if not useful_sections:
        return 0

    article_job_key = f"article:{title_id}"
    stmt = insert(AgentJob).values(
        {
            "job_type": JOB_TYPE,
            "status": "pending",
            "priority": priority,
            "title_id": title_id,
            "section_key": article_job_key,
            "payload_json": {
                "title": title,
                "title_id": title_id,
                "section_count": len(useful_sections),
                "section_keys": [section.section_key for section in useful_sections[:24]],
            },
            "attempts": 0,
            "max_attempts": 3,
            "locked_by": None,
            "locked_at": None,
            "last_error": None,
            "completed_at": None,
        }
    )
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
    await upsert_processing_state(
        session,
        title_id=title_id,
        section_key="",
        area="core_digest",
        state="pending",
        expected_count=1,
        pending_count=1,
        detail="L1 article core digest is queued.",
        reason="Article load/refresh requested a usable core object.",
        source="core_digest_enqueue",
        commit=False,
    )
    await session.commit()
    return result.rowcount or 0


async def process_core_digest_job(
    session: AsyncSession,
    settings: Settings,
    llm: LocalLLMClient,
    job: AgentJob,
) -> None:
    """Build and persist a bounded L1 article core object."""

    sections = await _load_article_sections(session, job.title_id)
    useful_sections = [section for section in sections if is_content_section(section)]
    if not useful_sections:
        raise ValueError(f"No content sections cached for title_id={job.title_id}")

    digest = _build_cpu_digest(useful_sections)
    run_id = f"{JOB_TYPE}:{job.id}:{uuid.uuid4()}"
    messages = _build_core_prompt(job.payload_json.get("title") or useful_sections[0].title, digest)
    trace = AgentTrace(
        run_id=run_id,
        step_name=JOB_TYPE,
        model_name=settings.llm_model,
        status="running",
        input_json={
            "title_id": job.title_id,
            "title": job.payload_json.get("title"),
            "section_keys": [section.section_key for section in useful_sections[:24]],
            "digest": digest,
        },
    )
    session.add(trace)
    await session.commit()

    started = time.perf_counter()
    raw_response = ""
    try:
        guided_json_enabled = bool(getattr(settings, "llm_guided_json_enabled", False))
        response = await llm.chat_completion(
            messages,
            temperature=0.0,
            max_tokens=700,
            response_format={"type": "json_object"},
            guided_json=CORE_DIGEST_GUIDED_JSON_SCHEMA if guided_json_enabled else None,
            timeout_seconds=max(settings.llm_timeout_seconds, 180),
        )
        raw_response = response["choices"][0]["message"]["content"]
        payload, parse_mode = parse_core_digest_response_with_mode(
            raw_response,
            guided_json_used=guided_json_enabled,
        )
        await _persist_core(session, useful_sections, payload, run_id)

        latency_ms = int((time.perf_counter() - started) * 1000)
        trace.status = "succeeded"
        trace.output_json = {
            "parse_mode": parse_mode,
            "guided_json_requested": guided_json_enabled,
            "summary": payload.summary,
            "entity_count": len(payload.entities),
            "dated_spine_count": len(payload.dated_spine),
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
            session,
            title_id=job.title_id,
            section_key="",
            area="core_digest",
            state="completed",
            expected_count=1,
            completed_count=1,
            detail="L1 article core digest is available.",
            reason="Core digest worker succeeded.",
            source="core_digest_worker",
            metadata={"entity_count": len(payload.entities), "dated_spine_count": len(payload.dated_spine)},
            commit=False,
        )
        await session.commit()
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        trace.status = "failed"
        trace.raw_response = raw_response or None
        trace.error_text = str(exc)
        trace.latency_ms = latency_ms
        trace.completed_at = datetime.utcnow()
        await session.commit()
        raise


def parse_core_digest_response(raw_response: str) -> ArticleCorePayload:
    """Parse the strict JSON shape returned by the core digest agent."""

    payload, _ = parse_core_digest_response_with_mode(raw_response)
    return payload


def parse_core_digest_response_with_mode(
    raw_response: str,
    *,
    guided_json_used: bool = False,
) -> tuple[ArticleCorePayload, str]:
    """Parse the core digest response and report JSON handling mode."""

    payload = _extract_json_object(raw_response)
    summary = " ".join(str(payload.get("summary") or "").split())[:900]
    topic_raw = payload.get("topic") if isinstance(payload.get("topic"), dict) else {}
    entities_raw = payload.get("key_entities") or payload.get("entities") or []
    spine_raw = payload.get("dated_spine") or []
    entities = [item for item in entities_raw if isinstance(item, dict)][:24] if isinstance(entities_raw, list) else []
    spine = [item for item in spine_raw if isinstance(item, dict)][:20] if isinstance(spine_raw, list) else []
    if not summary:
        raise ValueError("Core digest response did not include a summary")
    return ArticleCorePayload(
        summary=summary,
        topic=topic_raw,
        entities=entities,
        dated_spine=spine,
    ), "guided_json" if guided_json_used else "json_object"


async def _load_article_sections(session: AsyncSession, title_id: int) -> list[SectionClean]:
    result = await session.execute(
        select(SectionClean)
        .where(SectionClean.title_id == title_id)
        .order_by(SectionClean.heading_id.asc())
    )
    return list(result.scalars().all())


def _build_cpu_digest(sections: list[SectionClean]) -> dict[str, Any]:
    lead = sections[0]
    selected = [lead]
    selected.extend(section for section in sections[1:] if len(section.clean_text or "") >= 180)
    selected = selected[:7]

    section_summaries: list[dict[str, Any]] = []
    for section in selected:
        text = " ".join((section.clean_text or "").split())
        sentences = _sentences(text)
        high_signal = _high_signal_sentences(sentences)
        section_summaries.append(
            {
                "section_key": section.section_key,
                "heading": section.heading,
                "level": section.level,
                "text": text[:640],
                "high_signal_sentences": high_signal[:4],
            }
        )

    return {
        "lead": {
            "section_key": lead.section_key,
            "heading": lead.heading,
            "text": " ".join((lead.clean_text or "").split())[:950],
        },
        "headings": [section.heading for section in sections[:22] if section.heading],
        "sections": section_summaries,
    }


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if len(part.strip()) > 30]


def _high_signal_sentences(sentences: list[str]) -> list[str]:
    signal_pattern = re.compile(
        r"\b(was|were|became|founded|born|died|war|empire|king|queen|president|revolution|century|bce|ce|\d{3,4})\b",
        re.IGNORECASE,
    )
    ranked = sorted(
        sentences,
        key=lambda sentence: (
            0 if signal_pattern.search(sentence) else 1,
            abs(len(sentence) - 180),
        ),
    )
    return [sentence[:320] for sentence in ranked]


def _build_core_prompt(title: str, digest: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You create the L1 usable core for an encyclopedia timeline reader. "
                "Return only valid JSON. Use only the provided digest. Do not invent facts. "
                "Prefer grounded, article-level structure over exhaustive detail."
            ),
        },
        {
            "role": "user",
            "content": (
                "Create a compact article core object.\n"
                "JSON schema:\n"
                "{"
                "\"summary\":\"one grounded 1-2 sentence article summary\","
                "\"topic\":{\"primary\":\"short topic\",\"domains\":[\"domain\"],\"time_scope\":\"short string\"},"
                "\"key_entities\":[{\"name\":\"entity\",\"role\":\"why it matters\",\"type\":\"person/place/group/event/work\"}],"
                "\"dated_spine\":[{\"label\":\"date/year/period\",\"event\":\"grounded event\",\"section_key\":\"source section key\"}]"
                "}\n"
                "Keep summary under 70 words, each entity role under 20 words, and dated_spine to the most important anchors.\n\n"
                f"Article title: {title}\n"
                f"Digest JSON:\n{json.dumps(digest, ensure_ascii=False)}"
            ),
        },
    ]


async def _persist_core(
    session: AsyncSession,
    sections: list[SectionClean],
    payload: ArticleCorePayload,
    run_id: str,
) -> None:
    title = sections[0].title
    title_id = sections[0].title_id
    section_keys = [section.section_key for section in sections[:24]]
    stmt = insert(ArticleCore).values(
        {
            "title_id": title_id,
            "title": title,
            "summary": payload.summary,
            "topic_json": payload.topic,
            "entities_json": payload.entities,
            "dated_spine_json": payload.dated_spine,
            "source_section_keys_json": section_keys,
            "provenance_json": {
                "run_id": run_id,
                "source": JOB_TYPE,
                "source_section_keys": section_keys,
            },
            "model_version": MODEL_VERSION,
        }
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_article_core_title_model",
        set_={
            "title": stmt.excluded.title,
            "summary": stmt.excluded.summary,
            "topic_json": stmt.excluded.topic_json,
            "entities_json": stmt.excluded.entities_json,
            "dated_spine_json": stmt.excluded.dated_spine_json,
            "source_section_keys_json": stmt.excluded.source_section_keys_json,
            "provenance_json": stmt.excluded.provenance_json,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


def _extract_json_object(raw_response: str) -> dict[str, Any]:
    return extract_json_object(raw_response)
