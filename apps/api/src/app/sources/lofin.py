"""행정안전부 지방재정365 — 우리 지자체 예산서 (links to each government's budget book).

The API is the 지방재정365 open API hub: ``https://www.lofin365.go.kr/lf/hub/<데이터코드>`` with
``Key``, ``Type=json``, ``pIndex``, ``pSize`` (≤1,000) and ``fyr`` (required), answering
``{"BUDLK": [{"head": [{"list_total_count": n}, {"RESULT": {"CODE": "INFO-000", …}}]},
{"row": [...]}]}``; a failed or empty call answers ``{"RESULT": [{"CODE": …}]}`` instead. The
예산서 dataset's code, ``BUDLK``, is shown on its OpenApi tab (``sources.config.api_code``
overrides it).

Seen live on 2026-09-27 (``docs/real-data-budget.md`` §7): one row per 지자체 per 회계연도, 243
rows a year (17 시도 본청 + 226 시군구), six fields::

    {"fyr": "2025", "wa_laf_hg_nm": "서울", "laf_cd": "1133000", "laf_hg_nm": "서울강남구",
     "lnk_nm": "예산서", "lnk_url_nm": "https://www.gangnam.go.kr/board/B_000742/list.do?…"}

The region is the short 시도 name and the institution name carries it as a prefix ("서울본청" is
the 시도 itself). There is no 본예산/추경 field and no registration date. ``lnk_url_nm`` is the
government's own 예산서 *board page*, not a file: every one of the 243 links was a page on the
government's site. So a row only costs a download when its link names a PDF/HWP file, and the
bytes decide the type; page links are counted and kept in ``page_links`` for the board crawler
(``sources/crawler.py``), which follows boards politely.

Rows are filtered *before* anything is downloaded: by fiscal year, by the fetch window, and by an
optional ``institutions`` list from ``sources.config``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from app.domain.institutions import SIDO_ALIASES
from app.sources.base import (
    DocType,
    FetchWindow,
    RawRecord,
    parse_compact_date,
    parse_int,
    pick,
)
from app.sources.crawler import sniff_mime
from app.sources.http import FatalSourceError, ResilientClient

BASE_URL = "https://www.lofin365.go.kr"
PAGE_SIZE = 1000  # the hub's maximum

DEFAULTS: dict[str, Any] = {
    "api_code": "BUDLK",  # 우리 지자체 예산서, from the dataset's OpenApi tab on the portal
    "list_path": "/lf/hub/{api_code}",
    "year_param": "fyr",
    # The live names (2026-09-27). The listing has no 본예산/추경 or date field; the lists stay
    # configurable in case the dataset grows one.
    "institution_fields": ["laf_hg_nm"],
    "region_fields": ["wa_laf_hg_nm"],
    "year_fields": ["fyr"],
    "kind_fields": [],
    "url_fields": ["lnk_url_nm"],
    "id_fields": ["laf_cd"],
    "published_fields": [],
}
# "서울" → "서울특별시"; the listing speaks the short names.
_SIDO_FULL = {alias: full for full, aliases in SIDO_ALIASES.items() for alias in aliases}
_OWN_BODY = "본청"  # "서울본청": the 시도 itself
_DOC_PATH_RE = re.compile(r"\.(pdf|hwp|hwpx)$", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class BookRow:
    """One listing row, mapped but not yet downloaded."""

    institution: str
    region: str | None
    fiscal_year: int
    kind: str | None
    url: str
    published: date
    external_id: str


def _norm(name: str) -> str:
    return "".join(name.split())


class LofinBudgetAdapter:
    doc_type: DocType = "budget_book"

    def __init__(
        self,
        client: ResilientClient,
        api_key: str,
        *,
        key: str = "lofin_budget",
        api_code: str | None = None,
        institutions: Iterable[str] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        self.key = key
        self._client = client
        self._api_key = api_key
        self._cfg = DEFAULTS | (overrides or {})
        if api_code:
            self._cfg["api_code"] = api_code
        # None: every institution. Names compare without spaces, alone or after the region
        # ("서울특별시 강남구" matches a row 강남구 in 서울특별시).
        self._institutions = {_norm(n) for n in institutions} if institutions else None
        self.stats: Counter[str] = Counter()  # listed / kept / skipped:<why> / downloaded
        self.page_links: list[BookRow] = []  # rows whose link is a board page, not a file

    async def aclose(self) -> None:
        await self._client.aclose()

    def fiscal_years(self, window: FetchWindow) -> range:
        return range(window.since.year, window.until.year + 2)  # next FY books appear in Dec

    async def fetch(self, window: FetchWindow) -> AsyncIterator[RawRecord]:
        cfg = self._cfg
        path = self.list_path()
        years = self.fiscal_years(window)
        for year in years:
            page = 1
            while True:
                payload = await self._client.get_json(
                    path,
                    params={
                        "Key": self._api_key,
                        "Type": "json",
                        "pIndex": page,
                        "pSize": PAGE_SIZE,
                        cfg["year_param"]: year,
                    },
                )
                _raise_for_result(payload)
                rows = _rows(payload)
                for row in rows:
                    self.stats["listed"] += 1
                    book = self.select(row, year, window, years)
                    if book is None:
                        continue
                    self.stats["kept"] += 1
                    record = await self._download(book)
                    if record is not None:
                        yield record
                if len(rows) < PAGE_SIZE:
                    break
                page += 1

    def list_path(self) -> str:
        path = str(self._cfg["list_path"])
        if "{api_code}" in path:
            if not self._cfg.get("api_code"):
                raise FatalSourceError(
                    "lofin_budget needs sources.config.api_code: the 예산서 dataset's code on "
                    "the 지방재정365 hub (its OpenApi tab)"
                )
            path = path.format(api_code=self._cfg["api_code"])
        return path

    def map_row(self, row: dict[str, Any], year: int) -> BookRow | None:
        cfg = self._cfg
        url = pick(row, *cfg["url_fields"])
        inst = pick(row, *cfg["institution_fields"])
        if not url or not inst:
            return None
        fy = parse_int(pick(row, *cfg["year_fields"])) or year
        kind = pick(row, *cfg["kind_fields"])
        region_raw = pick(row, *cfg["region_fields"])
        region, inst = _split_name(str(region_raw) if region_raw else None, str(inst))
        published = parse_compact_date(pick(row, *cfg["published_fields"])) or date(fy - 1, 12, 20)
        code = pick(row, *cfg["id_fields"])  # 자치단체코드: the same every year
        ext = f"{code}-{fy}" if code else f"{region or ''}{inst}-{fy}"
        if kind:
            ext = f"{ext}-{kind}"
        return BookRow(
            institution=inst,
            region=region,
            fiscal_year=fy,
            kind=str(kind) if kind else None,
            url=str(url).strip(),
            published=published,
            external_id=ext,
        )

    def select(
        self, row: dict[str, Any], year: int, window: FetchWindow, years: range
    ) -> BookRow | None:
        """Map a row and decide, from the listing alone, whether its file is worth a download."""
        book = self.map_row(row, year)
        why = None
        if book is None:
            why = "unmapped"
        elif book.fiscal_year not in years:  # the list may ignore ``fyr``
            why = "fiscal_year"
        elif not window.since <= book.published <= window.until:
            why = "window"
        elif self._institutions is not None and not self._wanted(book):
            why = "institution"
        if why:
            self.stats[f"skipped:{why}"] += 1
            return None
        return book

    def _wanted(self, book: BookRow) -> bool:
        assert self._institutions is not None
        names = {_norm(book.institution)}
        if book.region:
            names.add(_norm(book.region + book.institution))
        return not names.isdisjoint(self._institutions)

    async def _download(self, book: BookRow) -> RawRecord | None:
        if not _DOC_PATH_RE.search(urlsplit(book.url).path):
            # A board page on the government's own site; the board crawler follows those.
            self.stats["skipped:page_link"] += 1
            self.page_links.append(book)
            return None
        content = await self._client.get_bytes(book.url)
        self.stats["downloaded"] += 1
        mime = sniff_mime(content)
        if mime is None:  # an error or landing page served under a file name
            self.stats["skipped:not_a_document"] += 1
            return None
        kind = f" {book.kind}" if book.kind else ""
        structured: dict[str, Any] = {"fiscal_year": book.fiscal_year}
        if book.kind:
            structured["budget_kind"] = book.kind
        return RawRecord(
            external_id=book.external_id,
            doc_type="budget_book",
            title=f"{book.fiscal_year}년도 {book.institution}{kind} 예산서",
            published_at=book.published,
            mime=mime,
            publisher_raw=book.institution,
            sido_hint=book.region,
            url=book.url,
            content=content,
            structured=structured,
        )


def _split_name(region: str | None, inst: str) -> tuple[str | None, str]:
    """("서울", "서울강남구") → ("서울특별시", "강남구"); ("서울", "서울본청") → ("서울특별시",
    "서울특별시"). Names without the prefix pass through with the region spelled out."""
    full = _SIDO_FULL.get(region, region) if region else None
    if region and inst.startswith(region) and len(inst) > len(region):
        inst = inst[len(region) :]
    if inst == _OWN_BODY and full:
        inst = full
    return full, inst


# INFO-000 정상, INFO-200 해당 데이터 없음 (seen live). ERROR-290 is a bad key and ERROR-300 a
# missing required parameter (seen live: no ``fyr``); every other code is an error too.
_OK_RESULTS = frozenset({"INFO-000", "INFO-200"})


def _results(payload: Any) -> list[dict[str, Any]]:
    """RESULT blocks wherever the hub puts them: at the top on a failed call, in ``head`` on a
    listing."""
    found: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return found
    top = payload.get("RESULT")
    for r in top if isinstance(top, list) else [top]:
        if isinstance(r, dict):
            found.append(r)
    for value in payload.values():
        if not isinstance(value, list):
            continue
        for part in value:
            head = part.get("head") if isinstance(part, dict) else None
            for item in head if isinstance(head, list) else []:
                if isinstance(item, dict) and isinstance(item.get("RESULT"), dict):
                    found.append(item["RESULT"])
    return found


def _raise_for_result(payload: Any) -> None:
    for result in _results(payload):
        code = str(result.get("CODE") or "")
        if code and code not in _OK_RESULTS:
            # The message never carries the key; the request URL (which does) is not logged here.
            raise FatalSourceError(f"지방재정365 {code}: {result.get('MESSAGE') or ''}".strip())


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for name, value in payload.items():
            if name == "RESULT":  # the envelope of an empty or failed call, not rows
                continue
            if isinstance(value, list):
                for part in value:
                    if isinstance(part, dict) and isinstance(part.get("row"), list):
                        return [r for r in part["row"] if isinstance(r, dict)]
                if value and all(isinstance(r, dict) for r in value):
                    return value
    return []
