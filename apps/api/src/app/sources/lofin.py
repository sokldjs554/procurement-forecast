"""행정안전부 지방재정365 — 우리 지자체 예산서 (links to each government's budget book).

Budget books are published as large PDF (sometimes scanned) or HWP files, one per 회계연도 and
per 본예산/추경. The listing API returns metadata and a file URL; we download the file and let
``parsing/`` decide between the PDF text layer, OCR, or the HWP reader.

Field names are configurable (see ``DEFAULTS``). The host, path and field names follow the
published spec and have not been confirmed against a live response: on 2026-09-27 the dev
container could not reach lofin.mois.go.kr (TLS reset) or www.lofin365.go.kr (egress policy),
see ``docs/real-data-budget.md``.

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
from app.sources.http import ResilientClient

BASE_URL = "https://lofin.mois.go.kr"

DEFAULTS: dict[str, Any] = {
    "list_path": "/HUB/BGTBOOK",
    "institution_fields": ["laf_nm", "LAF_NM", "wa_nm", "institutionName"],
    "region_fields": ["rgn_nm", "RGN_NM", "sido_nm", "regionName"],
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
        institutions: Iterable[str] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        self.key = key
        self._client = client
        self._api_key = api_key
        self._cfg = DEFAULTS | (overrides or {})
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
        years = self.fiscal_years(window)
        for year in years:
            page = 1
            while True:
                payload = await self._client.get_json(
                    cfg["list_path"],
                    params={
                        "Key": self._api_key,
                        "Type": "json",
                        "pIndex": page,
                        "pSize": 100,
                        "fyr": year,
                    },
                )
                rows = _rows(payload)
                for row in rows:
                    self.stats["listed"] += 1
                    book = self.select(row, year, window, years)
                    if book is None:
                        continue
                    self.stats["kept"] += 1
                    yield await self._download(book)
                if len(rows) < 100:
                    break
                page += 1

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
