"""Paid answers leave one database and come back into another (`manage llm-cache`)."""

import gzip
import json
from pathlib import Path

from sqlalchemy import delete, select

from app.db.models import LLMCacheEntry
from app.db.session import session_scope
from app.llm.cache_io import export_cache, import_cache
from app.llm.prompts import EXTRACT_PROMPT_VERSION

ANSWERS = {"test-io-a": {"signals": []}, "test-io-b": {"signals": [{"title": "수내교 전면개축"}]}}


async def test_cached_answers_round_trip(runtime, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "cache.jsonl.gz"
    try:
        async with session_scope() as s:
            for key, response in ANSWERS.items():
                s.add(
                    LLMCacheEntry(
                        key=key, task="extract", model="m", prompt_version="p", response=response
                    )
                )
        async with session_scope() as s:
            exported = await export_cache(s, path)
            await s.execute(delete(LLMCacheEntry).where(LLMCacheEntry.key.in_(ANSWERS)))
        async with session_scope() as s:
            added = await import_cache(s, path, model="claude-opus-5")
        async with session_scope() as s:
            again = await import_cache(s, path, model="claude-opus-5")
            back = (
                await s.execute(
                    select(LLMCacheEntry.key, LLMCacheEntry.response, LLMCacheEntry.prompt_version)
                    .where(LLMCacheEntry.key.in_(ANSWERS))
                    .order_by(LLMCacheEntry.key)
                )
            ).all()
    finally:
        async with session_scope() as s:
            await s.execute(delete(LLMCacheEntry).where(LLMCacheEntry.key.in_(ANSWERS)))
    with gzip.open(path, "rt", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]
    assert {"key", "response"} == set(lines[0])  # nothing else leaves the database
    assert exported >= 2 and added >= 2
    assert again == 0  # keys already present are left alone
    assert [(k, r, v) for k, r, v in back] == [
        (key, response, EXTRACT_PROMPT_VERSION) for key, response in ANSWERS.items()
    ]
