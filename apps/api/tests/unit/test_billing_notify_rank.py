import base64
import json
from datetime import UTC, date, datetime

import httpx
import pytest

from app.billing.service import add_month
from app.billing.toss import PaymentDeclinedError, PaymentUnavailableError, TossPaymentsClient
from app.db.models import CompanyProfile, Opportunity
from app.demo.synth import build_world, josa, spoken_krw
from app.notify.channels import PermanentDeliveryError, SlackChannel, TransientDeliveryError
from app.notify.dispatch import _quiet_until
from app.notify.render import render_email, render_kakao_variables, render_slack
from app.pipeline.link import budget_names_agree, terms_conflict, title_similarity
from app.pipeline.process import canonical_title
from app.pipeline.recommend import score_opportunity
from app.pipeline.triage import triage_chunk

PAYLOAD = {
    "org_name": "데모",
    "headline": "9월 25일, 새로 찾은 사업 1건",
    "settings_url": "https://app.example/app/alerts",
    "items": [
        {
            "opportunity_id": 1,
            "title": "스마트쉘터 설치",
            "institution": "서울특별시 강남구",
            "stage": "budget_line",
            "stage_label": "예산 편성",
            "budget": "3억 5,000만원",
            "when": "입찰 예상 2026년 7~12월",
            "score_pct": 82,
            "evidence": "<b>반영</b>하겠습니다",
            "url": "https://app.example/app/opportunities/1",
        },
    ],
}


# --- Toss ---------------------------------------------------------------------------------------
async def test_toss_billing_charge_sends_idempotency_key_and_basic_auth() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["idem"] = request.headers.get("Idempotency-Key")
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "paymentKey": "pk_1",
                "orderId": "o-1",
                "status": "DONE",
                "totalAmount": 99000,
                "approvedAt": "2026-09-25T10:00:00+09:00",
            },
        )

    client = TossPaymentsClient("test_sk_abc", transport=httpx.MockTransport(handler))
    result = await client.charge(
        billing_key="bk_1",
        customer_key="cus_1",
        amount=99000,
        order_id="o-1",
        order_name="발주 예측 Pro",
        idempotency_key="o-1",
    )
    assert result.status == "DONE" and result.amount == 99000
    assert seen["path"] == "/v1/billing/bk_1"
    assert seen["idem"] == "o-1"
    assert seen["auth"] == "Basic " + base64.b64encode(b"test_sk_abc:").decode()
    assert seen["body"] == {
        "customerKey": "cus_1",
        "amount": 99000,
        "orderId": "o-1",
        "orderName": "발주 예측 Pro",
    }


async def test_toss_decline_and_outage_are_distinguished() -> None:
    declined = TossPaymentsClient(
        "sk",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                400, json={"code": "REJECT_CARD_PAYMENT", "message": "한도초과"}
            )
        ),
    )
    with pytest.raises(PaymentDeclinedError) as exc:
        await declined.charge(
            billing_key="b",
            customer_key="c",
            amount=1,
            order_id="o",
            order_name="n",
            idempotency_key="o",
        )
    assert exc.value.code == "REJECT_CARD_PAYMENT"
    down = TossPaymentsClient(
        "sk", transport=httpx.MockTransport(lambda r: httpx.Response(502, json={}))
    )
    with pytest.raises(PaymentUnavailableError):
        await down.charge(
            billing_key="b",
            customer_key="c",
            amount=1,
            order_id="o",
            order_name="n",
            idempotency_key="o",
        )


def test_add_month_clamps_to_month_end() -> None:
    assert add_month(datetime(2026, 1, 31, tzinfo=UTC)).date() == date(2026, 2, 28)
    assert add_month(datetime(2026, 12, 15, tzinfo=UTC)).date() == date(2027, 1, 15)


# --- notifications ------------------------------------------------------------------------------
def test_email_render_escapes_html_and_has_text_part() -> None:
    email = render_email(PAYLOAD)
    assert "&lt;b&gt;반영&lt;/b&gt;" in email.html
    assert "<b>반영</b>" in email.text  # plain text is not HTML-escaped
    assert email.subject.startswith("[발주 예측]")
    assert "3억 5,000만원 · 입찰 예상 2026년 7~12월 · 적합도 82점" in email.text


def test_digests_queued_before_the_when_field_still_render() -> None:
    item = {k: v for k, v in PAYLOAD["items"][0].items() if k != "when"}  # type: ignore[union-attr]
    legacy = {**PAYLOAD, "items": [{**item, "window": "2026.07~2026.12"}]}
    assert "입찰 예상 2026.07~2026.12" in render_email(legacy).text
    assert "입찰 예상 2026.07~2026.12" in render_slack(legacy)["blocks"][1]["text"]["text"]


def test_old_window_labels_are_reworded_not_prefixed() -> None:
    from app.notify.render import when

    assert when({"window": "공고됨(2026.06.01)"}) == "2026.06.01 입찰공고"
    assert when({"window": "미정"}) == "입찰 시기 미정"
    assert when({"window": "2026.07~2026.12"}) == "입찰 예상 2026.07~2026.12"


def test_every_channel_counts_the_whole_digest() -> None:
    items = [{**PAYLOAD["items"][0], "opportunity_id": i} for i in range(12)]  # type: ignore[dict-item]
    digest = {**PAYLOAD, "items": items, "more": 5, "feed_url": "https://app.example/app"}
    texts = [b["text"]["text"] for b in render_slack(digest)["blocks"] if b["type"] == "section"]
    assert texts[-1] == "<https://app.example/app|나머지 7건도 보기>"  # 2 cut by Slack + 5 left out
    assert render_kakao_variables(digest)["#{건수}"] == "17"


def test_test_notifications_say_what_they_are() -> None:
    note = {**PAYLOAD, "headline": "테스트 알림이에요", "items": []}
    assert "설정은 끝났어요" in render_email(note).text
    assert "설정은 끝났어요" in render_slack(note)["blocks"][1]["text"]["text"]


def test_alert_timing_reads_as_a_date_or_a_forecast() -> None:
    from app.notify.dispatch import _when_label

    def when(start: date | None = None, end: date | None = None, **kw: object) -> str:
        opp = Opportunity(
            **{"bid_published_at": None, "bid_window_start": start, "bid_window_end": end, **kw}
        )
        return _when_label(opp, date(2026, 6, 15))

    assert when(bid_published_at=date(2026, 6, 1)) == "2026.06.01 입찰공고"
    assert when(date(2026, 7, 1), date(2026, 12, 31)) == "입찰 예상 2026년 7~12월"
    assert when(date(2026, 11, 1), date(2027, 2, 28)) == "입찰 예상 2026년 11월~2027년 2월"
    assert when(date(2026, 7, 1), date(2026, 7, 31)) == "입찰 예상 2026년 7월"
    assert when() == "입찰 시기 미정"
    # a window that has opened is what is left of it; one that has closed says so
    assert when(date(2026, 1, 1), date(2026, 11, 30)) == "입찰 예상 2026년 6~11월"
    assert when(date(2026, 1, 1), date(2026, 3, 31)) == "예상 시기(2026년 1~3월) 지남, 아직 공고 전"


def test_slack_and_kakao_render() -> None:
    slack = render_slack(PAYLOAD)
    assert slack["blocks"][1]["text"]["text"].startswith("*<https://app.example")
    kakao = render_kakao_variables(PAYLOAD)
    assert kakao["#{기관}"] == "서울특별시 강남구" and kakao["#{건수}"] == "1"


async def test_slack_revoked_webhook_is_permanent_and_429_transient() -> None:
    revoked = SlackChannel(httpx.MockTransport(lambda r: httpx.Response(404, text="no_service")))
    with pytest.raises(PermanentDeliveryError):
        await revoked.send("https://hooks.slack.com/services/T/B/X", PAYLOAD)
    limited = SlackChannel(httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(TransientDeliveryError):
        await limited.send("https://hooks.slack.com/services/T/B/X", PAYLOAD)
    with pytest.raises(PermanentDeliveryError):
        await limited.send("https://evil.example/hook", PAYLOAD)


def test_quiet_hours_in_kst() -> None:
    # 23:30 KST (14:30 UTC) inside 22→8 quiet hours → resume 08:00 KST = 23:00 UTC.
    resume = _quiet_until(datetime(2026, 9, 25, 14, 30, tzinfo=UTC), 22, 8)
    assert resume == datetime(2026, 9, 25, 23, 0, tzinfo=UTC)
    assert _quiet_until(datetime(2026, 9, 25, 3, 0, tzinfo=UTC), 22, 8) is None  # 12:00 KST


# --- linking & ranking --------------------------------------------------------------------------
def test_canonical_title_strips_notice_noise() -> None:
    assert (
        canonical_title("[긴급] 2027년 스마트쉘터 구축사업 (협상에 의한 계약)")
        == "스마트쉘터 구축사업"
    )


def test_title_similarity_sees_through_synonyms() -> None:
    assert (
        title_similarity("어린이보호구역 지능형 CCTV 설치", "스쿨존 AI 안전카메라 설치사업") > 0.5
    )
    assert title_similarity("스마트쉘터 설치", "스마트 버스정류장 조성사업") > 0.5
    assert title_similarity("스마트쉘터 설치", "공공도서관 리모델링") < 0.2
    assert terms_conflict("스마트쉘터 설치", "스마트폴 설치")
    assert not terms_conflict("수요응답형 교통(DRT) 도입", "DRT 시범운영 용역")


def test_budget_rows_keep_their_names_across_books() -> None:
    # Spacing, or a word added, is the same 세부사업 in next year's book…
    assert budget_names_agree("수내교 전면개축 공사", "수내교 전면 개축공사")
    assert budget_names_agree("여수동 복합문화시설 조성", "여수동 공공부지 복합문화시설 조성")
    # …a word replaced is another one, however much of the name is shared.
    assert not budget_names_agree("중원청소년수련관 시설개선", "수정청소년수련관 시설 개선")
    assert not budget_names_agree("제1공영주차장 조성", "제2공영주차장 조성")
    assert not budget_names_agree("수내교 전면개축 공사", "수내교 전면개축 실시설계")
    # Same field and amount, nothing else in common (§9.6 of real-data-minutes.md).
    assert not budget_names_agree("산성공원 관리", "국가암관리")


def _opp(**kw: object) -> Opportunity:
    base = dict(
        id=1,
        title="스마트쉘터 설치",
        keywords=["스마트쉘터"],
        category="smart_city",
        stage="budget_line",
        status="open",
        est_budget_krw=350_000_000,
        conversion_prob=0.8,
        bid_window_start=date(2027, 1, 15),
        bid_window_end=date(2027, 6, 30),
        bid_published_at=None,
        embedding=None,
        institution_code="LG-11680",
    )
    base.update(kw)
    return Opportunity(**base)


def _profile(**kw: object) -> CompanyProfile:
    base = dict(
        org_id=1,
        keywords=["스마트쉘터"],
        exclude_keywords=[],
        categories=["smart_city"],
        region_codes=[],
        budget_min=100_000_000,
        budget_max=1_000_000_000,
        embedding=None,
    )
    base.update(kw)
    return CompanyProfile(**base)


def test_ranker_prefers_actionable_lead_time() -> None:
    today = date(2026, 9, 25)
    early = score_opportunity(_opp(), _profile(), "11680", today)
    closing = score_opportunity(
        _opp(status="bid_open", bid_published_at=date(2026, 9, 20)), _profile(), "11680", today
    )
    assert early.score > closing.score
    assert early.breakdown["keyword_hits"] == ["스마트쉘터"]


def test_ranker_does_not_call_a_missed_window_imminent() -> None:
    today = date(2026, 9, 25)
    inside = score_opportunity(
        _opp(bid_window_start=date(2026, 1, 15), bid_window_end=date(2026, 11, 30)),
        _profile(),
        "11680",
        today,
    )
    missed = score_opportunity(
        _opp(bid_window_start=date(2026, 1, 15), bid_window_end=date(2026, 3, 31)),
        _profile(),
        "11680",
        today,
    )
    assert inside.breakdown["features"]["lead_time"] == 0.75
    assert missed.breakdown["features"]["lead_time"] == 0.35
    assert missed.score < inside.score


def test_ranker_excluded_keywords_bury_result() -> None:
    today = date(2026, 9, 25)
    normal = score_opportunity(_opp(), _profile(), None, today)
    excluded = score_opportunity(_opp(), _profile(exclude_keywords=["쉘터"]), None, today)
    assert excluded.score < normal.score * 0.3


def test_ranker_region_filter() -> None:
    today = date(2026, 9, 25)
    inside = score_opportunity(_opp(), _profile(region_codes=["11"]), "11680", today)
    outside = score_opportunity(_opp(), _profile(region_codes=["26"]), "11680", today)
    assert inside.breakdown["features"]["region"] == 1.0
    assert outside.breakdown["features"]["region"] == 0.0


# --- triage & synthetic world ---------------------------------------------------------------------
def test_triage_separates_procedure_from_commitment() -> None:
    commit = "○위원 박  스마트쉘터 계획은?\n○과장 이  내년도 본예산에 3억 5천만원을 반영하겠습니다."
    chatter = "○위원 김  민원실 대기 시간이 길다는 말씀이 있습니다.\n○과장 최  점검하고 있습니다. 감사합니다."
    assert triage_chunk(commit, kind="exchange", threshold=0.35).passed
    assert not triage_chunk(chatter, kind="exchange", threshold=0.35).passed
    assert not triage_chunk(
        "의석을 정돈하여 주시기 바랍니다", kind="procedure", threshold=0.0
    ).passed


def test_synthetic_world_is_deterministic() -> None:
    a = build_world(anchor=date(2026, 9, 25), seed=3, scale=0.3, render_pdfs=False)
    b = build_world(anchor=date(2026, 9, 25), seed=3, scale=0.3, render_pdfs=False)
    assert a.summary() == b.summary()
    assert [g.quote for g in a.gold] == [g.quote for g in b.gold]


def test_korean_surface_helpers() -> None:
    assert spoken_krw(352_000_000) == "3억 5천만원"
    assert spoken_krw(80_000_000) == "8천만원"
    assert josa("스마트폴", "을/를") == "스마트폴을" and josa("CCTV", "은/는") == "CCTV는"
