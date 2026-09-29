"""CLIK (국회도서관 지방의정포털) minutes adapter, against responses trimmed from the first live
calls on 2026-09-28 (docs/real-data-clik.md). Keys removed; text shortened."""

import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.parsing.chunking import GLUED_MEMBER_RE
from app.sources.base import FetchWindow, SkipsStored
from app.sources.clik import ClikMinutesAdapter, external_id, html_to_text, meeting_key
from app.sources.http import FatalSourceError, ResilientClient
from app.sources.resilience import MemoryBreaker, MemoryLimiter, QuotaExhaustedError


async def _no_sleep(_: float) -> None:
    return None


def _row(docid: str, day: str, odr: str, name: str = "본회의", sesn: str = "312") -> dict:
    return {
        "ROW": {
            "DOCID": docid,
            "RASMBLY_SESN": sesn,
            "RASMBLY_ID": "031013",
            "MTG_DE": day,
            "RASMBLY_NM": "경기도 성남시의회",
            "RASMBLY_NUMPR": "10",
            "MINTS_ODR": odr,
            "MTGNM": name,
        }
    }


def _list(rows: list[dict], total: int = 7338) -> list[dict]:
    return [
        {
            "SERVICE": "minutes",
            "RESULT_CODE": "SUCCESS",
            "RESULT_MESSAGE": "정상 처리되었습니다.",
            "TOTAL_COUNT": total,
            "LIST_COUNT": len(rows),
            "LIST": rows,
        }
    ]


# 성남시의회 hands CLIK its whole viewer page: header and menus around the minutes, members as
# "<strong>○<a>박기범</a>의원</strong>&nbsp;&nbsp;…", officials as "○의장 <a>강상태</a>".
SNCOUNCIL_HTML = (
    '<header class="main-header"><a href="/kr/main.do" class="logo"><span class="logo-lg">'
    '<b>성남시의회 회의록</b></span></a><nav class="navbar navbar-static-top">'
    '<a href="#" class="sidebar-toggle"><span class="sr-only">Toggle navigation</span></a>'
    "<select><option>굴림</option><option>돋움체</option></select></nav></header>"
    '<aside class="main-sidebar"><form class="sidebar-form"><input type="text"></form>'
    "<ul><li>제312회</li></ul></aside>"
    '<div class="content-wrapper"><section class="content">'
    '<p id="p1" align="right" style="color: red;"><font style="font-size: 16px;">'
    "[본 회의록은 최종 교정 전 임시회의록이므로 법적 효력이 없습니다]</font></p>"
    "<div>제312회 성남시의회(임시회)</div><div class='text-center'><h1 class='bold'>"
    "본회의회의록</h1></div><hr />일 시&nbsp;&nbsp;2026년 9월 7일(월) 10시<br /><hr />"
    "<strong>&nbsp;&nbsp;&nbsp; 의사일정</strong><br />"
    "<spk class='speaker00601'><strong>○의장 <a href='/kr/profile/profile.do?key=x'>강상태</a>"
    "</strong>&nbsp;&nbsp;의석을 정돈해 주시기 바랍니다.<br /></spk>"
    "<spk class='speaker09006'><strong>○<a href='/kr/profile/profile.do?key=y'>박기범</a>의원"
    "</strong>&nbsp;&nbsp;존경하는 92만 성남 시민 여러분! <br />&nbsp;&nbsp;둘째, 도촌야탑역 "
    "신설 즉각적인 보완 대책을 마련하십시오.<br /></spk>"
    "<spk class='speaker'><strong>○교통도로국장 유동</strong>&nbsp;&nbsp;내년도 본예산에 "
    "반영하겠습니다.<br /></spk></section></div>"
)

DETAIL = [
    {
        "SERVICE": "minutes",
        "RESULT_CODE": "SUCCESS",
        "RESULT_MESSAGE": "정상 처리되었습니다.",
        "DOCID": "CLIKC2946767454052674",
        "MTR_SJ": "\n o 5분자유발언(박기범·추선미 의원)\n 1. 성남시의회 인사청문회 조례안\n",
        "RASMBLY_SESN": "312",
        "RASMBLY_ID": "031013",
        "MTG_DE": "20260907",
        "RASMBLY_NM": "경기도 성남시의회",
        "RASMBLY_NUMPR": "10",
        "MINTS_ODR": "2",
        "MINTS_HTML": SNCOUNCIL_HTML,
        "ORGINL_FILE_URL": "https://www.sncouncil.go.kr/record/HwpDownload.do?key=b1c0",
        "MTGNM": "본회의",
    }
]

# Errors are a bare object; a list call with listCount=500 and an unknown docid, a one-element array.
BAD_KEY = {
    "SERVICE": "minutes",
    "RESULT_CODE": "ERROR01",
    "RESULT_MESSAGE": "인증키가 유효하지 않습니다.",
}
QUOTA = {
    "SERVICE": "minutes",
    "RESULT_CODE": "ERROR09",
    "RESULT_MESSAGE": "일별 허용 트래픽을 초과 하였습니다.",
}
LIST_COUNT_TOO_BIG = [
    {
        "SERVICE": "minutes",
        "RESULT_CODE": "ERROR05",
        "RESULT_MESSAGE": "리스트개수(listCount) 값이 유효하지 않습니다.",
    }
]
NO_SUCH_DOC = [
    {"SERVICE": "minutes", "RESULT_CODE": "SUCCESS", "RESULT_MESSAGE": "정상 처리되었습니다."}
]


def _adapter(
    pages: list[list[dict]], details: dict[str, object], **kwargs: object
) -> tuple[ClikMinutesAdapter, list[dict[str, list[str]]]]:
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = parse_qs(urlsplit(str(request.url)).query)
        seen.append(q)
        assert request.url.path == "/openapi/minutes.do"
        if q["displayType"] == ["list"]:
            page = int(q["startCount"][0]) // int(q["listCount"][0])
            return httpx.Response(200, json=_list(pages[page]) if page < len(pages) else _list([]))
        return httpx.Response(200, json=details.get(q["docid"][0], NO_SUCH_DOC))

    client = ResilientClient(
        "clik_minutes",
        base_url="https://clik.example",
        limiter=MemoryLimiter(),
        breaker=MemoryBreaker(),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        max_attempts=1,
    )
    return ClikMinutesAdapter(client, "SECRET", **kwargs), seen  # type: ignore[arg-type]


def test_html_to_text_drops_page_chrome_and_keeps_the_speaker_gap() -> None:
    text = html_to_text(SNCOUNCIL_HTML)
    lines = text.splitlines()
    assert "Toggle navigation" not in text and "굴림" not in text and "제312회" not in lines
    assert lines[0] == "[본 회의록은 최종 교정 전 임시회의록이므로 법적 효력이 없습니다]"
    assert "○의장 강상태  의석을 정돈해 주시기 바랍니다." in lines
    assert "○박기범의원  존경하는 92만 성남 시민 여러분!" in lines
    assert "○교통도로국장 유동  내년도 본예산에 반영하겠습니다." in lines
    # the member line is read as a member, not as role "박기범의원" + name "존경하는"
    member = next(line for line in lines if line.startswith("○박기범"))
    m = GLUED_MEMBER_RE.match(member)
    assert m and m["name"] == "박기범"


async def test_clik_reads_the_live_envelope_and_maps_a_meeting() -> None:
    page = [
        _row("CLIKC2955407453539737", "20260907", "2"),
        _row("CLIKC2946767454052674", "20260907", "2"),
    ]
    adapter, seen = _adapter([page], {"CLIKC2955407453539737": DETAIL})
    recs = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 1), date(2026, 9, 28)))]
    assert len(recs) == 1
    rec = recs[0]
    assert rec.external_id == "031013:10:312:2:20260907:본회의"
    assert rec.title == "경기도 성남시의회 제312회 본회의 제2차 (2026-09-07)"
    assert rec.published_at == date(2026, 9, 7)
    assert rec.publisher_raw == "경기도 성남시의회"
    assert rec.url and rec.url.endswith("DOCID=CLIKC2955407453539737")
    assert rec.content and "○박기범의원  존경하는" in rec.content.decode()
    assert rec.structured["revision_docids"] == ["CLIKC2955407453539737", "CLIKC2946767454052674"]
    assert rec.structured["original_file_url"].startswith("https://www.sncouncil.go.kr/")
    # two revisions of one meeting cost one detail call
    assert [q["displayType"][0] for q in seen] == ["list", "detail"]
    assert seen[0]["sort"] == ["MTG_DE/DESC"] and seen[0]["listCount"] == ["100"]
    assert "startDate" not in seen[0]  # the API has no date filter
    assert adapter.stats["revisions"] == 1 and adapter.stats["details"] == 1


async def test_clik_pages_newest_first_until_the_window_is_passed() -> None:
    page1 = [_row(f"A{i}", "20260920", str(i % 5), sesn=str(300 + i)) for i in range(100)]
    page2 = [_row("B1", "20260902", "1"), _row("B2", "20260830", "1")] + [
        _row(f"C{i}", "20260801", "1", sesn=str(i)) for i in range(98)
    ]
    page3 = [_row("D1", "20260701", "1")]
    adapter, seen = _adapter(
        [page1, page2, page3], {}, overrides={"meeting_pattern": "본회의|예산결산"}
    )
    recs = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 1), date(2026, 9, 28)))]
    assert recs == []  # every detail answered "no such document"
    lists = [q for q in seen if q["displayType"] == ["list"]]
    assert [q["startCount"] for q in lists] == [["0"], ["100"]]  # page 3 is never asked for
    assert adapter.stats["in_window"] == 101 and adapter.stats["empty"] == 101


async def test_clik_filters_meetings_by_name() -> None:
    page = [
        _row("X1", "20260904", "1", name="예산결산특별위원회"),
        _row("X2", "20260904", "1", name="도시건설위원회"),
    ]
    adapter, seen = _adapter([page], {}, overrides={"meeting_pattern": "본회의|예산결산"})
    _ = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 1), date(2026, 9, 28)))]
    assert [q["docid"] for q in seen if q["displayType"] == ["detail"]] == [["X1"]]
    assert adapter.stats["skipped:meeting"] == 1


async def test_clik_skips_stored_meetings_and_caps_detail_calls() -> None:
    page = [_row("K1", "20260907", "2"), _row("N1", "20260906", "1"), _row("N2", "20260905", "1")]
    adapter, seen = _adapter([page], {}, overrides={"max_details": 1})
    assert isinstance(adapter, SkipsStored)

    async def known(ids: list[str]) -> set[str]:
        return {external_id(meeting_key(page[0]["ROW"]))} & set(ids)

    adapter.known_external_ids = known
    _ = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 1), date(2026, 9, 28)))]
    assert [q["docid"] for q in seen if q["displayType"] == ["detail"]] == [["N1"]]
    assert adapter.stats["skipped:known"] == 1 and adapter.stats["skipped:cap"] == 1


async def test_clik_refetches_new_docid_even_when_old_revision_is_listed_first() -> None:
    page = [_row("OLD", "20260907", "2"), _row("CORRECTED", "20260907", "2")]
    adapter, seen = _adapter([page], {"CORRECTED": DETAIL})

    async def known(ids: list[str]) -> set[str]:
        return set(ids)

    async def revisions(ids: list[str]) -> dict[str, dict]:
        return {ids[0]: {"docid": "OLD", "revision_docids": ["OLD"]}}

    adapter.known_external_ids = known
    adapter.known_revision_metadata = revisions
    records = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 1), date(2026, 9, 28)))]
    assert len(records) == 1
    assert records[0].structured["docid"] == "CORRECTED"
    assert records[0].structured["revision_docids"] == ["OLD", "CORRECTED"]
    assert [q["docid"] for q in seen if q["displayType"] == ["detail"]] == [["CORRECTED"]]


async def test_clik_unchanged_revision_set_skips_detail_despite_list_reordering() -> None:
    page = [_row("B", "20260907", "2"), _row("A", "20260907", "2")]
    adapter, seen = _adapter([page], {"A": DETAIL, "B": DETAIL})

    async def revisions(ids: list[str]) -> dict[str, dict]:
        return {ids[0]: {"docid": "A", "revision_docids": ["A", "B"]}}

    adapter.known_revision_metadata = revisions
    records = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 1), date(2026, 9, 28)))]
    assert records == []
    assert [q["displayType"] for q in seen] == [["list"]]


async def test_clik_legacy_docid_metadata_detects_revision_without_prior_list() -> None:
    page = [_row("OLD", "20260907", "2"), _row("NEW", "20260907", "2")]
    adapter, _ = _adapter([page], {"NEW": DETAIL})

    async def revisions(ids: list[str]) -> dict[str, dict]:
        return {ids[0]: {"docid": "OLD"}}

    adapter.known_revision_metadata = revisions
    records = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 1), date(2026, 9, 28)))]
    assert len(records) == 1
    assert records[0].structured["docid"] == "NEW"


@pytest.mark.parametrize("cap", [None, 1])
async def test_clik_incremental_window_revisits_only_stored_older_meetings(cap: int | None) -> None:
    pages = [
        [_row("TODAY", "20260929", "1"), _row("UNKNOWN", "20260920", "1")],
        [_row("OLD", "20260907", "2"), _row("NEW", "20260907", "2")],
        [_row("TOO_OLD", "20260801", "3")],
    ]
    adapter, seen = _adapter(
        pages,
        {docid: {"MINTS_HTML": "<p>회의록</p>"} for docid in ("TODAY", "NEW", "UNKNOWN")},
        overrides={"page_size": 2, "max_details": cap},
    )
    stored_id = external_id(meeting_key(pages[1][0]["ROW"]))
    too_old_id = external_id(meeting_key(pages[2][0]["ROW"]))
    checked: list[str] = []

    async def revisions(ids: list[str]) -> dict[str, dict]:
        checked.extend(ids)
        return {stored_id: {"docid": "OLD", "revision_docids": ["OLD"]}}

    adapter.known_revision_metadata = revisions
    records = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 28), date(2026, 9, 29)))]
    expected = ["TODAY", "NEW"] if cap is None else ["TODAY"]
    assert [record.structured["docid"] for record in records] == expected
    assert stored_id in checked and too_old_id not in checked
    assert [q["startCount"] for q in seen if q["displayType"] == ["list"]] == [["0"], ["2"], ["4"]]
    assert [q["docid"][0] for q in seen if q["displayType"] == ["detail"]] == expected
    assert adapter.stats["skipped:unknown_lookback"] == 1
    assert adapter.stats["skipped:cap"] == (0 if cap is None else 1)


async def test_clik_revision_lookback_is_configurable_and_keeps_wider_backfill() -> None:
    pages = [[_row("OLD", "20260907", "2"), _row("NEW", "20260907", "2")]]

    async def revisions(ids: list[str]) -> dict[str, dict]:
        return {ids[0]: {"docid": "OLD"}} if ids else {}

    short, short_seen = _adapter(pages, {"NEW": DETAIL}, overrides={"revision_lookback_days": 7})
    short.known_revision_metadata = revisions
    assert [r async for r in short.fetch(FetchWindow(date(2026, 9, 28), date(2026, 9, 29)))] == []
    assert [q["displayType"] for q in short_seen] == [["list"]]

    wider, wider_seen = _adapter(pages, {"NEW": DETAIL}, overrides={"revision_lookback_days": 7})
    wider.known_revision_metadata = revisions
    records = [r async for r in wider.fetch(FetchWindow(date(2026, 8, 1), date(2026, 9, 29)))]
    assert [record.structured["docid"] for record in records] == ["NEW"]
    assert [q["docid"] for q in wider_seen if q["displayType"] == ["detail"]] == [["NEW"]]


async def test_clik_without_revision_callback_keeps_requested_window() -> None:
    pages = [
        [_row("TODAY", "20260929", "1"), _row("OLD", "20260907", "2")],
        [_row("NEW", "20260907", "2")],
    ]
    adapter, seen = _adapter(
        pages, {"TODAY": {"MINTS_HTML": "<p>회의록</p>"}}, overrides={"page_size": 2}
    )
    records = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 28), date(2026, 9, 29)))]
    assert [record.structured["docid"] for record in records] == ["TODAY"]
    assert [q["startCount"] for q in seen if q["displayType"] == ["list"]] == [["0"]]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (BAD_KEY, FatalSourceError),
        (LIST_COUNT_TOO_BIG, FatalSourceError),
        (QUOTA, QuotaExhaustedError),
    ],
)
async def test_clik_result_codes_stop_the_source(answer: object, error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answer)

    client = ResilientClient(
        "clik_minutes",
        base_url="https://clik.example",
        limiter=MemoryLimiter(),
        breaker=MemoryBreaker(),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    adapter = ClikMinutesAdapter(client, "SECRET")
    with pytest.raises(error) as info:
        _ = [r async for r in adapter.fetch(FetchWindow(date(2026, 9, 1), date(2026, 9, 28)))]
    assert "SECRET" not in str(info.value)


def test_clik_external_id_fits_the_column() -> None:
    row = _row("L", "20260907", "1", name="성남시재개발·재건축신속추진을위한특별위원회" * 4)["ROW"]
    ext = external_id(meeting_key(row))
    assert len(ext) <= 128 and ext.startswith("031013:10:312:1:20260907:")
    assert json.dumps(ext)


async def test_sources_check_reads_clik_list_and_one_detail() -> None:
    from app.sources.check import check_clik, problems, render

    page = [_row("CLIKC2946767454052674", "20260907", "2"), _row("CLIKC1", "20260904", "1")]

    def handler(request: httpx.Request) -> httpx.Response:
        q = parse_qs(urlsplit(str(request.url)).query)
        if q["displayType"] == ["list"]:
            return httpx.Response(200, json=_list(page))
        return httpx.Response(200, json=DETAIL)

    checks = await check_clik("SECRET", transport=httpx.MockTransport(handler))
    assert [(c.ok, c.items, c.mapped) for c in checks] == [(True, 2, 2), (True, 1, 1)]
    assert checks[0].total == 7338 and not problems(checks)
    md = render(checks, days=7)
    assert md.startswith("# CLIK API") and "SECRET" not in md


async def test_sources_check_masks_the_clik_key_on_error() -> None:
    from app.sources.check import check_clik, problems

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=BAD_KEY)

    checks = await check_clik("SECRET", transport=httpx.MockTransport(handler))
    assert not checks[0].ok and "ERROR01" in (checks[0].error or "")
    assert all("SECRET" not in (c.error or "") for c in checks)
    assert len(problems(checks)) == 2
