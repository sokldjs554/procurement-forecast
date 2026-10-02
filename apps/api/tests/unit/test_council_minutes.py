"""``manage eval council-minutes``: every matching meeting of named councils is archived once,
a run the quota stops is finished by running it again, and the archive yields the very records
the live adapter would have stored."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.eval.council_minutes import ArchivedMinutes, archive
from app.sources.base import FetchWindow
from app.sources.clik import ClikMinutesAdapter, external_id, meeting_key
from app.sources.http import ResilientClient
from app.sources.resilience import MemoryBreaker, MemoryLimiter


async def _no_sleep(_: float) -> None:
    return None


def _row(
    docid: str, council_id: str, council: str, meeting: str, day: str, odr: str = "1"
) -> dict[str, Any]:
    return {
        "DOCID": docid,
        "RASMBLY_ID": council_id,
        "RASMBLY_NM": council,
        "RASMBLY_NUMPR": "9",
        "RASMBLY_SESN": "301",
        "MINTS_ODR": odr,
        "MTGNM": meeting,
        "MTG_DE": day,
    }


# Newest first, as the API lists them. Two revisions of one 서산 meeting (S1, S1b).
ROWS = [
    _row("G1", "031099", "경기도 고양시의회", "본회의", "20251020"),
    _row("S1", "041099", "충청남도 서산시의회", "본회의", "20251015"),
    _row("S1b", "041099", "충청남도 서산시의회", "본회의", "20251015"),
    _row("X1", "011099", "서울특별시 다른구의회", "본회의", "20251014"),
    _row("S2", "041099", "충청남도 서산시의회", "행정자치위원회", "20250910"),  # not the pattern
    _row("S3", "041099", "충청남도 서산시의회", "예산결산특별위원회", "20250901", "2"),
    _row("G2", "031099", "경기도 고양시의회", "예산결산특별위원회", "20250820", "3"),
    _row("S4", "041099", "충청남도 서산시의회", "본회의", "20240901", "4"),  # before the window
]

MINUTES = "○위원 김하늘  쉼터를 더 설치합니까?\n○교통과장 박서준  내년 본예산에 4억 원을 반영하겠습니다.\n"


def _client(
    quota_after: int | None = None, lists: list[str] | None = None
) -> tuple[ResilientClient, list[str]]:
    details: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        if q["displayType"] == "list":
            if lists is not None:
                lists.append(q.get("rasmblyId", ""))
            rows = [r for r in ROWS if not q.get("rasmblyId") or r["RASMBLY_ID"] == q["rasmblyId"]]
            start = int(q["startCount"])
            page = rows[start : start + int(q["listCount"])]
            return httpx.Response(
                200,
                json=[
                    {
                        "RESULT_CODE": "SUCCESS",
                        "TOTAL_COUNT": len(rows),
                        "LIST": [{"ROW": r} for r in page],
                    }
                ],
            )
        if quota_after is not None and len(details) >= quota_after:
            return httpx.Response(
                200, json={"RESULT_CODE": "ERROR09", "RESULT_MESSAGE": "일별 허용 트래픽 초과"}
            )
        details.append(q["docid"])
        row = next(r for r in ROWS if r["DOCID"] == q["docid"])
        return httpx.Response(200, json=[{"RESULT_CODE": "SUCCESS", **row, "MINTS_HTML": MINUTES}])

    client = ResilientClient(
        "clik_minutes",
        base_url="https://clik.example",
        limiter=MemoryLimiter(),
        breaker=MemoryBreaker(),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    return client, details


def _adapter(client: ResilientClient) -> ClikMinutesAdapter:
    return ClikMinutesAdapter(client, "k", overrides={"page_size": 3})


KW = {
    "since": date(2025, 1, 1),
    "until": date(2025, 12, 31),
    "meeting_pattern": "본회의|예산결산",
}


async def test_only_the_named_councils_matching_meetings_in_the_window_are_read(
    tmp_path: Path,
) -> None:
    client, details = _client()
    manifest = await archive(_adapter(client), tmp_path, ["서산시의회", "고양시의회"], **KW)

    assert manifest["councils"] == {
        "서산시의회": {"id": "041099", "council": "충청남도 서산시의회", "listed_meetings": 2},
        "고양시의회": {"id": "031099", "council": "경기도 고양시의회", "listed_meetings": 2},
    }
    # one detail per meeting (the two revisions of S1 are one meeting), none outside the rule
    assert sorted(details) == ["G1", "G2", "S1", "S3"]
    assert manifest["complete"] is True
    archived = [m for m in manifest["meetings"].values() if m["status"] == "archived"]
    assert len(archived) == 4
    assert all((tmp_path / m["path"]).read_text(encoding="utf-8") for m in archived)


async def test_given_ids_are_checked_against_the_council_name_their_rows_carry(
    tmp_path: Path,
) -> None:
    lists: list[str] = []
    client, details = _client(lists=lists)
    ids = {"서산시의회": ["031099", "777777", "041099"]}  # 고양, nobody, then 서산
    manifest = await archive(_adapter(client), tmp_path, ["서산시의회"], **KW, ids=ids)

    assert manifest["councils"] == {
        "서산시의회": {"id": "041099", "council": "충청남도 서산시의회", "listed_meetings": 2}
    }
    assert manifest["runs"][-1]["tried_ids"] == {
        "서산시의회": {
            "031099": "경기도 고양시의회",
            "777777": None,
            "041099": "충청남도 서산시의회",
        }
    }
    assert manifest["runs"][-1]["not_found"] == []
    assert "" not in lists  # the national list was not read
    assert sorted(details) == ["S1", "S3"]
    assert manifest["complete"] is True


async def test_a_name_no_given_id_carries_is_not_found(tmp_path: Path) -> None:
    client, details = _client()
    manifest = await archive(
        _adapter(client), tmp_path, ["서산시의회"], **KW, ids={"서산시의회": ["031099"]}
    )
    assert manifest["councils"] == {}
    assert manifest["runs"][-1]["not_found"] == ["서산시의회"]
    assert details == []
    assert manifest["complete"] is False


async def test_a_run_the_quota_stops_is_finished_by_the_next_run(tmp_path: Path) -> None:
    client, _ = _client(quota_after=1)
    first = await archive(_adapter(client), tmp_path, ["서산시의회"], **KW)
    assert first["runs"][-1]["stopped"] == "quota"
    assert first["complete"] is False
    assert len([m for m in first["meetings"].values() if m["status"] == "archived"]) == 1

    client2, details2 = _client()
    second = await archive(_adapter(client2), tmp_path, ["서산시의회"], **KW)
    assert second["complete"] is True
    assert len(details2) == 1  # only the meeting the first run did not reach
    assert len(second["runs"]) == 2


async def test_an_archive_under_another_rule_is_refused(tmp_path: Path) -> None:
    client, _ = _client()
    await archive(_adapter(client), tmp_path, ["서산시의회"], **KW)
    with pytest.raises(ValueError, match="another rule"):
        await archive(
            _adapter(client), tmp_path, ["서산시의회"], **(KW | {"meeting_pattern": "본회의"})
        )


async def test_the_archive_yields_the_records_the_live_adapter_read(tmp_path: Path) -> None:
    client, _ = _client()
    await archive(_adapter(client), tmp_path, ["서산시의회"], **KW)

    live_client, _ = _client()
    live = _adapter(live_client)
    rows = [r for r in ROWS if r["DOCID"] in {"S1", "S1b"}]
    ext = external_id(meeting_key(rows[0]))
    expected = await live.read_meeting(ext, rows)
    assert expected is not None

    replayed = [
        r
        async for r in ArchivedMinutes(tmp_path).fetch(
            FetchWindow(date(2025, 1, 1), date(2025, 12, 31))
        )
    ]
    got = next(r for r in replayed if r.external_id == ext)
    assert got == expected
    assert got.content_hash() == expected.content_hash()


async def test_a_changed_archived_text_is_refused(tmp_path: Path) -> None:
    client, _ = _client()
    manifest = await archive(_adapter(client), tmp_path, ["서산시의회"], **KW)
    entry = next(m for m in manifest["meetings"].values() if m["status"] == "archived")
    (tmp_path / entry["path"]).write_text("고친 본문", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        [
            r
            async for r in ArchivedMinutes(tmp_path).fetch(
                FetchWindow(date(2000, 1, 1), date(2100, 1, 1))
            )
        ]
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["complete"] is True


async def test_a_run_the_host_stops_answering_keeps_what_it_read(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        if q["displayType"] == "detail" and calls:
            raise httpx.ConnectTimeout("timed out", request=request)
        if q["displayType"] == "detail":
            calls.append(q["docid"])
            row = next(r for r in ROWS if r["DOCID"] == q["docid"])
            return httpx.Response(
                200, json=[{"RESULT_CODE": "SUCCESS", **row, "MINTS_HTML": MINUTES}]
            )
        rows = [r for r in ROWS if not q.get("rasmblyId") or r["RASMBLY_ID"] == q["rasmblyId"]]
        start = int(q["startCount"])
        page = rows[start : start + int(q["listCount"])]
        return httpx.Response(
            200,
            json=[
                {
                    "RESULT_CODE": "SUCCESS",
                    "TOTAL_COUNT": len(rows),
                    "LIST": [{"ROW": r} for r in page],
                }
            ],
        )

    client = ResilientClient(
        "clik_minutes",
        base_url="https://clik.example",
        limiter=MemoryLimiter(),
        breaker=MemoryBreaker(),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    manifest = await archive(_adapter(client), tmp_path, ["서산시의회"], **KW)
    assert str(manifest["runs"][-1]["stopped"]).startswith("transient")
    assert manifest["complete"] is False
    saved = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert [m["status"] for m in saved["meetings"].values()] == ["archived"]
