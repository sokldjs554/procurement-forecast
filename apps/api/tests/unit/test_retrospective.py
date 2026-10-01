"""The retrospective check counts only reviewed pairs the declared candidate step proposed."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.eval.retrospective import (
    Judgment,
    candidate_pairs,
    overlap,
    report,
    squash,
    tokens,
)


def _signal(key: str, title: str, observed: str, stage: str = "council_mention") -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "observed_at": observed,
        "stage": stage,
        "budget_krw": None,
        "document": {"title": "회의록", "structured": {}},
    }


def _notice(
    no: str, title: str, registered: str, institution: str = "경기도 성남시"
) -> dict[str, Any]:
    return {
        "bidNtceNo": no,
        "bidNtceOrd": "000",
        "bidNtceNm": title,
        "bidNtceDt": f"{registered} 10:00:00",
        "ntceInsttNm": "조달청 경기지방조달청",
        "dminsttNm": institution,
    }


def test_generic_words_do_not_make_a_project_name() -> None:
    assert tokens("소규모 정비공사") == []
    assert tokens("2026년 대장지구 공공도서관 건립") == ["대장지구", "공공도서관"]
    assert tokens("돌마2터널 제연설비 설치 공사") == ["돌마2터널", "제연설비"]


def test_a_part_of_a_name_counts_by_its_longest_shared_end() -> None:
    score, shared = overlap(["대장지구", "공공도서관"], squash("대장지구 도서관 건립공사"))
    assert shared == ["대장지구", "도서관"]
    assert score == pytest.approx(7 / 9)
    assert overlap(["직원교육", "비교견학"], squash("직원 교육 용역")) == (0.5, ["직원교육"])


def test_candidates_come_from_the_same_city_whatever_the_notice_date() -> None:
    signals = [_signal("s1", "수내교 전면 개축공사", "2025-11-20")]
    notices = [
        _notice("R25BK1", "수내교 재가설공사 실시설계용역", "2025-04-01"),
        _notice("R26BK2", "수내교 재가설공사", "2026-03-02"),
        _notice("R26BK3", "수내교 재가설공사", "2026-03-02", institution="서울특별시 강남구"),
        _notice("R26BK4", "정자교 재가설공사", "2026-03-03"),
    ]
    found = candidate_pairs(signals, notices, "성남시")
    assert [c.notice for c in found] == ["R25BK1-000", "R26BK2-000"]


def _run(judgments: list[Judgment], notices: list[dict[str, Any]], signals: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    proposed = {(c.signal_key, c.notice) for c in candidate_pairs(signals, notices, "성남시")}
    return report(
        signals,
        notices,
        judgments,
        observed_through=date(2026, 9, 30),
        proposed=proposed,
    )


def test_hits_tenders_already_out_and_open_cases_are_kept_apart() -> None:
    signals = [
        _signal("hit", "돌마2터널 제연설비 설치 공사", "2025-09-12"),
        _signal("old", "판교대장 종합사회복지관 건립", "2025-11-20"),
        _signal("young", "오리공원 물놀이장 설치 공사", "2026-03-12"),
        _signal("none", "대장지구 공공도서관 건립", "2025-09-12"),
        _signal("unsure", "산성공원 재정비 및 숲속 커뮤니티 건립 사업", "2025-09-12"),
    ]
    notices = [
        _notice("A", "돌마2터널 제연설비 설치공사", "2026-02-10"),
        _notice("B", "판교대장 종합사회복지관 건립공사", "2025-06-01"),
        _notice("C", "대장지구 도서관 주차장 포장 보수", "2026-01-05"),
        _notice("D", "산성공원 숲속 커뮤니티센터 실시설계용역", "2026-04-01"),
    ]
    judgments = [
        Judgment("hit", "A-000", "same", "공사", "같은 터널의 같은 설비"),
        Judgment("old", "B-000", "same", "공사", "회의 전에 이미 공고"),
        Judgment("none", "C-000", "different", None, "같은 지구의 다른 일"),
        Judgment("unsure", "D-000", "ambiguous", "설계", "센터 범위가 발언과 다를 수 있음"),
    ]
    result = _run(judgments, notices, signals)
    mentions = result["strata"]["council_mention"]
    assert mentions["signals"] == 5
    assert mentions["hit"] == 1 and mentions["already_tendered"] == 1
    assert mentions["no_tender_seen"] == 1 and mentions["ambiguous"] == 1
    assert mentions["observing"] == 1  # six months is too young to call
    assert mentions["lead"] == {"n": 1, "median_days": 151, "min_days": 151, "max_days": 151}
    # 12-month rate: the already-tendered one is not a forecast, the young one is not eligible
    assert mentions["hit_within_12m"] == {"hit": 1, "eligible": 3}
    assert [h["notice"] for h in result["hits"]] == ["A-000"]


def test_a_judgment_on_a_pair_the_method_never_proposed_is_refused() -> None:
    signals = [_signal("s", "돌마2터널 제연설비 설치 공사", "2025-09-12")]
    notices = [
        _notice("A", "돌마2터널 제연설비 설치공사", "2026-02-10"),
        _notice("Z", "성남시 터널 안전점검 용역", "2026-02-11"),
    ]
    with pytest.raises(ValueError, match="not proposed"):
        _run([Judgment("s", "Z-000", "same")], notices, signals)
