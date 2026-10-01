"""``manage eval tenders``: every 입찰공고 of a period is read, kept by institution, and a slice
counts as complete only when the rows read reach the provider's totalCount."""

from __future__ import annotations

import gzip
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.eval.tender_census import BID_PATHS, NOTICES, load, planned_slices, run_census
from app.sources.http import ResilientClient
from app.sources.resilience import MemoryBreaker, MemoryLimiter


async def _no_sleep(_: float) -> None:
    return None


def _notice(no: int, institution: str) -> dict[str, Any]:
    return {
        "bidNtceNo": f"R25BK{no:08d}",
        "bidNtceOrd": "000",
        "bidNtceNm": f"사업 {no}",
        "bidNtceDt": "2025-03-04 10:00:00",
        "ntceInsttNm": "조달청 경기지방조달청",
        "dminsttNm": institution,
        "ntceInsttOfclNm": "담당자",
        "ntceInsttOfclTelNo": "031-000-0000",
        "ntceSpecDocUrl1": "https://example/file",
        "asignBdgtAmt": "120000000",
        "rgstTyNm": "",
    }


class Provider:
    """Answers like the 입찰공고 service: pages of ``numOfRows`` over a registration window."""

    def __init__(
        self, rows_by_week: dict[str, list[dict[str, Any]]], fail: frozenset[str] = frozenset()
    ) -> None:
        self.rows_by_week = rows_by_week
        self.fail = fail
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        start = date(int(q["inqryBgnDt"][:4]), int(q["inqryBgnDt"][4:6]), int(q["inqryBgnDt"][6:8]))
        op = request.url.path.rsplit("/", 1)[-1]
        page, size = int(q["pageNo"]), int(q["numOfRows"])
        self.calls.append((op, start.isoformat(), page))
        if f"{op}:{start.isoformat()}" in self.fail:
            return httpx.Response(503)
        rows = self.rows_by_week.get(f"{op}:{start.isoformat()}", [])
        body = {"items": rows[(page - 1) * size : page * size], "totalCount": len(rows)}
        return httpx.Response(
            200, json={"response": {"header": {"resultCode": "00"}, "body": body}}
        )


def _client(provider: Provider) -> ResilientClient:
    return ResilientClient(
        "g2b_bid",
        base_url="https://apis.example",
        limiter=MemoryLimiter(),
        breaker=MemoryBreaker(failure_threshold=100),
        transport=httpx.MockTransport(provider),
        sleep=_no_sleep,
        max_attempts=2,
    )


def _kept(out: Path) -> list[dict[str, Any]]:
    path = out / NOTICES
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def test_every_day_of_the_period_is_read_once_per_kind_of_work() -> None:
    slices = planned_slices("2025-01-01", "2025-01-20")
    assert len(slices) == 3 * 3  # weeks of 7, 7 and 6 days
    for path in BID_PATHS:
        days: list[str] = []
        for p, start, end in slices:
            if p != path:
                continue
            d = date.fromisoformat(start)
            while d <= date.fromisoformat(end):
                days.append(d.isoformat())
                d += timedelta(days=1)
        assert days == [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(20)]


async def test_a_census_reads_every_page_and_keeps_the_institutions_notices(tmp_path: Path) -> None:
    servc = BID_PATHS[0].rsplit("/", 1)[-1]
    week = [
        _notice(i, "경기도 성남시 분당구" if i % 3 == 0 else "서울특별시 종로구") for i in range(7)
    ]
    provider = Provider({f"{servc}:2025-03-01": week})
    census = await run_census(
        _client(provider),
        "key%2Bwith",
        tmp_path,
        since="2025-03-01",
        until="2025-03-07",
        institutions=["성남시"],
        rows=3,
        pause_seconds=0,
    )
    assert census.stopped is None
    assert census.summary()["slices_complete"] == 3
    assert census.summary()["incomplete"] == []
    assert [c for c in provider.calls if c[0] == servc] == [
        (servc, "2025-03-01", 1),
        (servc, "2025-03-01", 2),
        (servc, "2025-03-01", 3),
    ]
    kept = _kept(tmp_path)
    assert [k["bidNtceNo"] for k in kept] == ["R25BK00000000", "R25BK00000003", "R25BK00000006"]
    first = kept[0]
    assert first["_operation"] == servc
    assert "ntceInsttOfclNm" not in first and "ntceInsttOfclTelNo" not in first
    assert "ntceSpecDocUrl1" not in first and "rgstTyNm" not in first
    assert first["asignBdgtAmt"] == "120000000"


async def test_a_failed_slice_stays_incomplete_and_only_it_is_read_again(tmp_path: Path) -> None:
    servc, thng = (p.rsplit("/", 1)[-1] for p in BID_PATHS[:2])
    rows = {f"{servc}:2025-03-01": [_notice(1, "경기도 성남시")]}
    rows |= {f"{thng}:2025-03-01": [_notice(2, "경기도 성남시")]}
    first = Provider(rows, fail=frozenset({f"{thng}:2025-03-01"}))
    census = await run_census(
        _client(first),
        "k",
        tmp_path,
        since="2025-03-01",
        until="2025-03-07",
        institutions=["성남시"],
        pause_seconds=0,
    )
    summary = census.summary()
    assert summary["slices_complete"] == 2
    assert [(i["path"].rsplit("/", 1)[-1], i["start"]) for i in summary["incomplete"]] == [
        (thng, "2025-03-01")
    ]
    assert "gave up" in summary["incomplete"][0]["error"]
    assert [k["bidNtceNo"] for k in _kept(tmp_path)] == ["R25BK00000001"]

    again = Provider(rows)
    census = await run_census(
        _client(again),
        "k",
        tmp_path,
        since="2025-03-01",
        until="2025-03-07",
        institutions=["성남시"],
        pause_seconds=0,
    )
    assert again.calls == [(thng, "2025-03-01", 1)]
    assert census.summary()["slices_complete"] == 3
    assert [k["bidNtceNo"] for k in _kept(tmp_path)] == ["R25BK00000001", "R25BK00000002"]
    reloaded = load(tmp_path)
    assert reloaded is not None and reloaded.calls == 3  # pages read, counted across runs


async def test_a_directory_holds_one_census(tmp_path: Path) -> None:
    provider = Provider({})
    await run_census(
        _client(provider), "k", tmp_path, since="2025-03-01", until="2025-03-02",
        institutions=["성남시"], pause_seconds=0,
    )  # fmt: skip
    with pytest.raises(ValueError, match="new directory"):
        await run_census(
            _client(provider), "k", tmp_path, since="2025-03-01", until="2025-03-02",
            institutions=["고양시"], pause_seconds=0,
        )  # fmt: skip


async def test_a_refused_key_stops_the_run(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        body = {
            "response": {
                "header": {"resultCode": "30", "resultMsg": "SERVICE KEY IS NOT REGISTERED ERROR."}
            }
        }
        return httpx.Response(200, json=body)

    client = ResilientClient(
        "g2b_bid",
        base_url="https://apis.example",
        limiter=MemoryLimiter(),
        breaker=MemoryBreaker(),
        transport=httpx.MockTransport(refuse),
        sleep=_no_sleep,
    )
    census = await run_census(
        client, "k", tmp_path, since="2025-03-01", until="2025-03-14",
        institutions=["성남시"], pause_seconds=0,
    )  # fmt: skip
    assert census.stopped == "provider refused the request"
    assert census.calls == 0 and len(census.slices) == 1
