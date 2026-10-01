"""``manage eval fresh-minutes``: which meetings and which turns a new holdout gets is decided
by a recorded rule, not by whoever reads them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from app.eval.fresh_minutes import Rule, sample_minutes, select_excerpts
from app.sources.clik import ClikMinutesAdapter, external_id, meeting_key
from app.sources.http import ResilientClient
from app.sources.resilience import MemoryBreaker, MemoryLimiter


async def _no_sleep(_: float) -> None:
    return None


def _row(docid: str, council_id: str, council: str, meeting: str, day: str) -> dict[str, Any]:
    return {
        "DOCID": docid,
        "RASMBLY_ID": council_id,
        "RASMBLY_NM": council,
        "RASMBLY_NUMPR": "9",
        "RASMBLY_SESN": "301",
        "MINTS_ODR": "1",
        "MTGNM": meeting,
        "MTG_DE": day,
    }


ROWS = [
    _row("D1", "A", "경기도 성남시의회", "행정교육위원회", "20260905"),
    _row("D2", "B", "강원특별자치도 춘천시의회", "기획행정위원회", "20260904"),
    _row("D3", "B", "강원특별자치도 춘천시의회", "경제도시위원회", "20260903"),
    _row("D4", "C", "전라남도 순천시의회", "본회의", "20260903"),
    _row("D5", "D", "부산광역시 해운대구의회", "복지환경위원회", "20260902"),
    _row("D6", "D", "부산광역시 해운대구의회", "복지환경위원회", "20260820"),  # outside the window
]

MINUTES = (
    "○위원 김하늘  스마트 버스정류장 쉘터 몇 개소를 더 설치하실 계획입니까?\n"
    "○교통과장 박서준  내년 본예산에 12개소, 사업비 4억 8천만 원을 반영하겠습니다. "
    "상반기에 설계를 마치고 하반기에 발주하려고 합니다. 정류장별 이용객 수를 검토해 우선순위를 정하겠습니다.\n"
)


def _client() -> tuple[ResilientClient, list[str]]:
    details: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        if q["displayType"] == "list":
            start = int(q["startCount"])
            page = ROWS[start : start + int(q["listCount"])]
            body = [
                {
                    "RESULT_CODE": "SUCCESS",
                    "TOTAL_COUNT": len(ROWS),
                    "LIST": [{"ROW": r} for r in page],
                }
            ]
            return httpx.Response(200, json=body)
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


def _ext(docid: str) -> str:
    return external_id(meeting_key(next(r for r in ROWS if r["DOCID"] == docid)))


async def test_the_rule_not_the_reader_picks_the_meetings(tmp_path: Path) -> None:
    client, details = _client()
    adapter = ClikMinutesAdapter(client, "k")
    rule = Rule("2026-09-01", "2026-09-07", "위원회", ("성남시",), meetings=5, per_council=1)
    manifest = await sample_minutes(adapter, tmp_path, rule)

    # 성남 excluded, 본회의 not a committee, D6 outside the window; one meeting per council,
    # the council's first in SHA-256 order of the meeting id.
    chuncheon = min(("D2", "D3"), key=lambda d: hashlib.sha256(_ext(d).encode()).hexdigest())
    assert manifest["listed_meetings"] == 5
    assert manifest["eligible_meetings"] == 3
    assert sorted(details) == sorted([chuncheon, "D5"])
    archived = [s for s in manifest["sources"] if s["status"] == "archived"]
    assert {s["institution"] for s in archived} == {"강원특별자치도 춘천시", "부산광역시 해운대구"}
    for source in archived:
        raw = (tmp_path / source["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == source["sha256"]
        assert "스마트 버스정류장" in raw.decode()
    saved = json.loads((tmp_path / "sample.json").read_text(encoding="utf-8"))
    assert saved["rule"]["order"] == "sha256(meeting id) ascending"


def test_excerpts_are_blocks_with_money_or_a_purchase_whatever_the_speaker_layout() -> None:
    from app.eval.fresh_minutes import blocks

    # 서초구의회 writes the role and the name on lines of their own, with no ○.
    layout = (
        "위원장\n신정태\n의석을 정돈하여 주시기 바랍니다. 성원이 되었으므로 개의하겠습니다.\n" * 12
        + "도로과장\n남만조\n금회 추경에 반영한 6억원은 포트홀 긴급 보수 위주로 편성했습니다.\n"
        * 12
        + "위원장\n신정태\n다음 위원님 질의하여 주시기 바랍니다. 질의하실 위원님 계십니까?\n" * 12
    )
    spans = blocks(layout, target=300)
    assert spans[0][0] == 0 and all(a < b for a, b in spans)
    assert all(layout[a:b] == layout[a:b].strip("\n") for a, b in spans)  # whole lines only
    picked = select_excerpts("s1", layout, per_source=10)
    assert picked and all("6억원" in p["text"] or "편성" in p["text"] for p in picked)
    assert not any("6억원" not in p["text"] and "보수" not in p["text"] for p in picked)
    for p in picked:
        assert layout[p["text_char_start"] :].startswith(p["text"])
    starts = [p["text_char_start"] for p in picked]
    assert starts == sorted(starts)
    assert len(select_excerpts("s1", layout, per_source=1)) == 1


def test_a_very_long_line_is_cut_at_sentence_ends() -> None:
    from app.eval.fresh_minutes import blocks

    line = "예산 3억 원을 반영했습니다. " * 400  # one 6,800-character line
    spans = blocks(line, target=1000, longest=1800)
    assert len(spans) > 4
    assert all(b - a <= 1600 for a, b in spans)
    assert "".join(line[a:b] for a, b in spans).replace(" ", "") == line.strip().replace(" ", "")


async def test_drafted_excerpts_freeze_into_a_holdout_the_evaluator_accepts(tmp_path: Path) -> None:
    import pytest

    from app.eval.fresh_minutes import draft_cases, freeze
    from app.eval.holdout import load_holdout

    client, _ = _client()
    rule = Rule("2026-09-01", "2026-09-07", "위원회", ("성남시",), meetings=5, per_council=1)
    await sample_minutes(ClikMinutesAdapter(client, "k"), tmp_path, rule)
    counts = draft_cases(tmp_path, per_source=6, development=1)
    assert counts == {"dev": 1, "test": 1}  # each short meeting is one priced block

    for split in ("dev", "test"):
        draft = (tmp_path / f"cases-{split}.draft.jsonl").read_text(encoding="utf-8")
        with pytest.raises(FileNotFoundError):
            freeze(tmp_path, split, label_origin="test")  # nothing labelled yet
        rows = [json.loads(line) for line in draft.splitlines()]
        assert rows[0]["expected"] is None
        rows[0]["expected"] = [{"title_keywords": ["쉘터"], "budget_krw": 480_000_000}]
        (tmp_path / f"cases-{split}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        manifest = freeze(tmp_path, split, label_origin="test")
        _, cases = load_holdout(manifest)
        assert len(cases) == 1 and "교통과장 박서준" in cases[0]["_source_labels"]
        with pytest.raises(ValueError, match="never rewritten"):
            freeze(tmp_path, split, label_origin="test")
