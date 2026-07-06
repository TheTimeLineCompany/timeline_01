"""Generate sentence-transformer embeddings for cached sections.

Reads un-embedded section_clean rows in batches, encodes them with
all-MiniLM-L6-v2 (384-dim, matches schema), and upserts into
timeline_v4.section_embedding.

Usage:
    python scripts/generate_embeddings.py [--title "Abraham Lincoln"] [--batch 32] [--limit 0]

    --title   Only embed sections for one article (by exact title, case-sensitive).
    --batch   Sentences per encode call (default: 32).
    --limit   Stop after N sections total (0 = all, default: 0).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone

import asyncpg

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


async def _load_settings() -> dict:
    from app.core.config import get_settings
    s = get_settings()
    return {
        "host": s.pg_host,
        "port": s.pg_port,
        "user": s.pg_user,
        "password": s.pg_password or None,
        "database": s.pg_database,
        "schema": s.pg_schema,
        "parser_version": s.parser_version,
    }


async def _fetch_unembedded(conn: asyncpg.Connection, schema: str, title: str | None, limit: int) -> list[asyncpg.Record]:
    where_parts = [f"sc.section_key NOT IN (SELECT section_key FROM {schema}.section_embedding)"]
    args: list = []
    if title:
        args.append(title)
        where_parts.append(f"sc.title = ${len(args)}")
    where_clause = " AND ".join(where_parts)
    limit_clause = f"LIMIT ${len(args) + 1}" if limit > 0 else ""
    if limit > 0:
        args.append(limit)
    sql = f"""
        SELECT sc.section_key, sc.title_id, sc.heading_id,
               sc.clean_text, sc.provenance_json
        FROM {schema}.section_clean sc
        WHERE {where_clause}
        ORDER BY sc.title_id, sc.heading_id
        {limit_clause}
    """
    return await conn.fetch(sql, *args)


async def _upsert_batch(
    conn: asyncpg.Connection,
    schema: str,
    rows: list[asyncpg.Record],
    vectors: list[list[float]],
    model_name: str,
) -> None:
    now = datetime.now(timezone.utc)
    for row, vec in zip(rows, vectors):
        vec_literal = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"
        provenance = row["provenance_json"] if isinstance(row["provenance_json"], str) else json.dumps(row["provenance_json"])
        await conn.execute(
            f"""
            INSERT INTO {schema}.section_embedding
                (section_key, title_id, heading_id, embedding, embedding_model, provenance_json, created_at, updated_at)
            VALUES ($1, $2, $3, $4::vector, $5, $6::jsonb, $7, $7)
            ON CONFLICT (section_key) DO UPDATE
                SET embedding = EXCLUDED.embedding,
                    embedding_model = EXCLUDED.embedding_model,
                    updated_at = EXCLUDED.updated_at
            """,
            row["section_key"],
            row["title_id"],
            row["heading_id"],
            vec_literal,
            model_name,
            provenance,
            now,
        )


async def run(title: str | None, batch_size: int, total_limit: int) -> None:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print("sentence-transformers not installed. Run:")
        print("  pip install sentence-transformers")
        return

    print(f"Loading model {EMBEDDING_MODEL}...")
    model = SentenceTransformer(EMBEDDING_MODEL)

    cfg = await _load_settings()
    schema = cfg["schema"]

    conn = await asyncpg.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
    )

    try:
        rows = await _fetch_unembedded(conn, schema, title, total_limit)
        total = len(rows)
        print(f"Found {total} sections to embed.")
        if not total:
            return

        done = 0
        t0 = time.monotonic()
        for offset in range(0, total, batch_size):
            chunk = rows[offset: offset + batch_size]
            texts = [r["clean_text"] or "" for r in chunk]
            vectors = model.encode(texts, batch_size=batch_size, show_progress_bar=False).tolist()
            await _upsert_batch(conn, schema, chunk, vectors, EMBEDDING_MODEL)
            done += len(chunk)
            elapsed = time.monotonic() - t0
            rate = done / elapsed if elapsed > 0 else 0
            print(f"  [{done}/{total}] {rate:.1f} sections/s", end="\r", flush=True)

        print(f"\nDone. Embedded {done} sections in {time.monotonic() - t0:.1f}s.")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate section embeddings for V4.")
    parser.add_argument("--title", default=None, help="Limit to one article title.")
    parser.add_argument("--batch", type=int, default=32, help="Batch size for encoding.")
    parser.add_argument("--limit", type=int, default=0, help="Max sections (0=all).")
    args = parser.parse_args()
    asyncio.run(run(args.title, args.batch, args.limit))


if __name__ == "__main__":
    main()
