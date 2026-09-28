"""LLM layer: schema, Anthropic request shape, fallback chain — all without network."""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from app.llm.budget import MemorySpendGuard
from app.llm.prompts import EXTRACT_SYSTEM, ChunkContext, extract_user_message
from app.llm.providers.anthropic_provider import AnthropicProvider
from app.llm.providers.heuristic import HeuristicProvider, _answer_text, _budget_list_signals
from app.llm.schemas import ExtractionOutput, strict_json_schema
from app.llm.service import LLMService, cache_key
from app.llm.types import LLMConfigError, LLMRefusedError, LLMUnavailableError, Usage

CTX = ChunkContext(
    doc_type="council_minutes",
    title="제301회 임시회",
    institution="서울특별시 강남구의회",
    document_date=date(2025, 11, 20),
    labels=["위원 박지훈", "스마트도시과장 이정민"],
    text=(
        "○위원 박지훈  버스정류장에 냉난방이 되는 스마트쉘터를 더 늘릴 계획이 있습니까?\n"
        "○스마트도시과장 이정민  내년도 본예산에 스마트쉘터 7개소 추가 설치 사업비 3억 5천만원을 "
        "반영하겠습니다."
    ),
)

GOOD_OUTPUT = {
    "signals": [
        {
            "title": "스마트쉘터 설치",
            "summary": "강남구가 스마트쉘터 7개소 추가 설치 예정",
            "category": "smart_city",
            "institution_mention": "강남구",
            "department": "스마트도시과",
            "budget_text": "3억 5천만원",
            "budget_krw": 350000000,
            "timing_text": "내년도 본예산에",
            "expected_year": 2026,
            "expected_half": None,
            "commitment": "committed",
            "procurement_type": "goods",
            "keywords": ["스마트쉘터"],
            "evidence": [
                "내년도 본예산에 스마트쉘터 7개소 추가 설치 사업비 3억 5천만원을 반영하겠습니다."
            ],
            "confidence": 0.9,
        }
    ]
}


def _response(payload: dict[str, Any] | str, stop_reason: str = "end_turn") -> Any:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop_reason,
        stop_details=None,
        model="claude-opus-5",
        usage=SimpleNamespace(
            input_tokens=420,
            output_tokens=180,
            cache_read_input_tokens=1400,
            cache_creation_input_tokens=0,
        ),
        _request_id="req_test",
    )


class FakeMessages:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _provider(outcomes: list[Any]) -> tuple[AnthropicProvider, FakeMessages]:
    messages = FakeMessages(outcomes)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    provider = AnthropicProvider(
        api_key="test",
        extract_model="claude-opus-5",
        extract_effort="low",
        brief_model="claude-opus-5",
        brief_effort="medium",
        client=client,  # type: ignore[arg-type]
    )
    return provider, messages


class FakeSession:
    """Just enough AsyncSession for LLMService (cache miss path + call recording)."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.executed = 0

    async def get(self, *_: Any) -> None:
        return None

    async def execute(self, *_: Any) -> None:
        self.executed += 1

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def test_strict_schema_has_no_unsupported_keywords() -> None:
    schema = json.dumps(strict_json_schema(ExtractionOutput))
    for keyword in ('"minimum"', '"maximum"', '"minLength"', '"maxItems"'):
        assert keyword not in schema
    s = strict_json_schema(ExtractionOutput)
    signal = s["$defs"]["ExtractedSignal"]
    assert signal["additionalProperties"] is False
    assert set(signal["required"]) == set(signal["properties"])
    assert "title" in signal["properties"]  # a *property* called title survives


def test_system_prompt_is_frozen_and_cacheable() -> None:
    # No per-request values in the cached prefix, and long enough to cache (≥512 tokens).
    assert "{" not in EXTRACT_SYSTEM.replace('{"signals": []}', "")
    assert len(EXTRACT_SYSTEM) > 2500
    assert "2025-11-20" in extract_user_message(CTX)


async def test_anthropic_request_shape_and_parse() -> None:
    provider, messages = _provider([_response(GOOD_OUTPUT)])
    result = await provider.extract(CTX)
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in call  # adaptive by default on Opus 5; effort is the lever
    assert result.value.signals[0].budget_krw == 350_000_000
    assert result.usage.cache_read_tokens == 1400
    assert result.served_by == "claude-opus-5"


async def test_effort_is_omitted_for_models_without_the_knob() -> None:
    messages = FakeMessages([_response(GOOD_OUTPUT)])
    provider = AnthropicProvider(
        api_key="test",
        extract_model="claude-haiku-4-5",
        extract_effort=None,
        brief_model="claude-haiku-4-5",
        brief_effort=None,
        server_side_fallback=False,
        client=SimpleNamespace(beta=SimpleNamespace(messages=messages)),  # type: ignore[arg-type]
    )
    await provider.extract(CTX)
    call = messages.calls[0]
    assert "effort" not in call["output_config"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "fallbacks" not in call and "betas" not in call


async def test_missing_credentials_are_a_config_error_not_a_crash() -> None:
    # What the SDK raises at request time when no key, token or profile was found.
    provider, _ = _provider([TypeError("Could not resolve authentication method.")])
    with pytest.raises(LLMConfigError):
        await provider.extract(CTX)


def test_cache_key_separates_efforts() -> None:
    message = extract_user_message(CTX)
    assert cache_key("claude-opus-5", "low", message) != cache_key(
        "claude-opus-5", "medium", message
    )


async def test_refusal_is_typed() -> None:
    provider, _ = _provider([_response({"signals": []}, stop_reason="refusal")])
    with pytest.raises(LLMRefusedError):
        await provider.extract(CTX)


async def test_rate_limit_maps_to_unavailable() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.RateLimitError(
        "slow down", response=httpx.Response(429, request=request), body=None
    )
    provider, _ = _provider([error])
    with pytest.raises(LLMUnavailableError):
        await provider.extract(CTX)


async def test_service_records_cost_and_uses_primary() -> None:
    provider, _ = _provider([_response(GOOD_OUTPUT)])
    guard = MemorySpendGuard(10)
    service = LLMService(primary=provider, fallback=HeuristicProvider(), guard=guard)
    session = FakeSession()
    attempt = await service.extract(session, CTX)  # type: ignore[arg-type]
    assert not attempt.degraded
    assert attempt.extractor.startswith("anthropic:claude-opus-5")
    assert await guard.spent_today() > 0
    assert session.added[0].status == "ok"


async def test_service_degrades_when_budget_exhausted() -> None:
    provider, messages = _provider([_response(GOOD_OUTPUT)])
    service = LLMService(primary=provider, fallback=HeuristicProvider(), guard=MemorySpendGuard(0))
    session = FakeSession()
    attempt = await service.extract(session, CTX)  # type: ignore[arg-type]
    assert attempt.degraded and attempt.reason == "budget_exceeded"
    assert not messages.calls  # the model was never called
    assert attempt.output.signals[0].title  # heuristic still produced the signal
    assert session.added[0].status == "budget_skip"


async def test_service_propagates_transient_errors_until_final_attempt() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    overloaded = anthropic.InternalServerError(
        "overloaded", response=httpx.Response(529, request=request), body=None
    )
    provider, _ = _provider([overloaded, overloaded])
    service = LLMService(primary=provider, fallback=HeuristicProvider(), guard=MemorySpendGuard(10))
    with pytest.raises(LLMUnavailableError):
        await service.extract(FakeSession(), CTX, final_attempt=False)  # type: ignore[arg-type]
    attempt = await service.extract(FakeSession(), CTX, final_attempt=True)  # type: ignore[arg-type]
    assert attempt.degraded and attempt.reason == "provider_unavailable"


async def test_heuristic_extracts_council_commitment() -> None:
    result = await HeuristicProvider().extract(CTX)
    [signal] = result.value.signals
    assert signal.commitment == "committed"
    assert signal.budget_krw == 350_000_000
    assert signal.expected_year == 2026
    assert signal.category.value == "smart_city"
    assert "스마트쉘터" in signal.title


async def test_heuristic_skips_operating_cost_budget_lines() -> None:
    ctx = ChunkContext(
        "budget_book",
        "예산서",
        "강남구",
        date(2025, 12, 18),
        ["부서: 총무과"],
        "세부사업: 업무추진비  51,555  48,977  2,578",
        fiscal_year=2026,
    )
    assert (await HeuristicProvider().extract(ctx)).value.signals == []


def test_usage_cost_accounts_for_cache() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000)
    assert usage.cost_usd("claude-opus-5") == Decimal("5.5")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # circumstance qualifiers are not part of the project name
        (
            "취지는 공감하나 현재 재정 여건상 스마트쉘터 설치는 당분간 추진하기 어렵습니다.",
            "스마트쉘터 설치",
        ),
        # a spoken relative clause is kept whole rather than cut mid-clause
        (
            "AI가 이상행동을 먼저 잡아주는 CCTV는 필요성은 공감합니다만, 효과성 검증이 필요해서 적극 검토하겠습니다.",
            "AI가 이상행동을 먼저 잡아주는 CCTV",
        ),
    ],
)
def test_heuristic_titles_read_like_project_names(answer: str, expected: str) -> None:
    from app.llm.providers.heuristic import _guess_title

    assert _guess_title(answer, "") == expected


def _brief_facts(stage: str, last_commitment: str, **kw: Any) -> Any:
    from app.domain.stages import Stage
    from app.llm.prompts import BriefFacts, BriefSignal, PastTender

    signal = BriefSignal(
        date(2025, 11, 20),
        Stage.COUNCIL,
        "스마트쉘터 설치",
        350_000_000,
        "committed",
        "내년도 본예산에 반영하겠습니다.",
    )
    fields: dict[str, Any] = {
        "today": date(2026, 3, 10),
        "title": "스마트쉘터 설치",
        "institution": "서울특별시 강남구",
        "department": "교통행정과",
        "stage": Stage(stage),
        "status": "open",
        "est_budget_krw": 350_000_000,
        "window_start": date(2026, 9, 1),
        "window_end": date(2026, 11, 30),
        "bid_published_at": None,
        "best_commitment": "committed",
        "conversion_prob": 0.72,
        "signals": (
            signal,
            BriefSignal(
                date(2026, 3, 2), Stage.COUNCIL, "스마트쉘터 설치", None, last_commitment, "…"
            ),
        ),
        "history": (PastTender(date(2025, 6, 1), "스마트폴 구축", 400_000_000),),
    }
    return BriefFacts(**(fields | kw))


def test_template_brief_advice_follows_the_stage_reached() -> None:
    from app.pipeline.brief import template_brief

    prespec = template_brief(_brief_facts("prespec", "committed"))
    assert "의견등록 기간" in prespec
    assert "예산에 편성되기 전" not in prespec

    council = template_brief(_brief_facts("council_mention", "reviewing"))
    assert "예산에 편성되기 전" in council
    assert "가장 최근 발언이 확약은 아니었어요" in council


def test_template_brief_reads_like_a_person_wrote_it() -> None:
    from app.pipeline.brief import template_brief

    brief = template_brief(_brief_facts("budget_line", "committed"))
    summary = brief.split("\n")[1]
    assert summary.startswith("서울특별시 강남구 교통행정과의 「스마트쉘터 설치」 건이에요.")
    assert "입찰은 2026년 9~11월쯤 나올 것으로 보고 있어요." in summary
    assert "예산은 3억 5,000만원으로 잡혀 있어요." in summary
    # the timeline speaks in dates people use, says what the council answered, and keeps the quote
    assert (
        "- **2025.11.20 · 의회 발언** — 스마트쉘터 설치 (3억 5,000만원). '반영하겠다'고 답했어요."
        in brief
    )
    assert "  > 「내년도 본예산에 반영하겠습니다.」" in brief
    assert "- 2025.06.01 · 스마트폴 구축 (4억원)" in brief
    assert "2026-09-01" not in brief  # no raw ISO dates leak into the prose

    assert "공고로 이어질 가능성: 72% 정도로 봐요" in brief


def test_once_the_tender_is_out_nothing_is_estimated() -> None:
    from app.pipeline.brief import template_brief

    # what link.py stores for a published tender: the date is the window, conversion is 1.0
    out = date(2026, 6, 1)
    facts = _brief_facts(
        "bid_notice",
        "committed",
        bid_published_at=out,
        window_start=out,
        window_end=out,
        conversion_prob=1.0,
    )
    brief, prompt = template_brief(facts), facts.as_prompt()
    assert "입찰공고는 2026.06.01에 나왔어요." in brief
    assert "공고로 이어질 가능성" not in brief and "100%" not in brief
    assert "- 입찰공고일: 2026-06-01" in prompt
    assert "공고 전환 확률" not in prompt and "입찰 예상 시기" not in prompt
    # the stage alone is enough (an award without a stored notice date): both paths agree
    award = _brief_facts("award", "committed", conversion_prob=1.0)
    assert award.tender_out
    award_brief = template_brief(award)
    assert "입찰공고는 이미 나왔어요." in award_brief and "입찰 예상" not in award_brief
    assert "계약까지 끝난 사업이에요" in award_brief and "제안요청서" not in award_brief
    assert "- 입찰공고일: 날짜 미상" in award.as_prompt()


def test_the_tender_out_predicate_is_shared() -> None:
    from app.domain.stages import Stage, tender_is_out

    assert tender_is_out(Stage.BID, None) and tender_is_out("award", None)
    assert tender_is_out("council_mention", date(2026, 6, 1))
    assert not tender_is_out(Stage.PRESPEC, None)


def test_template_brief_quotes_speech_not_table_rows() -> None:
    from app.domain.stages import Stage
    from app.llm.prompts import BriefSignal
    from app.pipeline.brief import template_brief

    row = BriefSignal(
        date(2025, 12, 18),
        Stage.BUDGET,
        "스마트폴 설치",
        411_000_000,
        "committed",
        "세부사업: 스마트폴 설치  411,000  0  411,000",
    )
    base = _brief_facts("budget_line", "committed")
    brief = template_brief(_brief_facts("budget_line", "committed", signals=(*base.signals, row)))
    assert "- **2025.12.18 · 예산 편성** — 스마트폴 설치 (4억 1,100만원)" in brief
    assert "411,000  0" not in brief
    assert "  > 「내년도 본예산에 반영하겠습니다.」" in brief


def test_template_brief_does_not_forecast_a_window_that_has_passed() -> None:
    from app.pipeline.brief import template_brief

    facts = _brief_facts("budget_line", "committed", today=date(2027, 1, 5), window_passed=True)
    stale = template_brief(facts)
    assert "예상했던 입찰 시기(2026년 9~11월)가 지났는데 아직 공고는 안 나왔어요." in stale
    assert "나올 것으로 보고 있어요" not in stale
    assert "2026-11-30 (이 기간이 지났지만 아직 입찰공고 없음)" in facts.as_prompt()


def test_a_quote_that_spans_lines_keeps_its_signal() -> None:
    from app.domain.stages import Stage
    from app.llm.prompts import BriefSignal
    from app.pipeline.brief import template_brief

    weak = BriefSignal(
        date(2026, 3, 2),
        Stage.COUNCIL,
        "스마트쉘터 설치",
        None,
        "reviewing",
        "적극 검토하겠습니다.\n다만 예산이",
    )
    facts = _brief_facts("council_mention", "committed", signals=(weak,))
    brief = template_brief(facts)
    assert "- **2026.03.02 · 의회 발언** — 스마트쉘터 설치. '검토하겠다'는 정도였어요." in brief
    assert "  > 「적극 검토하겠습니다. 다만 예산이」" in brief
    assert "가장 최근 발언이 확약은 아니었어요" in brief
    prompt = facts.as_prompt()
    assert prompt.startswith("기준일: 2026-03-10")
    assert (
        "- 2026-03-02 [의회 발언] 스마트쉘터 설치, 검토 중: 「적극 검토하겠습니다. 다만 예산이」"
        in prompt
    )


# Rows from 성남시's 세출예산사업명세서 (2026 본예산, 2026 제1회 추경), as the chunker hands them over.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "시설물 정비공사 4,096,440 2,974,472 1,121,968\n"
            "401 시설비및부대비 4,096,440 2,974,472 1,121,968\n"
            "01 시설비 4,095,440 2,973,472 1,121,968\n"
            " ○희망대공원 산책계단 및 휴게공간 정비공",
            ("시설물 정비공사", 4_096_440_000, 2026),
        ),
        # cut to nothing in the 추경: no plan left to procure
        ("미니태양광 보급지원사업 0 73,080 △73,080\n401 시설비및부대비 0 73,080 △73,080", None),
        # a 부서's running costs, in every book
        ("기본경비 71,720 40,590 31,130\n201 일반운영비 27,150 23,550 3,600", None),
    ],
)
async def test_heuristic_reads_real_budget_table_rows(
    text: str, expected: tuple[str, int, int] | None
) -> None:
    ctx = ChunkContext(
        "budget_book",
        "예산서",
        "경기도 성남시",
        date(2026, 6, 18),
        ["부서: 공원과"],
        text,
        fiscal_year=2026,
    )
    signals = (await HeuristicProvider().extract(ctx)).value.signals
    if expected is None:
        assert signals == []
    else:
        assert [(s.title, s.budget_krw, s.expected_year) for s in signals] == [expected]


def test_heuristic_reads_a_glued_member_line_as_the_question() -> None:
    # 성남시의회 prints members as "○조우현위원"; that line is the question, not an answer.
    question, answer = _answer_text(
        "○조우현위원  분당구청 전동보장구 충전시설 이게 예산에 잡혀 있나요?\n"
        "○분당구청장 정상철  설치되어 있는 걸 제외하고 이번에 다 하는 겁니다."
    )
    assert question == "분당구청 전동보장구 충전시설 이게 예산에 잡혀 있나요?"
    assert answer == "설치되어 있는 걸 제외하고 이번에 다 하는 겁니다."


# 성남시의회 제309회 본회의 제1차(2026.03.12.): 2026년 제1회 추경 제안 설명, 행정기획조정실장.
BUDGET_BILL = (
    "○행정기획조정실장 전재환  2026년도 제1회 추가경정예산안에 대하여 제안 설명 드리겠습니다.\n"
    "  주요사업비 예산 반영 내역으로는 판교 시스템반도체 연구센터 조성 263억 원, 수정청소년수련관 "
    "시설 개선 20억 원, 오리공원 물놀이장 설치 공사비 10억 원, 수내역 광장 재정비 공사비 5억 원, "
    "판교개발부담금 과오납 환급금 147억 원 등을 반영하였습니다.\n"
)


async def test_heuristic_reads_each_item_of_a_budget_bill_list() -> None:
    ctx = ChunkContext(
        doc_type="council_minutes",
        title="제309회 본회의 제1차(2026.03.12.)",
        institution="경기도 성남시의회",
        document_date=date(2026, 3, 12),
        labels=["행정기획조정실장 전재환"],
        text=BUDGET_BILL,
    )
    signals = (await HeuristicProvider().extract(ctx)).value.signals
    got = {s.title: (s.budget_krw, s.expected_year, s.commitment) for s in signals}
    assert got["오리공원 물놀이장 설치 공사"] == (1_000_000_000, 2026, "committed")
    assert got["수내역 광장 재정비 공사"] == (500_000_000, 2026, "committed")
    assert "판교개발부담금 과오납 환급금" not in got  # a refund, not a project
    for s in signals:  # every quote is in the text, so the verifier can ground it
        assert s.evidence[0] in BUDGET_BILL and s.budget_text in s.evidence[0]


def test_budget_list_needs_the_executive_and_the_list_heading() -> None:
    member = "○조우현위원  주요사업비 예산 반영 내역으로는 가 공원 조성 1억 원, 나 도로 정비 2억 원, 다 청사 신축 3억 원\n"
    ctx = ChunkContext("council_minutes", "회의", "성남시의회", date(2026, 3, 12), [], member)
    assert _budget_list_signals(ctx) == []  # a member reading numbers is not the budget
    revenue = "○행정기획조정실장 전재환  지방세 1016억 원, 세외수입 64억 원, 지방교부세 46억 원이 증액됐습니다.\n"
    ctx = ChunkContext("council_minutes", "회의", "성남시의회", date(2026, 3, 12), [], revenue)
    assert _budget_list_signals(ctx) == []


def test_budget_list_keeps_amounts_written_with_thousands_commas() -> None:
    # constructed: the same list shape with "1,050억 원" (성남 writes "1050억 원")
    text = (
        "○행정기획조정실장 전재환  주요사업비 예산 반영 내역으로는 수내교 전면 개축공사 1,050억 원, "
        "박물관 건립 168억 원, 성남시 보훈회관 이전 건립 15억 원 등을 반영하였습니다.\n"
    )
    ctx = ChunkContext("council_minutes", "회의", "성남시의회", date(2025, 11, 20), [], text)
    got = {s.title: s.budget_krw for s in _budget_list_signals(ctx)}
    assert got["수내교 전면 개축공사"] == 105_000_000_000
