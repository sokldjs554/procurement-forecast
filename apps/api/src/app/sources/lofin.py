"""행정안전부 지방재정365 — 우리 지자체 예산서 (links to each government's budget book).

Budget books are published as large PDF (sometimes scanned) or HWP files, one per 회계연도 and
per 본예산/추경. The listing API returns metadata and a file URL; we download the file and let
``parsing/`` decide between the PDF text layer, OCR, or the HWP reader.

The API is the 지방재정365 open API hub: ``https://www.lofin365.go.kr/lf/hub/<데이터코드>`` with
``Key``, ``Type=json``, ``pIndex``, ``pSize`` (≤1,000) and the dataset's filters, answering
``{"<코드>": [{"head": [{"list_total_count": n}, {"RESULT": {"CODE": "INFO-000", …}}]},
{"row": [...]}]}``. The shape is taken from the MIT-licensed kpubdata client, which calls the same
hub for other 지방재정365 datasets; the 예산서 dataset's own code (``api_code``) is shown on its
OpenApi tab and set in ``sources.config``. The old host lofin.mois.go.kr no longer answers
(2026-09-27, ``docs/real-data-budget.md``). Field names are configurable (see ``DEFAULTS``);
the ones below are the hub's naming (``laf_hg_nm`` 자치단체명, ``wa_laf_hg_nm`` 광역자치단체명,
``fyr`` 회계연도) plus the earlier guesses.

A book is tens to hundreds of MB, so rows are filtered *before* anything is downloaded: by
fiscal year, by the fetch window (registration date), and by an optional ``institutions`` list
from ``sources.config``. Only the rows that pass cost a file download.
"""

from __future__ import annotations

import mimetypes
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.sources.base import (
    DocType,
    FetchWindow,
    RawRecord,
    parse_compact_date,
    parse_int,
    pick,
)
from app.sources.http import FatalSourceError, ResilientClient

BASE_URL = "https://www.lofin365.go.kr"
PAGE_SIZE = 1000  # the hub's maximum

DEFAULTS: dict[str, Any] = {
    "api_code": None,  # the 예산서 dataset's code on the hub; set in sources.config
    "list_path": "/lf/hub/{api_code}",
    "year_param": "fyr",
    "institution_fields": ["laf_hg_nm", "laf_nm", "LAF_NM", "wa_nm", "institutionName"],
    "region_fields": ["wa_laf_hg_nm", "rgn_nm", "RGN_NM", "sido_nm", "regionName"],
    "year_fields": ["fyr", "FYR", "accnut_year", "year"],
    "kind_fields": ["bgt_kind_nm", "BGT_KIND_NM", "budgetKind"],
    "url_fields": ["file_url", "FILE_URL", "link_url", "url"],
    "id_fields": ["bgtbook_id", "BGTBOOK_ID", "seq"],
    "published_fields": ["reg_dt", "REG_DT", "published"],
}


@dataclass(slots=True, frozen=True)
class BookRow:
    """One listing row, mapped but not yet downloaded."""

    institution: str
    region: str | None
    fiscal_year: int
    kind: str
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
                    yield await self._download(book)
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
        kind = str(pick(row, *cfg["kind_fields"]) or "본예산")
        region = pick(row, *cfg["region_fields"])
        published = parse_compact_date(pick(row, *cfg["published_fields"])) or date(fy - 1, 12, 20)
        ext = pick(row, *cfg["id_fields"]) or f"{inst}-{fy}-{kind}"
        return BookRow(
            institution=str(inst),
            region=str(region) if region else None,
            fiscal_year=fy,
            kind=kind,
            url=str(url),
            published=published,
            external_id=str(ext),
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

    async def _download(self, book: BookRow) -> RawRecord:
        content = await self._client.get_bytes(book.url)
        self.stats["downloaded"] += 1
        mime = mimetypes.guess_type(book.url)[0] or "application/pdf"
        if book.url.lower().endswith(".hwp"):
            mime = "application/x-hwp"
        return RawRecord(
            external_id=book.external_id,
            doc_type="budget_book",
            title=f"{book.fiscal_year}년도 {book.institution} {book.kind} 예산서",
            published_at=book.published,
            mime=mime,
            publisher_raw=book.institution,
            sido_hint=book.region,
            url=book.url,
            content=content,
            structured={"fiscal_year": book.fiscal_year, "budget_kind": book.kind},
        )


# INFO-000 정상, INFO-200 해당 데이터 없음; ERROR-290/300 are key errors, the rest bad requests.
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
        for value in payload.values():
            if isinstance(value, list):
                for part in value:
                    if isinstance(part, dict) and isinstance(part.get("row"), list):
                        return [r for r in part["row"] if isinstance(r, dict)]
                if value and all(isinstance(r, dict) for r in value):
                    return value
    return []
