"""Move paid extraction answers between databases: ``manage llm-cache export`` / ``import``.

A measuring container that goes away takes its database with it; the first live Claude run on
the 성남시의회 minutes lost 571 paid answers that way (docs/real-data-minutes-claude.md §10).
The file holds only each answer and its cache key. The key already binds the model, effort,
prompt, schema and chunk text, so an answer loaded under other settings is never served — it
just never matches.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LLMCacheEntry
from app.llm.prompts import EXTRACT_PROMPT_VERSION

_BATCH = 500


async def export_cache(session: AsyncSession, path: Path) -> int:
    rows = (
        await session.execute(
            select(LLMCacheEntry.key, LLMCacheEntry.response)
            .where(LLMCacheEntry.task == "extract")
            .order_by(LLMCacheEntry.key)
        )
    ).all()
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for key, response in rows:
            f.write(json.dumps({"key": key, "response": response}, ensure_ascii=False) + "\n")
    return len(rows)


async def import_cache(session: AsyncSession, path: Path, *, model: str) -> int:
    """Returns how many answers were new here; keys already present are left as they are."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    added = 0
    for at in range(0, len(rows), _BATCH):
        values = [
            {
                "key": r["key"],
                "task": "extract",
                "model": model,
                "prompt_version": EXTRACT_PROMPT_VERSION,
                "response": r["response"],
                "hits": 0,
            }
            for r in rows[at : at + _BATCH]
        ]
        inserted = await session.scalars(
            insert(LLMCacheEntry)
            .values(values)
            .on_conflict_do_nothing(index_elements=["key"])
            .returning(LLMCacheEntry.key)
        )
        added += len(inserted.all())
    return added
