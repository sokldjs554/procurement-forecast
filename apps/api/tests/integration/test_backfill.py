"""Real-shaped 조달청 records through ingest → process → link, and the backfill command's path."""

import dataclasses
from datetime import date
from functools import partial
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
from pydantic import SecretStr
from sqlalchemy import delete, func, select, update

from app.db.models import (
    Document,
    DocumentChunk,
    IngestRun,
    InstitutionRow,
    LLMCacheEntry,
    LLMCall,
    OpportunitySignal,
    Signal,
    Source,
)
from app.db.session import get_sessionmaker, session_scope
from app.domain.institutions import InstitutionRegistry, load_registry_csv
from app.llm.budget import MemorySpendGuard
from app.llm.providers.anthropic_provider import AnthropicProvider
from app.llm.providers.heuristic import HeuristicProvider
from app.llm.service import LLMService
from app.pipeline.backfill import ingest_window, process_pending
from app.pipeline.ingest import reresolve_institutions, upsert_record
from app.pipeline.link import link_signals
from app.pipeline.process import pending_document_ids, process_document
from app.sources import registry as registry_module
from app.sources.base import FetchWindow, RawRecord
from app.sources.g2b import map_item
from app.sources.http import ResilientClient

# Trimmed from live rows (2026-09-26); names and numbers changed, contacts removed.
PLAN = {
    "orderPlanUntyNo": "R26DD90000001",
    "bizNm": "유성구 청사 UPS 배터리 교체공사",
    "nticeDt": "2026-09-22 10:00:00",
    "orderInsttNm": "대전광역시 유성구",
    "sumOrderAmt": "76230000",
    "orderYear": "2026",
    "orderMnth": "10",
    "bidNtceNoList": "R26BK90000001000",  # registered once the 공고 was out
}
BID = {
    "bidNtceNo": "R26BK90000001",
    "bidNtceOrd": "000",
    "bidNtceNm": "유성구 청사 UPS 배터리 교체공사",
    "bidNtceDt": "2026-09-20 09:00:00",
    "dminsttNm": "대전광역시 유성구",
    "bdgtAmt": "76230000",
    "presmptPrce": "69300000",
    "bfSpecRgstNo": "",
    "orderPlanUntyNo": "",  # 26% of live 공고 leave it empty
}


async def test_a_plan_linked_after_its_bid_joins_the_bids_opportunity(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    async with get_sessionmaker()() as s:
        source = Source(key="test_g2b_shapes", name="t", adapter="g2b", enabled=False, config={})
        s.add(source)
        await s.flush()
        signal_ids: list[int] = []
        for doc_type, item in (("bid_notice", BID), ("order_plan", PLAN)):  # publication order
            rec = map_item(doc_type, item)  # type: ignore[arg-type]
            assert rec is not None
            doc, _ = await upsert_record(s, source, rec, runtime)
            signal_ids += (await process_document(s, runtime, doc.id)).signal_ids
        await link_signals(s, runtime, signal_ids, today=date(2026, 9, 26))
        links = (
            await s.execute(
                select(OpportunitySignal.opportunity_id, OpportunitySignal.method).where(
                    OpportunitySignal.signal_id.in_(signal_ids)
                )
            )
        ).all()
        await s.rollback()
    assert len(links) == 2
    assert len({opp for opp, _ in links}) == 1
    assert [m for _, m in links] == ["seed", "ref"]


def _g2b_transport(calls: dict[str, int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        path = request.url.path
        items: list[dict[str, Any]] = []
        if path.endswith("Servc") and request.url.params["inqryBgnDt"].startswith("20260920"):
            items = [BID | {"bidNtceNo": f"R26BK9100000{i}"} for i in range(3)]
        body = {"response": {"header": {"resultCode": "00"}, "body": {"items": items}}}
        body["response"]["body"]["totalCount"] = len(items)  # type: ignore[index]
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


async def test_backfill_twice_stores_each_record_once(demo_world, runtime, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = {"n": 0}
    monkeypatch.setattr(
        registry_module,
        "ResilientClient",
        partial(ResilientClient, transport=_g2b_transport(calls)),
    )
    settings = runtime.settings.model_copy(update={"data_go_kr_service_key": SecretStr("KEY")})
    rt = dataclasses.replace(runtime, settings=settings)
    window = FetchWindow(date(2026, 9, 20), date(2026, 9, 26))
    reports = []
    try:
        for _ in range(2):
            async with session_scope() as s:
                src = await s.scalar(select(Source).where(Source.key == "g2b_bid"))
                reports += await ingest_window(s, rt, [src], window, rows=999, max_calls=10)
        async with session_scope() as s:
            stored = await s.scalar(
                select(func.count()).select_from(Document).where(Document.source_id == src.id)
            )
    finally:
        async with session_scope() as s:
            src = await s.scalar(select(Source).where(Source.key == "g2b_bid"))
            await s.execute(delete(Document).where(Document.source_id == src.id))
            await s.execute(delete(IngestRun).where(IngestRun.source_id == src.id))
            src.cursor = {}
    first, second = reports
    assert stored == 3
    assert (first["created"], first["skipped"]) == (3, 0)
    assert (second["created"], second["updated"], second["skipped"]) == (0, 0, 3)
    assert first["calls"] == second["calls"] == 3  # one slice x 3 업무구분, one page each
    assert calls["n"] == 6
    assert first["operations"]["getBidPblancListInfoServc"]["mapped"] == 3
    assert first["error"] is None


async def test_backfill_stops_a_source_at_its_call_budget(demo_world, runtime, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = {"n": 0}
    monkeypatch.setattr(
        registry_module,
        "ResilientClient",
        partial(ResilientClient, transport=_g2b_transport(calls)),
    )
    settings = runtime.settings.model_copy(update={"data_go_kr_service_key": SecretStr("KEY")})
    rt = dataclasses.replace(runtime, settings=settings)
    try:
        async with session_scope() as s:
            src = await s.scalar(select(Source).where(Source.key == "g2b_bid"))
            [report] = await ingest_window(
                s, rt, [src], FetchWindow(date(2026, 9, 20), date(2026, 9, 26)), max_calls=1
            )
    finally:
        async with session_scope() as s:
            src = await s.scalar(select(Source).where(Source.key == "g2b_bid"))
            await s.execute(delete(Document).where(Document.source_id == src.id))
            await s.execute(delete(IngestRun).where(IngestRun.source_id == src.id))
            src.cursor = {}
    assert calls["n"] == 1
    assert report["calls"] == 1
    assert report["status"] == "partial"  # what the first call brought in is kept
    assert report["created"] == 3
    assert "CallBudgetExhaustedError" in report["error"]


async def test_look_alike_plans_with_different_numbers_stay_apart(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    # One 기관, two 발주계획 a few days apart, titles one word apart (common in live data).
    first = PLAN | {"bizNm": "관내 도로 정비공사(1차)", "bidNtceNoList": ""}
    second = first | {"orderPlanUntyNo": "R26DD90000002", "bizNm": "관내 도로 정비공사(2차)"}
    second |= {"nticeDt": "2026-09-24 10:00:00"}
    bid = BID | {"bidNtceNo": "R26BK90000002", "bidNtceNm": "관내 도로 정비공사(2차)"}
    bid |= {"bidNtceDt": "2026-09-25 10:00:00", "orderPlanUntyNo": "R26DD90000002"}
    async with get_sessionmaker()() as s:
        source = Source(key="test_g2b_lookalike", name="t", adapter="g2b", enabled=False, config={})
        s.add(source)
        await s.flush()
        signal_ids: list[int] = []
        for doc_type, item in (("order_plan", first), ("order_plan", second), ("bid_notice", bid)):
            rec = map_item(doc_type, item)  # type: ignore[arg-type]
            assert rec is not None
            doc, _ = await upsert_record(s, source, rec, runtime)
            signal_ids += (await process_document(s, runtime, doc.id)).signal_ids
        await link_signals(s, runtime, signal_ids, today=date(2026, 9, 26))
        opp_of = dict(
            (
                await s.execute(
                    select(OpportunitySignal.signal_id, OpportunitySignal.opportunity_id).where(
                        OpportunitySignal.signal_id.in_(signal_ids)
                    )
                )
            ).all()
        )
        await s.rollback()
    plan1, plan2, bid_signal = signal_ids
    assert opp_of[plan1] != opp_of[plan2]
    assert opp_of[bid_signal] == opp_of[plan2]


SCHOOL_PLAN = {
    "orderPlanUntyNo": "R26DD90000010",
    "bizNm": "2027학년도 신입생 교복(동복)",
    "nticeDt": "2026-09-21 16:01:50",
    "orderInsttCd": "7069990",
    "orderInsttNm": "서울특별시중부교육청 테스트중학교",
    "sumOrderAmt": "29315000",
    "orderYear": "2027",
    "orderMnth": "02",
}


async def test_a_school_is_known_by_its_procurement_code(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    rt = dataclasses.replace(runtime, registry=load_registry_csv())
    async with get_sessionmaker()() as s:
        source = Source(key="test_g2b_school", name="t", adapter="g2b", enabled=False, config={})
        s.add(source)
        await s.flush()
        first = map_item("order_plan", SCHOOL_PLAN)
        later = map_item(
            "order_plan",
            SCHOOL_PLAN | {"orderPlanUntyNo": "R26DD90000011", "orderInsttNm": "테스트중학교"},
        )
        assert first is not None and later is not None
        doc1, _ = await upsert_record(s, source, first, rt)
        doc2, _ = await upsert_record(s, source, later, rt)
        row = await s.get(InstitutionRow, "G2B-7069990")
        stored = (row.name, row.kind, row.sido, row.region_code) if row else None
        signal_ids = (await process_document(s, rt, doc1.id)).signal_ids
        verdict = await s.scalar(select(Signal.verdict).where(Signal.id == signal_ids[0]))
        methods = (
            doc1.structured["institution_resolution"],
            doc2.structured["institution_resolution"],
        )
        codes = (doc1.institution_code, doc2.institution_code)
        await s.rollback()
    assert stored == (
        "서울특별시중부교육청 테스트중학교",
        "public_agency",
        "서울특별시",
        "11",
    )
    assert codes == ("G2B-7069990", "G2B-7069990")
    assert methods == ("provider", "code")  # the second one never reaches name matching
    assert verdict == "accepted"  # so it is linked, not left in the review queue


async def test_a_local_government_the_table_misses_goes_to_review(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    # The table as it was before 2026-07-01 codes came in: no 제물포구 yet.
    before = [i for i in load_registry_csv().all() if i.sigungu != "제물포구"]
    rt = dataclasses.replace(runtime, registry=InstitutionRegistry(before))
    item = SCHOOL_PLAN | {"orderInsttCd": "3999990", "orderInsttNm": "인천광역시 제물포구"}
    async with get_sessionmaker()() as s:
        source = Source(key="test_g2b_gap", name="t", adapter="g2b", enabled=False, config={})
        s.add(source)
        await s.flush()
        rec = map_item("order_plan", item)
        assert rec is not None
        doc, _ = await upsert_record(s, source, rec, rt)
        stored = await s.get(InstitutionRow, "G2B-3999990")
        await s.rollback()
    assert doc.institution_code is None
    assert stored is None


async def test_reresolve_picks_up_documents_stored_before_the_table_knew_them(
    demo_world, runtime
) -> None:  # type: ignore[no-untyped-def]
    rt = dataclasses.replace(runtime, registry=load_registry_csv())
    async with get_sessionmaker()() as s:
        source = Source(key="test_g2b_again", name="t", adapter="g2b", enabled=False, config={})
        s.add(source)
        await s.flush()
        rec = map_item("order_plan", SCHOOL_PLAN)
        assert rec is not None
        rec.provider_institution_code = None  # as ingested before codes were used
        doc, _ = await upsert_record(s, source, rec, rt)
        await process_document(s, rt, doc.id)
        before = (doc.institution_code, doc.parse_status)

        report = await reresolve_institutions(s, rt)
        await s.refresh(doc)
        after = (doc.institution_code, doc.parse_status, doc.structured["institution_resolution"])
        await s.rollback()
    assert before == (None, "parsed")
    assert after == ("G2B-7069990", "pending", "provider")
    assert report["resolved"] >= 1
    assert report["institutions_added"] >= 1


async def test_reresolve_gives_a_codeless_prespec_the_institution_its_bid_names(
    demo_world, runtime
) -> None:  # type: ignore[no-untyped-def]
    # 사전규격 responses have no institution code; the same name on a coded 공고 supplies it,
    # even when the 사전규격 was stored first.
    rt = dataclasses.replace(runtime, registry=load_registry_csv())
    prespec = map_item(
        "prespec",
        {
            "bfSpecRgstNo": "R26BD90000020",
            "prdctClsfcNoNm": "본관 냉난방기 교체",
            "rcptDt": "2026-09-14 10:00:00",
            "orderInsttNm": "테스트시설관리공단",
            "rlDminsttNm": "테스트시설관리공단",
            "asignBdgtAmt": "88000000",
        },
    )
    bid = map_item(
        "bid_notice",
        BID
        | {
            "bidNtceNo": "R26BK90000020",
            "bidNtceNm": "본관 냉난방기 교체",
            "dminsttCd": "B559990",
            "dminsttNm": "테스트시설관리공단",
            "bfSpecRgstNo": "R26BD90000020",
        },
    )
    assert prespec is not None and bid is not None
    assert prespec.provider_institution_code is None
    async with get_sessionmaker()() as s:
        source = Source(key="test_g2b_order", name="t", adapter="g2b", enabled=False, config={})
        s.add(source)
        await s.flush()
        bid.provider_institution_code = None  # both stored before codes were used
        docs = [(await upsert_record(s, source, rec, rt))[0] for rec in (prespec, bid)]
        await reresolve_institutions(s, rt)
        for doc in docs:
            await s.refresh(doc)
        codes = [d.institution_code for d in docs]
        await s.rollback()
    assert codes == ["G2B-B559990", "G2B-B559990"]


async def test_a_framework_contract_for_every_buyer_is_no_institution(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    # 제3자단가계약 name their 수요기관 "각 수요기관" under a placeholder code; one institution
    # made of them would gather every such contract into one opportunity.
    rt = dataclasses.replace(runtime, registry=load_registry_csv())
    item = BID | {
        "bidNtceNo": "R26BK90000030",
        "bidNtceNm": "우수조달물품(2026999, 테스트장치) 제3자단가계약",
        "dminsttCd": "ZZ99999",
        "dminsttNm": "각 수요기관",
        "ntceInsttNm": "조달청",
    }
    async with get_sessionmaker()() as s:
        source = Source(key="test_g2b_each", name="t", adapter="g2b", enabled=False, config={})
        s.add(source)
        await s.flush()
        rec = map_item("bid_notice", item)
        assert rec is not None
        doc, _ = await upsert_record(s, source, rec, rt)
        stored = await s.get(InstitutionRow, "G2B-ZZ99999")
        await s.rollback()
    assert doc.institution_code is None
    assert stored is None


async def test_a_codeless_prespec_finds_a_coded_institution_another_process_stored(
    demo_world, runtime
) -> None:  # type: ignore[no-untyped-def]
    # The worker that stored the 공고's institution is not the one that meets the 사전규격: after
    # a restart, in another worker, or in `pipeline reresolve`, the registry starts from the CSV.
    prespec = map_item(
        "prespec",
        {
            "bfSpecRgstNo": "R26BD90000040",
            "prdctClsfcNoNm": "선로 전기설비 개량",
            "rcptDt": "2026-09-15 10:00:00",
            "orderInsttNm": "테스트철도공단",
            "rlDminsttNm": "테스트철도공단",
            "asignBdgtAmt": "412000000",
        },
    )
    bid = map_item(
        "bid_notice",
        BID
        | {
            "bidNtceNo": "R26BK90000040",
            "bidNtceNm": "선로 전기설비 개량",
            "dminsttCd": "B554990",
            "dminsttNm": "테스트철도공단",
            "bfSpecRgstNo": "R26BD90000040",
        },
    )
    assert prespec is not None and bid is not None
    async with get_sessionmaker()() as s:
        source = Source(key="test_g2b_restart", name="t", adapter="g2b", enabled=False, config={})
        s.add(source)
        await s.flush()
        await upsert_record(
            s, source, bid, dataclasses.replace(runtime, registry=load_registry_csv())
        )
        fresh = dataclasses.replace(runtime, registry=load_registry_csv())
        doc, _ = await upsert_record(s, source, prespec, fresh)
        method = doc.structured["institution_resolution"]
        await s.rollback()
    assert doc.institution_code == "G2B-B554990"
    assert method == "exact"


async def test_reresolve_in_a_new_process_gives_a_prespec_its_bids_institution(
    demo_world, runtime
) -> None:  # type: ignore[no-untyped-def]
    # A fresh DB loads 사전규격 before 입찰공고, so a 공단 first named by a 공고 is unknown to its
    # 사전규격 at ingest; `pipeline reresolve` runs later, in a process of its own.
    prespec = map_item(
        "prespec",
        {
            "bfSpecRgstNo": "R26BD90000041",
            "prdctClsfcNoNm": "청사 승강기 교체",
            "rcptDt": "2026-09-15 10:00:00",
            "orderInsttNm": "테스트환경공단",
            "rlDminsttNm": "테스트환경공단",
            "asignBdgtAmt": "95000000",
        },
    )
    bid = map_item(
        "bid_notice",
        BID
        | {
            "bidNtceNo": "R26BK90000041",
            "bidNtceNm": "청사 승강기 교체",
            "dminsttCd": "B553990",
            "dminsttNm": "테스트환경공단",
            "bfSpecRgstNo": "R26BD90000041",
        },
    )
    assert prespec is not None and bid is not None
    async with get_sessionmaker()() as s:
        source = Source(key="test_g2b_later", name="t", adapter="g2b", enabled=False, config={})
        s.add(source)
        await s.flush()
        ingest_rt = dataclasses.replace(runtime, registry=load_registry_csv())
        doc, _ = await upsert_record(s, source, prespec, ingest_rt)
        await upsert_record(s, source, bid, ingest_rt)
        before = doc.institution_code
        fresh = dataclasses.replace(runtime, registry=load_registry_csv())
        report = await reresolve_institutions(s, fresh)
        await s.refresh(doc)
        after = doc.institution_code
        await s.rollback()
    assert before is None
    assert after == "G2B-B553990"
    assert report["resolved"] == 1


# A 공고, its 취소공고 (a later 차수 with no 발주계획 number, as in live data) and a 재공고.
C_PLAN = PLAN | {"orderPlanUntyNo": "R26DD92000001", "bidNtceNoList": "", "bizNm": "청사 CCTV 교체"}
C_BID = BID | {
    "bidNtceNo": "R26BK92000001",
    "bidNtceNm": "청사 CCTV 교체",
    "orderPlanUntyNo": "R26DD92000001",
    "ntceKindNm": "등록공고",
}
C_CANCEL = C_BID | {
    "bidNtceOrd": "001",
    "ntceKindNm": "취소공고",
    "bidNtceDt": "2026-09-22 09:00:00",
    "orderPlanUntyNo": "",
}
C_REBID = C_BID | {"bidNtceOrd": "002", "ntceKindNm": "재공고", "bidNtceDt": "2026-09-24 09:00:00"}


async def _opportunities_after(runtime, records):  # type: ignore[no-untyped-def]
    from app.db.models import Opportunity
    from app.domain.stages import tender_is_out

    async with get_sessionmaker()() as s:
        source = Source(key="test_g2b_cancel", name="t", adapter="g2b", enabled=False, config={})
        s.add(source)
        await s.flush()
        signal_ids: list[int] = []
        for doc_type, item in records:
            rec = map_item(doc_type, item)
            assert rec is not None
            doc, _ = await upsert_record(s, source, rec, runtime)
            signal_ids += (await process_document(s, runtime, doc.id)).signal_ids
        await link_signals(s, runtime, signal_ids, today=date(2026, 9, 26))
        opp_ids = set(
            (
                await s.scalars(
                    select(OpportunitySignal.opportunity_id).where(
                        OpportunitySignal.signal_id.in_(signal_ids)
                    )
                )
            ).all()
        )
        opps = [await s.get(Opportunity, i) for i in opp_ids]
        seen = [
            (o.stage, o.status, o.bid_published_at, tender_is_out(o.stage, o.bid_published_at))
            for o in opps
            if o is not None
        ]
        await s.rollback()
    return seen


async def test_a_cancelled_tender_is_no_tender(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    seen = await _opportunities_after(
        runtime, [("order_plan", C_PLAN), ("bid_notice", C_BID), ("bid_notice", C_CANCEL)]
    )
    # One opportunity (the 취소 joins by its 공고 number), back to 공고 전 on the plan.
    assert seen == [("order_plan", "open", None, False)]


async def test_a_tender_with_nothing_but_its_cancel_is_over(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    seen = await _opportunities_after(runtime, [("bid_notice", C_BID), ("bid_notice", C_CANCEL)])
    assert seen == [("bid_notice", "closed", date(2026, 9, 20), True)]


async def test_a_reannouncement_reopens_it(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    seen = await _opportunities_after(
        runtime,
        [
            ("order_plan", C_PLAN),
            ("bid_notice", C_BID),
            ("bid_notice", C_CANCEL),
            ("bid_notice", C_REBID),
        ],
    )
    assert seen == [("bid_notice", "bid_open", date(2026, 9, 20), True)]


async def test_another_order_of_the_same_notice_joins_by_number(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    # A 변경공고 can rename the project and change the budget; its number still says it is
    # the same purchase, whatever the similarity score.
    changed = C_BID | {
        "bidNtceOrd": "001",
        "ntceKindNm": "변경공고",
        "bidNtceNm": "통합관제센터 영상감시장치 증설 및 스마트 연계",
        "bdgtAmt": "412000000",
        "orderPlanUntyNo": "",
        "bidNtceDt": "2026-09-23 09:00:00",
    }
    seen = await _opportunities_after(runtime, [("bid_notice", C_BID), ("bid_notice", changed)])
    assert seen == [("bid_notice", "bid_open", date(2026, 9, 20), True)]


# Two 구청 rows of 성남시's 2026 본예산 세출예산사업명세서 (pp. 1,2xx and 1,4xx), as pypdf reads
# them. Similarity joined them (0.84) on the live run; they are two projects.
SEONGNAM_ROWS = """부서: 중원구 건설과
정책: 도로시설 관리
단위: 도로 정비 (단위:천원)
빗물받이 및 측구 정비공사 360,000 360,000 0
401 시설비및부대비 360,000 360,000 0
01 시설비 360,000 360,000 0
 ○빗물받이 및 측구 정비공사
360,000
부서: 분당구 건설과
정책: 도로시설관리
단위: 도로시설물 유지관리 (단위:천원)
빗물받이 정비공사 1,401,000 751,000 650,000
401 시설비및부대비 1,401,000 751,000 650,000
01 시설비 1,400,000 750,000 650,000
 ○빗물받이정비공사(1구역)
200,000
"""


async def test_two_rows_of_one_budget_book_are_two_opportunities(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    rec = RawRecord(
        external_id="/humanframe/file/sncity/bgt/2026/11608_3.pdf",
        doc_type="budget_book",
        title="2026년 세입세출예산서 › 일반회계 › 세출예산사업명세서 › 전체",
        published_at=date(2026, 6, 18),
        mime="text/plain",
        publisher_raw="경기도 성남시",
        institution_code_hint="LG-41130",
        content=SEONGNAM_ROWS.encode(),
        structured={"fiscal_year": 2026, "budget_kind": "본"},
    )
    async with get_sessionmaker()() as s:
        source = Source(
            key="test_budget_rows", name="t", adapter="crawler", enabled=False, config={}
        )
        s.add(source)
        await s.flush()
        doc, _ = await upsert_record(s, source, rec, runtime)
        signal_ids = (await process_document(s, runtime, doc.id)).signal_ids
        await link_signals(s, runtime, signal_ids, today=date(2026, 9, 26))
        rows = (
            await s.execute(
                select(
                    Signal.title,
                    Signal.department,
                    Signal.budget_krw,
                    OpportunitySignal.opportunity_id,
                )
                .join(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
                .where(Signal.id.in_(signal_ids))
                .order_by(Signal.id)
            )
        ).all()
        await s.rollback()
    assert [(r.title, r.department, r.budget_krw) for r in rows] == [
        ("빗물받이 및 측구 정비공사", "중원구 건설과", 360_000_000),
        ("빗물받이 정비공사", "분당구 건설과", 1_401_000_000),
    ]
    assert rows[0].opportunity_id != rows[1].opportunity_id


class _NoCredit:
    """The Anthropic client once the balance is gone (docs/real-data-minutes-claude.md §6.2)."""

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **_: Any) -> Any:
        self.calls += 1
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.BadRequestError(
            "Your credit balance is too low to access the Anthropic API.",
            response=httpx.Response(400, request=request),
            body=None,
        )


async def test_pipeline_run_stops_when_the_llm_account_runs_out_of_credit(
    demo_world, runtime
) -> None:  # type: ignore[no-untyped-def]
    messages = _NoCredit()
    provider = AnthropicProvider(
        api_key="test",
        extract_model="claude-opus-5",
        extract_effort="low",
        brief_model="claude-opus-5",
        brief_effort="medium",
        client=SimpleNamespace(beta=SimpleNamespace(messages=messages)),  # type: ignore[arg-type]
    )
    llm = LLMService(primary=provider, fallback=HeuristicProvider(), guard=MemorySpendGuard(10))
    rt = dataclasses.replace(runtime, llm=llm)
    async with session_scope() as s:
        assert not await pending_document_ids(s)
        ids = list(
            (
                await s.scalars(
                    select(Document.id)
                    .where(
                        Document.doc_type == "council_minutes",
                        Document.id.in_(select(Signal.document_id)),
                    )
                    .order_by(Document.id)
                    .limit(2)
                )
            ).all()
        )
        count = select(func.count()).select_from(Signal).where(Signal.document_id.in_(ids))
        signals_before = await s.scalar(count)
        await s.execute(update(Document).where(Document.id.in_(ids)).values(parse_status="pending"))
    try:
        async with session_scope() as s:
            report = await process_pending(s, rt)
        async with session_scope() as s:
            statuses = (
                await s.scalars(select(Document.parse_status).where(Document.id.in_(ids)))
            ).all()
            signals_after = await s.scalar(count)
    finally:
        async with session_scope() as s:
            await s.execute(
                update(Document).where(Document.id.in_(ids)).values(parse_status="parsed")
            )
    assert len(ids) == 2
    assert messages.calls == 1  # the second document never reached the API
    assert report["stopped"].startswith("LLMSetupError")
    assert report["documents"] == 0 and report["failed"] == 0
    assert statuses == ["pending", "pending"]  # picked up again once the account is fixed
    assert signals_after == signals_before  # the stopped document keeps what it had


# 2026 제1회 추경: the book row (dated by its file, 2026-06-18) and the statement that announced it
# (2026-03-12), as on the live 성남시 run (docs/real-data-minutes.md §8.6).
SUPPLEMENTARY_ROW = """부서: 분당구 공원과
정책: 공원 조성
단위: 공원 시설 (단위:천원)
오리공원 물놀이장 설치공사 1,000,000 0 1,000,000
401 시설비및부대비 1,000,000 0 1,000,000
01 시설비 1,000,000 0 1,000,000
 ○오리공원 물놀이장 설치공사
1,000,000
"""
SUPPLEMENTARY_BILL = (
    "○행정기획조정실장 전재환  2026년도 제1회 추가경정예산안에 대하여 제안 설명 드리겠습니다.\n"
    "  주요사업비 예산 반영 내역으로는 수정청소년수련관 시설 개선 20억 원, 오리공원 물놀이장 설치 "
    "공사비 10억 원, 수내역 광장 재정비 공사비 5억 원 등을 반영하였습니다.\n"
)


async def test_a_statement_linked_after_the_book_it_announced_still_joins_it(
    demo_world, runtime
) -> None:  # type: ignore[no-untyped-def]
    book = RawRecord(
        external_id="/humanframe/file/sncity/bgt/2026/order-test.pdf",
        doc_type="budget_book",
        title="2026년 1회 추경 세입세출예산서 › 세출예산사업명세서",
        published_at=date(2026, 6, 18),
        mime="text/plain",
        publisher_raw="경기도 성남시",
        institution_code_hint="LG-41130",
        content=SUPPLEMENTARY_ROW.encode(),
        structured={"fiscal_year": 2026, "budget_kind": "제1회 추가경정"},
    )
    minutes = RawRecord(
        external_id="order-test-309-1",
        doc_type="council_minutes",
        title="제309회 본회의 제1차(2026.03.12.)",
        published_at=date(2026, 4, 1),
        mime="text/plain",
        publisher_raw="경기도 성남시의회",
        institution_code_hint="CN-41130",
        content=SUPPLEMENTARY_BILL.encode(),
        structured={"meeting_date": "2026-03-12"},
    )
    async with get_sessionmaker()() as s:
        source = Source(
            key="test_link_order", name="t", adapter="crawler", enabled=False, config={}
        )
        s.add(source)
        await s.flush()
        ids: dict[str, list[int]] = {}
        for rec in (book, minutes):  # the book first: 98 days after the statement
            doc, _ = await upsert_record(s, source, rec, runtime)
            ids[rec.doc_type] = (await process_document(s, runtime, doc.id)).signal_ids
            await link_signals(s, runtime, ids[rec.doc_type], today=date(2026, 9, 26))
        rows = (
            await s.execute(
                select(Signal.stage, Signal.title, OpportunitySignal.opportunity_id)
                .join(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
                .where(
                    Signal.id.in_(ids["budget_book"] + ids["council_minutes"]),
                    Signal.title.contains("오리공원"),
                )
                .order_by(Signal.id)
            )
        ).all()
        await s.rollback()
    assert [(r.stage, r.title) for r in rows] == [
        ("budget_line", "오리공원 물놀이장 설치공사"),
        ("council_mention", "오리공원 물놀이장 설치 공사"),
    ]
    assert rows[0].opportunity_id == rows[1].opportunity_id


def _empty_answer() -> Any:
    """A successful extraction that found nothing — enough to be paid for and cached."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"signals": []}')],
        stop_reason="end_turn",
        stop_details=None,
        model="claude-opus-5",
        usage=SimpleNamespace(
            input_tokens=400,
            output_tokens=10,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        _request_id="req_test",
    )


class _PaysOnceThenNoCredit(_NoCredit):
    def __init__(self, paid: int) -> None:
        super().__init__()
        self.paid = paid
        self.sent: list[str] = []

    async def create(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs["messages"][0]["content"])
        if len(self.sent) <= self.paid:
            return _empty_answer()
        return await super().create(**kwargs)


def _provider_on(messages: Any) -> AnthropicProvider:
    return AnthropicProvider(
        api_key="test",
        extract_model="claude-opus-5",
        extract_effort="low",
        brief_model="claude-opus-5",
        brief_effort="medium",
        client=SimpleNamespace(beta=SimpleNamespace(messages=messages)),  # type: ignore[arg-type]
    )


async def test_what_was_paid_for_survives_a_stop_mid_document(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    first = _PaysOnceThenNoCredit(paid=1)
    llm = LLMService(
        primary=_provider_on(first),
        fallback=HeuristicProvider(),
        guard=MemorySpendGuard(10),
        durable=get_sessionmaker,
    )
    async with session_scope() as s:
        assert not await pending_document_ids(s)
        doc_id = await s.scalar(
            select(DocumentChunk.document_id)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.doc_type == "council_minutes", DocumentChunk.triage_passed)
            .group_by(DocumentChunk.document_id)
            .having(func.count() >= 2)
            .order_by(DocumentChunk.document_id)
            .limit(1)
        )
        cached_before = set((await s.scalars(select(LLMCacheEntry.key))).all())
        await s.execute(
            update(Document).where(Document.id == doc_id).values(parse_status="pending")
        )
    try:
        async with session_scope() as s:
            report = await process_pending(s, dataclasses.replace(runtime, llm=llm))
        async with session_scope() as s:
            status = await s.scalar(select(Document.parse_status).where(Document.id == doc_id))
            calls = (
                await s.scalars(select(LLMCall.status).where(LLMCall.document_id == doc_id))
            ).all()
            new_keys = set((await s.scalars(select(LLMCacheEntry.key))).all()) - cached_before

        # Run it again with the credit back: the chunk already paid for comes from the cache.
        again = _PaysOnceThenNoCredit(paid=100)
        llm_again = LLMService(
            primary=_provider_on(again),
            fallback=HeuristicProvider(),
            guard=MemorySpendGuard(10),
            durable=get_sessionmaker,
        )
        async with get_sessionmaker()() as s:
            await process_document(s, dataclasses.replace(runtime, llm=llm_again), doc_id)
            await s.rollback()
    finally:
        async with session_scope() as s:
            await s.execute(delete(LLMCall).where(LLMCall.document_id == doc_id))
            await s.execute(delete(LLMCacheEntry).where(LLMCacheEntry.key.notin_(cached_before)))
            await s.execute(
                update(Document).where(Document.id == doc_id).values(parse_status="parsed")
            )
    assert report["stopped"].startswith("LLMSetupError")
    assert status == "pending"
    assert len(first.sent) == 2  # one answer paid for, then out of credit
    assert list(calls) == ["ok"]  # the paid call is on the books although the document rolled back
    assert len(new_keys) == 1  # …and so is its answer
    assert first.sent[0] not in again.sent  # the next run does not pay for it twice
    assert len(again.sent) >= 1


def _book_rows(*rows: tuple[str, int], department: str = "수정구 청소년과") -> str:
    """세출예산사업명세서 rows in one department, the way the live 성남시 books print them."""
    out = [f"부서: {department}", "정책: 청소년 시설 운영", "단위: 청소년 시설 (단위:천원)"]
    for name, amount in rows:
        out += [
            f"{name} {amount:,} 0 {amount:,}",
            f"401 시설비및부대비 {amount:,} 0 {amount:,}",
            f"01 시설비 {amount:,} 0 {amount:,}",
            f" ○{name}",
            f"{amount:,}",
        ]
    return "\n".join(out) + "\n"


async def test_rows_of_two_books_join_only_under_the_same_name(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    # 2025 본예산 and 2026 제1회 추경, as on the live run (real-data-minutes.md §9.4): the
    # 중원 hall's row and the 수정 hall's row shared an opportunity; the bridge is one project.
    books = [
        RawRecord(
            external_id=f"/humanframe/file/sncity/bgt/{year}/names-test.pdf",
            doc_type="budget_book",
            title=title,
            published_at=published,
            mime="text/plain",
            publisher_raw="경기도 성남시",
            institution_code_hint="LG-41130",
            content=_book_rows(*rows).encode(),
            structured={"fiscal_year": year, "budget_kind": kind},
        )
        for year, kind, title, published, rows in (
            (
                2025,
                "본예산",
                "2025년 본예산 세입세출예산서 › 세출예산사업명세서",
                date(2024, 12, 31),
                [("중원청소년수련관 시설개선공사", 278_774), ("탄천 보행교 설치공사", 4_100_000)],
            ),
            (
                2026,
                "제1회 추가경정",
                "2026년 1회 추경 세입세출예산서 › 세출예산사업명세서",
                date(2026, 6, 18),
                [("수정청소년수련관 시설 개선공사", 2_014_000), ("탄천 보행교 설치 공사", 900_000)],
            ),
        )
    ]
    async with get_sessionmaker()() as s:
        source = Source(
            key="test_book_names", name="t", adapter="crawler", enabled=False, config={}
        )
        s.add(source)
        await s.flush()
        ids: list[int] = []
        for rec in books:
            doc, _ = await upsert_record(s, source, rec, runtime)
            ids += (await process_document(s, runtime, doc.id)).signal_ids
        await link_signals(s, runtime, ids, today=date(2026, 9, 26))
        rows = dict(
            (
                await s.execute(
                    select(Signal.title, OpportunitySignal.opportunity_id)
                    .join(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
                    .where(Signal.id.in_(ids))
                )
            ).all()
        )
        await s.rollback()
    assert set(rows) == {
        "중원청소년수련관 시설개선공사",
        "탄천 보행교 설치공사",
        "수정청소년수련관 시설 개선공사",
        "탄천 보행교 설치 공사",
    }
    assert rows["탄천 보행교 설치공사"] == rows["탄천 보행교 설치 공사"]  # one project, two books
    assert rows["중원청소년수련관 시설개선공사"] != rows["수정청소년수련관 시설 개선공사"]


async def test_a_name_every_dong_carries_joins_the_same_dongs_row(demo_world, runtime) -> None:  # type: ignore[no-untyped-def]
    # "소규모 정비공사" is a row of every 동 (45 in one live 성남시 book, real-data-minutes.md
    # §10): the name cannot tell them apart, the department can.
    def book(year: int, amounts: dict[str, int]) -> RawRecord:
        return RawRecord(
            external_id=f"/humanframe/file/sncity/bgt/{year}/dong-test.pdf",
            doc_type="budget_book",
            title=f"{year}년 본예산 세입세출예산서 › 세출예산사업명세서",
            published_at=date(year - 1, 12, 31),
            mime="text/plain",
            publisher_raw="경기도 성남시",
            institution_code_hint="LG-41130",
            content="".join(
                _book_rows(("소규모 정비공사", amount), department=dept)
                for dept, amount in amounts.items()
            ).encode(),
            structured={"fiscal_year": year, "budget_kind": "본"},
        )

    books = [
        book(2025, {"분당구 수내1동": 30_000, "분당구 정자1동": 30_000, "중원구 성남동": 30_000}),
        # 은행1동 has no row of that name in 2025: generic, so no other 동's row will do.
        # Linked first, before the other 동 rows of its book could rule those opportunities out.
        # …nor 하대원동, linked last: the other 동 rows of its book have taken their 2025
        # opportunities out, and 성남동's, of another department, is the one candidate left.
        book(
            2026,
            {
                "분당구 은행1동": 30_000,
                "분당구 정자1동": 30_000,
                "분당구 수내1동": 30_000,
                "중원구 하대원동": 30_000,
            },
        ),
    ]
    async with get_sessionmaker()() as s:
        source = Source(key="test_dong_rows", name="t", adapter="crawler", enabled=False, config={})
        s.add(source)
        await s.flush()
        ids: list[int] = []
        for rec in books:
            doc, _ = await upsert_record(s, source, rec, runtime)
            ids += (await process_document(s, runtime, doc.id)).signal_ids
        await link_signals(s, runtime, ids, today=date(2026, 9, 26))
        rows = (
            await s.execute(
                select(Signal.department, Signal.expected_year, OpportunitySignal.opportunity_id)
                .join(OpportunitySignal, OpportunitySignal.signal_id == Signal.id)
                .where(Signal.id.in_(ids))
            )
        ).all()
        await s.rollback()
    opp = {(dept, year): opp_id for dept, year, opp_id in rows}
    assert len(opp) == 7
    assert opp[("분당구 수내1동", 2025)] == opp[("분당구 수내1동", 2026)]
    assert opp[("분당구 정자1동", 2025)] == opp[("분당구 정자1동", 2026)]
    assert opp[("분당구 수내1동", 2025)] != opp[("분당구 정자1동", 2025)]
    for alone, year in (
        ("분당구 은행1동", 2026),
        ("중원구 하대원동", 2026),
        ("중원구 성남동", 2025),
    ):
        assert opp[(alone, year)] not in {opp[k] for k in opp if k[0] != alone}
