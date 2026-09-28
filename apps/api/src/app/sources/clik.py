"""국회도서관 지방의정포털(CLIK) Open API — local council minutes.

Minutes are the earliest public trace of most municipal purchases: a council member asks, a
department head answers "내년도 본예산에 반영하겠습니다", and 6–18 months later a tender appears.

The API, as published on the portal's Open API page (``/potal/guide/resourceCenter.do``) and seen
live on 2026-09-28 (``docs/real-data-clik.md``):

* ``GET /openapi/minutes.do`` with ``key``, ``type=json`` and ``displayType=list`` (``startCount``
  is a row offset, ``listCount`` ≤ 100 — 500 answers ``ERROR05``, ``searchType=ALL``, optional
  ``rasmblyId`` and ``sort=MTG_DE/DESC``) or ``displayType=detail`` with ``docid``. There is
  **no date filter**: the list is read newest first and paging stops once a page reaches past the
  window.
* Success is a one-element array ``[{"SERVICE": "minutes", "RESULT_CODE": "SUCCESS", …,
  "TOTAL_COUNT": n, "LIST": [{"ROW": {…}}, …]}]``; an error is a bare object
  ``{"RESULT_CODE": "ERROR01", …}`` (HTTP 200 either way). A missing key and a wrong key both
  answer ``ERROR01``; an unknown ``docid`` answers ``SUCCESS`` with no fields.
* List rows carry ``DOCID``, ``RASMBLY_ID``, ``RASMBLY_NM`` ("경기도 성남시의회"),
  ``RASMBLY_NUMPR`` (대수), ``RASMBLY_SESN`` (회수), ``MINTS_ODR`` (차수), ``MTGNM`` ("본회의",
  "예산결산특별위원회", …) and ``MTG_DE`` (``YYYYMMDD``). The detail adds ``MTR_SJ`` (agenda),
  ``MINTS_HTML`` and ``ORGINL_FILE_URL`` (the council's own file link, often empty).
* **One meeting, many DOCIDs.** CLIK keeps each revision of a council's minutes (임시 → 수정 →
  확정) as its own DOCID with nothing in the list to tell them apart — 110 DOCIDs for 성남시의회's
  34 본회의·예결특위 meetings in a year. Rows are grouped by meeting and only one detail call is
  made per meeting (the first listed), and none for a meeting already stored
  (``known_external_ids``). The external id is the meeting, not the DOCID.
* ``MINTS_HTML`` is whatever the council publishes: for some councils a clean fragment, for
  성남시의회 the whole viewer page (header, menus, font pickers). :func:`html_to_text` drops page
  chrome and keeps the two spaces after a bold speaker label that ``parsing/chunking.py`` uses
  to tell "○조우현위원  질문" (a member) from "○교통도로국장 유동  답변".

1,000 calls/day per key; the shared limiter counts every attempt.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from html.parser import HTMLParser
from typing import Any

from app.sources.base import DocType, FetchWindow, RawRecord, parse_compact_date, pick
from app.sources.http import FatalSourceError, ResilientClient, TransientSourceError
from app.sources.resilience import QuotaExhaustedError, next_kst_midnight

BASE_URL = "https://clik.nanet.go.kr"
VIEW_URL = BASE_URL + "/potal/search/searchView.do?collection=minutes&DOCID={docid}"

DEFAULTS: dict[str, Any] = {
    "path": "/openapi/minutes.do",
    "page_size": 100,  # the API's maximum
    # Only meetings whose MTGNM matches (e.g. "본회의|예산결산"); None keeps every meeting.
    "meeting_pattern": None,
    # Stop after this many detail calls in one run (None: no cap). The daily quota is shared by
    # every council, so a first backfill of all councils is spread over several days.
    "max_details": None,
}

_QUOTA_CODES = {"ERROR09"}  # 일별 허용 트래픽 초과
_TRANSIENT_CODES = {"ERROR11"}  # 호출 중 오류
# ERROR01 key invalid (also: no key), ERROR10 key disabled, ERROR02–08 a bad parameter.

MeetingKey = tuple[str, str, str, str, str, str]


class _TextExtractor(HTMLParser):
    _BREAKS = frozenset({"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "hr", "spk"})
    # Page chrome around the minutes when a council hands CLIK its whole viewer page.
    _SKIP = frozenset(
        {"script", "style", "header", "nav", "aside", "form", "select", "button", "noscript"}
    )
    _BOLD = frozenset({"b", "strong"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._bold: list[int] = []  # index into parts where each open bold element starts

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in self._BREAKS:
            self.parts.append("\n")
        if tag in self._BOLD:
            self._bold.append(len(self.parts))

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in self._BOLD and self._bold:
            label = "".join(self.parts[self._bold.pop() :]).strip()
            if label[:1] in "○◯◎":
                self.parts.append(_LABEL_END)
        if tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


_LABEL_END = "\x00"


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    # A bold "○…" label ends the speaker; the speech follows after two spaces, as in the HWP.
    text = re.sub(r" ?\x00 ?", "  ", text)
    text = re.sub(r" +\n", "\n", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


class ClikMinutesAdapter:
    doc_type: DocType = "council_minutes"

    def __init__(
        self,
        client: ResilientClient,
        api_key: str,
        *,
        key: str = "clik_minutes",
        council_ids: list[str] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        self.key = key
        self.client = client
        self._api_key = api_key
        self._councils = council_ids or [""]
        self._cfg = DEFAULTS | (overrides or {})
        pattern = self._cfg.get("meeting_pattern")
        self._meeting_re = re.compile(pattern) if pattern else None
        # Set by ``run_ingest``: which of these external ids are stored already.
        self.known_external_ids: Callable[[list[str]], Awaitable[set[str]]] | None = None
        # listed / in_window / meetings / revisions / skipped:<why> / details / empty
        self.stats: Counter[str] = Counter()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def fetch(self, window: FetchWindow) -> AsyncIterator[RawRecord]:
        meetings: dict[MeetingKey, list[dict[str, Any]]] = {}
        for council in self._councils:
            async for row in self._list(council, window):
                meetings.setdefault(meeting_key(row), []).append(row)
        self.stats["meetings"] = len(meetings)
        self.stats["revisions"] = sum(len(rows) - 1 for rows in meetings.values())
        ids = {external_id(k): k for k in meetings}
        known = await self.known_external_ids(list(ids)) if self.known_external_ids else set()
        cap = self._cfg.get("max_details")
        for ext, mk in ids.items():
            if ext in known:
                self.stats["skipped:known"] += 1
                continue
            if cap is not None and self.stats["details"] >= int(cap):
                self.stats["skipped:cap"] += 1
                continue
            rec = await self.read_meeting(ext, meetings[mk])
            if rec is not None:
                yield rec

    async def _list(self, council: str, window: FetchWindow) -> AsyncIterator[dict[str, Any]]:
        size = int(self._cfg["page_size"])
        start = 0
        while True:
            params: dict[str, Any] = {
                "key": self._api_key,
                "type": "json",
                "displayType": "list",
                "startCount": start,
                "listCount": size,
                "searchType": "ALL",
                "sort": "MTG_DE/DESC",
            }
            if council:
                params["rasmblyId"] = council
            payload = await self.client.get_json(self._cfg["path"], params=params)
            rows = list_rows(envelope(payload))
            self.stats["listed"] += len(rows)
            oldest = None
            for row in rows:
                day = parse_compact_date(row.get("MTG_DE"))
                if day is None or not row.get("DOCID"):
                    self.stats["skipped:unmapped"] += 1
                    continue
                oldest = day if oldest is None else min(oldest, day)
                if not window.since <= day <= window.until:
                    continue
                if self._meeting_re and not self._meeting_re.search(str(row.get("MTGNM") or "")):
                    self.stats["skipped:meeting"] += 1
                    continue
                self.stats["in_window"] += 1
                yield row
            if len(rows) < size or (oldest is not None and oldest < window.since):
                return
            start += size

    async def read_meeting(self, ext: str, rows: list[dict[str, Any]]) -> RawRecord | None:
        """One detail call for the first of a meeting's revisions."""
        row = rows[0]
        docid = str(row["DOCID"])
        self.stats["details"] += 1
        payload = await self.client.get_json(
            self._cfg["path"],
            params={"key": self._api_key, "type": "json", "displayType": "detail", "docid": docid},
        )
        detail = envelope(payload)
        body = detail.get("MINTS_HTML")
        if not body or not str(body).strip():
            self.stats["empty"] += 1
            return None
        text = html_to_text(str(body)) if "<" in str(body) else str(body).strip()
        if not text:
            self.stats["empty"] += 1
            return None
        merged = row | {k: v for k, v in detail.items() if v not in (None, "")}
        day = parse_compact_date(merged.get("MTG_DE"))
        assert day is not None  # rows without a date never reach here
        council = pick(merged, "RASMBLY_NM")
        return RawRecord(
            external_id=ext,
            doc_type="council_minutes",
            title=meeting_title(merged),
            published_at=day,
            mime="text/plain; charset=utf-8",
            publisher_raw=str(council) if council else None,
            url=VIEW_URL.format(docid=docid),
            content=text.encode("utf-8"),
            structured={
                "meeting_date": day.isoformat(),
                "council": council,
                "council_id": merged.get("RASMBLY_ID"),
                "term": merged.get("RASMBLY_NUMPR"),
                "session": merged.get("RASMBLY_SESN"),
                "sitting": merged.get("MINTS_ODR"),
                "meeting": merged.get("MTGNM"),
                "docid": docid,
                "revision_docids": [str(r["DOCID"]) for r in rows],
                "original_file_url": merged.get("ORGINL_FILE_URL") or None,
            },
        )


def envelope(payload: Any) -> dict[str, Any]:
    """The first object of the answer, after checking its result code."""
    obj = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(obj, dict):
        raise TransientSourceError(f"CLIK: unexpected answer {type(payload).__name__}")
    code = str(obj.get("RESULT_CODE") or "SUCCESS")
    if code == "SUCCESS":
        return obj
    message = f"CLIK {code}: {obj.get('RESULT_MESSAGE') or ''}".strip()
    if code in _QUOTA_CODES:
        raise QuotaExhaustedError("clik_minutes", next_kst_midnight())
    if code in _TRANSIENT_CODES:
        raise TransientSourceError(message)
    raise FatalSourceError(message)  # never carries the key; the URL is not logged here


def list_rows(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in envelope.get("LIST") or []:
        row = item.get("ROW") if isinstance(item, dict) else None
        if isinstance(row, dict):
            rows.append(row)
    return rows


def meeting_key(row: dict[str, Any]) -> MeetingKey:
    return tuple(  # type: ignore[return-value]
        str(row.get(k) or "").strip()
        for k in ("RASMBLY_ID", "RASMBLY_NUMPR", "RASMBLY_SESN", "MINTS_ODR", "MTG_DE", "MTGNM")
    )


def external_id(key: MeetingKey) -> str:
    """``031013:10:312:2:20260907:본회의`` — one per meeting, whichever revision was read."""
    ext = ":".join(key[:5]) + ":" + re.sub(r"\s+", "", key[5])
    if len(ext) > 128:
        ext = ":".join(key[:5]) + ":" + hashlib.sha256(key[5].encode()).hexdigest()[:16]
    return ext


def meeting_title(row: dict[str, Any]) -> str:
    """ "경기도 성남시의회 제312회 본회의 제2차 (2026-09-07)". 차수 0 is left out: councils use
    it for 개회식 and for single-sitting committee meetings alike."""
    parts = [str(row.get("RASMBLY_NM") or "").strip()]
    if sesn := str(row.get("RASMBLY_SESN") or "").strip():
        parts.append(f"제{sesn}회")
    parts.append(" ".join(str(row.get("MTGNM") or "회의록").split()))  # "본회의\n[임시]"
    odr = str(row.get("MINTS_ODR") or "").strip()
    if odr and odr != "0":
        parts.append(f"제{odr}차")
    day = parse_compact_date(row.get("MTG_DE"))
    title = " ".join(p for p in parts if p)
    return f"{title} ({day.isoformat()})" if day else title
