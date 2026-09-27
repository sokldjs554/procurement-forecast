import ssl
from collections.abc import Callable
from datetime import date

import httpx
import pytest

from app.sources.base import FetchWindow
from app.sources.crawler import (
    BoardCrawlerAdapter,
    RobotsRules,
    budget_title_facts,
    canonical_url,
    compile_script_links,
    parse_attachments,
    parse_board_rows,
    sniff_mime,
)
from app.sources.http import ResilientClient, legacy_cipher_context
from app.sources.resilience import MemoryBreaker, MemoryLimiter

PDF = b"%PDF-1.7\n" + b"x" * 100
HWP5 = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 100
HWPX = b"PK\x03\x04" + b"\x00" * 26 + b"mimetypeapplication/hwp+zip Contents/section0.xml"


def test_board_rows_take_title_link_and_row_date() -> None:
    html = """
    <table><thead><tr><th>번호</th><th>제목</th><th>등록일</th></tr></thead><tbody>
      <tr><td>12</td><td><a href="view.do?nttId=77&amp;menuNo=1">2027년도 강남구 본예산서</a></td>
          <td>2026.12.18</td></tr>
      <tr><td>11</td><td><a href="/other/list.do">게시판 이동</a></td><td>2026-12-01</td></tr>
    </tbody></table>"""
    rows = parse_board_rows(html, "https://gov.example/board/list.do", r"view\.do")
    assert len(rows) == 1
    assert rows[0].title == "2027년도 강남구 본예산서"
    assert rows[0].url == "https://gov.example/board/view.do?nttId=77&menuNo=1"
    assert rows[0].posted == date(2026, 12, 18)


def test_attachments_by_url_pattern_or_document_name_without_duplicates() -> None:
    html = """
      <a href="/cmm/fms/FileDown.do?atchFileId=A1&fileSn=0">예산서.hwp</a>
      <a href="/files/2027_budget.pdf">2027 예산서.pdf</a>
      <a href="/cmm/fms/FileDown.do?atchFileId=A1&fileSn=0">예산서.hwp (다시)</a>
      <a href="/board/list.do">목록</a>"""
    found = parse_attachments(html, "https://gov.example/board/view.do", r"FileDown|download")
    assert [a.href for a in found] == [
        "https://gov.example/cmm/fms/FileDown.do?atchFileId=A1&fileSn=0",
        "https://gov.example/files/2027_budget.pdf",
    ]


def test_type_comes_from_bytes_not_names() -> None:
    assert sniff_mime(PDF) == "application/pdf"
    assert sniff_mime(HWP5) == "application/x-hwp"
    assert sniff_mime(HWPX) == "application/hwp+zip"
    assert sniff_mime(b"\x89PNG\r\n\x1a\n") is None
    assert (
        sniff_mime("<html>로그인이 필요합니다</html>".encode()) is None
    )  # error page saved as .pdf


def test_budget_title_facts_and_canonical_urls() -> None:
    assert budget_title_facts("2027년도 서울특별시 강남구 제1회 추가경정예산서") == {
        "fiscal_year": 2027,
        "budget_kind": "제1회 추가경정",
    }
    assert budget_title_facts("2027년도 본예산서")["budget_kind"] == "본"
    assert (
        canonical_url("https://Gov.Example/view.do?nttId=7&jsessionid=X&menuNo=3#top", ("nttId",))
        == "https://gov.example/view.do?nttId=7"
    )


# -- the adapter against a small fake site ------------------------------------------------------
def _site(
    *,
    robots: httpx.Response | None = None,
    posts: int = 12,
    big_file: bool = False,
) -> Callable[[httpx.Request], httpx.Response]:
    """Newest-first board, 5 rows per page, one budget book per post (day i of Jan 2026)."""
    items = [(f"P{i}", f"2026년도 예산서 {i}", date(2026, 1, i)) for i in range(posts, 0, -1)]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        if path == "/robots.txt":
            return robots or httpx.Response(200, text="User-agent: *\nDisallow: /secret/\n")
        if path == "/list.do":
            page = int(params.get("pageIndex", "1"))
            chunk = items[(page - 1) * 5 : page * 5]
            rows = "".join(
                f'<tr><td><a href="view.do?nttId={n}">{t}</a></td><td>{d:%Y-%m-%d}</td></tr>'
                for n, t, d in chunk
            )
            return httpx.Response(200, text=f"<table>{rows}</table>")
        if path == "/view.do":
            n = params["nttId"]
            return httpx.Response(200, text=f'<a href="/download.do?id={n}">{n}.pdf</a>')
        if path == "/download.do":
            body = PDF + (b"y" * 3 * 1024 * 1024 if big_file else b"")
            return httpx.Response(200, content=body)
        return httpx.Response(404)

    return handler


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    breaker: MemoryBreaker | None = None,
    **config: object,
) -> tuple[BoardCrawlerAdapter, list[str]]:
    requested: list[str] = []
    breaker = breaker or MemoryBreaker()

    def recording(request: httpx.Request) -> httpx.Response:
        requested.append(f"{request.url.path}?{request.url.query.decode()}")
        return handler(request)

    transport = httpx.MockTransport(recording)

    async def no_sleep(_s: float) -> None:
        return None

    def client_for_host(host: str) -> ResilientClient:
        return ResilientClient(
            f"test@{host}",
            base_url=f"https://{host}",
            limiter=MemoryLimiter(),
            breaker=breaker,
            max_attempts=2,
            transport=transport,
            sleep=no_sleep,
        )

    cfg = {
        "boards": [{"url": "https://gov.example/list.do", "institution_code": "LG-1"}],
        "detail_pattern": r"view\.do",
        "attachment_pattern": r"download\.do",
        "delay_seconds": 0,
        **config,
    }
    return BoardCrawlerAdapter("test", cfg, client_for_host), requested


async def _crawl(adapter: BoardCrawlerAdapter, since: date) -> list[str]:
    out = [r.external_id async for r in adapter.fetch(FetchWindow(since, date(2026, 12, 31)))]
    await adapter.aclose()
    return out


async def test_incremental_crawl_stops_paging_once_rows_predate_the_window() -> None:
    adapter, requested = _adapter(_site())
    ids = await _crawl(adapter, since=date(2026, 1, 9))
    assert ids == ["P12", "P11", "P10", "P9"]
    # page 1 (P12..P8) already reaches back past Jan 9 → page 2 is never requested
    assert not any("pageIndex=2" in r for r in requested)
    assert adapter.stats.files == 4


async def test_full_crawl_walks_pages_until_the_board_runs_out() -> None:
    adapter, requested = _adapter(_site(posts=12))
    ids = await _crawl(adapter, since=date(2025, 1, 1))
    assert len(ids) == 12
    assert sum("list.do" in r for r in requested) == 4  # pages 1-3, then an empty page 4


async def test_robots_disallow_blocks_the_board() -> None:
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /list.do\n")
    adapter, requested = _adapter(_site(robots=robots))
    assert await _crawl(adapter, since=date(2025, 1, 1)) == []
    assert adapter.stats.skipped_robots == 1
    assert requested == ["/robots.txt?"]


@pytest.mark.parametrize(
    ("status", "crawled"),
    [(404, True), (503, False)],  # RFC 9309: 4xx = no rules; 5xx/unreachable = stay away
)
async def test_robots_errors_follow_rfc_9309(status: int, crawled: bool) -> None:
    adapter, _ = _adapter(_site(robots=httpx.Response(status), posts=3))
    ids = await _crawl(adapter, since=date(2025, 1, 1))
    assert bool(ids) is crawled


async def test_oversized_files_are_dropped_not_buffered() -> None:
    adapter, _ = _adapter(_site(big_file=True, posts=2), max_file_mb=1)
    assert await _crawl(adapter, since=date(2025, 1, 1)) == []
    assert adapter.stats.skipped_size == 2


# -- shapes met on real 지자체 sites (trimmed copies, 2026-09-27) --------------------------------
# www.gangnam.go.kr/robots.txt as served: a wildcard path and a second ``*`` group.
GANGNAM_ROBOTS = """User-agent: *
Disallow: /assign/*
Disallow: /file/*
Disallow: /ceremony/*

User-Agent: Googlebot-Image
Disallow: /assign/*
Disallow: /board/pressroom_data/*

User-agent: *
Disallow: /board/B_000057/*
"""

# 강남구 예산 board (B_000742): the title link is ``javascript:;`` and the file sits in the row.
GANGNAM_ROWS = """
<table class="table table-hover"><tbody class="grid">
<tr class="grid-item">
<td class="num">117</td>
<td class="align-l tit">
<a href="javascript:;"   class="linkEmpty" >
2026년도 강남구 예산서
</a>
</td>
<td class="fil">
<a href="/file/1/get/5ed06c6e-d200-402f-b64b-3d158c7da1e7/download.do" title="다운로드">
<img src="/assets/images/icon/file_pdf.png" alt="2026년 강남구 예산서(최종).pdf 다운로드">
</a>
<a href="/file/1/get/5ed06c6e-d200-402f-b64b-3d158c7da1e7/preview.do" onclick="window.open(this.href, '미리보기');return false;" class="btn-preview" title="새창열림">미리보기</a>
</td>
<td>
본예산
</td>
<td>2026-01-13</td>
</tr>
<tr class="grid-item">
<td class="num">115</td>
<td class="align-l tit">
<a href="javascript:;"   class="linkEmpty" >
2025년 제2회 강남구 추가경정예산서
</a>
</td>
<td class="fil">
<a href="/file/1/get/0b1c2d3e-0000-0000-0000-000000000115/download.do" title="다운로드">
<img src="/assets/images/icon/file_pdf.png" alt="2025년 제2회 추가경정예산서.pdf 다운로드">
</a>
</td>
<td>
추가경정예산
</td>
<td>2025-09-30</td>
</tr>
</tbody></table>"""

# 성남시 /cn03050201: one page for every year; the real URL is built by site JS from the onclick.
SEONGNAM_PAGE = """
<div id="pdf_list_wrap" class="mb50 bgt-pdf">
<div data-year="339">
<h4 class="htitle">2026년 세입세출예산서</h4>
<ul class="pdf_list">
<li class="wd100">
<p>세입·세출예산규모 및 총괄표</p>
<span class="btn sm new view">
<a href="#viewer"
onclick="javascript:fileView('/contents/down/budget/2026/','11608_1.pdf','11608_1.pdf'); return false;"
target="_blank" title="새창열림">바로보기</a>
</span>
<span class="btn down">
<a href="#download"
onclick="javascript:fileDownload('/contents/down/budget/2026/','11608_1.pdf','11608_1.pdf'); return false;"
title="PDF 다운로드">다운로드</a>
</span>
</li>
</ul>
<h5 class="harrow">일반회계</h5>
<h6 class="hdot">세출예산사업명세서</h6>
<ul class="pdf_list">
<li>전체 <span class="btn sm new view">
<a href="#viewer"
onclick="javascript:fileView('/contents/down/budget/2026/','11608_3.pdf','11608_3.pdf'); return false;"
target="_blank" title="새창열림">바로보기</a>
</span>
<span class="btn down">
<a href="#download"
onclick="javascript:fileDownload('/contents/down/budget/2026/','11608_3.pdf','11608_3.pdf'); return false;"
title="PDF 다운로드">다운로드</a>
</span>
</li>
<li>의회사무국 <span class="btn down">
<a href="#download"
onclick="javascript:fileDownload('/contents/down/budget/2026/','11608_3_1.pdf','11608_3_1.pdf'); return false;"
title="PDF 다운로드">다운로드</a>
</span>
</li>
</ul>
<h5 class="harrow">상수도공기업특별회계</h5>
<ul class="pdf_list">
<li class="wd100">
<p>세출예산사업명세서</p>
<span class="btn down">
<a href="#download"
onclick="javascript:fileDownload('/contents/down/budget/2026/','11608_6_1.pdf','11608_6_1.pdf'); return false;"
title="PDF 다운로드">다운로드</a>
</span>
</li>
</ul>
</div>
<div data-year="340">
<h4 class="htitle">2026년 1회 추경 세입세출예산서</h4>
<h6 class="hdot">세출예산사업명세서</h6>
<ul class="pdf_list">
<li class="wd100">
<p>세출예산사업명세서</p>
<span class="btn down">
<a href="#download"
onclick="javascript:fileDownload('/contents/down/budget/2026/','11610_1_3.pdf','11610_1_3.pdf'); return false;"
title="PDF 다운로드">다운로드</a>
</span>
</li>
</ul>
</div>
<div data-year="337">
<h4 class="htitle">2024년 세입세출예산서</h4>
<h5 class="harrow">일반회계</h5>
<h6 class="hdot">세출예산사업명세서</h6>
<ul class="pdf_list">
<li>전체 <span class="btn down">
<a href="#download"
onclick="javascript:fileDownload('/contents/down/budget/2024/','11570_3.pdf','11570_3.pdf'); return false;"
title="PDF 다운로드">다운로드</a>
</span>
</li>
</ul>
</div>
</div>"""
SEONGNAM_SCRIPT_LINKS = [
    {
        "pattern": r"fileDownload\('[^']*/(20\d\d)/','([^']+\.pdf)'",
        "url": r"/humanframe/file/sncity/bgt/\1/\2",
    }
]
# 일반회계 세출예산사업명세서 only: "전체" for 본예산, the single file for each 추경.
SEONGNAM_TITLE = r"세입세출예산서 › (일반회계 › )?세출예산사업명세서( › 전체)?$"


def test_robots_reads_wildcards_and_every_star_group() -> None:
    rules = RobotsRules(GANGNAM_ROBOTS)
    assert not rules.can_fetch("https://www.gangnam.go.kr/file/1/get/5ed06c6e/download.do")
    assert not rules.can_fetch("https://www.gangnam.go.kr/board/B_000057/list.do")
    assert rules.can_fetch("https://www.gangnam.go.kr/board/B_000742/list.do?mid=ID05_050302")
    assert rules.can_fetch("https://www.gangnam.go.kr/robots.txt")
    # longest rule wins, Allow wins a tie, and ``$`` anchors the end
    rules = RobotsRules("User-agent: *\nDisallow: /a\nAllow: /a/b\nDisallow: /*.pdf$\n")
    assert rules.can_fetch("https://x.example/a/b/c")
    assert not rules.can_fetch("https://x.example/a/c")
    assert not rules.can_fetch("https://x.example/f/x.pdf")
    assert rules.can_fetch("https://x.example/f/x.pdf?download=1")
    # a group naming our agent replaces the ``*`` groups
    ours = RobotsRules(
        "User-agent: *\nDisallow: /\n\nUser-agent: Procurement-Forecast-Collector\nAllow: /\n"
    )
    assert ours.can_fetch("https://x.example/list.do")


def test_list_rows_that_carry_their_files_are_posts() -> None:
    rows = parse_board_rows(
        GANGNAM_ROWS,
        "https://www.gangnam.go.kr/board/B_000742/list.do?mid=ID05_050302",
        r"B_000742/view\.do",
        r"/download\.do",
    )
    assert [(r.title, r.posted) for r in rows] == [
        ("2026년도 강남구 예산서", date(2026, 1, 13)),
        ("2025년 제2회 강남구 추가경정예산서", date(2025, 9, 30)),
    ]
    assert [a.href for a in rows[0].attachments] == [
        "https://www.gangnam.go.kr/file/1/get/5ed06c6e-d200-402f-b64b-3d158c7da1e7/download.do"
    ]
    assert rows[0].url == rows[0].attachments[0].href
    # without an attachment pattern such rows are not posts (the old behaviour)
    assert parse_board_rows(GANGNAM_ROWS, "https://x/", r"B_000742/view\.do") == []
    # and the default ``view\.do`` would take the "미리보기" link (preview.do) for a post
    assert [r.title for r in parse_board_rows(GANGNAM_ROWS, "https://x/", r"view\.do")] == [
        "미리보기"
    ]


def test_script_links_and_headings_give_url_and_title() -> None:
    found = parse_attachments(
        SEONGNAM_PAGE,
        "https://www.seongnam.go.kr/cn03050201",
        r"(?!)",
        compile_script_links(SEONGNAM_SCRIPT_LINKS),
    )
    assert [(a.href.rsplit("/", 2)[-2:], a.title) for a in found] == [
        (["2026", "11608_1.pdf"], "2026년 세입세출예산서 › 세입·세출예산규모 및 총괄표"),
        (["2026", "11608_3.pdf"], "2026년 세입세출예산서 › 일반회계 › 세출예산사업명세서 › 전체"),
        (
            ["2026", "11608_3_1.pdf"],
            "2026년 세입세출예산서 › 일반회계 › 세출예산사업명세서 › 의회사무국",
        ),
        (
            ["2026", "11608_6_1.pdf"],
            "2026년 세입세출예산서 › 상수도공기업특별회계 › 세출예산사업명세서",
        ),
        (["2026", "11610_1_3.pdf"], "2026년 1회 추경 세입세출예산서 › 세출예산사업명세서"),
        (["2024", "11570_3.pdf"], "2024년 세입세출예산서 › 일반회계 › 세출예산사업명세서 › 전체"),
    ]
    assert found[1].href == "https://www.seongnam.go.kr/humanframe/file/sncity/bgt/2026/11608_3.pdf"
    # the fileView twin of each download link is not a second attachment
    assert len({a.href for a in found}) == len(found)


def test_budget_title_facts_on_real_titles() -> None:
    assert budget_title_facts("2025년 제2회 강남구 추가경정예산서") == {
        "fiscal_year": 2025,
        "budget_kind": "제2회 추가경정",
    }
    assert budget_title_facts("2026년 1회 추경 세입세출예산서 › 세출예산사업명세서") == {
        "fiscal_year": 2026,
        "budget_kind": "제1회 추가경정",
    }
    assert budget_title_facts("2026년도 강남구 예산서") == {
        "fiscal_year": 2026,
        "budget_kind": "본",
    }
    assert budget_title_facts("2025회계연도 지방보조사업 운용평가 결과")["fiscal_year"] == 2025


def _real_site(robots: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if path == "/board/B_000742/list.do":
            page = request.url.params.get("pgno", "1")
            return httpx.Response(200, text=GANGNAM_ROWS if page == "1" else "<table></table>")
        if path == "/cn03050201":
            return httpx.Response(200, text=SEONGNAM_PAGE)
        if path.startswith(("/file/", "/humanframe/file/")):
            return httpx.Response(
                200, content=PDF, headers={"Last-Modified": "Thu, 18 Jun 2026 13:16:30 GMT"}
            )
        return httpx.Response(404)

    return handler


async def test_board_files_in_rows_still_obey_robots() -> None:
    board = {
        "url": "https://www.gangnam.go.kr/board/B_000742/list.do?mid=ID05_050302",
        "institution_code": "LG-11680",
        "detail_pattern": r"B_000742/view\.do",
        "attachment_pattern": r"/download\.do",
        "page_param": "pgno",
    }
    adapter, requested = _adapter(_real_site(GANGNAM_ROBOTS), boards=[board])
    assert await _crawl(adapter, since=date(2025, 1, 1)) == []
    assert adapter.stats.posts == 2
    assert adapter.stats.skipped_robots == 2  # /file/* is disallowed → nothing downloaded
    assert not any(r.startswith("/file/") for r in requested)

    adapter, _ = _adapter(_real_site("User-agent: *\nDisallow:\n"), boards=[board])
    ids = await _crawl(adapter, since=date(2025, 1, 1))
    assert ids == [
        "/file/1/get/5ed06c6e-d200-402f-b64b-3d158c7da1e7/download.do",
        "/file/1/get/0b1c2d3e-0000-0000-0000-000000000115/download.do",
    ]


async def test_page_layout_picks_by_title_fiscal_year_and_cap() -> None:
    board = {
        "url": "https://www.seongnam.go.kr/cn03050201",
        "institution_code": "LG-41130",
        "publisher": "경기도 성남시",
        "layout": "page",
        "attachment_pattern": r"(?!)",
        "script_links": SEONGNAM_SCRIPT_LINKS,
        "title_keywords": [],
        "title_pattern": SEONGNAM_TITLE,
    }
    adapter, requested = _adapter(_real_site("User-agent: *\nDisallow: /api/\n"), boards=[board])
    window = FetchWindow(date(2025, 8, 23), date(2026, 9, 27))
    records = [r async for r in adapter.fetch(window)]
    assert [(r.title, r.structured["budget_kind"]) for r in records] == [
        ("2026년 세입세출예산서 › 일반회계 › 세출예산사업명세서 › 전체", "본"),
        ("2026년 1회 추경 세입세출예산서 › 세출예산사업명세서", "제1회 추가경정"),
    ]
    assert records[0].published_at == date(2026, 6, 18)  # Last-Modified, in KST
    assert records[0].structured["published_from"] == "last_modified"
    assert records[0].external_id == "/humanframe/file/sncity/bgt/2026/11608_3.pdf"
    assert adapter.stats.skipped_title == 3
    assert adapter.stats.skipped_window == 1  # 2024 is before the window
    assert sum(r.startswith("/humanframe/") for r in requested) == 2

    capped, requested = _adapter(_real_site(""), boards=[board], max_files=1)
    assert len([r async for r in capped.fetch(window)]) == 1
    assert capped.stats.skipped_cap == 1
    assert sum(r.startswith("/humanframe/") for r in requested) == 1


async def test_a_host_whose_circuit_is_open_skips_only_its_own_boards() -> None:
    breaker = MemoryBreaker(failure_threshold=1)
    await breaker.record_failure("test@down.example")  # failed in an earlier run
    boards = [
        {"url": "https://down.example/list.do", "institution_code": "LG-1"},
        {"url": "https://gov.example/list.do", "institution_code": "LG-2"},
    ]
    adapter, _ = _adapter(_site(posts=2), breaker, boards=boards)
    ids = await _crawl(adapter, since=date(2025, 1, 1))
    assert ids == ["P2", "P1"]
    assert adapter.stats.skipped_robots == 1


def test_legacy_ciphers_keep_certificate_checks() -> None:
    # www.seongnam.go.kr offers only TLS 1.2 AES128-SHA; Python's default list has no such suite
    ctx = legacy_cipher_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname
    assert "AES128-SHA" in {c["name"] for c in ctx.get_ciphers()}
    assert "AES128-SHA" not in {c["name"] for c in ssl.create_default_context().get_ciphers()}
