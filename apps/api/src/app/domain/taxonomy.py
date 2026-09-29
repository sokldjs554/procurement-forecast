"""Demand taxonomy.

The categories mirror how local-government IT and facility demand actually clusters in
procurement data (조달청 세부품명 groups × 행정안전부 지역정보화 사업 분류), coarse enough that a
council member's spoken phrasing and a 사전규격's formal title land in the same bucket.
The lexicon doubles as the triage vocabulary and as feature weights for the offline embedder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Category(StrEnum):
    SMART_CITY = "smart_city"
    SAFETY_CCTV = "safety_cctv"
    PUBLIC_SW = "public_sw"
    AI_DATA = "ai_data"
    MOBILITY = "mobility"
    ENERGY_ENV = "energy_env"
    WELFARE_CARE = "welfare_care"
    EDUCATION = "education"
    TOURISM_CULTURE = "tourism_culture"
    FACILITY = "facility"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class CategoryInfo:
    label: str
    keywords: tuple[str, ...]


CATEGORIES: dict[Category, CategoryInfo] = {
    Category.SMART_CITY: CategoryInfo(
        "스마트시티·IoT",
        (
            "스마트시티",
            "스마트쉘터",
            "스마트 쉘터",
            "스마트정류장",
            "스마트 정류장",
            "IoT",
            "사물인터넷",
            "스마트폴",
            "스마트 폴",
            "디지털트윈",
            "디지털 트윈",
            "도시통합플랫폼",
            "스마트 가로등",
            "스마트가로등",
            "스마트 횡단보도",
            "스마트횡단보도",
        ),
    ),
    Category.SAFETY_CCTV: CategoryInfo(
        "안전·지능형 CCTV",
        (
            "CCTV",
            "씨씨티비",
            "선별관제",
            "지능형 관제",
            "방범",
            "통합관제",
            "재난",
            "안전",
            "침수",
            "비상벨",
            "어린이보호구역",
            "스쿨존",
            "산불감시",
        ),
    ),
    Category.PUBLIC_SW: CategoryInfo(
        "공공 SW·정보시스템",
        (
            "정보시스템",
            "시스템 구축",
            "고도화",
            "홈페이지",
            "누리집",
            "챗봇",
            "민원",
            "클라우드",
            "전환",
            "행정",
            "모바일 앱",
            "전자",
            "플랫폼 구축",
            "유지관리",
        ),
    ),
    Category.AI_DATA: CategoryInfo(
        "AI·데이터",
        (
            "인공지능",
            "AI",
            "빅데이터",
            "데이터",
            "분석",
            "예측",
            "상권분석",
            "머신러닝",
            "데이터 허브",
            "데이터허브",
            "대시보드",
        ),
    ),
    Category.MOBILITY: CategoryInfo(
        "교통·모빌리티",
        (
            "주차",
            "교통",
            "버스",
            "DRT",
            "수요응답",
            "공유",
            "킥보드",
            "모빌리티",
            "신호",
            "주차정보",
            "전기차 충전",
            "충전소",
        ),
    ),
    Category.ENERGY_ENV: CategoryInfo(
        "에너지·환경",
        (
            "태양광",
            "에너지",
            "탄소",
            "LED",
            "미세먼지",
            "환경",
            "수소",
            "재생에너지",
            "기후",
            "그린",
            "폐기물",
            "공기청정",
        ),
    ),
    Category.WELFARE_CARE: CategoryInfo(
        "복지·돌봄",
        (
            "돌봄",
            "독거",
            "어르신",
            "고독사",
            "복지",
            "치매",
            "장애인",
            "AI 스피커",
            "인공지능 스피커",
            "건강관리",
            "보건소",
            "노인",
        ),
    ),
    Category.EDUCATION: CategoryInfo(
        "교육·에듀테크",
        (
            "교육",
            "학교",
            "디지털 배움터",
            "메이커",
            "코딩",
            "평생학습",
            "도서관",
            "청소년",
            "에듀테크",
        ),
    ),
    Category.TOURISM_CULTURE: CategoryInfo(
        "관광·문화",
        (
            "관광",
            "축제",
            "미디어파사드",
            "미디어 파사드",
            "메타버스",
            "야간관광",
            "문화",
            "전시",
            "콘텐츠",
            "XR",
            "실감",
        ),
    ),
    Category.FACILITY: CategoryInfo(
        "시설·공사",
        ("리모델링", "증축", "신축", "공사", "보수", "정비", "건립", "조성공사", "개보수", "설계"),
    ),
    Category.OTHER: CategoryInfo("기타", ()),
}

# Words that indicate an actual intent to buy something (triage + heuristic extractor).
PROCUREMENT_VERBS: tuple[str, ...] = (
    "구축",
    "도입",
    "설치",
    "조성",
    "추진",
    "용역",
    "발주",
    "사업비",
    "예산",
    "반영",
    "편성",
    "시범사업",
    "확대",
    "교체",
    "고도화",
    "구매",
    "임차",
    "공모",
    "착수",
    "계약",
)

# Bureaucratic commitment phrases, strongest first. The heuristic extractor and the prompt both
# use this ladder; the backtest measures how often each rung turns into a real tender.
COMMITMENT_LADDER: dict[str, tuple[str, ...]] = {
    "committed": (
        "반영하였",
        "반영했",
        "편성하였",
        "편성했",
        "확보하였",
        "확보했",
        "발주할 예정",
        "발주 예정",
        "추진하겠습니다",
        "반영하겠습니다",
        "편성하겠습니다",
        "착수할 예정",
        "확정",
    ),
    "planned": (
        "계획입니다",
        "계획하고 있",
        "예정입니다",
        "추진할 계획",
        "준비하고 있",
        "추진 중",
        "하반기",
        "내년도",
        "본예산에",
        "추경에",
    ),
    "reviewing": (
        "검토하겠습니다",
        "검토해 보겠습니다",
        "검토하도록",
        "적극 검토",
        "살펴보겠습니다",
        "협의하겠습니다",
        "고민하겠습니다",
        "연구용역",
    ),
    "declined": (
        "어렵습니다",
        "곤란합니다",
        "어려운 실정",
        "추진하지 않",
        "보류",
        "불가",
    ),
}

NOISE_MARKERS: tuple[str, ...] = (
    "의사일정",
    "성원이 되었",
    "개의를 선포",
    "산회를 선포",
    "정회를 선포",
    "의석을 정돈",
    "회의록 서명",
    "감사합니다",
    "수고하셨습니다",
    "속기",
    "(의사봉",
)


# Words that say "some IT/public project" but not *which kind*: they vote, but weakly, so that
# "디지털트윈 플랫폼 구축" is smart_city (specific) rather than public_sw (generic "플랫폼 구축").
GENERIC_KEYWORDS: frozenset[str] = frozenset(
    (
        "시스템 구축",
        "플랫폼 구축",
        "고도화",
        "전환",
        "데이터",
        "분석",
        "안전",
        "행정",
        "전자",
        "유지관리",
        "공사",
        "설계",
        "문화",
        "공유",
        "신호",
        "그린",
        "환경",
    )
)


def classify_category(text: str) -> tuple[Category, float]:
    """Weighted keyword vote. Returns the best category and a crude confidence in [0, 1]."""
    lowered = text.lower()
    scores: dict[Category, float] = {}
    for cat, info in CATEGORIES.items():
        weight = 0.0
        for kw in info.keywords:
            if kw.lower() in lowered:
                # Longer, specific keywords are stronger evidence than short generic ones.
                weight += (0.3 if kw in GENERIC_KEYWORDS else 1.0) * (
                    1 + len(kw.replace(" ", "")) / 4
                )
        if weight:
            scores[cat] = weight
    if not scores:
        return Category.OTHER, 0.0
    best = max(scores, key=lambda c: scores[c])
    total = sum(scores.values())
    return best, round(scores[best] / total, 3)


# Budget titles name the funded purpose. Account labels and technology used to deliver it
# are weaker evidence: an IoT care service is care, a youth concert is culture, and a
# library remodel buys construction rather than educational programming.
_BUDGET_TARGETS: dict[Category, re.Pattern[str]] = {
    Category.FACILITY: re.compile(r"신축|증축|리모델링|개보수|보수공사|정비공사|시설개선공사|건립"),
    Category.PUBLIC_SW: re.compile(r"홈페이지|누리집|웹접근성|정보시스템|챗봇|클라우드"),
}
_BUDGET_PURPOSES: dict[Category, re.Pattern[str]] = {
    Category.WELFARE_CARE: re.compile(r"돌봄|고독사|안부확인|건강관리|건강증진|구강보건|치매"),
    Category.EDUCATION: re.compile(r"교육|코딩|평생학습|메이커|에듀테크"),
    Category.TOURISM_CULTURE: re.compile(
        r"교향악|페스티벌|공연|축제|관광|전시|미디어아트|미디어파사드"
    ),
    Category.SAFETY_CCTV: re.compile(
        r"침수|재난|인명구조|이안류|방범|선별관제|과속단속|산불감시|cctv"
    ),
    Category.ENERGY_ENV: re.compile(
        r"태양광|에너지|탄소|폐기물|병해충|방제|미세먼지|쓰레기|수소|led.*(?:가로등|조명)"
    ),
    Category.MOBILITY: re.compile(r"주차|교통|(?<!메타)버스|모빌리티|수요응답|전기차"),
}
_BUDGET_SMART_ASSETS = re.compile(
    r"스마트쉘터|스마트정류장|스마트버스정류장|스마트폴|디지털트윈|스마트가로등|스마트횡단보도"
)
_BUDGET_WEAK_WORDS = GENERIC_KEYWORDS | {"AI", "인공지능", "IoT", "사물인터넷", "예측"}
_GENERIC_BUDGET_TITLE = re.compile(
    r"(?:신규|노후|공공|관련)?(?:사업|장비|시설|물품|시스템)"
    r"(?:추진|구입|구매|도입|구축|개선|교체|설치)?(?:사업)?"
)
_BUDGET_DETAIL = re.compile(r"^\s*[○ㅇ∘o°·-]\s*(.+)")
_NEXT_BUDGET_ROW = re.compile(
    r"^\s*세\s*부\s*사\s*업|^[가-힣A-Za-z(].*\s+\d[\d,]*\s+\d[\d,]*\s+△?\d[\d,]*\s*$"
)


def _budget_title_category(title: str) -> tuple[Category, float]:
    compact = re.sub(r"\s+", "", title).lower()
    targets = {category for category, pattern in _BUDGET_TARGETS.items() if pattern.search(compact)}
    if targets:
        return (next(iter(targets)), 0.9) if len(targets) == 1 else (Category.OTHER, 0.0)
    purposes = set()
    if _BUDGET_SMART_ASSETS.search(compact):
        purposes.add(Category.SMART_CITY)
        # "버스" inside a named smart shelter is not a second transport project.
        compact = _BUDGET_SMART_ASSETS.sub("", compact)
    purposes.update(
        category for category, pattern in _BUDGET_PURPOSES.items() if pattern.search(compact)
    )
    if purposes:
        return (next(iter(purposes)), 0.9) if len(purposes) == 1 else (Category.OTHER, 0.0)
    scores = {
        category: sum(
            1 + len(keyword.replace(" ", "")) / 4
            for keyword in info.keywords
            if keyword not in _BUDGET_WEAK_WORDS and keyword.replace(" ", "").lower() in compact
        )
        for category, info in CATEGORIES.items()
    }
    total = sum(scores.values())
    if not total:
        return Category.OTHER, 0.0
    best = max(scores, key=lambda category: scores[category])
    confidence = scores[best] / total
    return (best, round(confidence, 3)) if confidence >= 0.6 else (Category.OTHER, 0.0)


def classify_budget_category(title: str, details: str = "") -> tuple[Category, float]:
    """Use the project's purchase/purpose before incidental account or technology words.

    Detail fallback is restricted to genuinely generic titles and explicit basis bullets.
    A named but unrecognised program, multiple purposes, or conflicting details stays other;
    a heuristic score is not a measured probability of correct classification.
    """
    category, confidence = _budget_title_category(title)
    if category is not Category.OTHER:
        return category, confidence
    if not _GENERIC_BUDGET_TITLE.fullmatch(re.sub(r"\s+", "", title)):
        return Category.OTHER, 0.0
    categories: set[Category] = set()
    for line in details.splitlines():
        if _NEXT_BUDGET_ROW.search(line):
            break
        if match := _BUDGET_DETAIL.match(line):
            category, _ = _budget_title_category(match[1])
            if category is not Category.OTHER:
                categories.add(category)
            else:
                return Category.OTHER, 0.0
    return (next(iter(categories)), 0.6) if len(categories) == 1 else (Category.OTHER, 0.0)


def commitment_level(text: str) -> str | None:
    for level, phrases in COMMITMENT_LADDER.items():
        if any(p in text for p in phrases):
            return level
    return None
