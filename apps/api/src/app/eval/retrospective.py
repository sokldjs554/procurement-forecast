"""Retrospective check (``docs/retrospective-validation.md``): did signals caught in past minutes
and budget books reach an official 입찰공고, and how long after?

Two steps, kept apart on purpose:

1. :func:`candidate_pairs` proposes, for every signal, the census notices of the same 지자체
   whose titles share a distinctive part of the signal's project name. It only proposes; a
   shared word is not a match ("도로 정비" is in hundreds of notices).
2. A reviewer writes one judgment per proposed pair (``same`` / ``different`` / ``ambiguous``,
   with the stage — 설계·공사·감리·구매·용역 — and a reason). :func:`report` counts only those
   judgments: a signal is a hit when a ``same`` notice was registered after the signal's date,
   already-tendered when the first ``same`` notice came on or before it, and "no tender seen"
   otherwise — never "not procured", because 수의계약 and tenders outside 나라장터 are not in
   the census.
"""

from __future__ import annotations

import gzip
import json
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

# Words that say what is done, not to what; on their own they match anything.
GENERIC = frozenset(
    """
    사업 사업비 용역 공사 구매 설치 제작 및 등 위한 추진 운영 관리 지원 조성 정비 개선 보수 확충
    구축 도입 건립 신축 증축 개축 교체 설계 실시설계 기본설계 감리 건설사업관리 유지 유지관리 시설
    시설물 개보수 리모델링 보강 재정비 확장 이전 신설 계획 기본계획 수립 연구 조사 점검 진단
    정밀안전진단 안전점검 물품 장비 시스템 노후 긴급 일반 기타 추가 신규 내 의 연간 단가
    전면 부분 소규모 년 년도 년도분 차 1차 2차 3차 일원 일대 외 개소 식 대 회 건 종합 기반
    시 구 동 공공 지역 시민 행정
    """.split()  # noqa: SIM905 - a word list reads better as words
)
_TOKEN = re.compile(r"[가-힣A-Za-z0-9]+")
_YEAR = re.compile(r"^(19|20)\d{2}(년|년도)?$")


def tokens(title: str) -> list[str]:
    """Distinctive parts of a project name: not generic, not a year, two characters or more."""
    out: list[str] = []
    for raw in _TOKEN.findall(title):
        token = raw
        for suffix in ("사업", "공사", "용역", "설치", "구매", "건립", "조성", "정비"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                token = token[: -len(suffix)]
                break
        if len(token) < 2 or token in GENERIC or _YEAR.match(token) or token.isdigit():
            continue
        if token not in out:
            out.append(token)
    return out


def squash(title: str) -> str:
    return re.sub(r"\s+", "", title)


_PLACE_SUFFIX = re.compile(r"(특별자치시|특별자치도|특별시|광역시|시|군|구|도)$")


# How a 도's tenders and speeches shorten its name ("충북 RISE 사업").
_PROVINCE_SHORT = {
    "충청북도": "충북",
    "충청남도": "충남",
    "전라북도": "전북",
    "전북특별자치도": "전북",
    "전라남도": "전남",
    "경상북도": "경북",
    "경상남도": "경남",
    "강원특별자치도": "강원",
    "강원도": "강원",
    "경기도": "경기",
    "제주특별자치도": "제주",
}
_PROVINCE = re.compile(r"(도|특별자치도|특별시|광역시|특별자치시)$")
_LOCAL = re.compile(r"[가-힣]+(시|군|구)$")


def is_province(institution: str) -> bool:
    """A 광역 지자체 (도, 특별시, 광역시): ``충청북도``, not ``서산시``."""
    return bool(_PROVINCE.search(institution)) and not _LOCAL.fullmatch(institution)


def belongs_to(demand: str, institution: str) -> bool:
    """Whether a notice's 수요기관 is ``institution`` or one of its own offices.

    For a 시·군·구 that is its name anywhere in the 수요기관 ("경기도 고양시 덕양구"). A 도's
    name is also in every 시·군 under it and in its 교육청, which are other 지자체 or bodies:
    "충청북도 도로관리사업소" is 충청북도, "충청북도 청주시" and "충청북도교육청" are not."""
    at = demand.find(institution)
    if at < 0:
        return False
    if not is_province(institution):
        return True
    rest = demand[at + len(institution) :]
    if rest and not rest[0].isspace():  # "충청북도교육청", "충청북도충주의료원": another body
        return False
    first = rest.split()[0] if rest.split() else ""
    return not _LOCAL.fullmatch(first)


def without_place(title: str, institution: str) -> str:
    """``title`` without the 지자체's own name ("서산시", "서산", "충북"): every notice of that
    지자체 may carry it, so it says nothing about which project a notice is."""
    stem = _PLACE_SUFFIX.sub("", institution)
    names = {institution, stem, _PROVINCE_SHORT.get(institution, "")}
    for name in sorted(names - {""}, key=len, reverse=True):
        if len(name) >= 2:
            title = title.replace(name, " ")
    return title


def overlap(parts: list[str], haystack: str) -> tuple[float, list[str]]:
    """Share of a signal's distinctive characters found in a notice title (``squash``-ed).

    A part counts by its longest prefix or suffix of three characters or more that the title
    contains, so "공공도서관" still finds "대장지구도서관" through "도서관"."""
    if not parts:
        return 0.0, []
    found: list[str] = []
    matched = 0
    for part in parts:
        for size in range(len(part), 2, -1):
            piece = next((p for p in (part[:size], part[-size:]) if p in haystack), None)
            if piece:
                found.append(piece)
                matched += size
                break
        else:
            if len(part) == 2 and part in haystack:
                found.append(part)
                matched += 2
    return matched / sum(map(len, parts)), found


def notice_id(notice: dict[str, Any]) -> str:
    return f"{notice.get('bidNtceNo')}-{notice.get('bidNtceOrd') or '000'}"


def notice_date(notice: dict[str, Any]) -> date | None:
    raw = str(notice.get("bidNtceDt") or notice.get("rgstDt") or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def demand_institution(notice: dict[str, Any]) -> str:
    """수요기관 when given (조달청 tenders for a city name the city there), else 공고기관."""
    return str(notice.get("dminsttNm") or notice.get("ntceInsttNm") or "")


def load_notices(path: Path) -> list[dict[str, Any]]:
    """Census notices, one per 공고번호-차수 (a resumed census never writes one twice, but a
    notice re-registered under the same number would be)."""
    seen: dict[str, dict[str, Any]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            seen.setdefault(notice_id(row), row)
    return list(seen.values())


def load_signals(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def stratum(signal: dict[str, Any]) -> str:
    doc = signal.get("document") or {}
    if signal.get("stage") == "council_mention":
        return "council_mention"
    title = str(doc.get("title") or "")
    year = (doc.get("structured") or {}).get("fiscal_year")
    kind = "supplementary" if "추경" in title else "main"
    return f"budget_{year}_{kind}"


@dataclass(frozen=True, slots=True)
class Candidate:
    signal_key: str
    notice: str
    score: float
    shared: tuple[str, ...]
    notice_title: str
    notice_date: str | None
    institution: str


def candidate_pairs(
    signals: Iterable[dict[str, Any]],
    notices: list[dict[str, Any]],
    institution: str,
    *,
    min_score: float = 0.5,
    min_shared_chars: int = 3,
    per_signal: int = 8,
) -> list[Candidate]:
    """Notices of ``institution`` sharing at least half of a signal's distinctive characters
    (and at least ``min_shared_chars`` of them), best first, at most ``per_signal`` each."""
    own = [
        (n, title, squash(without_place(title, institution)))
        for n in notices
        if belongs_to(demand_institution(n), institution)
        for title in [str(n.get("bidNtceNm") or "")]
    ]
    out: list[Candidate] = []
    for signal in signals:
        parts = tokens(without_place(str(signal.get("title") or ""), institution))
        found: list[Candidate] = []
        for notice, title, haystack in own:
            score, shared = overlap(parts, haystack)
            if score < min_score or sum(map(len, shared)) < min_shared_chars:
                continue
            when = notice_date(notice)
            found.append(
                Candidate(
                    signal_key=str(signal["key"]),
                    notice=notice_id(notice),
                    score=round(score, 3),
                    shared=tuple(shared),
                    notice_title=title,
                    notice_date=when.isoformat() if when else None,
                    institution=demand_institution(notice),
                )
            )
        found.sort(key=lambda c: (-c.score, c.notice_date or "9999", c.notice))
        out += found[:per_signal]
    return out


@dataclass(frozen=True, slots=True)
class Judgment:
    signal_key: str
    notice: str
    verdict: str  # same | different | ambiguous
    # 설계 · 공사 · 감리 · 구매 · 용역 · 기타, or 연례: the same yearly contract (road resurfacing,
    # pest control, vaccine purchase) let again every year, which no one needs a forecast to see.
    stage: str | None = None
    reason: str = ""


VERDICTS = {"same", "different", "ambiguous"}
RECURRING = "연례"


def load_judgments(path: Path) -> list[Judgment]:
    out: list[Judgment] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw.get("verdict") not in VERDICTS:
            raise ValueError(f"{path}:{n}: verdict must be one of {sorted(VERDICTS)}")
        out.append(
            Judgment(
                signal_key=raw["signal_key"],
                notice=raw["notice"],
                verdict=raw["verdict"],
                stage=raw.get("stage"),
                reason=raw.get("reason", ""),
            )
        )
    return out


def load_title_judgments(path: Path) -> dict[tuple[str, str], Judgment]:
    """Judgments written once per (signal name, notice name), keyed by both names ``squash``-ed.

    The same project name recurs across budget books and meetings, and whether two names are one
    project does not depend on which book or meeting said it. Writing them by name also keeps the
    reviewer blind to dates: a name pair carries no signal date, so it cannot lean toward a hit."""
    out: dict[tuple[str, str], Judgment] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw.get("verdict") not in VERDICTS:
            raise ValueError(f"{path}:{n}: verdict must be one of {sorted(VERDICTS)}")
        key = (squash(raw["signal_title"]), squash(raw["notice_title"]))
        if key in out:
            raise ValueError(
                f"{path}:{n}: judged twice: {raw['signal_title']} / {raw['notice_title']}"
            )
        out[key] = Judgment("", "", raw["verdict"], raw.get("stage"), raw.get("reason", ""))
    return out


def judgments_for_pairs(
    by_title: dict[tuple[str, str], Judgment],
    proposed: Iterable[dict[str, Any]],
    signals: Iterable[dict[str, Any]],
) -> tuple[list[Judgment], list[dict[str, Any]]]:
    """One judgment per proposed pair from the name-level ones, and the pairs no name-level
    judgment covers (a report needs none of those)."""
    title_of = {str(s["key"]): str(s.get("title") or "") for s in signals}
    out: list[Judgment] = []
    unjudged: list[dict[str, Any]] = []
    for pair in proposed:
        key = (squash(title_of[str(pair["signal_key"])]), squash(str(pair["notice_title"])))
        found = by_title.get(key)
        if found is None:
            unjudged.append(pair)
            continue
        out.append(
            Judgment(
                str(pair["signal_key"]),
                str(pair["notice"]),
                found.verdict,
                found.stage,
                found.reason,
            )
        )
    return out, unjudged


def months_between(start: date, end: date) -> float:
    return (end - start).days / 30.4375


def report(
    signals: list[dict[str, Any]],
    notices: list[dict[str, Any]],
    judgments: list[Judgment],
    *,
    observed_through: date,
    proposed: set[tuple[str, str]],
    window_months: int = 12,
) -> dict[str, Any]:
    """Counts per stratum from the judgments alone. ``proposed`` are the pairs the candidate
    step offered; a judgment on any other pair is refused, so a reviewer cannot add matches
    the declared method would not have found."""
    by_notice = {notice_id(n): n for n in notices}
    judged: dict[str, list[Judgment]] = defaultdict(list)
    for j in judgments:
        if (j.signal_key, j.notice) not in proposed:
            raise ValueError(f"judgment on a pair that was not proposed: {j.signal_key} {j.notice}")
        if j.notice not in by_notice:
            raise ValueError(f"judgment names a notice outside the census: {j.notice}")
        judged[j.signal_key].append(j)

    strata: dict[str, Counter[str]] = defaultdict(Counter)
    leads: dict[str, list[int]] = defaultdict(list)
    leads_project: dict[str, list[int]] = defaultdict(list)
    within: dict[str, Counter[str]] = defaultdict(Counter)
    hits: list[dict[str, Any]] = []
    for signal in signals:
        name = stratum(signal)
        seen = date.fromisoformat(str(signal["observed_at"])[:10])
        mine = judged.get(str(signal["key"]), [])
        same = sorted(
            (
                (notice_date(by_notice[j.notice]) or date.max, j)
                for j in mine
                if j.verdict == "same"
            ),
            key=lambda pair: (pair[0], pair[1].notice),
        )
        counts = strata[name]
        counts["signals"] += 1
        followed = months_between(seen, observed_through)
        recurring = bool(same) and same[0][1].stage == RECURRING
        if same and same[0][0] <= seen:
            counts["already_tendered"] += 1  # not a forecast: out of every denominator below
            counts["already_tendered_recurring"] += recurring
            continue
        if followed >= window_months:
            within[name]["eligible"] += 1
            if not recurring:
                within[name]["eligible_project"] += 1
        if same:
            first_date, first = same[0]
            lead = (first_date - seen).days
            counts["hit"] += 1
            counts["hit_recurring"] += recurring
            leads[name].append(lead)
            if not recurring:
                leads_project[name].append(lead)
            if followed >= window_months and months_between(seen, first_date) <= window_months:
                within[name]["hit"] += 1
                if not recurring:
                    within[name]["hit_project"] += 1
            notice = by_notice[first.notice]
            hits.append(
                {
                    "stratum": name,
                    "signal_key": signal["key"],
                    "signal_title": signal.get("title"),
                    "signal_date": seen.isoformat(),
                    "budget_krw": signal.get("budget_krw"),
                    "notice": first.notice,
                    "notice_title": notice.get("bidNtceNm"),
                    "notice_date": first_date.isoformat(),
                    "institution": demand_institution(notice),
                    "stage": first.stage,
                    "recurring": recurring,
                    "lead_days": lead,
                    "reason": first.reason,
                }
            )
            continue
        if any(j.verdict == "ambiguous" for j in mine):
            counts["ambiguous"] += 1
        elif followed < window_months:
            counts["observing"] += 1
        else:
            counts["no_tender_seen"] += 1

    def lead_summary(values: list[int]) -> dict[str, Any] | None:
        if not values:
            return None
        return {
            "n": len(values),
            "median_days": statistics.median(values),
            "min_days": min(values),
            "max_days": max(values),
        }

    return {
        "observed_through": observed_through.isoformat(),
        "window_months": window_months,
        "strata": {
            name: dict(counts)
            | {
                "lead": lead_summary(leads[name]),
                "lead_excluding_recurring": lead_summary(leads_project[name]),
                f"hit_within_{window_months}m": {
                    "hit": within[name]["hit"],
                    "eligible": within[name]["eligible"],
                },
                # A yearly contract let again is no forecast; it leaves numerator and denominator.
                f"hit_within_{window_months}m_excluding_recurring": {
                    "hit": within[name]["hit_project"],
                    "eligible": within[name]["eligible_project"],
                },
            }
            for name, counts in sorted(strata.items())
        },
        "judgments": Counter(j.verdict for j in judgments),
        "hits": sorted(hits, key=lambda h: (h["stratum"], h["signal_date"], h["notice_date"])),
    }
