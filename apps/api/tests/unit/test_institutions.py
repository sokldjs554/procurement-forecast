import json
from pathlib import Path

import pytest

from app.domain.institutions import (
    SIDO_REGION_CODES,
    Institution,
    InstitutionRegistry,
    load_registry_csv,
    looks_like_local_government,
    parse_name,
    provider_institution,
    region_matches,
)

SNAPSHOT = Path(__file__).resolve().parents[2] / "scripts/data/stan_regin_cd_2026-09-27.json"


@pytest.fixture(scope="module")
def registry():  # type: ignore[no-untyped-def]
    return load_registry_csv()


@pytest.mark.parametrize(
    ("raw", "code", "dept"),
    [
        ("서울특별시 강남구청 스마트도시과", "LG-11680", "스마트도시과"),
        ("강남구청", "LG-11680", None),
        ("서울 강남구", "LG-11680", None),
        ("서울특별시강남구청", "LG-11680", None),
        ("강남구의회", "CN-11680", None),
        ("수원특례시 정보통신과", "LG-41110", "정보통신과"),
        ("제주특별자치도 관광정책과", "LG-50000", "관광정책과"),
        ("세종시청", "LG-36110", None),
        ("부산진구청", "LG-26230", None),
        ("해운대구 스마트도시과", "LG-26350", "스마트도시과"),
    ],
)
def test_resolves_spelling_variants(registry, raw: str, code: str, dept: str | None) -> None:  # type: ignore[no-untyped-def]
    res = registry.resolve(raw)
    assert res.institution is not None, res
    assert res.institution.code == code
    assert res.department == dept


def test_ambiguous_sigungu_is_not_guessed(registry) -> None:  # type: ignore[no-untyped-def]
    res = registry.resolve("중구청 기획예산과")
    assert res.institution is None
    assert res.method == "ambiguous"
    assert len(res.candidates) == 6


def test_context_hint_disambiguates(registry) -> None:  # type: ignore[no-untyped-def]
    res = registry.resolve("중구청 기획예산과", sido_hint="대전")
    assert res.institution is not None
    assert res.institution.code == "LG-30140"
    assert res.method == "context"


def test_council_demand_is_attributed_to_executive(registry) -> None:  # type: ignore[no-untyped-def]
    res = registry.resolve("성남시의회")
    assert res.institution is not None
    assert res.institution.kind == "council"
    assert res.institution.demand_owner_code == "LG-41130"


def test_code_hint_short_circuits(registry) -> None:  # type: ignore[no-untyped-def]
    res = registry.resolve("아무 이름", code_hint="LG-47110")
    assert res.institution is not None
    assert res.method == "code"


def test_fuzzy_catches_ocr_typo(registry) -> None:  # type: ignore[no-untyped-def]
    res = registry.resolve("해운데구청")  # 대 -> 데 (OCR)
    assert res.institution is not None
    assert res.institution.code == "LG-26350"
    assert res.method == "fuzzy"


def test_parse_name_splits_department() -> None:
    parsed = parse_name("경기도 성남시청 (도시정보센터)")
    assert parsed.sido == "경기도"
    assert parsed.sigungu == "성남시"
    assert parsed.department == "도시정보센터"


# 수요기관 names from 30 days of 조달청 data (2026-09-26). Each of these used to resolve to the
# 시도 or 구 in the left column; none of them is that institution.
@pytest.mark.parametrize(
    "raw",
    [
        "서울교통공사",  # was 서울특별시: the 시도 name anywhere in the text was enough
        "분당서울대학교병원",
        "부산대학교 산학협력단",
        "국토교통부 부산지방국토관리청 포항국토관리사무소",
        "한국농어촌공사 제주지역본부",
        "경기주택도시공사",
        "경기도 신성중학교",
        "서울시 강서구시설관리공단",
        # Bodies of their own named after a 시도 or 시군구 (live shapes, 2026-09-26).
        "충청남도 청양의료원",
        "재단법인 보은군 문화관광재단",
        "경상북도 영덕군 수산업협동조합",
        "진주시 시설관리공단",
        "재단법인 부산광역시 중구 문화재단",
    ],
)
def test_names_that_only_carry_a_place_name_stay_unresolved(registry, raw: str) -> None:  # type: ignore[no-untyped-def]
    # By name, that is: at ingest their 조달청 code makes them institutions of their own.
    assert registry.resolve(raw).institution is None


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("경기도 광주시", "LG-41610"),  # was 경기도: 광주시 read as 광주광역시 and skipped
        ("대전광역시 서구", "LG-30170"),  # was 유성구 (fuzzy), then unresolved
        ("부산광역시 동래구", "LG-26260"),  # was 동구 (fuzzy), then unresolved
        ("인천광역시 부평구", "LG-28237"),  # was 중구 (fuzzy), then unresolved
    ],
)
def test_live_names_the_demo_table_missed_now_resolve_exactly(
    registry, raw: str, code: str
) -> None:  # type: ignore[no-untyped-def]
    res = registry.resolve(raw)
    assert res.institution is not None, res
    assert (res.institution.code, res.method) == (code, "exact")


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("서울특별시", "LG-11000"),
        ("서울특별시 물재생센터 중랑물재생센터", "LG-11000"),
        ("서울특별시 영등포소방서", "LG-11000"),
        ("경기도 경기도건설본부", "LG-41000"),
        ("경기도청 북부청사", "LG-41000"),
        ("제주특별자치도 상하수도본부", "LG-50000"),
        ("경상북도 포항시 맑은물사업소", "LG-47110"),
        ("한국도로공사 서울경기본부", "PA-EXKR"),
    ],
)
def test_live_names_of_the_institution_itself_resolve(registry, raw: str, code: str) -> None:  # type: ignore[no-untyped-def]
    res = registry.resolve(raw)
    assert res.institution is not None, res
    assert res.institution.code == code


def test_gyeonggi_gwangju_is_a_city_of_gyeonggi() -> None:
    parsed = parse_name("경기도 광주시")
    assert (parsed.sido, parsed.sigungu) == ("경기도", "광주시")
    assert parse_name("광주광역시 동구").sido == "광주광역시"


# --- The full table (scripts/build_institutions.py) -------------------------------------------


def _snapshot() -> tuple[dict[str, str], dict[str, str]]:
    """(시도 region code → name, 시군구 region code → full name) in force, from the 법정동코드
    snapshot the table is built from. 세종 (36110) is a 시도; 행정구 ("경기도 수원시 장안구") are
    left out."""
    rows = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["rows"]
    top = [r for r in rows if r["locathigh_cd"] == "0000000000"]
    sido = {r["region_cd"][:5]: r["locatadd_nm"] for r in top}
    sigungu = {
        r["region_cd"][:5]: r["locatadd_nm"]
        for r in rows
        if r not in top and len(r["locatadd_nm"].split()) == 2
    }
    return sido, sigungu


def test_table_has_every_local_government_in_force(registry) -> None:  # type: ignore[no-untyped-def]
    sido, sigungu = _snapshot()
    insts = [i for i in registry.all() if i.kind != "public_agency"]
    in_force = [i for i in insts if i.region_code in sido or i.region_code in sigungu]
    sido_level = [i for i in in_force if i.kind == "local_gov" and i.sigungu is None]
    local = [i for i in in_force if i.kind == "local_gov" and i.sigungu]
    councils = [i for i in in_force if i.kind == "council"]
    # 17 시도 less 광주광역시 and 전라남도, plus 전남광주통합특별시 (2026-07-01).
    assert len(sido) == len(sido_level) == 16
    assert len(local) == 227 + 2  # + 제주시·서귀포시, 행정시 without a council
    assert len(councils) == 16 + 227
    offices = {i.region_code[:2] for i in insts if i.kind == "education_office"}
    assert {c[:2] for c in sido} <= offices
    for i in [*sido_level, *local]:
        assert i.name == {**sido, **sigungu}[i.region_code], i
        assert i.region_code[:2] == SIDO_REGION_CODES[i.sido], i
    assert {i.region_code for i in local} == set(sigungu)
    assert {i.sido for i in insts} == set(SIDO_REGION_CODES)
    for c in councils:
        executive = registry.get(c.executive_code)
        assert executive is not None, c
        assert (executive.kind, executive.sido, executive.sigungu, executive.region_code) == (
            "local_gov",
            c.sido,
            c.sigungu,
            c.region_code,
        )


def test_abolished_governments_stay_for_older_documents(registry) -> None:  # type: ignore[no-untyped-def]
    sido, sigungu = _snapshot()
    abolished = {"LG-29000", "LG-46000", "LG-28110", "LG-28140", "LG-28260"}
    for code in abolished | {"CN" + c[2:] for c in abolished}:
        inst = registry.get(code)
        assert inst is not None, code
        assert inst.region_code not in sido and inst.region_code not in sigungu, inst
    assert registry.get("EO-29000") is not None and registry.get("EO-46000") is not None
    # Only these rows name a region code that is no longer in force.
    stale = {
        i.code
        for i in registry.all()
        if i.kind in ("local_gov", "council")
        and i.region_code not in sido
        and i.region_code not in sigungu
    }
    assert stale == abolished | {"CN" + c[2:] for c in abolished}


def test_a_government_under_a_new_code_keeps_its_institution(registry) -> None:  # type: ignore[no-untyped-def]
    # 2026-07-01: 전라남도 순천시 46150 → 전남광주통합특별시 순천시 12150, 광주 동구 29110 → 12210.
    # The government goes on, so does its code and its old name.
    for code, name, region, old in [
        ("LG-46150", "전남광주통합특별시 순천시", "12150", "전라남도 순천시"),
        ("CN-46150", "전남광주통합특별시 순천시의회", "12150", "전라남도 순천시의회"),
        ("LG-29110", "전남광주통합특별시 동구", "12210", "광주광역시 동구"),
    ]:
        inst = registry.get(code)
        assert (inst.name, inst.region_code) == (name, region)
        assert old in inst.aliases
    # New bodies get their own official codes, and a council each. 인천 서구 split into 서해구 and
    # 검단구, both under new codes (조달청's too): no old code goes on.
    for code in ("12000", "28125", "28155", "28275", "28290"):
        assert registry.get(f"LG-{code}").region_code == code
        assert registry.get(f"CN-{code}").executive_code == f"LG-{code}"
    assert registry.get("EO-12000").name == "전남광주통합특별시교육청"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("강원특별자치도 춘천시", "LG-51110"),
        ("강원도 춘천시청 정보통신과", "LG-51110"),  # the old name still in older documents
        ("전북특별자치도 전주시", "LG-52110"),
        ("전라북도 전주시의회", "CN-52110"),
        ("대구광역시 군위군", "LG-27720"),  # 경상북도 until 2023-07
        ("군위군청", "LG-27720"),
        ("고양특례시 스마트도시과", "LG-41280"),
        ("창원특례시의회", "CN-48120"),
        ("서울특별시의회", "CN-11000"),
        ("충청남도교육청", "EO-44000"),
        ("제주특별자치도 제주시", "LG-50110"),
        ("부산광역시 기장군", "LG-26710"),
    ],
)
def test_resolves_local_governments_beyond_the_demo(registry, raw: str, code: str) -> None:  # type: ignore[no-untyped-def]
    res = registry.resolve(raw)
    assert res.institution is not None, res
    assert res.institution.code == code


def test_same_name_in_two_provinces_is_not_guessed(registry) -> None:  # type: ignore[no-untyped-def]
    res = registry.resolve("고성군청")
    assert res.method == "ambiguous"
    assert {c for c, _ in res.candidates} == {"LG-51820", "LG-48820"}
    assert registry.resolve("고성군청", sido_hint="경남").institution.code == "LG-48820"


# --- Institutions known by 조달청's code --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind", "sido", "region"),
    [
        ("서울특별시중부교육청 선린중학교", "public_agency", "서울특별시", "11"),
        ("충북대학교병원", "public_agency", "충청북도", "43"),
        ("한국농어촌공사 제주지역본부", "public_agency", "", ""),
        ("조달청", "central", "", ""),
        ("국토교통부 부산지방국토관리청", "central", "", ""),
        ("경기도화성오산교육지원청", "education_office", "경기도", "41"),
        ("경기도소방재난본부", "public_agency", "경기도", "41"),  # 본부 ends in 부, no 부처
    ],
)
def test_provider_institution_reads_kind_and_sido_off_the_name(
    name: str, kind: str, sido: str, region: str
) -> None:
    inst = provider_institution("B550590", name)
    assert inst.code == "G2B-B550590"
    assert (inst.kind, inst.sido, inst.region_code) == (kind, sido, region)
    assert inst.sigungu is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("인천광역시 제물포구", True),  # a 구 the table lacks would be a gap, not a new body
        ("서울특별시 강남구의회", True),
        ("경기도", True),
        ("충북대학교병원", False),
        ("서울특별시중부교육청 선린중학교", False),
        ("경기도 신성중학교", False),
        # Not councils or 군 at all; each comes with a 조달청 code and becomes its own institution.
        ("사단법인 통영시관광협의회", False),
        ("사회복지법인 전라남도사회복지협의회", False),
        ("해군 정비창", False),
        ("국방부 국군조직 육군 제5군수지원사령부", False),
        ("사단법인 한국국제구호기구", False),
        ("충청남도 청양의료원", False),
        ("재단법인 보은군 문화관광재단", False),
    ],
)
def test_local_government_shape(raw: str, expected: bool) -> None:
    assert looks_like_local_government(raw) is expected


def test_provider_code_finds_the_institution_it_registered() -> None:
    registry = load_registry_csv()
    registry.add(provider_institution("7060012", "서울특별시중부교육청 선린중학교"))

    by_code = registry.resolve("선린중학교 행정실", provider_code="7060012")
    assert (by_code.institution.code, by_code.method) == ("G2B-7060012", "code")
    by_name = registry.resolve("서울특별시중부교육청 선린중학교")
    assert by_name.institution.code == "G2B-7060012"
    # Known by code only: a near spelling is not fuzzy-matched to it.
    assert registry.resolve("서울특별시중부교육청 선린중학고").institution is None


def test_provider_name_never_takes_over_a_table_name() -> None:
    registry = load_registry_csv()
    registry.add(provider_institution("9999999", "서울특별시 강남구"))
    assert registry.resolve("서울특별시 강남구").institution.code == "LG-11680"


def test_county_health_center_hospital_is_the_county() -> None:
    # A 군's 보건의료원 is its 보건소; a 지방의료원 is a body of its own.
    registry = load_registry_csv()
    assert registry.resolve("충청남도 청양군 보건의료원").institution.code == "LG-44790"
    assert registry.resolve("충청남도 청양의료원").institution is None


def test_a_local_government_is_not_taken_by_a_body_that_filed_under_its_code() -> None:
    # Live shape (2026-09-26): a city's 재단 filed a 발주계획 under the city's own 조달청 code
    # first; the city's 공고 with that code must still go to the city.
    registry = load_registry_csv()
    registry.add(provider_institution("3999100", "(재)오산시문화재단"))
    res = registry.resolve("경기도 오산시", provider_code="3999100")
    assert res.institution.code == "LG-41370"
    assert registry.resolve("(재)오산시문화재단", provider_code="3999100").method == "code"


def test_two_codes_with_one_name_keep_their_own_institutions() -> None:
    registry = load_registry_csv()
    registry.add(provider_institution("Z099001", "테스트대학교 산학협력단"))
    other = registry.resolve("테스트대학교 산학협력단", provider_code="ZT09002")
    assert other.institution is None  # left for its own code to register
    # Without a code of its own (사전규격), the name still finds the first one.
    assert registry.resolve("테스트대학교 산학협력단").institution.code == "G2B-Z099001"


def test_a_two_syllable_gu_is_never_a_typo_for_another() -> None:
    # At the default threshold these never come close (대전 서구 vs 동구 scores 79), so check
    # the rule itself at a looser one: one syllable apart is a different 구 when it is the
    # whole name. The OCR-typo case for longer names is test_fuzzy_catches_ocr_typo.
    registry = InstitutionRegistry(
        [Institution("LG-30110", "대전광역시 동구", "local_gov", "대전광역시", "동구", "30110")]
    )
    assert registry.resolve("대전광역시 서구청", fuzzy_threshold=75).institution is None
    assert registry.resolve("대전광역시 동구청", fuzzy_threshold=75).institution is not None


# --- 2026-07-01 reorganisations (행정안전부 법정동코드, pulled 2026-09-27) ---------------------


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        # Names as 조달청 sent them after 2026-07-01 (30 days to 2026-09-26).
        ("전남광주통합특별시 순천시", "LG-46150"),
        ("전남광주통합특별시 해남군", "LG-46820"),
        ("전남광주통합특별시 광산구", "LG-29200"),
        ("전남광주통합특별시 동구의회", "CN-29110"),
        ("전남광주통합특별시", "LG-12000"),
        ("전남광주통합특별시의회", "CN-12000"),
        ("전남광주통합특별시교육청", "EO-12000"),
        ("인천광역시 서해구", "LG-28275"),
        ("인천광역시 검단구", "LG-28290"),
        ("인천광역시 제물포구", "LG-28125"),
        ("인천광역시 영종구", "LG-28155"),
        ("인천광역시 영종구의회", "CN-28155"),
        # Older documents: the same governments under the names they had.
        ("전라남도 순천시", "LG-46150"),
        ("전라남도 순천시 스마트도시과", "LG-46150"),
        ("순천시청", "LG-46150"),
        ("전라남도 해남군의회", "CN-46820"),
        ("광주광역시 동구청", "LG-29110"),
        ("광주 동구", "LG-29110"),
        ("광주광역시 광산구의회", "CN-29200"),
        # ... and the ones that are gone, which wrote them.
        ("인천광역시 서구", "LG-28260"),
        ("인천광역시 서구의회", "CN-28260"),
        ("전라남도", "LG-46000"),
        ("광주광역시의회", "CN-29000"),
        ("전라남도교육청", "EO-46000"),
        ("인천광역시 중구", "LG-28110"),
        ("인천광역시 동구", "LG-28140"),
    ],
)
def test_resolves_the_2026_reorganisations(registry, raw: str, code: str) -> None:  # type: ignore[no-untyped-def]
    res = registry.resolve(raw)
    assert res.institution is not None, res
    assert res.institution.code == code


def test_a_hint_with_a_former_sido_still_narrows(registry) -> None:  # type: ignore[no-untyped-def]
    assert registry.resolve("동구청").method == "ambiguous"
    assert registry.resolve("동구청", sido_hint="광주").institution.code == "LG-29110"
    assert registry.resolve("동구청", sido_hint="전남광주통합특별시").institution.code == "LG-29110"
    assert registry.resolve("동구청", sido_hint="인천").institution.code == "LG-28140"


def test_the_integrated_sido_is_not_read_as_jeonnam() -> None:
    # 44 institutions named "전남광주통합특별시…" were filed under 전라남도 before (2026-09-26).
    inst = provider_institution("B551234", "전남광주통합특별시북구시설관리공단")
    assert (inst.sido, inst.region_code) == ("전남광주통합특별시", "12")
    assert provider_institution("B551235", "전라남도 도로관리사업소").sido == "전라남도"


@pytest.mark.parametrize(
    ("region", "wanted", "expected"),
    [
        ("12150", "12", True),
        ("12150", "46", True),  # a profile saved before 2026-07-01
        ("46000", "12", True),  # 전라남도's own older documents
        ("29", "12", True),  # a 조달청 institution named under 광주광역시
        ("29110", "46", True),  # 광주 and 전남 are one 시도 now
        ("11680", "12", False),
        ("41110", "41", True),
        ("41110", "41110", True),
        ("41130", "41110", False),
    ],
)
def test_region_filter_follows_merged_sido(region: str, wanted: str, expected: bool) -> None:
    assert region_matches(region, wanted) is expected
