"""Resilient HTTP client + provider contract tests (no network)."""

from collections.abc import Callable
from datetime import date

import httpx
import pytest

from app.sources.base import FetchWindow
from app.sources.g2b import G2BAdapter, map_item
from app.sources.http import FatalSourceError, ResilientClient, TransientSourceError
from app.sources.resilience import (
    BudgetedLimiter,
    CallBudgetExhaustedError,
    CircuitOpenError,
    MemoryBreaker,
    MemoryLimiter,
    QuotaExhaustedError,
)

DATA_GO_KR_QUOTA_XML = """<OpenAPI_ServiceResponse><cmmMsgHeader>
<errMsg>SERVICE ERROR</errMsg><returnAuthMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>
<returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""
DATA_GO_KR_BAD_KEY_XML = DATA_GO_KR_QUOTA_XML.replace(">22<", ">30<")


async def _no_sleep(_: float) -> None:
    return None


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> ResilientClient:  # type: ignore[no-untyped-def]
    return ResilientClient(
        "g2b_bid",
        base_url="https://apis.example",
        limiter=kwargs.pop("limiter", MemoryLimiter()),
        breaker=kwargs.pop("breaker", MemoryBreaker(failure_threshold=3)),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        **kwargs,
    )


async def test_retries_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"}, "body": {}}})

    client = _client(handler, max_attempts=4)
    data = await client.get_json("/x")
    assert calls["n"] == 3
    assert data["response"]["header"]["resultCode"] == "00"


async def test_gives_up_after_max_attempts() -> None:
    client = _client(lambda r: httpx.Response(502), max_attempts=2)
    with pytest.raises(TransientSourceError):
        await client.get_json("/x")


async def test_give_up_message_names_a_blank_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("")  # what a stalled TLS handshake to data.go.kr looks like

    with pytest.raises(TransientSourceError, match="gave up after 2 attempts: ConnectTimeout"):
        await _client(handler, max_attempts=2).get_json("/x")


async def test_http_200_quota_envelope_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=DATA_GO_KR_QUOTA_XML, headers={"content-type": "text/xml"})

    with pytest.raises(QuotaExhaustedError):
        await _client(handler).get_json("/x")
    assert calls["n"] == 1


async def test_bad_service_key_is_fatal() -> None:
    handler = lambda r: httpx.Response(200, text=DATA_GO_KR_BAD_KEY_XML)  # noqa: E731
    with pytest.raises(FatalSourceError):
        await _client(handler).get_json("/x")


# What apis.data.go.kr actually sent on 2026-09-26 for a service the key was not applied for:
# HTTP 403 with a JSON gateway envelope (not HTTP 200, not ``response.header``).
DATA_GO_KR_NOT_REGISTERED_JSON = {
    "OpenAPI_ServiceResponse": {
        "cmmMsgHeader": {
            "errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
            "returnAuthMsg": "등록되지 않은 서비스키",
            "returnReasonCode": "30",
        }
    }
}


async def test_http_403_gateway_envelope_names_the_provider_code() -> None:
    calls = {"n": 0}
    breaker = MemoryBreaker(failure_threshold=1)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, json=DATA_GO_KR_NOT_REGISTERED_JSON)

    with pytest.raises(FatalSourceError) as info:
        await _client(handler, breaker=breaker).get_json("/x")
    assert "30 SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in str(info.value)
    assert info.value.status == 403 and calls["n"] == 1
    await breaker.before_call("g2b_bid")  # a key problem does not open the circuit


async def test_http_200_gateway_envelope_is_not_read_as_an_empty_page() -> None:
    handler = lambda r: httpx.Response(200, json=DATA_GO_KR_NOT_REGISTERED_JSON)  # noqa: E731
    with pytest.raises(FatalSourceError, match="SERVICE_KEY_IS_NOT_REGISTERED_ERROR"):
        await _client(handler).get_json("/x")


async def test_http_429_quota_envelope_waits_for_the_reset() -> None:
    body = {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": "22"}}}
    with pytest.raises(QuotaExhaustedError):
        await _client(lambda r: httpx.Response(429, json=body)).get_json("/x")


async def test_http_4xx_without_envelope_is_still_fatal() -> None:
    with pytest.raises(FatalSourceError, match="HTTP 404"):
        await _client(lambda r: httpx.Response(404, text="not found")).get_json("/x")


async def test_json_result_code_error_is_classified() -> None:
    body = {"response": {"header": {"resultCode": "22", "resultMsg": "LIMITED"}}}
    with pytest.raises(QuotaExhaustedError):
        await _client(lambda r: httpx.Response(200, json=body)).get_json("/x")


async def test_circuit_opens_after_consecutive_failures() -> None:
    breaker = MemoryBreaker(failure_threshold=2, cooldown_seconds=60)
    client = _client(lambda r: httpx.Response(500), breaker=breaker, max_attempts=2)
    with pytest.raises(TransientSourceError):
        await client.get_json("/x")
    with pytest.raises(CircuitOpenError):
        await client.get_json("/x")


async def test_daily_quota_counts_every_attempt() -> None:
    limiter = MemoryLimiter(daily_quota={"g2b_bid": 2})
    client = _client(lambda r: httpx.Response(503), limiter=limiter, max_attempts=5)
    with pytest.raises(QuotaExhaustedError):
        await client.get_json("/x")


def test_g2b_bid_item_contract() -> None:
    item = {
        "bidNtceNo": "R26BK00012345",
        "bidNtceOrd": "000",
        "bidNtceNm": "[긴급] 2026년 스마트쉘터 구축사업 (협상에 의한 계약)",
        "ntceInsttNm": "조달청",
        "dminsttNm": "서울특별시 강남구",
        "dminsttCd": "3220000",
        "bidNtceDt": "2026-03-15 14:00:00",
        "bidClseDt": "2026-03-29 10:00:00",
        "asignBdgtAmt": "350000000",
        "presmptPrce": "318181818",
        "bfSpecRgstNo": "R26BD00000001",
        "bidNtceDtlUrl": "https://www.g2b.go.kr/x",
    }
    rec = map_item("bid_notice", item)
    assert rec is not None
    assert rec.external_id == "R26BK00012345-000"
    assert rec.published_at == date(2026, 3, 15)
    assert rec.publisher_raw == "서울특별시 강남구"
    assert rec.structured["amount_krw"] == 350_000_000
    assert rec.structured["prespec_no"] == "R26BD00000001"


def test_g2b_rows_without_ids_are_dropped() -> None:
    assert map_item("prespec", {"prdctClsfcNoNm": "x", "rcptDt": "2026-01-01"}) is None


async def test_g2b_adapter_pages_and_slices_windows() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        seen.append((params["inqryBgnDt"], params["pageNo"]))
        page = int(params["pageNo"])
        items = [
            {
                "bidNtceNo": f"B{params['inqryBgnDt']}{page}{i}",
                "bidNtceNm": "스마트폴 구축",
                "bidNtceDt": "2026-01-02 10:00:00",
                "dminsttNm": "성남시",
            }
            for i in range(100 if page == 1 else 3)
        ]
        body = {
            "response": {
                "header": {"resultCode": "00"},
                "body": {"items": items, "totalCount": 103, "pageNo": page},
            }
        }
        return httpx.Response(200, json=body)

    adapter = G2BAdapter("g2b_bid", _client(handler), "KEY")
    records = [r async for r in adapter.fetch(FetchWindow(date(2026, 1, 1), date(2026, 1, 10)))]
    # 2 weekly slices x 3 service types x 2 pages
    assert len(seen) == 12
    assert len(records) == 2 * 3 * 103


# 나라장터's own error wrapper, as sent live on 2026-09-26 (HTTP 200) when 발주계획 was called
# without orderBgnYm/orderEndYm. ``_items`` found no ``response.body`` and reported 0 items.
G2B_MISSING_PARAM_JSON = {
    "nkoneps.com.response.ResponseError": {
        "header": {"resultCode": "08", "resultMsg": "필수값 입력 에러"}
    }
}


async def test_g2b_error_wrapper_is_not_read_as_an_empty_page() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=G2B_MISSING_PARAM_JSON)

    with pytest.raises(FatalSourceError, match="08 필수값 입력 에러"):
        await _client(handler).get_json("/x")
    assert calls["n"] == 1  # a missing parameter will not fix itself


async def test_g2b_order_plan_asks_for_an_order_month_range() -> None:
    seen: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params)
        body = {
            "response": {"header": {"resultCode": "00"}, "body": {"items": [], "totalCount": 0}}
        }
        return httpx.Response(200, json=body)

    adapter = G2BAdapter("g2b_order_plan", _client(handler), "KEY")
    _ = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 20), date(2026, 9, 26)))]
    assert len(seen) == 3
    assert {(p["orderBgnYm"], p["orderEndYm"]) for p in seen} == {("202501", "202712")}

    bids = G2BAdapter("g2b_bid", _client(handler), "KEY")
    seen.clear()
    _ = [r async for r in bids.fetch(FetchWindow(date(2026, 9, 20), date(2026, 9, 26)))]
    assert all("orderBgnYm" not in p for p in seen)


# Trimmed items from the first live call (2026-09-26); contact fields replaced.
LIVE_ORDER_PLAN_THNG = {
    "bsnsDivCd": "01",
    "bsnsDivNm": "물품",
    "orderYear": "2027",
    "orderInsttCd": "7060012",
    "totlmngInsttNm": "서울특별시교육청",
    "orderInsttNm": "서울특별시중부교육청 선린중학교",
    "orderPlanSno": "0",
    "prcrmntMethd": "자체조달",
    "orderMnth": "02",
    "bizNm": "2026학년도 선린중학교 신입생 교복(동복)",
    "cnstwkRgnNm": "",
    "cntrctMthdNm": "제한경쟁",
    "orderContrctAmt": "0",
    "sumOrderAmt": "29315000",
    "deptNm": "행정실",
    "ofclNm": "담당자",
    "telNo": "02-000-0000",
    "prdctClsfcNoNm": "교복",
    "nticeDt": "2026-09-21 16:01:50",
    "orderPlanUntyNo": "R26DD20877187",
    "bidNtceNoList": "R26BK01739589000",
    "chgDt": "",
    "orderPlanDtlUrl": "https://www.g2b.go.kr/link/PRPA015_01/single/?oderPlanNo=R26DD20877187",
}
LIVE_PRESPEC_SERVC = {
    "bsnsDivNm": "일반용역",
    "refNo": "회계과-39725",
    "prdctClsfcNoNm": "구미동 96-3번지 상수관 정비공사 폐기물처리용역",
    "orderInsttNm": "경기도 성남시",
    "rlDminsttNm": "경기도 성남시",
    "asignBdgtAmt": "65625000",
    "rcptDt": "2026-09-21 15:04:22",
    "opninRgstClseDt": "2026-09-28 23:59:00",
    "bfSpecRgstNo": "R26BD00276604",
    "specDocFileUrl1": "https://www.g2b.go.kr/pn/pnz/pnza/UntyAtchFile/downloadFile.do",
    "rgstDt": "2026-09-20 08:43:24",
    "bidNtceNoList": "R26BK01737975",
    # no orderPlanUntyNo: 사전규격 responses do not carry one
}
LIVE_BID_CNSTWK = {
    "bidNtceNo": "R26BK01727884",
    "bidNtceOrd": "001",
    "ntceKindNm": "취소공고",
    "bidNtceDt": "2026-09-20 07:38:15",
    "bidNtceNm": "UPS 노후부품(응급실, 수술장, 외상중환자실) 배터리교체 공사",
    "ntceInsttCd": "B550590",
    "ntceInsttNm": "충북대학교병원",
    "dminsttCd": "B550590",
    "dminsttNm": "충북대학교병원",
    "cntrctCnclsMthdNm": "수의계약",
    "bidClseDt": "2026-09-18 10:00:00",
    "bdgtAmt": "76230000",  # 공사 has no asignBdgtAmt
    "presmptPrce": "69300000",
    "VAT": "6930000",
    "bfSpecRgstNo": "",
    "orderPlanUntyNo": "R26DD20870511",
    "bidNtceDtlUrl": "https://www.g2b.go.kr/link/PNPE027_01/single/?bidPbancNo=R26BK01727884",
    "rgstDt": "2026-09-20 07:38:15",
}


def test_g2b_live_order_plan_item() -> None:
    rec = map_item("order_plan", LIVE_ORDER_PLAN_THNG)
    assert rec is not None
    assert rec.external_id == "R26DD20877187"
    assert rec.published_at == date(2026, 9, 21)
    assert rec.publisher_raw == "서울특별시중부교육청 선린중학교"
    s = rec.structured
    assert s["amount_krw"] == 29_315_000
    assert (s["order_year"], s["order_month"]) == (2027, 2)
    assert s["department"] == "행정실"


def test_g2b_live_prespec_item() -> None:
    rec = map_item("prespec", LIVE_PRESPEC_SERVC)
    assert rec is not None
    assert rec.external_id == "R26BD00276604"
    assert rec.published_at == date(2026, 9, 21)
    s = rec.structured
    assert s["amount_krw"] == 65_625_000
    assert s["bid_notice_nos"] == ["R26BK01737975"]
    assert s["opinion_deadline"] == "2026-09-28 23:59:00"
    assert "order_plan_no" not in s


def test_g2b_live_construction_bid_takes_the_budget_not_the_estimate() -> None:
    rec = map_item("bid_notice", LIVE_BID_CNSTWK)
    assert rec is not None
    assert rec.external_id == "R26BK01727884-001"
    s = rec.structured
    assert s["amount_krw"] == 76_230_000  # bdgtAmt, not presmptPrce (VAT excluded)
    assert s["estimated_price"] == 69_300_000
    assert s["order_plan_no"] == "R26DD20870511"
    assert "prespec_no" not in s
    assert s["notice_kind"] == "취소공고"  # this live row withdraws 공고 R26BK01727884


# Shapes seen in 30 days of live data (2026-08-28 ~ 2026-09-26, 57,514 rows); values trimmed.
def test_g2b_placeholder_amounts_are_not_budgets() -> None:
    # 단가계약 and undisclosed budgets send 0, 1, 10원…; a 0 must not hide a real estimate.
    unit_price = LIVE_BID_CNSTWK | {"bidNtceNm": "학생생활관 방수공사", "bdgtAmt": "10"}
    unit_price |= {"presmptPrce": "1"}
    rec = map_item("bid_notice", unit_price)
    assert rec is not None
    assert "amount_krw" not in rec.structured
    assert "estimated_price" not in rec.structured

    zero_budget = LIVE_BID_CNSTWK | {"bdgtAmt": "0", "presmptPrce": "69300000"}
    rec = map_item("bid_notice", zero_budget)
    assert rec is not None and rec.structured["amount_krw"] == 69_300_000

    plan = map_item("order_plan", LIVE_ORDER_PLAN_THNG | {"sumOrderAmt": "1"})
    assert plan is not None and "amount_krw" not in plan.structured


def test_g2b_only_current_bid_numbers_lose_a_trailing_order() -> None:
    # Older 나라장터 numbers (연월 + 일련번호) and anything unknown are kept whole: cutting three
    # digits off a number we do not recognise would make it match nothing, or the wrong 공고.
    plan = map_item(
        "order_plan",
        LIVE_ORDER_PLAN_THNG
        | {"bidNtceNoList": "20231234567,2023123456700001,R26BK01739589000,R26BK0173958"},
    )
    assert plan is not None
    assert plan.structured["bid_notice_nos"] == [
        "20231234567",
        "2023123456700001",
        "R26BK01739589",
        "R26BK0173958",
    ]


def test_g2b_bid_number_lists_match_bid_numbers() -> None:
    # 발주계획 append the 3-digit 차수; 사전규격 do not; both can list several.
    plan = map_item(
        "order_plan",
        LIVE_ORDER_PLAN_THNG | {"bidNtceNoList": "R26BK01739589000,R26BK01702619001"},
    )
    assert plan is not None
    assert plan.structured["bid_notice_nos"] == ["R26BK01739589", "R26BK01702619"]
    spec = map_item(
        "prespec", LIVE_PRESPEC_SERVC | {"bidNtceNoList": "R26BK01735175,R26BK01717757"}
    )
    assert spec is not None
    assert spec.structured["bid_notice_nos"] == ["R26BK01735175", "R26BK01717757"]
    empty = map_item("prespec", LIVE_PRESPEC_SERVC | {"bidNtceNoList": ""})
    assert empty is not None and "bid_notice_nos" not in empty.structured


async def test_g2b_adapter_counts_calls_per_operation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pageNo"])
        n = int(request.url.params["numOfRows"])
        items = [
            LIVE_BID_CNSTWK | {"bidNtceNo": f"R26BK{page:03d}{i:05d}"}
            for i in range(n if page == 1 else 2)
        ]
        items.append({"bidNtceNm": "번호 없는 행"})  # unusable: counted, not stored
        body = {
            "response": {
                "header": {"resultCode": "00"},
                "body": {"items": items, "totalCount": n + 2},
            }
        }
        return httpx.Response(200, json=body)

    adapter = G2BAdapter("g2b_bid", _client(handler), "KEY", rows=999)
    records = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 20), date(2026, 9, 26)))]
    assert len(records) == 3 * (999 + 2)
    stats = adapter.path_stats["/ad/BidPublicInfoService/getBidPblancListInfoCnstwk"]
    assert (stats.pages, stats.total, stats.mapped, stats.items) == (2, 1001, 1001, 1003)
    assert stats.dropped_keys == [["bidNtceNm"], ["bidNtceNm"]]
    assert adapter.client.stats["attempts"] == 6


async def test_client_counts_retries_and_errors_by_kind() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("")
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"}, "body": {}}})

    client = _client(handler, max_attempts=4)
    await client.get_json("/x")
    assert dict(client.stats) == {
        "attempts": 3,
        "retries": 2,
        "error:ConnectTimeout": 1,
        "error:HTTP 503": 1,
    }
    quota = _client(lambda r: httpx.Response(200, text=DATA_GO_KR_QUOTA_XML))
    with pytest.raises(QuotaExhaustedError):
        await quota.get_json("/x")
    assert quota.stats["error:quota"] == 1


async def test_budgeted_limiter_stops_a_run_before_the_shared_quota() -> None:
    shared = MemoryLimiter(daily_quota={"g2b_bid": 1000})
    run = BudgetedLimiter(shared, budget=2)
    await run.acquire("g2b_bid")
    await run.acquire("g2b_bid")
    with pytest.raises(CallBudgetExhaustedError):
        await run.acquire("g2b_bid")
    assert run.calls == {"g2b_bid": 2}
    assert shared.calls == {"g2b_bid": 2}  # the refused call did not count against the day


# 지방재정365 예산서. The envelope and field names follow the published spec (lofin.py DEFAULTS);
# no live response has been seen yet (docs/real-data-budget.md), so this is a contract test.
def _lofin_row(inst: str, fyr: int, kind: str = "본예산", region: str = "서울특별시") -> dict:
    return {
        "fyr": str(fyr),
        "rgn_nm": region,
        "laf_nm": inst,
        "bgt_kind_nm": kind,
        "file_url": f"https://files.example/{fyr}/{inst}-{kind}.pdf",
        "reg_dt": f"{fyr - 1}1220",
    }


def _hub_page(code: str, rows: list[dict], result: str = "INFO-000") -> dict:
    # The 지방재정365 hub envelope, as the kpubdata client (MIT) parses it for its datasets.
    head = [
        {"list_total_count": len(rows)},
        {"RESULT": {"CODE": result, "MESSAGE": "정상 처리되었습니다."}},
    ]
    return {code: [{"head": head}, {"row": rows}]}


def _lofin_adapter(rows: list[dict], downloads: list[str], seen: list | None = None, **kwargs):  # type: ignore[no-untyped-def]
    from app.sources.lofin import LofinBudgetAdapter

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.example":
            downloads.append(request.url.path)
            return httpx.Response(200, content=b"%PDF-1.7 book")
        if seen is not None:
            seen.append(request.url)
        fyr = int(request.url.params["fyr"])
        listed = [r for r in rows if r.get("_listed_under", r["fyr"]) == str(fyr)]
        page = listed if request.url.params["pIndex"] == "1" else []
        return httpx.Response(200, json=_hub_page("TESTCD", page))

    kwargs.setdefault("api_code", "TESTCD")
    return LofinBudgetAdapter(_client(handler), "SECRET", **kwargs)


async def test_lofin_calls_the_365_hub_with_the_dataset_code() -> None:
    seen: list = []
    rows = [_lofin_row("강남구", 2025) | {"laf_hg_nm": "강남구", "wa_laf_hg_nm": "서울특별시"}]
    adapter = _lofin_adapter(rows, [], seen)
    got = [rec async for rec in adapter.fetch(FetchWindow(date(2024, 12, 1), date(2025, 12, 31)))]

    assert [r.publisher_raw for r in got] == ["강남구"]
    first = seen[0]
    assert first.path == "/lf/hub/TESTCD"
    assert dict(first.params) == {
        "Key": "SECRET",
        "Type": "json",
        "pIndex": "1",
        "pSize": "1000",
        "fyr": "2024",
    }


async def test_lofin_hub_errors_stop_the_source() -> None:
    from app.sources.http import FatalSourceError
    from app.sources.lofin import LofinBudgetAdapter

    def bad_key(request: httpx.Request) -> httpx.Response:
        # A failed call answers with RESULT at the top, not inside the dataset envelope.
        return httpx.Response(
            200, json={"RESULT": {"CODE": "ERROR-290", "MESSAGE": "인증키가 유효하지 않습니다."}}
        )

    adapter = LofinBudgetAdapter(_client(bad_key), "SECRET", api_code="TESTCD")
    with pytest.raises(FatalSourceError, match="ERROR-290"):
        [rec async for rec in adapter.fetch(FetchWindow(date(2025, 1, 1), date(2025, 1, 2)))]

    no_code = LofinBudgetAdapter(_client(bad_key), "SECRET")
    with pytest.raises(FatalSourceError, match="api_code"):
        [rec async for rec in no_code.fetch(FetchWindow(date(2025, 1, 1), date(2025, 1, 2)))]


async def test_lofin_an_empty_year_is_not_an_error() -> None:
    from app.sources.lofin import LofinBudgetAdapter

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_hub_page("TESTCD", [], result="INFO-200"))

    adapter = LofinBudgetAdapter(_client(empty), "SECRET", api_code="TESTCD")
    assert [
        rec async for rec in adapter.fetch(FetchWindow(date(2025, 1, 1), date(2025, 1, 2)))
    ] == []


async def test_lofin_filters_rows_before_downloading_a_book() -> None:
    rows = [
        _lofin_row("강남구", 2025),
        _lofin_row("강남구", 2025, "제1회 추가경정"),
        _lofin_row("서초구", 2025),  # not a wanted institution
        _lofin_row("중구", 2025, region="부산광역시"),  # a different 중구
        _lofin_row("중구", 2025),
        # a list that ignores ``fyr`` would still return an old book
        _lofin_row("강남구", 2020) | {"_listed_under": "2025", "reg_dt": "20241220"},
    ]
    downloads: list[str] = []
    adapter = _lofin_adapter(rows, downloads, institutions=["서울특별시 강남구", "서울특별시중구"])
    window = FetchWindow(date(2024, 12, 1), date(2025, 12, 31))

    got = [rec async for rec in adapter.fetch(window)]

    assert [r.title for r in got] == [
        "2025년도 강남구 본예산 예산서",
        "2025년도 강남구 제1회 추가경정 예산서",
        "2025년도 중구 본예산 예산서",
    ]
    assert len(downloads) == 3
    assert got[0].sido_hint == "서울특별시"
    assert got[0].structured == {"fiscal_year": 2025, "budget_kind": "본예산"}
    assert adapter.stats["downloaded"] == 3
    assert adapter.stats["skipped:institution"] == 2  # 서초구 and 부산광역시 중구
    assert adapter.stats["skipped:fiscal_year"] == 1


async def test_lofin_without_an_institution_list_takes_every_book_in_the_window() -> None:
    rows = [_lofin_row("강남구", 2025), _lofin_row("해운대구", 2025, region="부산광역시")]
    rows.append(_lofin_row("강남구", 2026) | {"reg_dt": "20260105"})  # registered after the window
    downloads: list[str] = []
    adapter = _lofin_adapter(rows, downloads)
    got = [rec async for rec in adapter.fetch(FetchWindow(date(2024, 12, 1), date(2025, 12, 31)))]
    assert [r.publisher_raw for r in got] == ["강남구", "해운대구"]
    assert len(downloads) == 2
    assert adapter.stats["skipped:window"] == 1
    assert adapter.stats["listed"] == 3
