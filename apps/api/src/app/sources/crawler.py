"""Board crawler — 지자체 누리집 게시판에서 첨부 문서(예산서·사업설명서 등)를 수집합니다.

지방재정365가 법정 예산서를 모아 주지만, 추경 예산서·사업설명서·기본계획처럼 기관 누리집의
"예산 공개" / "고시·공고" 게시판에 HWP·PDF 첨부로만 먼저 올라오는 문서가 많고, 여기에는 API가
없습니다. 이 어댑터는 게시판 목록 → 상세 → 첨부를 따라가며 문서를 가져옵니다.

A crawler that gets blocked collects nothing, so politeness is part of correctness:

* **robots.txt** is fetched once per host per run and obeyed as RFC 9309 reads it: every group
  for our agent (or for ``*``) counts, ``*`` and ``$`` are wildcards, and the longest rule wins.
  A 4xx means no rules; a 5xx or unreachable robots.txt means "disallow everything" this run.
* **Rate**: every request goes through the shared token bucket keyed per host
  (``<source>@<host>``), so several workers never add up to a burst; on top of that each host
  gets a minimum delay between requests (``delay_seconds``, default 1s).
* **Incremental**: boards list newest first; paging stops at the first page whose oldest row
  predates the fetch window (the ingest cursor keeps a 3-day overlap for late posts).
* **Untrusted files**: attachments are capped (``max_file_mb``) while streaming, and the type is
  decided by magic bytes, not by the server's Content-Type or the file name. ``max_files`` caps
  how many files one run downloads.

Boards differ in markup but almost all are a table (or list) of rows with a title link and a
date. The parser keys on that shape plus two URL patterns from the source config, so adding a
government is configuration, not code::

    {"boards": [{"url": "https://www.gangnam.go.kr/board/B_000052/list.do",
                 "institution_code": "LG-11680", "publisher": "서울특별시 강남구"}],
     "doc_type": "budget_book", "title_keywords": ["예산서", "사업명세서"],
     "detail_pattern": "view\\.do", "attachment_pattern": "download|fileDown|atchFile",
     "id_param": "nttId", "page_param": "pageIndex", "max_pages": 20}

Real sites stray from that shape in three ways, each handled by configuration:

* **Files in the list row.** A row with no detail link but with attachment links is its own
  post; the files are taken from the row and no detail page is fetched.
* **Links only in JavaScript.** ``script_links`` turns ``onclick="fileDownload('/d/','a.pdf')"``
  into a URL: ``[{"pattern": "fileDownload\\('[^']*/(20\\d\\d)/','([^']+)'",
  "url": "/files/\\1/\\2"}]`` (``re`` group references).
* **One page, no board** (``"layout": "page"``). Every attachment on the page is a document;
  its title is the headings above it plus its list item's text, the fiscal year in that title
  decides whether it is in the window, and the file's ``Last-Modified`` is its date.

Any of ``layout``, ``detail_pattern``, ``attachment_pattern``, ``id_param``, ``page_param``,
``title_keywords``, ``title_pattern`` (a regex the title must match), ``script_links`` and
``title_attr`` (take the post link's ``title`` attribute as the post title) can be set per board, overriding the source-level value. ``legacy_tls_hosts`` lists hosts that only
speak old TLS cipher suites (see ``http.legacy_cipher_context``; certificates are still verified).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from app.clock import KST, today_kst
from app.log import get_logger
from app.sources.base import DocType, FetchWindow, RawRecord
from app.sources.http import (
    FatalSourceError,
    ResilientClient,
    ResponseTooLargeError,
    TransientSourceError,
)
from app.sources.resilience import CircuitOpenError

log = get_logger(__name__)

USER_AGENT = "procurement-forecast-collector"

_DATE_RE = re.compile(r"(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})")
# "2027년도", "2025년 제2회", "2026년 1회 추경", "2025회계연도"
_FISCAL_YEAR_RE = re.compile(r"(20\d{2})\s*(?:회계)?(?:년도|연도|년)")
_ROUND_RE = re.compile(r"(\d+)\s*회(?!계)")  # "제2회", "1회" — not "2025회계연도"
_DOC_EXT_RE = re.compile(r"\.(pdf|hwp|hwpx)\s*$", re.IGNORECASE)


# ------------------------------------------------------------------------------------------------
# robots.txt (RFC 9309)
# ------------------------------------------------------------------------------------------------
class RobotsRules:
    """robots.txt as RFC 9309 reads it. ``urllib.robotparser`` does not: it takes ``*`` in a
    path literally (so ``Disallow: /file/*`` blocks nothing) and keeps only the first
    ``User-agent: *`` group. Real 지자체 robots.txt files use both."""

    def __init__(self, text: str, agent: str = USER_AGENT) -> None:
        groups: list[tuple[set[str], list[tuple[bool, str]]]] = []
        current: tuple[set[str], list[tuple[bool, str]]] | None = None
        for raw in text.splitlines():
            key, sep, value = raw.split("#", 1)[0].partition(":")
            key, value = key.strip().lower(), value.strip()
            if not sep:
                continue
            if key == "user-agent":
                if current is None or current[1]:  # a user-agent line after rules starts a group
                    current = (set(), [])
                    groups.append(current)
                current[0].add(value.lower())
            elif key in ("allow", "disallow") and current is not None:
                current[1].append((key == "allow", value))
        token = agent.lower()
        mine = [rules for agents, rules in groups if token in agents]
        if not mine:
            mine = [rules for agents, rules in groups if "*" in agents]
        self._rules = [
            (allow, len(path), self._compile(path))
            for rules in mine
            for allow, path in rules
            if path
        ]

    @staticmethod
    def _compile(path: str) -> re.Pattern[str]:
        anchored = path.endswith("$")
        body = ".*".join(re.escape(part) for part in path.rstrip("$").split("*"))
        return re.compile(body + ("$" if anchored else ""))

    def can_fetch(self, url: str) -> bool:
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if path == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None  # (length, allow); longest wins, allow wins a tie
        for allow, length, pattern in self._rules:
            if pattern.match(path) and (best is None or (length, allow) > best):
                best = (length, allow)
        return best is None or best[1]


# ------------------------------------------------------------------------------------------------
# HTML parsing
# ------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class Link:
    href: str
    text: str
    onclick: str = ""
    context: tuple[str, ...] = ()  # headings above the link, then its row's own text
    tooltip: str = ""  # the link's title="…" attribute

    @property
    def title(self) -> str:
        parts: list[str] = []
        for part in self.context:
            if part and (not parts or parts[-1] != part):
                parts.append(part)
        return " › ".join(parts)


@dataclass(slots=True)
class BoardRow:
    title: str
    url: str
    posted: date | None
    attachments: list[Link] = field(default_factory=list)  # files listed in the row itself


@dataclass(slots=True)
class _Row:
    texts: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)  # text outside links
    links: list[Link] = field(default_factory=list)


ScriptLinks = list[tuple[re.Pattern[str], str]]


class _RowCollector(HTMLParser):
    """Collects table rows (or list items when the board has no table) with their links, and
    the headings each link sits under."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_Row] = []
        self.links: list[Link] = []
        self._row: _Row | None = None
        self._row_tag: str | None = None
        self._in_link = False
        self._href = ""
        self._onclick = ""
        self._tooltip = ""
        self._link_text: list[str] = []
        self._headings: dict[int, str] = {}
        self._heading: tuple[int, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("tr", "li") and self._row is None:
            self._row, self._row_tag = _Row(), tag
        elif tag == "a":
            a = dict(attrs)
            self._href, self._onclick = a.get("href") or "", a.get("onclick") or ""
            self._tooltip = " ".join((a.get("title") or "").split())
            self._in_link = bool(self._href or self._onclick)
            self._link_text = []
        elif len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self._heading = (int(tag[1]), [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link:
            labels = tuple(self._row.labels) if self._row is not None else ()
            link = Link(
                self._href,
                " ".join("".join(self._link_text).split()),
                self._onclick,
                (*(self._headings[k] for k in sorted(self._headings)), " ".join(labels)),
                self._tooltip,
            )
            self.links.append(link)
            if self._row is not None:
                self._row.links.append(link)
            self._in_link = False
        elif tag == self._row_tag and self._row is not None:
            self.rows.append(self._row)
            self._row, self._row_tag = None, None
        elif self._heading is not None and tag == f"h{self._heading[0]}":
            level, texts = self._heading
            self._headings = {k: v for k, v in self._headings.items() if k < level}
            self._headings[level] = " ".join(" ".join(texts).split())
            self._heading = None

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._link_text.append(data)
        if self._heading is not None:
            self._heading[1].append(data)
        if self._row is not None and data.strip():
            self._row.texts.append(data.strip())
            if not self._in_link:
                self._row.labels.append(data.strip())


def compile_script_links(rules: list[dict[str, str]] | None) -> ScriptLinks:
    return [(re.compile(r["pattern"]), r["url"]) for r in rules or []]


def _script_href(link: Link, script_links: ScriptLinks) -> str | None:
    """The URL a ``javascript:``/``onclick`` download link opens, per the configured rules."""
    for pattern, url in script_links:
        for code in (link.onclick, link.href):
            if m := pattern.search(code):
                return m.expand(url)
    return None


def parse_date(text: str) -> date | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _attachments(
    links: list[Link], base_url: str, attach_re: re.Pattern[str], script_links: ScriptLinks
) -> list[Link]:
    seen: set[str] = set()
    found: list[Link] = []
    for link in links:
        href = _script_href(link, script_links)
        if href is None:
            if not (attach_re.search(link.href) or _DOC_EXT_RE.search(link.text)):
                continue
            href = link.href
        url = urljoin(base_url, href)
        if url not in seen:
            seen.add(url)
            found.append(Link(url, link.text, context=link.context))
    return found


def parse_board_rows(
    html: str,
    base_url: str,
    detail_pattern: str,
    attachment_pattern: str | None = None,
    script_links: ScriptLinks | None = None,
    title_attr: bool = False,
) -> list[BoardRow]:
    """Rows that link to a post: title = link text, date = first date in the row.

    With ``title_attr`` the post link's ``title="…"`` attribute is the title when it has one:
    some boards print only a short label ("[임시] 본회의") and keep the full name there.

    With ``attachment_pattern``, a row that has no post link but lists files is a post by
    itself: its title is the first other link's text and its files come with it."""
    parser = _RowCollector()
    parser.feed(html)
    detail_re = re.compile(detail_pattern)
    attach_re = re.compile(attachment_pattern) if attachment_pattern else None
    rows: list[BoardRow] = []
    for row in parser.rows:
        posted = parse_date(" ".join(row.texts))
        link = next((ln for ln in row.links if detail_re.search(ln.href) and ln.text), None)
        if link is not None:
            name = (link.tooltip if title_attr else "") or link.text
            rows.append(BoardRow(name, urljoin(base_url, link.href), posted))
            continue
        if attach_re is None:
            continue
        files = _attachments(row.links, base_url, attach_re, script_links or [])
        file_urls = {f.href for f in files}
        title = next(
            (
                ln.text
                for ln in row.links
                if ln.text
                and urljoin(base_url, ln.href) not in file_urls
                and _script_href(ln, script_links or []) is None
            ),
            None,
        )
        if files and title:
            rows.append(BoardRow(title, files[0].href, posted, files))
    return rows


def parse_attachments(
    html: str, base_url: str, attachment_pattern: str, script_links: ScriptLinks | None = None
) -> list[Link]:
    """Links that look like file downloads (by URL pattern, by a document file name, or by a
    ``script_links`` rule), each with the headings it sits under."""
    parser = _RowCollector()
    parser.feed(html)
    return _attachments(parser.links, base_url, re.compile(attachment_pattern), script_links or [])


def sniff_mime(content: bytes) -> str | None:
    """Decide the document type from the bytes; ``None`` for anything we can't parse."""
    if content[:5] == b"%PDF-":
        return "application/pdf"
    if content[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):  # OLE2 compound file → HWP 5.x
        return "application/x-hwp"
    head = content[:65_536]
    if head[:4] == b"PK\x03\x04" and (b"Contents/section" in head or b"hwp+zip" in head):
        return "application/hwp+zip"  # HWPX (OWPML in a zip)
    return None


def budget_title_facts(title: str) -> dict[str, Any]:
    """ "2027년도 서울특별시 강남구 제1회 추가경정예산서" → fiscal year and 본/추경."""
    facts: dict[str, Any] = {}
    if m := _FISCAL_YEAR_RE.search(title):
        facts["fiscal_year"] = int(m.group(1))
    if "추가경정" in title or "추경" in title:
        n = _ROUND_RE.search(title)
        facts["budget_kind"] = f"제{int(n.group(1))}회 추가경정" if n else "추가경정"
    elif "본예산" in title or "예산서" in title:
        facts["budget_kind"] = "본"
    return facts


def canonical_url(url: str, keep: tuple[str, ...]) -> str:
    """Drop session/tracking parameters so the same post always has the same URL."""
    parts = urlsplit(url)
    query = {k: v for k, v in parse_qs(parts.query).items() if k in keep}
    return urlunsplit(
        (parts.scheme, parts.netloc.lower(), parts.path, urlencode(query, doseq=True), "")
    )


def file_key(url: str) -> str:
    """A file's id on its host: path and query. eGov boards tell files apart only by the query
    (``/cmm/fms/FileDown.do?atchFileId=…&fileSn=0``), so the path alone would give every file
    one id."""
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _last_modified(resp: httpx.Response) -> date | None:
    value = resp.headers.get("Last-Modified")
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(KST).date()
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------
# Adapter
# ------------------------------------------------------------------------------------------------
_BOARD_OPTIONS = (
    "layout",
    "detail_pattern",
    "attachment_pattern",
    "id_param",
    "page_param",
    "title_keywords",
    "title_pattern",
    "script_links",
    "title_attr",
)


@dataclass(slots=True)
class Board:
    url: str
    institution_code: str | None = None
    publisher: str | None = None
    layout: str = "board"  # "board": list → post → files; "page": one page of files
    detail_pattern: str = r"view\.do"
    attachment_pattern: str = r"download|fileDown|atchFile"
    id_param: str = "nttId"
    page_param: str = "pageIndex"
    title_keywords: list[str] = field(default_factory=lambda: ["예산서", "사업명세서"])
    title_pattern: str | None = None
    script_links: ScriptLinks = field(default_factory=list)
    title_attr: bool = False

    @classmethod
    def from_config(cls, board: dict[str, Any], defaults: dict[str, Any]) -> Board:
        values = {k: defaults[k] for k in _BOARD_OPTIONS if k in defaults} | board
        values["script_links"] = compile_script_links(values.get("script_links"))
        return cls(**values)

    def wants(self, title: str) -> bool:
        if self.title_keywords and not any(k in title for k in self.title_keywords):
            return False
        return self.title_pattern is None or re.search(self.title_pattern, title) is not None


@dataclass(slots=True)
class CrawlStats:
    pages: int = 0
    posts: int = 0
    files: int = 0
    skipped_robots: int = 0
    skipped_type: int = 0
    skipped_size: int = 0
    skipped_title: int = 0
    skipped_window: int = 0
    skipped_cap: int = 0


class _HostThrottle:
    """Minimum spacing between requests to one host, within this process."""

    def __init__(self, delay: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._delay = delay
        self._clock = clock
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str) -> None:
        if self._delay <= 0:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = self._last.get(host, float("-inf")) + self._delay - self._clock()
            if gap > 0:
                await asyncio.sleep(gap)
            self._last[host] = self._clock()


class BoardCrawlerAdapter:
    def __init__(
        self,
        key: str,
        config: dict[str, Any],
        client_for_host: Callable[[str], ResilientClient],
    ) -> None:
        self.key = key
        self.doc_type: DocType = config.get("doc_type", "budget_book")
        self.boards = [Board.from_config(b, config) for b in config.get("boards", [])]
        self.max_pages: int = int(config.get("max_pages", 20))
        self.max_bytes: int = int(float(config.get("max_file_mb", 50)) * 1024 * 1024)
        self.max_files: int | None = config.get("max_files")
        self.extra_structured: dict[str, Any] = config.get("structured", {})
        self.stats = CrawlStats()
        self._client_for_host = client_for_host
        self._clients: dict[str, ResilientClient] = {}
        self._robots: dict[str, RobotsRules | None] = {}
        self._throttle = _HostThrottle(float(config.get("delay_seconds", 1.0)))

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()

    # -- HTTP ------------------------------------------------------------------------------------
    def _client(self, host: str) -> ResilientClient:
        if host not in self._clients:
            self._clients[host] = self._client_for_host(host)
        return self._clients[host]

    async def _get(self, url: str, *, max_bytes: int | None = None) -> httpx.Response:
        host = urlsplit(url).netloc.lower()
        await self._throttle.wait(host)
        return await self._client(host).request("GET", url, soft_errors=False, max_bytes=max_bytes)

    async def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if host not in self._robots:
            robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
            rules: RobotsRules | None
            try:
                body = (await self._get(robots_url, max_bytes=512 * 1024)).content
                rules = RobotsRules(body.decode("utf-8", errors="replace"))
            except FatalSourceError as exc:
                if exc.status is not None and 400 <= exc.status < 500:
                    rules = RobotsRules("")  # no robots.txt → no restrictions
                else:
                    rules = None
            except (TransientSourceError, ResponseTooLargeError, CircuitOpenError):
                # unreachable (or failing in an earlier run) → the whole host is disallowed this
                # run; the other boards still get crawled
                rules = None
            self._robots[host] = rules
        found = self._robots[host]
        return found is not None and found.can_fetch(url)

    # -- crawl -----------------------------------------------------------------------------------
    def _page_url(self, board: Board, page: int) -> str:
        parts = urlsplit(board.url)
        query = parse_qs(parts.query)
        query[board.page_param] = [str(page)]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), "")
        )

    async def fetch(self, window: FetchWindow) -> AsyncIterator[RawRecord]:
        for board in self.boards:
            crawl = self._crawl_page if board.layout == "page" else self._crawl_board
            async for rec in crawl(board, window):
                yield rec
        log.info("crawler.finished", source=self.key, **asdict(self.stats))

    async def _crawl_board(self, board: Board, window: FetchWindow) -> AsyncIterator[RawRecord]:
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            url = self._page_url(board, page)
            if not await self._allowed(url):
                self.stats.skipped_robots += 1
                return
            html = (await self._get(url)).content.decode("utf-8", errors="replace")
            self.stats.pages += 1
            rows = parse_board_rows(
                html,
                url,
                board.detail_pattern,
                board.attachment_pattern,
                board.script_links,
                title_attr=board.title_attr,
            )
            fresh = [r for r in rows if r.url not in seen]
            if not fresh:
                return  # past the last page (many boards repeat the last page)
            for row in fresh:
                seen.add(row.url)
                if row.posted is None or not (window.since <= row.posted <= window.until):
                    continue
                if not board.wants(row.title):
                    self.stats.skipped_title += 1
                    continue
                async for rec in self._crawl_post(board, row):
                    yield rec
            dated = [r.posted for r in fresh if r.posted is not None]
            if dated and min(dated) < window.since:
                return  # newest-first board: everything further back is older still

    async def _crawl_post(self, board: Board, row: BoardRow) -> AsyncIterator[RawRecord]:
        if row.attachments:  # the list row carries the files; there is no post page
            self.stats.posts += 1
            post_url, post_id = row.url, file_key(row.url)  # the row's first file
            attachments = row.attachments
        else:
            if self._capped():
                self.stats.skipped_cap += 1  # don't open a post whose files we won't take
                return
            if not await self._allowed(row.url):
                self.stats.skipped_robots += 1
                return
            self.stats.posts += 1
            html = (await self._get(row.url)).content.decode("utf-8", errors="replace")
            post_url = canonical_url(row.url, (board.id_param,))
            post_id = parse_qs(urlsplit(post_url).query).get(board.id_param, [post_url])[0]
            attachments = parse_attachments(
                html, row.url, board.attachment_pattern, board.script_links
            )
        docs: list[tuple[Link, bytes, str, date | None]] = []
        for att in attachments:
            if (got := await self._download(att)) is not None:
                docs.append((att, *got))
        for i, (att, content, mime, _) in enumerate(docs, start=1):
            assert row.posted is not None
            yield self._record(
                board,
                external_id=post_id if len(docs) == 1 else f"{post_id}-{i}",
                title=row.title,
                posted=row.posted,
                url=post_url,
                att=att,
                content=content,
                mime=mime,
            )

    async def _crawl_page(self, board: Board, window: FetchWindow) -> AsyncIterator[RawRecord]:
        """A page that lists the documents themselves (no rows, dates or posts)."""
        if not await self._allowed(board.url):
            self.stats.skipped_robots += 1
            return
        html = (await self._get(board.url)).content.decode("utf-8", errors="replace")
        self.stats.pages += 1
        for att in parse_attachments(html, board.url, board.attachment_pattern, board.script_links):
            title = att.title
            if not board.wants(title):
                self.stats.skipped_title += 1
                continue
            # No posting date on the page: the fiscal year decides. Next year's 본예산 is
            # published in December, so the window's last year + 1 still counts.
            year = budget_title_facts(title).get("fiscal_year")
            if year is not None and not (window.since.year <= year <= window.until.year + 1):
                self.stats.skipped_window += 1
                continue
            self.stats.posts += 1
            got = await self._download(att)
            if got is None:
                continue
            content, mime, modified = got
            yield self._record(
                board,
                external_id=file_key(att.href),
                title=title,
                posted=modified or today_kst(),
                url=att.href,
                att=att,
                content=content,
                mime=mime,
                date_from="last_modified" if modified else "crawled",
            )

    def _capped(self) -> bool:
        return self.max_files is not None and self.stats.files >= self.max_files

    async def _download(self, att: Link) -> tuple[bytes, str, date | None] | None:
        if self._capped():
            self.stats.skipped_cap += 1
            return None
        if not await self._allowed(att.href):
            self.stats.skipped_robots += 1
            return None
        try:
            resp = await self._get(att.href, max_bytes=self.max_bytes)
        except ResponseTooLargeError:
            self.stats.skipped_size += 1
            log.warning("crawler.file_too_large", url=att.href)
            return None
        mime = sniff_mime(resp.content)
        if mime is None:
            self.stats.skipped_type += 1
            return None
        self.stats.files += 1
        return resp.content, mime, _last_modified(resp)

    def _record(
        self,
        board: Board,
        *,
        external_id: str,
        title: str,
        posted: date,
        url: str,
        att: Link,
        content: bytes,
        mime: str,
        date_from: str | None = None,
    ) -> RawRecord:
        return RawRecord(
            external_id=external_id,
            doc_type=self.doc_type,
            title=title,
            published_at=posted,
            mime=mime,
            publisher_raw=board.publisher,
            institution_code_hint=board.institution_code,
            url=url,
            content=content,
            structured={
                **(budget_title_facts(title) if self.doc_type == "budget_book" else {}),
                "file_name": att.text,
                "file_url": att.href,
                "board_url": board.url,
                **({"published_from": date_from} if date_from else {}),
                **self.extra_structured,
            },
        )
