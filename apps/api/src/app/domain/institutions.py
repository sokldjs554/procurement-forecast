"""Institution resolution — turning "강남구청 스마트도시과", "서울 강남구", "강남구의회" into one entity.

Why this is harder than it looks:

* Six metropolitan cities each have a 중구, five have a 동구 — the bare name is ambiguous and only
  the document's context (which council published the minutes, which sido the 발주기관 sits in)
  can disambiguate. Ambiguity is surfaced, never guessed.
* Councils (구의회) talk about projects the executive (구청) will buy. Demand is attributed to the
  executive via ``executive_code``; the council is kept as the *speaker*.
* Departments are noise for identity but signal for sales ("스마트도시과" is who to call), so they
  are split off and kept.
* Most of what 조달청 buys for is not a 지자체: on 30 days of live data (2026-09-26) schools,
  hospitals, universities and 공사·공단 made up most of 6,344 수요기관 names. Those records carry the
  provider's own institution code, so they are identified by that code instead of by name
  (:func:`provider_institution`); names are only guessed where no code comes with them.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib import resources
from typing import Literal

from rapidfuzz import fuzz, process

from app.domain.text import normalize, to_jamo

InstitutionKind = Literal["local_gov", "council", "education_office", "public_agency", "central"]

SIDO_ALIASES: dict[str, tuple[str, ...]] = {
    "서울특별시": ("서울특별시", "서울시", "서울"),
    "부산광역시": ("부산광역시", "부산시", "부산"),
    "대구광역시": ("대구광역시", "대구시", "대구"),
    "인천광역시": ("인천광역시", "인천시", "인천"),
    "광주광역시": ("광주광역시", "광주시", "광주"),
    "대전광역시": ("대전광역시", "대전시", "대전"),
    "울산광역시": ("울산광역시", "울산시", "울산"),
    "세종특별자치시": ("세종특별자치시", "세종시", "세종"),
    "경기도": ("경기도", "경기"),
    "강원특별자치도": ("강원특별자치도", "강원도", "강원"),
    "충청북도": ("충청북도", "충북"),
    "충청남도": ("충청남도", "충남"),
    "전북특별자치도": ("전북특별자치도", "전라북도", "전북"),
    "전라남도": ("전라남도", "전남"),
    "전남광주통합특별시": ("전남광주통합특별시",),
    "경상북도": ("경상북도", "경북"),
    "경상남도": ("경상남도", "경남"),
    "제주특별자치도": ("제주특별자치도", "제주도", "제주"),
}
_SIDO_LOOKUP = {alias: full for full, aliases in SIDO_ALIASES.items() for alias in aliases}
# 법정동코드 시도 part; the profile's region filter speaks these. 광주광역시 and 전라남도 were
# abolished on 2026-07-01 but stay: older documents and saved profiles still carry them.
SIDO_REGION_CODES: dict[str, str] = {
    "서울특별시": "11",
    "부산광역시": "26",
    "대구광역시": "27",
    "인천광역시": "28",
    "광주광역시": "29",
    "대전광역시": "30",
    "울산광역시": "31",
    "세종특별자치시": "36",
    "경기도": "41",
    "강원특별자치도": "51",
    "충청북도": "43",
    "충청남도": "44",
    "전북특별자치도": "52",
    "전라남도": "46",
    "전남광주통합특별시": "12",
    "경상북도": "47",
    "경상남도": "48",
    "제주특별자치도": "50",
}
# 시도 codes that went into another; a region filter on either side sees both.
SIDO_SUCCESSORS: dict[str, str] = {"29": "12", "46": "12"}
PROVIDER_CODE_PREFIX = "G2B-"
# Longest alias first so "서울특별시" wins over "서울".
_SIDO_RE = re.compile("|".join(sorted(map(re.escape, _SIDO_LOOKUP), key=len, reverse=True)))

_DEPT_SUFFIX_RE = re.compile(
    r"^[가-힣A-Za-z0-9·]{2,}(?:과|팀|담당관|사업소|센터|보건소|국|실|단|본부|추진단|지원단)$"
)
_SIGUNGU_RE = re.compile(r"([가-힣]{1,5}(?:시|군|구))(?:청)?$")
# "사단법인 한국복숭아생산자협의회" is no 의회, and "해군 잠수함수리창" no 군.
_COUNCIL_RE = re.compile(r"(?<!협)의회")
_ARMED_FORCES = frozenset({"육군", "해군", "공군"})


@dataclass(frozen=True, slots=True)
class Institution:
    code: str
    name: str
    kind: InstitutionKind
    sido: str
    sigungu: str | None
    region_code: str
    executive_code: str | None = None
    aliases: tuple[str, ...] = ()

    @property
    def demand_owner_code(self) -> str:
        """Councils discuss what the executive will buy."""
        return self.executive_code or self.code


@dataclass(frozen=True, slots=True)
class ParsedName:
    raw: str
    sido: str | None
    sigungu: str | None
    is_council: bool
    is_education: bool
    department: str | None
    key: str
    sido_only: bool = False  # the name is the 시도 itself (plus its own departments)


@dataclass(slots=True)
class Resolution:
    institution: Institution | None
    department: str | None
    score: float
    method: Literal["code", "exact", "context", "fuzzy", "ambiguous", "provider", "none"]
    candidates: list[tuple[str, float]] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.institution is not None


def _strip_sido_prefix(token: str) -> str:
    """ "서울특별시강남구청" -> "강남구청", but keep "부산진구" (a real 구, not 부산 + 진구)."""
    m = _SIDO_RE.match(token)
    if not m:
        return token
    rest = token[m.end() :]
    core = rest[:-1] if rest.endswith("청") else rest
    if rest and (len(m.group(0)) >= 3 or len(core) >= 3):
        return rest
    return token


def parse_name(raw: str) -> ParsedName:
    text = normalize(raw).replace("(", " ").replace(")", " ").strip()
    is_council = _COUNCIL_RE.search(text) is not None
    is_education = "교육청" in text or "교육지원청" in text
    sido_match = _SIDO_RE.search(text)
    sido = _SIDO_LOOKUP[sido_match.group(0)] if sido_match else None

    department: str | None = None
    sigungu: str | None = None
    for tok in text.split():
        if tok in _ARMED_FORCES:
            continue
        tok_clean = tok.replace("의회", "")
        # "강남구청장", "성남시장", "영월군수" name the institution through its head.
        tok_clean = re.sub(r"^([가-힣]{1,5}(?:구|시|군))(?:청장|장|수)$", r"\1", tok_clean)
        if tok_clean in _SIDO_LOOKUP and sido and _SIDO_LOOKUP[tok_clean] != sido:
            # "경기도 광주시" is a 시 in 경기도, not 광주광역시 (seen on 조달청 data).
            if not sigungu and tok_clean.endswith("시"):
                sigungu = tok_clean
            continue
        if tok_clean in _SIDO_LOOKUP or not tok_clean:
            continue
        if _DEPT_SUFFIX_RE.match(tok_clean) and not re.search(r"(?:시|군|구)(?:청)?$", tok_clean):
            department = department or tok_clean
            continue
        stripped = _strip_sido_prefix(tok_clean)
        m = _SIGUNGU_RE.search(stripped)
        if m and not sigungu and m.group(1) not in _SIDO_LOOKUP:
            candidate = m.group(1)
            # "서울시" alone is a sido, not a sigungu.
            if candidate not in {
                "서울시",
                "부산시",
                "대구시",
                "인천시",
                "광주시",
                "대전시",
                "울산시",
            }:
                sigungu = candidate
            trailing = stripped[m.end() :]
            if trailing and _DEPT_SUFFIX_RE.match(trailing):
                department = department or trailing
        elif not sigungu and not department:
            m2 = re.match(
                r"^([가-힣]{1,5}(?:시|군|구))(?:청)?([가-힣]{2,}(?:과|팀|센터|담당관))$", tok_clean
            )
            if m2:
                sigungu, department = m2.group(1), m2.group(2)
    key = re.sub(r"\s+", "", f"{sido or ''}{sigungu or ''}{'의회' if is_council else ''}")
    if not key:
        key = re.sub(r"\s+", "", text)
    return ParsedName(
        raw, sido, sigungu, is_council, is_education, department, key, _is_sido_only(text, sido)
    )


# Bodies of their own that often carry a 시도 or 시군구 name: "경기도 신성중학교", "서울시
# 강서구시설관리공단", and on 30 days of 조달청 data (2026-09-26) "충청남도 천안의료원", "재단법인
# 영동군 문화관광재단", "경주시 수산업협동조합", "사단법인 거제시관광협의회", "한국국제기아대책기구".
# A 군's 보건의료원 is the 군's own office, like a 보건소.
_OWN_BODY = ("학교", "공사", "공단", "조합", "재단", "의료원", "협의회", "기구")


def _is_own_body(text: str) -> bool:
    return any(w in text.replace("보건의료원", "") for w in _OWN_BODY)


def _leading_sido(text: str) -> str | None:
    """The 시도 when the name *starts* with one as a word ("경기도 …", "서울시청 …"). The ``sido``
    found by searching anywhere is looser: "해운대구" contains 대구, "서울교통공사" 서울."""
    tokens = text.split()
    if not tokens:
        return None
    head = tokens[0].replace("의회", "")
    if head.endswith("청") and head[:-1] in _SIDO_LOOKUP:
        head = head[:-1]
    return _SIDO_LOOKUP.get(head)


def _is_sido_only(text: str, sido: str | None) -> bool:
    """ "경기도", "서울시청 스마트도시과", "서울특별시 영등포소방서": the 시도 and its own offices.
    Not "서울교통공사", "부산대학교 산학협력단" or "국토교통부 부산지방국토관리청 …", which only carry
    a place name, and not "경기도 신성중학교" (the 교육청's) or a 구's 시설관리공단. On 30 days of
    조달청 data (2026-09-26) names like these were most of what resolved to a 시도, all to the
    wrong demand owner."""
    if sido is None or _leading_sido(text) != sido:
        return False
    return not any(_is_own_body(t) or _SIDO_LOOKUP.get(t, sido) != sido for t in text.split()[1:])


class InstitutionRegistry:
    def __init__(self, institutions: Iterable[Institution]) -> None:
        self._by_code: dict[str, Institution] = {}
        self._by_sigungu: dict[str, list[Institution]] = {}
        self._sido_level: dict[tuple[str, InstitutionKind], list[Institution]] = {}
        self._alias_index: dict[str, str] = {}
        self._jamo_index: dict[str, str] = {}
        # Every 시도 an institution was named under: "전라남도 순천시" is now "전남광주통합특별시
        # 순천시", and its old name, kept as an alias, still says which 순천시.
        self._sidos: dict[str, frozenset[str]] = {}
        for inst in institutions:
            self.add(inst)

    def add(self, inst: Institution) -> None:
        self._by_code[inst.code] = inst
        if inst.code.startswith(PROVIDER_CODE_PREFIX):
            # Known by code. Exact spellings still find it, but it stays out of the sigungu pools
            # and the fuzzy index, and never takes a name over from the curated table.
            self._alias_index.setdefault(_compact(inst.name), inst.code)
            return
        if inst.sigungu:
            self._by_sigungu.setdefault(inst.sigungu, []).append(inst)
        else:
            self._sido_level.setdefault((inst.sido, inst.kind), []).append(inst)
        for alias in (inst.name, *inst.aliases):
            compact = _compact(alias)
            self._alias_index[compact] = inst.code
            self._jamo_index[to_jamo(compact)] = inst.code
        former = {parse_name(a).sido for a in inst.aliases}
        self._sidos[inst.code] = frozenset({inst.sido} | {s for s in former if s})

    def _in_sido(self, inst: Institution, sido: str) -> bool:
        return sido in self._sidos.get(inst.code, (inst.sido,))

    def knows_name(self, raw: str) -> bool:
        return _compact(raw) in self._alias_index

    def __len__(self) -> int:
        return len(self._by_code)

    def __iter__(self) -> Iterable[Institution]:
        return iter(self._by_code.values())

    def get(self, code: str) -> Institution | None:
        return self._by_code.get(code)

    def all(self) -> Sequence[Institution]:
        return list(self._by_code.values())

    def resolve(
        self,
        raw: str | None,
        *,
        sido_hint: str | None = None,
        code_hint: str | None = None,
        provider_code: str | None = None,
        fuzzy_threshold: float = 90.0,
    ) -> Resolution:
        if code_hint and (inst := self._by_code.get(code_hint)):
            dept = parse_name(raw).department if raw else None
            return Resolution(inst, dept, 1.0, "code")
        own_code = PROVIDER_CODE_PREFIX + provider_code if provider_code else None
        # A 지자체 is the table's to name, whatever its code: "(재)구리시상권활성화재단" filed a
        # 발주계획 under 구리시's own code (live, 2026-09-26), and 구리시's 공고 followed it.
        if (
            own_code
            and (inst := self._by_code.get(own_code))
            and not (raw and looks_like_local_government(raw))
        ):
            return Resolution(inst, None, 1.0, "code")
        if not raw or not raw.strip():
            return Resolution(None, None, 0.0, "none")
        parsed = parse_name(raw)
        sido = parsed.sido or (_SIDO_LOOKUP.get(sido_hint, sido_hint) if sido_hint else None)

        compact = _compact(raw)
        code = self._alias_index.get(compact)
        if own_code and code and code.startswith(PROVIDER_CODE_PREFIX) and code != own_code:
            code = None  # two 조달청 codes, one name: each record keeps its own code's
        if code is not None:
            return Resolution(self._by_code[code], parsed.department, 1.0, "exact")
        # Named after its place, not run by it: at ingest its 조달청 code identifies it.
        own_body = _is_own_body(normalize(raw))

        if parsed.sigungu and not own_body:
            pool = self._by_sigungu.get(parsed.sigungu, [])
            wanted: InstitutionKind = "council" if parsed.is_council else "local_gov"
            pool = [i for i in pool if i.kind == wanted] or pool
            if parsed.sido:
                pool = [i for i in pool if self._in_sido(i, parsed.sido)]
            if len(pool) == 1:
                return Resolution(pool[0], parsed.department, 1.0, "exact")
            if len(pool) > 1 and sido:
                narrowed = [i for i in pool if self._in_sido(i, sido)]
                if len(narrowed) == 1:
                    return Resolution(narrowed[0], parsed.department, 0.9, "context")
            if len(pool) > 1:
                return Resolution(
                    None, parsed.department, 0.0, "ambiguous", [(i.code, 1.0) for i in pool]
                )
        elif parsed.sido_only and not parsed.is_education and not own_body:
            # "경기도" alone, or "제주특별자치도 관광정책과".
            wanted_sido: InstitutionKind = "council" if parsed.is_council else "local_gov"
            sido_level = self._sido_level.get((parsed.sido or "", wanted_sido), [])
            if len(sido_level) == 1:
                return Resolution(sido_level[0], parsed.department, 0.95, "exact")

        found = self._fuzzy(compact, parsed.department, fuzzy_threshold)
        inst = found.institution
        if inst is not None and (
            (own_body and inst.kind in ("local_gov", "council"))
            or (parsed.department and _is_own_body(parsed.department))
            or (
                (lead := _leading_sido(normalize(raw))) is not None
                and not self._in_sido(inst, lead)
            )
            or (
                parsed.sigungu
                and inst.sigungu
                and parsed.sigungu.endswith("구")
                and inst.sigungu.endswith("구")
                and parsed.sigungu != inst.sigungu
                and (len(inst.sigungu) != len(parsed.sigungu) or len(parsed.sigungu) == 2)
            )
        ):
            # A misread swaps a syllable ("해운데구"); a different name ("대전 서구" → 유성구,
            # "부산 동래구" → 동구, both seen on 조달청 data) is another institution, and so is
            # a 공단 parsed as a department ("서울시 강서구시설관리공단"). In a two-syllable 구
            # (동·서·남·북·중구) the one syllable that differs is the whole name.
            return Resolution(None, parsed.department, 0.0, "none", found.candidates)
        return found

    def _fuzzy(self, compact: str, department: str | None, threshold: float) -> Resolution:
        # Compare at jamo level so one-jamo OCR slips (대→데) cost little. Strip the department
        # first: it is not part of the institution's identity.
        if department:
            compact = compact.replace(department, "")
        scored = process.extract(
            to_jamo(compact), list(self._jamo_index), scorer=fuzz.ratio, limit=12
        )
        best_by_code: dict[str, float] = {}
        for alias_jamo, score, _ in scored:
            code = self._jamo_index[alias_jamo]
            best_by_code[code] = max(best_by_code.get(code, 0.0), float(score))
        ranked = sorted(best_by_code.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked:
            return Resolution(None, department, 0.0, "none")
        best_code, best_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        if best_score >= threshold and best_score - runner_up >= 4:
            return Resolution(
                self._by_code[best_code], department, best_score / 100, "fuzzy", ranked[:3]
            )
        return Resolution(None, department, 0.0, "none", ranked[:3])


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", normalize(text))


compact_name = _compact


def region_matches(region_code: str, wanted: str) -> bool:
    """A profile's region ("41", or a 시군구 "41110") against an institution's region code. A
    시도 code that went into another (광주 29, 전남 46 → 전남광주 12) matches on both sides, so
    saved profiles and institutions from before 2026-07-01 still meet."""

    def sido(code: str) -> str:
        return SIDO_SUCCESSORS.get(code[:2], code[:2])

    if len(wanted) <= 2:
        return sido(region_code) == sido(wanted)
    return region_code == wanted


def looks_like_local_government(raw: str) -> bool:
    """A 시도, 시군구 or council by the shape of the name. One the table does not know is a gap in
    the table, to be seen in the review queue, not a new institution."""
    if _is_own_body(normalize(raw)):
        return False
    parsed = parse_name(raw)
    return parsed.sigungu is not None or parsed.sido_only or parsed.is_council


# Not a buyer: 조달청's 제3자단가계약 (framework contracts any institution can order from) name
# their 수요기관 "각 수요기관", under a placeholder code (ZZ99999). One institution made of them
# gathered 141 unrelated contracts in 30 days of live data (2026-09-26), and similarity linking
# merged them into one opportunity.
_NO_INSTITUTION = frozenset({"각수요기관"})


def names_no_institution(raw: str) -> bool:
    return _compact(raw) in _NO_INSTITUTION


def provider_institution(code: str, name: str) -> Institution:
    """An institution known only from a provider record: 조달청's 수요기관코드 and name. Kind and
    시도 are read off the name for display and the region filter, and left blank when the name
    does not say ("한국농어촌공사"). "대구대학교" reads as 대구 though it sits in 경산: good enough
    for an 8% ranking feature, and never used for identity."""
    name = normalize(name).strip()
    head = name.split()[0] if name.split() else name
    kind: InstitutionKind
    if name.endswith(("교육청", "교육지원청")):
        kind = "education_office"
    elif head.endswith(("부", "처", "청")) and "교육" not in head and not head.endswith("본부"):
        kind = "central"  # 조달청, 국토교통부 …, 국회사무처; not "경기도소방재난본부"
    else:
        kind = "public_agency"  # 학교, 병원, 대학교, 공사·공단, 연구원
    m = _SIDO_RE.match(name)
    sido = _SIDO_LOOKUP[m.group(0)] if m else ""
    return Institution(
        code=PROVIDER_CODE_PREFIX + code,
        name=name,
        kind=kind,
        sido=sido,
        sigungu=None,
        region_code=SIDO_REGION_CODES.get(sido, ""),
    )


def load_registry_csv(path: str | None = None) -> InstitutionRegistry:
    """Load the institution table: every 시도·시군구, their councils and the 시도 교육청, built by
    ``scripts/build_institutions.py`` from 행정안전부 법정동코드. Everything else (schools,
    hospitals, 공사·공단, 국가기관) arrives with a provider code; see :func:`provider_institution`."""
    if path is None:
        ref = resources.files("app.domain").joinpath("data/institutions.csv")
        text = ref.read_text(encoding="utf-8")
    else:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    rows = csv.DictReader(text.splitlines())
    institutions = [
        Institution(
            code=row["code"],
            name=row["name"],
            kind=row["kind"],  # type: ignore[arg-type]
            sido=row["sido"],
            sigungu=row["sigungu"] or None,
            region_code=row["region_code"],
            executive_code=row["executive_code"] or None,
            aliases=tuple(a for a in row["aliases"].split("|") if a),
        )
        for row in rows
    ]
    return InstitutionRegistry(institutions)
