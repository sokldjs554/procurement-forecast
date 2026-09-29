"""Signals from one run, linked again elsewhere (`manage link export` / `replay`)."""

import gzip
import json
from datetime import date
from pathlib import Path

from app.db.models import Source
from app.db.session import get_sessionmaker
from app.pipeline.ingest import upsert_record
from app.pipeline.link import link_signals
from app.pipeline.link_replay import export_signals, load_export, replay_links
from app.pipeline.process import process_document
from app.sources.base import RawRecord

# A 2026 제1회 추경 row and the statement that announced it, as in test_backfill.
ROW = """부서: 분당구 공원과
정책: 공원 조성
단위: 공원 시설 (단위:천원)
오리공원 물놀이장 설치공사 1,000,000 0 1,000,000
401 시설비및부대비 1,000,000 0 1,000,000
01 시설비 1,000,000 0 1,000,000
 ○오리공원 물놀이장 설치공사
1,000,000
"""
BILL = (
    "○행정기획조정실장 전재환  2026년도 제1회 추가경정예산안에 대하여 제안 설명 드리겠습니다.\n"
    "  주요사업비 예산 반영 내역으로는 수정청소년수련관 시설 개선 20억 원, 오리공원 물놀이장 설치 "
    "공사비 10억 원, 수내역 광장 재정비 공사비 5억 원 등을 반영하였습니다.\n"
)
RECORDS = (
    RawRecord(
        external_id="replay-309-1",
        doc_type="council_minutes",
        title="제309회 본회의 제1차(2026.03.12.)",
        published_at=date(2026, 4, 1),
        mime="text/plain",
        publisher_raw="경기도 성남시의회",
        institution_code_hint="CN-41130",
        content=BILL.encode(),
        structured={"meeting_date": "2026-03-12"},
    ),
    RawRecord(
        external_id="/humanframe/file/sncity/bgt/2026/replay-test.pdf",
        doc_type="budget_book",
        title="2026년 1회 추경 세입세출예산서 › 세출예산사업명세서",
        published_at=date(2026, 6, 18),
        mime="text/plain",
        publisher_raw="경기도 성남시",
        institution_code_hint="LG-41130",
        content=ROW.encode(),
        structured={"fiscal_year": 2026, "budget_kind": "제1회 추가경정"},
    ),
)
TODAY = date(2026, 9, 26)


async def test_a_replay_links_the_exported_signals_as_the_run_did(  # type: ignore[no-untyped-def]
    demo_world, runtime, tmp_path: Path
) -> None:
    path = tmp_path / "signals.jsonl.gz"
    async with get_sessionmaker()() as s:
        source = Source(key="test_link_replay", name="t", adapter="crawler", enabled=False)
        s.add(source)
        await s.flush()
        run = await s.begin_nested()
        ids: list[int] = []
        for rec in RECORDS:  # a `pipeline run`: documents by publication, then one link pass
            doc, _ = await upsert_record(s, source, rec, runtime)
            ids += (await process_document(s, runtime, doc.id)).signal_ids
        await link_signals(s, runtime, ids, today=TODAY)
        exported = await export_signals(
            s,
            path,
            doc_types=["budget_book", "council_minutes"],
            source_keys=["test_link_replay"],
        )
        await run.rollback()  # the run's signals and opportunities are gone; the file stays

        together = await s.begin_nested()
        same = await replay_links(s, runtime, load_export(path), today=TODAY)
        await together.rollback()
        books_first = await replay_links(
            s, runtime, load_export(path), first=["budget_book"], today=TODAY
        )
        await s.rollback()

    with gzip.open(path, "rt", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]
    assert exported == len(lines) == len(ids) == 4  # three statements, one row
    assert "embedding" not in lines[0]  # recomputed from the same text
    row = next(r for r in lines if r["stage"] == "budget_line")
    assert row["labels"][:1] == ["부서: 분당구 공원과"]
    assert row["document"]["structured"] == {"fiscal_year": 2026, "budget_kind": "제1회 추가경정"}

    assert same.recorded is not None and same.recorded >= 3
    assert (
        same.same_as_recorded == same.recorded
    )  # the replay is the run, opportunity for opportunity
    ori = {r["key"] for r in lines if "오리공원" in r["title"]}
    assert len(ori) == 2
    assert any(ori <= set(g) for g in same.groups)
    assert any(ori <= set(g) for g in books_first.groups)  # the other order, same answer (#43)
