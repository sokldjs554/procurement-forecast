"""Request/response DTOs. The frontend's TypeScript types are generated from these via OpenAPI
(``pnpm gen:api``), so a renamed field is a compile error in the web app, not a runtime bug."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.taxonomy import Category

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth ------------------------------------------------------------------------------------
class SignupIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=100)
    company_name: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("이메일 주소를 다시 확인해 주세요")
        return v


class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(_Out):
    id: int
    email: str
    name: str
    role: str
    is_staff: bool


class OrgOut(_Out):
    id: int
    name: str
    plan: str
    credit_balance: int


class MeOut(BaseModel):
    user: UserOut
    org: OrgOut
    extractor_mode: str


# --- profile & reference ------------------------------------------------------------------
class ProfileIO(_Out):
    description: str = ""
    keywords: list[str] = Field(default_factory=list, max_length=30)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=30)
    categories: list[str] = Field(default_factory=list)
    region_codes: list[str] = Field(default_factory=list, max_length=20)
    budget_min: int | None = Field(default=None, ge=0)
    budget_max: int | None = Field(default=None, ge=0)


class InstitutionOut(_Out):
    code: str
    name: str
    kind: str
    sido: str
    sigungu: str | None
    region_code: str


class CategoryOut(BaseModel):
    key: str
    label: str


# --- opportunities ---------------------------------------------------------------------------
class InstitutionRef(BaseModel):
    code: str | None
    name: str


class OpportunityCard(BaseModel):
    id: int
    title: str
    institution: InstitutionRef
    department: str | None
    category: str
    category_label: str
    stage: str
    stage_label: str
    status: str
    est_budget_krw: int | None
    bid_window_start: date | None
    bid_window_end: date | None
    # the forecast window's end has passed with no tender yet; the window is then shown as-is
    window_passed: bool = False
    bid_published_at: date | None
    tender_out: bool = False  # 입찰공고 is out (by date or stage): nothing left to estimate
    conversion_prob: float
    signal_count: int
    first_seen_at: date
    last_signal_at: date
    score: float | None
    reasons: list[str]
    feedback: str | None
    lead_days: int | None
    head_start_days: int | None = None  # first public signal → 입찰공고 (actual or forecast)


class FeedPage(BaseModel):
    items: list[OpportunityCard]
    next_cursor: str | None
    total: int
    stage_counts: dict[str, int] = {}  # same filters, per stage, ignoring the stage filter


class EvidenceOut(BaseModel):
    quote: str
    found: bool
    score: float
    start: int | None
    end: int | None
    method: str


class DocumentRef(BaseModel):
    id: int
    title: str
    doc_type: str
    url: str | None
    publisher_raw: str | None
    parse_method: str | None
    published_at: date


class LinkOut(BaseModel):
    score: float
    method: str
    tentative: bool
    reasons: dict[str, Any]


class SignalOut(BaseModel):
    id: int
    stage: str
    stage_label: str
    observed_at: date
    title: str
    summary: str
    category: str
    department: str | None
    budget_krw: int | None
    expected_year: int | None
    expected_half: str | None
    commitment: str | None
    confidence: float
    verdict: str
    extractor: str
    evidence: list[EvidenceOut]
    context: str | None
    context_offset: int | None
    document: DocumentRef
    link: LinkOut | None


class BriefOut(_Out):
    id: int
    content_md: str
    model: str
    credits_spent: int
    created_at: datetime


class BudgetPoint(BaseModel):
    observed_at: date
    amount: int
    stage: str
    stage_label: str


class OpportunityDetail(OpportunityCard):
    keywords: list[str]
    best_commitment: str | None
    signals: list[SignalOut]
    budget_trajectory: list[BudgetPoint]
    breakdown: dict[str, Any] | None
    briefs: list[BriefOut]


class FeedbackIn(BaseModel):
    feedback: Literal["relevant", "irrelevant", "won", "dismissed"] | None


# --- alerts ------------------------------------------------------------------------------------
class AlertRuleIO(_Out):
    mode: Literal["instant", "daily", "weekly"] = "daily"
    min_score: float = Field(default=0.55, ge=0, le=1)
    stages: list[str] = Field(default_factory=list)
    quiet_start: int = Field(default=22, ge=0, le=23)
    quiet_end: int = Field(default=8, ge=0, le=23)


class AlertChannelIn(BaseModel):
    kind: Literal["email", "slack", "kakao"]
    target: str = Field(min_length=3, max_length=500)
    label: str = Field(default="", max_length=100)


class AlertChannelOut(_Out):
    id: int
    kind: str
    target: str
    label: str
    enabled: bool
    last_error: str | None


class NotificationOut(_Out):
    id: int
    kind: str
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None
    channel_id: int
    headline: str


# --- billing -----------------------------------------------------------------------------------
class PlanOut(BaseModel):
    key: str
    name: str
    monthly_price_krw: int
    monthly_credits: int
    max_regions: int | None
    channels: list[str]
    instant_alerts: bool


class CreditPackOut(BaseModel):
    key: str
    credits: int
    price_krw: int


class LedgerOut(_Out):
    id: int
    delta: int
    balance_after: int
    reason: str
    ref_type: str | None
    ref_id: str | None
    created_at: datetime


class PaymentOut(_Out):
    id: int
    order_id: str
    kind: str
    order_name: str
    amount: int
    status: str
    failure_message: str | None
    created_at: datetime
    paid_at: datetime | None


class SubscriptionOut(_Out):
    plan: str
    status: str
    card_summary: str | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    next_charge_at: datetime | None
    canceled_at: datetime | None
    failed_attempts: int


class BillingOut(BaseModel):
    subscription: SubscriptionOut
    credit_balance: int
    brief_cost: int
    plans: list[PlanOut]
    credit_packs: list[CreditPackOut]
    ledger: list[LedgerOut]
    payments: list[PaymentOut]
    payment_provider: str
    toss_client_key: str | None
    customer_key: str


class CardIn(BaseModel):
    auth_key: str = Field(min_length=1, max_length=300)
    customer_key: str = Field(min_length=1, max_length=64)


class PlanChangeIn(BaseModel):
    plan: Literal["free", "pro", "team"]


class CreditPurchaseIn(BaseModel):
    pack: str


# --- admin -------------------------------------------------------------------------------------
class SourceOut(BaseModel):
    key: str
    name: str
    adapter: str
    enabled: bool
    last_run_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    circuit: dict[str, Any]
    documents: int
    last_run: dict[str, Any] | None


class JobRunOut(_Out):
    id: int
    job: str
    job_id: str
    attempt: int
    status: str
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    result: dict[str, Any]


class ReviewItemOut(BaseModel):
    id: int
    status: str
    reasons: list[str]
    created_at: datetime
    signal: SignalOut
    institution_name: str | None


class ReviewDecisionIn(BaseModel):
    action: Literal["approve", "reject", "edit"]
    title: str | None = None
    budget_krw: int | None = None
    expected_year: int | None = None
    commitment: Literal["committed", "planned", "reviewing", "declined"] | None = None
    institution_code: str | None = None
    category: Category | None = None


class LLMUsageRow(BaseModel):
    day: date
    task: str
    model: str
    status: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float


class LLMUsageOut(BaseModel):
    rows: list[LLMUsageRow]
    spent_today_usd: float
    daily_budget_usd: float
    cache_hit_rate: float | None
    degraded_rate: float | None
    extractor_mode: str


class EvalRunOut(_Out):
    id: int
    kind: str
    label: str
    metrics: dict[str, Any]
    params: dict[str, Any]
    git_sha: str | None
    created_at: datetime


class FunnelOut(BaseModel):
    documents: dict[str, int]
    chunks: int
    chunks_triaged: int
    signals: dict[str, int]
    opportunities: dict[str, int]
    review_open: int


class OverviewOut(BaseModel):
    funnel: FunnelOut
    jobs_last_24h: dict[str, int]
    failed_jobs: int
    llm_spent_today_usd: float
    llm_daily_budget_usd: float
    queue_depth: int | None
    sources_unhealthy: int


class DocumentDetailOut(BaseModel):
    id: int
    title: str
    doc_type: str
    parse_method: str | None
    text_quality: float | None
    text: str | None
    chunks: list[dict[str, Any]]
    structured: dict[str, Any]
