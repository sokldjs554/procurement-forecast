"""Query benchmark at production-like volume: ``manage bench``.

The demo world is a few hundred rows, where every query is fast and every plan is a sequential
scan. That hides exactly the problems that appear later: an index the planner can't use, an
approximate vector index that silently returns fewer rows than asked, a JSONB lookup that scans
a whole table. This command makes them visible:

1. creates a throwaway database ``<name>_bench`` and migrates it to the **initial** schema (0001);
2. fills it with generated rows (sizes below — roughly several years of national coverage);
3. runs the application's hot queries under ``EXPLAIN (ANALYZE, BUFFERS)``, in the shape the code
   used before tuning, and measures vector-search recall against an exact scan;
4. migrates to **head** and runs the tuned query shapes;
5. loads explicitly synthetic fixtures for new head-only queries, with no prior timing;
6. writes the comparison to ``docs/performance.md``.

Timings are medians of warm runs on whatever machine runs the command; the report states the
machine. Plans and row counts are the part to read — they don't depend on hardware.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
from sqlalchemy import BigInteger, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.orm import defer
from sqlalchemy.sql import ClauseElement

from app.db.models import (
    Document,
    DocumentChunk,
    LinkReconciliationState,
    Notification,
    Opportunity,
    OpportunityCustomerAnchor,
    OpportunityRelation,
    OpportunityRelationEvent,
    OpportunitySignal,
    Recommendation,
    ReviewItem,
    Signal,
    Source,
)
from app.eval.review_dataset import RESOLVED_STATUSES
from app.pipeline.link_reconcile import reconciliation_scope_ids, reconciliation_signals_query
from app.pipeline.link_state import (
    customer_reprocessing_query,
    link_settled,
    protected_opportunities_query,
)
from app.pipeline.recommend import CANDIDATES_PER_SOURCE, OPEN_ONLY
from app.pipeline.relations import CONTRACT_STAGES, PROJECT_STAGES
from app.settings import get_settings
from app.sources.coverage import coverage_documents_query, latest_source_runs_query

SIZES: dict[str, int] = {
    "institutions": 250,
    "documents": 150_000,
    "chunks": 600_000,
    "opportunities": 100_000,
    "signals": 400_000,
    "organizations": 2_000,  # × 150 recommendations each
    "job_runs": 100_000,
}
RECS_PER_ORG = 150
DIM = 512
NOISE = 0.6  # per-dimension spread around a topic center (centers are uniform in ±0.5)

_TITLES = (
    "스마트쉘터 설치",
    "지능형 CCTV 선별관제 고도화",
    "스마트폴 구축",
    "디지털트윈 플랫폼 구축",
    "공영주차장 주차안내 시스템",
    "수요응답형 교통 도입",
    "도시침수 예경보 시스템",
    "독거어르신 AI 돌봄서비스",
    "민원 상담 AI 챗봇",
    "공공청사 태양광 설치",
    "전기차 급속충전기 설치",
    "누리집 통합 재구축",
    "클라우드 전환 사업",
    "스쿨존 안전카메라 설치",
    "메이커스페이스 조성",
    "공공도서관 리모델링",
    "야간관광 미디어파사드",
    "상권분석 플랫폼 구축",
    "LED 가로등 교체",
    "하천 수위 원격감시",
)
_KEYWORDS = (
    "스마트쉘터",
    "CCTV",
    "스마트폴",
    "디지털트윈",
    "주차안내",
    "DRT",
    "침수",
    "돌봄",
    "챗봇",
    "태양광",
    "충전기",
    "누리집",
    "클라우드",
    "안전카메라",
    "메이커스페이스",
    "리모델링",
    "미디어파사드",
    "상권분석",
    "가로등",
    "수위",
)
_CATEGORIES = (
    "smart_city",
    "safety_cctv",
    "mobility",
    "energy_env",
    "welfare_care",
    "education",
    "tourism_culture",
    "facility",
    "public_sw",
    "other",
)
_STAGES = ("council_mention", "budget_line", "order_plan", "prespec", "bid_notice")


def _arr(values: tuple[str, ...]) -> str:
    return "(ARRAY[" + ",".join("'" + v.replace("'", "''") + "'" for v in values) + "])"


# ------------------------------------------------------------------------------------------------
# The queries. "before" is the shape the code used when the initial schema shipped; "after" is the
# tuned shape (same results, or — for vector search — the results the code meant to get).
# ------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class Query:
    key: str
    title: str
    before: str | None
    after: str
    params: list[Any] = field(default_factory=list)
    setup_after: tuple[str, ...] = ()
    exact: str | None = None  # exact (index-free) reference for recall, when approximate


def _feed_sorted(order: str) -> str:
    # the feed's other orders: every one of the org's open recommendations is sorted per page,
    # tie-broken by score like the API
    return (
        "SELECT r.opportunity_id, r.score FROM recommendations r "
        "JOIN opportunities o ON o.id = r.opportunity_id WHERE r.org_id = 7 "
        "AND o.status IN ('open', 'bid_open') "
        "AND (r.feedback IS NULL OR r.feedback NOT IN ('dismissed', 'irrelevant')) "
        f"ORDER BY {order}, r.score DESC, o.id DESC LIMIT 21"
    )


def queries(vec: str, inst: str, ref_hit: str, ref_miss: str) -> list[Query]:
    open_ = "status IN ('open', 'bid_open')"
    eligible_candidates = (
        "SELECT o.* FROM opportunities o WHERE o.institution_code = $1 "
        "AND o.signal_count > 0 AND o.last_signal_at >= date '2025-01-01' "
        "AND o.first_seen_at <= date '2026-12-31' "
        "AND EXISTS (SELECT os.signal_id FROM opportunity_signals os "
        "JOIN signals s ON s.id = os.signal_id WHERE os.opportunity_id = o.id "
        "AND NOT os.tentative AND s.verdict = 'accepted' AND s.institution_code = $1) "
        "ORDER BY o.id"
    )
    metadata_columns = ", ".join(
        f"o.{column.name}" for column in Opportunity.__table__.columns if column.name != "embedding"
    )
    eligible_metadata = eligible_candidates.replace("SELECT o.*", f"SELECT {metadata_columns}", 1)
    surviving_embeddings = (
        "SELECT id, embedding FROM opportunities WHERE id = ANY($1::bigint[]) ORDER BY id"
    )
    scoped_reference = (
        "SELECT DISTINCT o.* FROM opportunities o "
        "JOIN opportunity_signals os ON os.opportunity_id = o.id "
        "JOIN signals s ON s.id = os.signal_id "
        "WHERE s.id <> 0 AND s.institution_code = $1 AND o.institution_code = $1 "
        "AND s.verdict = 'accepted' AND NOT os.tentative "
        "AND (s.external_refs @> $2::jsonb OR s.external_refs @> $3::jsonb) ORDER BY o.id"
    )
    eligible_refs = (
        "SELECT os.opportunity_id, s.external_refs FROM opportunity_signals os "
        "JOIN signals s ON s.id = os.signal_id "
        f"WHERE os.opportunity_id IN (SELECT c.id FROM ({eligible_candidates}) c) "
        "AND s.verdict = 'accepted' AND NOT os.tentative"
    )
    revalidation_page = (
        "SELECT s.id FROM signals s JOIN documents d ON d.id = s.document_id "
        "JOIN sources src ON src.id = d.source_id "
        "WHERE s.verdict = 'accepted' AND s.id > $1 "
        "AND d.doc_type IN ('council_minutes', 'budget_book') AND src.key = $2 "
        "ORDER BY s.id LIMIT 1001"
    )
    return [
        Query(
            "link_eligible_candidates",
            "기회 연결: 기관·기간 내 전체 후보 (전: 벡터 포함, 후: 메타데이터만)",
            eligible_candidates,
            eligible_metadata,
            [inst],
            exact=eligible_candidates,
        ),
        Query(
            "link_surviving_embeddings",
            "기회 연결: 구조 필터를 통과한 후보의 벡터 일괄 조회 (4건 예시)",
            surviving_embeddings,
            surviving_embeddings,
            [[11, 22, 33, 44]],
        ),
        Query(
            "link_reference_scoped",
            "기회 연결: 기관·승인·확정 조건의 모든 번호 일치 대상 (모호성 검사)",
            scoped_reference,
            scoped_reference,
            [inst, json.dumps({"order_plan_no": ref_hit}), json.dumps({"prespec_no": ref_miss})],
        ),
        Query(
            "link_eligible_candidate_refs",
            "기회 연결: 전체 적격 후보의 승인된 확정 번호 (계약 충돌 검사)",
            eligible_refs,
            eligible_refs,
            [inst],
        ),
        Query(
            "stored_revalidation_page",
            "저장 신호 재검증: 수집원·문서 유형·승인 조건의 ID 커서 1,001건",
            revalidation_page,
            revalidation_page,
            [100_000, "bench"],
        ),
        Query(
            "backtest_public_dates",
            "백테스트: 승인된 확정 연결의 공개일·첫 신호 조회",
            "SELECT o.*, s.* FROM opportunities o "
            "JOIN opportunity_signals os ON os.opportunity_id=o.id "
            "JOIN signals s ON s.id=os.signal_id ORDER BY o.id, s.observed_at",
            "SELECT o.id, s.id, s.stage, s.observed_at, s.commitment, s.external_refs, "
            "d.published_at, d.structured FROM opportunities o "
            "JOIN opportunity_signals os ON os.opportunity_id=o.id "
            "JOIN signals s ON s.id=os.signal_id JOIN documents d ON d.id=s.document_id "
            "WHERE s.verdict='accepted' AND os.tentative=false "
            "ORDER BY o.id, d.published_at, s.id",
        ),
        Query(
            "recommend_semantic",
            "추천 후보: 회사 소개와 가까운 진행 중 공고 300건 (벡터)",
            f"SELECT id FROM opportunities WHERE {open_} "
            "ORDER BY embedding <=> $1::vector LIMIT 300",
            f"SELECT id FROM opportunities WHERE {open_} "
            "ORDER BY embedding <=> $1::vector LIMIT 300",
            [vec],
            setup_after=("SET LOCAL hnsw.ef_search = 300",),
            exact=f"SELECT id FROM opportunities WHERE {open_} "
            "ORDER BY embedding <=> $1::vector LIMIT 300",
        ),
        Query(
            "recommend_keywords",
            "추천 후보: 관심 키워드가 제목·키워드에 있는 진행 중 공고",
            f"SELECT id FROM opportunities WHERE {open_} AND (title ILIKE '%스마트쉘터%' "
            "OR title ILIKE '%선별관제%' OR '스마트쉘터' = ANY(keywords) "
            "OR '선별관제' = ANY(keywords)) LIMIT 300",
            f"SELECT id FROM opportunities WHERE {open_} AND (title ILIKE '%스마트쉘터%' "
            "OR title ILIKE '%선별관제%' OR keywords @> ARRAY['스마트쉘터'] "
            "OR keywords @> ARRAY['선별관제']) LIMIT 300",
        ),
        Query(
            "link_reference_miss",
            "기회 연결: 처음 보는 발주계획번호로 기존 기회 찾기 (없음)",
            "SELECT os.opportunity_id FROM opportunity_signals os "
            "JOIN signals s ON s.id = os.signal_id WHERE s.external_refs @> $1::jsonb LIMIT 1",
            "SELECT os.opportunity_id FROM opportunity_signals os "
            "JOIN signals s ON s.id = os.signal_id WHERE s.external_refs @> $1::jsonb LIMIT 1",
            [json.dumps({"order_plan_no": ref_miss})],
        ),
        Query(
            "link_reference_hit",
            "기회 연결: 이미 있는 발주계획번호로 기존 기회 찾기",
            "SELECT os.opportunity_id FROM opportunity_signals os "
            "JOIN signals s ON s.id = os.signal_id WHERE s.external_refs @> $1::jsonb LIMIT 1",
            "SELECT os.opportunity_id FROM opportunity_signals os "
            "JOIN signals s ON s.id = os.signal_id WHERE s.external_refs @> $1::jsonb LIMIT 1",
            [json.dumps({"order_plan_no": ref_hit})],
        ),
        Query(
            "link_candidates",
            "기회 연결: 같은 기관·기간의 가까운 기회 12건 (벡터)",
            "SELECT id FROM opportunities WHERE institution_code = $1 "
            "AND last_signal_at >= date '2025-01-01' AND first_seen_at <= date '2026-12-31' "
            "ORDER BY embedding <=> $2::vector LIMIT 12",
            "SELECT id FROM opportunities WHERE institution_code = $1 "
            "AND last_signal_at >= date '2025-01-01' AND first_seen_at <= date '2026-12-31' "
            "ORDER BY embedding <=> $2::vector LIMIT 12",
            [inst, vec],
            exact="SELECT id FROM opportunities WHERE institution_code = $1 "
            "AND last_signal_at >= date '2025-01-01' AND first_seen_at <= date '2026-12-31' "
            "ORDER BY embedding <=> $2::vector LIMIT 12",
        ),
        Query(
            "link_candidate_refs",
            "기회 연결: 후보 기회 12건이 가진 번호 (다른 번호면 후보에서 제외)",
            "SELECT os.opportunity_id, s.external_refs FROM opportunity_signals os "
            "JOIN signals s ON s.id = os.signal_id WHERE os.opportunity_id IN "
            "(11, 22, 33, 44, 55, 66, 77, 88, 99, 110, 121, 132)",
            "SELECT os.opportunity_id, s.external_refs FROM opportunity_signals os "
            "JOIN signals s ON s.id = os.signal_id WHERE os.opportunity_id IN "
            "(11, 22, 33, 44, 55, 66, 77, 88, 99, 110, 121, 132)",
        ),
        Query(
            "link_candidate_same_book",
            "기회 연결: 후보 기회 12건에 같은 예산서의 다른 행이 있는지 (있으면 제외)",
            "SELECT os.opportunity_id FROM opportunity_signals os "
            "JOIN signals s ON s.id = os.signal_id WHERE os.opportunity_id IN "
            "(11, 22, 33, 44, 55, 66, 77, 88, 99, 110, 121, 132) "
            "AND s.document_id = 7 AND s.id <> 1",
            "SELECT os.opportunity_id FROM opportunity_signals os "
            "JOIN signals s ON s.id = os.signal_id WHERE os.opportunity_id IN "
            "(11, 22, 33, 44, 55, 66, 77, 88, 99, 110, 121, 132) "
            "AND s.document_id = 7 AND s.id <> 1",
        ),
        Query(
            "feed_page",
            "고객 피드 첫 페이지 (점수순 21건)",
            "SELECT r.opportunity_id, r.score FROM recommendations r "
            f"JOIN opportunities o ON o.id = r.opportunity_id WHERE r.org_id = 7 AND o.{open_} "
            "AND (r.feedback IS NULL OR r.feedback NOT IN ('dismissed', 'irrelevant')) "
            "ORDER BY r.score DESC, o.id DESC LIMIT 21",
            "SELECT r.opportunity_id, r.score FROM recommendations r "
            f"JOIN opportunities o ON o.id = r.opportunity_id WHERE r.org_id = 7 AND o.{open_} "
            "AND (r.feedback IS NULL OR r.feedback NOT IN ('dismissed', 'irrelevant')) "
            "ORDER BY r.score DESC, o.id DESC LIMIT 21",
        ),
        Query(
            "feed_page_soon",
            "고객 피드 첫 페이지 (입찰이 가까운 순 21건)",
            _feed_sorted(
                "greatest(coalesce(o.bid_window_start, date '9999-12-31'), date '2026-09-25') ASC"
            ),
            _feed_sorted(
                "greatest(coalesce(o.bid_window_start, date '9999-12-31'), date '2026-09-25') ASC"
            ),
        ),
        Query(
            "feed_page_recent",
            "고객 피드 첫 페이지 (새 소식 순 21건)",
            _feed_sorted("o.last_signal_at DESC"),
            _feed_sorted("o.last_signal_at DESC"),
        ),
        Query(
            "feed_stage_counts",
            "고객 피드 단계별 건수 (칩·머리말, GROUP BY)",
            "SELECT o.stage, count(*) FROM recommendations r "
            f"JOIN opportunities o ON o.id = r.opportunity_id WHERE r.org_id = 7 AND o.{open_} "
            "AND (r.feedback IS NULL OR r.feedback NOT IN ('dismissed', 'irrelevant')) "
            "GROUP BY o.stage",
            "SELECT o.stage, count(*) FROM recommendations r "
            f"JOIN opportunities o ON o.id = r.opportunity_id WHERE r.org_id = 7 AND o.{open_} "
            "AND (r.feedback IS NULL OR r.feedback NOT IN ('dismissed', 'irrelevant')) "
            "GROUP BY o.stage",
        ),
        Query(
            "admin_funnel",
            "운영 개요: 파이프라인 퍼널 집계 (5개 COUNT)",
            "SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM document_chunks), "
            "(SELECT count(*) FROM document_chunks WHERE triage_passed), "
            "(SELECT count(*) FROM signals), (SELECT count(*) FROM opportunities)",
            "SELECT (SELECT count(*) FROM documents), c.total, c.triaged, "
            "(SELECT count(*) FROM signals), (SELECT count(*) FROM opportunities) "
            "FROM (SELECT count(*) AS total, count(*) FILTER (WHERE triage_passed) AS triaged "
            "FROM document_chunks) c",
        ),
        Query(
            "pending_sweep",
            "배치: 처리 대기 문서 200건 (10분마다)",
            "SELECT id FROM documents WHERE parse_status = 'pending' ORDER BY id LIMIT 200",
            "SELECT id FROM documents WHERE parse_status = 'pending' ORDER BY id LIMIT 200",
        ),
    ]


# ------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class Result:
    ms: float
    rows: int
    plan: list[str]
    recall: float | None = None


def _flatten(node: dict[str, Any], out: list[str]) -> list[str]:
    label = node["Node Type"]
    if "Index Name" in node:
        label += f" ({node['Index Name']})"
    elif "Relation Name" in node and node["Node Type"] == "Seq Scan":
        label += f" ({node['Relation Name']})"
    out.append(label)
    for child in node.get("Plans", []):
        _flatten(child, out)
    return out


async def _explain(
    conn: asyncpg.Connection, sql: str, params: list[Any], setup: tuple[str, ...]
) -> tuple[float, int, list[str]]:
    async with conn.transaction():
        for stmt in setup:
            await conn.execute(stmt)
        raw = await conn.fetchval(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", *params)
    doc = json.loads(raw)[0]
    plan = doc["Plan"]
    return float(doc["Execution Time"]), int(plan["Actual Rows"]), _flatten(plan, [])


async def _ids(
    conn: asyncpg.Connection, sql: str, params: list[Any], setup: tuple[str, ...]
) -> set[int]:
    async with conn.transaction():
        for stmt in setup:
            await conn.execute(stmt)
        return {r[0] for r in await conn.fetch(sql, *params)}


async def run_query(
    conn: asyncpg.Connection, q: Query, phase: str, repeats: int = 5
) -> Result | None:
    sql = q.before if phase == "before" else q.after
    if sql is None:
        return None
    setup = () if phase == "before" else q.setup_after
    times: list[float] = []
    rows = 0
    plan: list[str] = []
    for _ in range(repeats):
        ms, rows, plan = await _explain(conn, sql, q.params, setup)
        times.append(ms)
    recall = None
    if q.exact:
        exact_setup = ("SET LOCAL enable_indexscan = off", "SET LOCAL enable_bitmapscan = off")
        truth = await _ids(conn, q.exact, q.params, exact_setup)
        got = await _ids(conn, sql, q.params, setup)
        recall = len(truth & got) / len(truth) if truth else 1.0
    return Result(statistics.median(times[1:] or times), rows, plan, recall)


def additional_queries(
    sizes: dict[str, int] | None = None, *, vec: str | None = None
) -> list[Query]:
    """Head-only application queries; initial-schema timings do not exist.

    Compile ORM projections so document/embedding payload choices match the application.
    All literals below are fixed benchmark probes, never user-controlled SQL.
    """

    def query(key: str, title: str, stmt: ClauseElement) -> Query:
        # This third-party dialect constructor has no type annotations. Named parameters
        # also avoid DBAPI percent escaping in the SQL passed directly to asyncpg.
        pg = PGDialect(paramstyle="named")  # type: ignore[no-untyped-call]
        sql = str(stmt.compile(dialect=pg, compile_kwargs={"literal_binds": True}))
        return Query(key, title, None, sql)

    sizes = sizes or SIZES
    # Group 1 has one member per opportunities-sized block. Keep the source-location
    # probe faithful to scaled benchmark runs; institution queries remain unbounded.
    summary_ids = list(range(1, sizes["signals"] + 1, sizes["opportunities"]))
    document_signal_ids = list(range(1, sizes["signals"] + 1, sizes["documents"]))
    unprotected_stride = 4 * sizes["institutions"]
    state = LinkReconciliationState
    recommendation_base = select(Opportunity).where(OPEN_ONLY, link_settled())
    vector = json.loads(vec) if vec is not None else [1.0, *([0.0] * (DIM - 1))]
    settled_semantic = query(
        "recommend_semantic_settled",
        "추천 후보: 미정합 기관 제외 + 벡터 최근접 300건 (전체 기회 투영)",
        recommendation_base.order_by(Opportunity.embedding.cosine_distance(vector)).limit(
            CANDIDATES_PER_SOURCE
        ),
    )
    settled_semantic.setup_after = (f"SET LOCAL hnsw.ef_search = {CANDIDATES_PER_SOURCE}",)
    settled_semantic.exact = settled_semantic.after
    endpoint = select(OpportunityRelation).where(
        OpportunityRelation.id > 0,
        or_(OpportunityRelation.project_id == 1, OpportunityRelation.contract_id == 1),
    )
    result = [
        settled_semantic,
        query(
            "recommend_keywords_settled",
            "추천 후보: 미정합 기관 제외 + 제목·키워드 300건 (전체 기회 투영)",
            recommendation_base.where(
                or_(
                    Opportunity.title.ilike("%스마트쉘터%"),
                    Opportunity.keywords.contains(["스마트쉘터"]),
                )
            ).limit(CANDIDATES_PER_SOURCE),
        ),
        query(
            "recommend_category_settled",
            "추천 후보: 미정합 기관 제외 + 분야 300건 (전체 기회 투영)",
            recommendation_base.where(Opportunity.category.in_(["smart_city"])).limit(
                CANDIDATES_PER_SOURCE
            ),
        ),
        query(
            "notify_candidates_settled",
            "알림 후보: 회사 7·점수 0.5 이상·미정합 기관 제외 (추천·기회 투영)",
            select(Recommendation, Opportunity)
            .join(Opportunity, Opportunity.id == Recommendation.opportunity_id)
            .where(
                Recommendation.org_id == 7,
                Recommendation.score >= 0.5,
                or_(Recommendation.feedback.is_(None), Recommendation.feedback == "relevant"),
                Opportunity.status.in_(("open", "bid_open")),
                link_settled(),
            )
            .order_by(Recommendation.score.desc()),
        ),
        query(
            "coverage_documents",
            "수집 현황: 전체 문서의 좁은 메타데이터 투영",
            coverage_documents_query(),
        ),
        query(
            "coverage_latest_runs", "수집 현황: 수집원별 최신 실행 기록", latest_source_runs_query()
        ),
        query(
            "reconciliation_institution_signals",
            "자동 연결 재정합: 기관 전체 신호·연결·벡터·원문 위치",
            reconciliation_signals_query("B-0001"),
        ),
        query(
            "reconciliation_protected_opportunities",
            "자동 연결 재정합: 기관 범위의 사람 결정·고객 이력 전체 보호 조회",
            protected_opportunities_query({"B-0001"}),
        ),
        query(
            "reconciliation_strict_opportunities",
            "자동 연결 재정합: 엄격 보호 전체 조회 (고객 원본 근거 유효성 포함)",
            protected_opportunities_query({"B-0001"}, include_customer=False),
        ),
        query(
            "reconciliation_customer_anchor_scope",
            "자동 연결 재정합: 기관 전체 고객 원본 근거 ID·생성 시각",
            select(OpportunityCustomerAnchor).where(
                OpportunityCustomerAnchor.opportunity_id.in_(reconciliation_scope_ids("B-0001"))
            ),
        ),
        query(
            "reconciliation_dirty_sweep",
            "자동 연결 재정합: 미정합 세대가 있는 기관 전체 조회",
            select(state.institution_code)
            .where(state.generation > state.reconciled_generation)
            .order_by(state.institution_code),
        ),
        query(
            "reconciliation_pending_dispatch",
            "자동 연결 재정합: 미정합 또는 추천 미전달 세대 200기관",
            select(state.institution_code, state.generation)
            .where(
                or_(
                    state.generation > state.reconciled_generation,
                    state.reconciled_generation > state.recommendations_generation,
                )
            )
            .order_by(state.institution_code)
            .limit(200),
        ),
        query(
            "link_summary_tie_metadata",
            "기회 요약: 날짜가 같은 구성 신호의 원문 위치 일괄 조회",
            select(
                Signal.id,
                Source.key,
                Document.external_id,
                Document.doc_type,
                DocumentChunk.seq,
                DocumentChunk.char_start,
                DocumentChunk.char_end,
                DocumentChunk.labels,
            )
            .join(Document, Document.id == Signal.document_id)
            .join(Source, Source.id == Document.source_id)
            .outerjoin(DocumentChunk, DocumentChunk.id == Signal.chunk_id)
            .where(Signal.id.in_(summary_ids)),
        ),
        query(
            "relation_endpoint_page",
            "사업·계약 관계: 양쪽 ID + 커서 51건",
            endpoint.order_by(OpportunityRelation.id).limit(51),
        ),
        query(
            "relation_endpoint_status_page",
            "사업·계약 관계: 양쪽 ID + 확정 상태 + 커서 51건",
            endpoint.where(OpportunityRelation.status == "confirmed")
            .order_by(OpportunityRelation.id)
            .limit(51),
        ),
        query(
            "relation_status_page",
            "사업·계약 관계: 제안 상태 + ID 커서 51건",
            select(OpportunityRelation)
            .where(OpportunityRelation.id > 0, OpportunityRelation.status == "proposed")
            .order_by(OpportunityRelation.id)
            .limit(51),
        ),
    ]
    for label, target in (
        ("customer", 3 * unprotected_stride),
        ("invalid_core", 5 * unprotected_stride),
    ):
        result.append(
            query(
                f"reconciliation_strict_target_{label}",
                f"자동 연결: 잠근 단일 후보의 최신 엄격 보호 확인 ({label})",
                protected_opportunities_query(
                    {"B-0001"}, opportunity_ids={target}, include_customer=False
                ).limit(1),
            )
        )
    for label, ids in (
        ("hit", document_signal_ids),
        # -1 deliberately exists in the invalid-core fixture; it is not a miss probe.
        ("miss", [-(sizes["signals"] + sid) for sid in document_signal_ids]),
    ):
        result.append(
            query(
                f"customer_anchor_document_protection_{label}",
                f"문서 재처리 보호: 문서 신호 ID와 고객 원본 근거 겹침 ({label})",
                customer_reprocessing_query(ids),
            )
        )
    for label, signal_id in (("hit", 1), ("miss", 0)):
        result.append(
            query(
                f"reconciliation_evidence_contains_{label}",
                f"자동 연결 보호: 구성 신호 ID를 보존한 관계 이력 ({label})",
                select(OpportunityRelationEvent.id)
                .where(
                    OpportunityRelationEvent.evidence_signal_ids.contains(
                        cast([signal_id], ARRAY(BigInteger))
                    )
                )
                .limit(1),
            )
        )
    for label, document_id in (("hit", 2), ("miss", 0)):
        result.append(
            query(
                f"relation_evidence_protection_{label}",
                f"관계 이력: 문서 근거 재처리 보호 JSONB ({label})",
                select(OpportunityRelationEvent.id)
                .where(
                    OpportunityRelationEvent.evidence_snapshot.contains(
                        cast(literal(json.dumps([{"document_id": document_id}])), JSONB)
                    )
                )
                .limit(1),
            )
        )
    for label, opportunity_id in (("hit", 1), ("miss", 0)):
        previous_link = cast(
            literal(json.dumps({"previous_link": {"opportunity_id": opportunity_id}})), JSONB
        )
        history = cast(
            literal(
                json.dumps({"history": [{"previous_link": {"opportunity_id": opportunity_id}}]})
            ),
            JSONB,
        )
        result += [
            query(
                f"reconciliation_review_history_{label}",
                f"자동 연결 보호: 사람 검토의 이전 기회 JSONB 근거 ({label})",
                select(ReviewItem.id)
                .where(
                    or_(
                        ReviewItem.resolution.contains(previous_link),
                        ReviewItem.resolution.contains(history),
                    )
                )
                .limit(1),
            ),
            query(
                f"reconciliation_notification_history_{label}",
                f"자동 연결 보호: 보존된 알림의 기회 JSONB 근거 ({label})",
                select(Notification.id)
                .where(
                    Notification.payload.contains(
                        cast(
                            literal(json.dumps({"items": [{"opportunity_id": opportunity_id}]})),
                            JSONB,
                        )
                    )
                )
                .limit(1),
            ),
        ]
    result += [
        query(
            "relation_mixed_audit",
            "기존 혼합 그룹 감사: 사업·계약 단계 HAVING + 커서 51건",
            select(OpportunitySignal.opportunity_id)
            .join(Signal, Signal.id == OpportunitySignal.signal_id)
            .where(
                OpportunitySignal.opportunity_id > 0,
                Signal.verdict == "accepted",
                OpportunitySignal.tentative.is_(False),
            )
            .group_by(OpportunitySignal.opportunity_id)
            .having(
                func.count().filter(Signal.stage.in_(PROJECT_STAGES)) > 0,
                func.count().filter(Signal.stage.in_(CONTRACT_STAGES)) > 0,
            )
            .order_by(OpportunitySignal.opportunity_id)
            .limit(51),
        ),
        query(
            "review_export_page",
            "사람 검토 내보내기: 리뷰·신호·문서·청크 첫 501건 (벡터 제외)",
            select(ReviewItem, Signal, Document, DocumentChunk)
            .join(Signal, Signal.id == ReviewItem.signal_id)
            .join(Document, Document.id == Signal.document_id)
            .outerjoin(DocumentChunk, DocumentChunk.id == Signal.chunk_id)
            .where(ReviewItem.status.in_(RESOLVED_STATUSES))
            .order_by(ReviewItem.id)
            .options(defer(Signal.embedding))
            .limit(501),
        ),
    ]
    return result


# ------------------------------------------------------------------------------------------------
def _dsn(db: str | None = None) -> str:
    url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    base, _, name = url.rpartition("/")
    name = name.split("?")[0]
    return f"{base}/{db or name}"


def _bench_db() -> str:
    return _dsn().rpartition("/")[2] + "_bench"


def _alembic(revision: str, db: str) -> None:
    root = Path(__file__).resolve().parents[2]
    url = _dsn(db).replace("postgresql://", "postgresql+asyncpg://")
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "-c", str(root / "alembic.ini"), "upgrade", revision],
        check=True,
        cwd=root,
        env={**os.environ, "APP_DATABASE_URL": url, "APP_LOG_JSON": "false"},
        capture_output=True,
    )


async def _recreate(db: str) -> None:
    conn = await asyncpg.connect(_dsn("postgres"))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{db}"')
        await conn.execute(
            f"ALTER DATABASE \"{db}\" SET maintenance_work_mem = '1GB'; "
            f'ALTER DATABASE "{db}" SET max_parallel_maintenance_workers = 3'
        )
    finally:
        await conn.close()


async def _load(conn: asyncpg.Connection, sizes: dict[str, int], log: Any) -> None:
    n = sizes
    await conn.execute("SELECT setseed(0.42)")
    # Vector indexes are rebuilt after the bulk load (inserting into HNSW row by row is slow).
    hnsw = await conn.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE indexdef ILIKE '%USING hnsw%'"
    )
    for row in hnsw:
        await conn.execute(f"DROP INDEX {row['indexname']}")
    # Embeddings of real project descriptions cluster by topic; uniformly random 512-d vectors
    # are the pathological case for any ANN index (all distances concentrate). One center per
    # project type, noise around it.
    await conn.execute(
        f"""CREATE TEMP TABLE bench_topics AS
        SELECT t AS topic, (SELECT array_agg(random() - 0.5) FROM generate_series(1, {DIM} + 0 * t))
               AS v FROM generate_series(0, {len(_TITLES) - 1}) t"""
    )
    steps = [
        (
            "institutions",
            f"""INSERT INTO institutions (code, name, kind, sido, region_code)
            SELECT 'B-' || lpad(i::text, 4, '0'), '벤치 기관 ' || i, 'local_gov', '벤치도',
                   lpad(i::text, 5, '0') FROM generate_series(1, {n["institutions"]}) i""",
        ),
        (
            "sources",
            "INSERT INTO sources (key, name, adapter, enabled) "
            "VALUES ('bench', 'Synthetic query-volume benchmark only', 'benchmark', false)",
        ),
        (
            "documents",
            f"""INSERT INTO documents (source_id, external_id, doc_type, title, published_at,
                                       content_hash, mime, parse_status)
            SELECT (SELECT id FROM sources WHERE key = 'bench'), 'D' || i,
                   {
                _arr(("council_minutes", "budget_book", "order_plan", "prespec", "bid_notice"))
            }[1 + i % 5],
                   '문서 ' || i, date '2022-01-01' + (i % 1700), md5(i::text), 'application/pdf',
                   CASE WHEN i % 500 = 0 THEN 'pending' ELSE 'parsed' END
            FROM generate_series(1, {n["documents"]}) i""",
        ),
        (
            "document_chunks",
            f"""INSERT INTO document_chunks (document_id, seq, char_start, char_end, text,
                                             triage_passed)
            SELECT 1 + i % {n["documents"]}, i, 0, 400, repeat('회의록 본문 ', 40), i % 3 = 0
            FROM generate_series(1, {n["chunks"]}) i""",
        ),
        (
            "opportunities",
            f"""INSERT INTO opportunities (title, category, stage, status, first_seen_at,
                  last_signal_at, signal_count, conversion_prob, institution_code, keywords,
                  embedding, est_budget_krw, bid_window_start)
            SELECT {_arr(_TITLES)}[1 + (i * 7) % {len(_TITLES)}] || ' ' || i,
                   {_arr(_CATEGORIES)}[1 + i % {len(_CATEGORIES)}],
                   {_arr(_STAGES)}[1 + i % {len(_STAGES)}],
                   CASE WHEN i % 100 < 15 THEN 'open' WHEN i % 100 < 18 THEN 'bid_open'
                        WHEN i % 100 < 78 THEN 'closed' ELSE 'dormant' END,
                   date '2022-01-01' + (i % 1500), date '2022-01-01' + (i % 1500) + (i % 300),
                   1 + i % 6, random(),
                   'B-' || lpad((1 + i % {n["institutions"]})::text, 4, '0'),
                   ARRAY[{_arr(_KEYWORDS)}[1 + (i * 7) % {len(_KEYWORDS)}]],
                   (SELECT array_agg(t.v[d] + (random() - 0.5) * {NOISE} ORDER BY d)
                      FROM generate_series(1, {DIM}) d
                      JOIN bench_topics t ON t.topic = (i * 7) % {len(_TITLES)})::vector,
                   (100 + i % 900) * 1000000, date '2022-06-01' + (i % 1600)
            FROM generate_series(1, {n["opportunities"]}) i""",
        ),
        (
            "signals",
            f"""INSERT INTO signals (document_id, stage, title, summary, category, confidence,
                  verdict, extractor, observed_at, dedupe_key, institution_code, external_refs)
            SELECT 1 + i % {n["documents"]}, {_arr(_STAGES)}[1 + i % {len(_STAGES)}],
                   '신호 ' || i, '', {_arr(_CATEGORIES)}[1 + i % {len(_CATEGORIES)}], 0.9,
                   CASE WHEN i % 50 = 0 THEN 'needs_review' ELSE 'accepted' END, 'bench',
                   date '2022-01-01' + (i % 1700), 'S' || i,
                   'B-' || lpad((1 + i % {n["institutions"]})::text, 4, '0'),
                   CASE WHEN i % 10 < 3 THEN jsonb_build_object('order_plan_no', 'R' || i)
                        WHEN i % 10 = 3 THEN jsonb_build_object('prespec_no', 'P' || i)
                        ELSE '{{}}'::jsonb END
            FROM generate_series(1, {n["signals"]}) i""",
        ),
        (
            "opportunity_signals",
            f"""INSERT INTO opportunity_signals (opportunity_id, signal_id, score, method,
                                                 tentative)
            SELECT 1 + (s.id - 1) % {n["opportunities"]}, s.id, 1.0, 'ref', false FROM signals s""",
        ),
        (
            "organizations",
            f"""INSERT INTO organizations (name) SELECT '벤치 회사 ' || i
            FROM generate_series(1, {n["organizations"]}) i""",
        ),
        (
            "recommendations",
            f"""INSERT INTO recommendations (org_id, opportunity_id, score, breakdown,
                                             ranker_version)
            SELECT o.id, 1 + ((o.id * 7919 + k * 104729) % {n["opportunities"]}), random(),
                   '{{}}'::jsonb, 'bench'
            FROM organizations o, generate_series(1, {RECS_PER_ORG}) k""",
        ),
        (
            "job_runs",
            f"""INSERT INTO job_runs (job, job_id, attempt, status, started_at)
            SELECT {
                _arr(("ingest_source", "process_document", "link_signals", "deliver_notifications"))
            }[1 + i % 4], 'job:' || i, 1,
                   CASE WHEN i % 97 = 0 THEN 'failed' ELSE 'succeeded' END,
                   now() - (i % 43200) * interval '1 minute'
            FROM generate_series(1, {n["job_runs"]}) i""",
        ),
    ]
    for name, sql in steps:
        started = datetime.now(UTC)
        await conn.execute(sql)
        log(f"  loaded {name} in {(datetime.now(UTC) - started).total_seconds():.1f}s")
    for row in hnsw:
        started = datetime.now(UTC)
        await conn.execute(row["indexdef"])
        log(f"  built {row['indexname']} in {(datetime.now(UTC) - started).total_seconds():.1f}s")
    await conn.execute("VACUUM ANALYZE")


async def _load_additional(
    conn: asyncpg.Connection, sizes: dict[str, int], log: Any
) -> dict[str, int]:
    """Synthetic query-volume fixtures, added only after the original before/after runs.

    Relation endpoints and review resolutions are not semantic ground truth. Keep the
    original row counts and distributions intact until their comparison is complete.
    """
    relations = sizes["opportunities"] // 2
    reviews = sizes["signals"] // 20
    ingest_runs = sizes["opportunities"] // 10
    notifications = sizes["opportunities"] // 2
    project_width = max(1, min(relations, 200))
    # Reserve every fourth B-0001 identity without bulk-generated anchors. Four of
    # these receive individual protection types below; the rest must remain unprotected.
    # This affects only head fixtures, never the initial before/after measurements.
    unprotected_stride = 4 * sizes["institutions"]
    counts: dict[str, int] = {}
    steps = [
        (
            "opportunity_relations",
            f"""INSERT INTO opportunity_relations
                (project_id, contract_id, kind, status, version, evidence_signal_ids,
                 evidence_snapshot, note)
            SELECT 1 + (i - 1) % {project_width}, {relations} + i, 'project_contract',
                   {_arr(("proposed", "confirmed", "rejected"))}[1 + i % 3], 2,
                   ARRAY[CASE WHEN i % {unprotected_stride} = 0 THEN i + 1 ELSE i END]::bigint[],
                   jsonb_build_array(jsonb_build_object(
                       'document_id', 1 + (1 + (i - 1) % {sizes["signals"]}) % {sizes["documents"]},
                       'synthetic_benchmark', true)),
                   'Query-volume fixture; not a validated project/contract relationship'
            FROM generate_series(1, {relations}) i
            WHERE ({relations} + i) % {unprotected_stride} <> 0
              AND (1 + (i - 1) % {project_width}) % {unprotected_stride} <> 0""",
        ),
        (
            "opportunity_relation_events",
            """INSERT INTO opportunity_relation_events
                (relation_id, version, status, idempotency_key, request_digest,
                 actor_snapshot, evidence_signal_ids, evidence_snapshot, note)
            SELECT r.id, v, CASE WHEN v = 1 THEN 'proposed' ELSE r.status END,
                   'benchmark:' || r.id || ':' || v,
                   repeat(md5(r.id::text || ':' || v::text), 2),
                   '{"benchmark_only": true}'::jsonb,
                   r.evidence_signal_ids, r.evidence_snapshot, r.note
            FROM opportunity_relations r CROSS JOIN generate_series(1, 2) v""",
        ),
        (
            "review_items",
            f"""INSERT INTO review_items
                (signal_id, reasons, status, resolution, resolved_at)
            SELECT i, ARRAY['synthetic_query_volume'],
                   {_arr(RESOLVED_STATUSES)}[1 + i % 3],
                   jsonb_build_object('benchmark_only', true,
                       'previous_link', jsonb_build_object('opportunity_id', i),
                       'history', jsonb_build_array(jsonb_build_object('previous_link',
                           jsonb_build_object('opportunity_id',
                               CASE WHEN (1 + i % {sizes["opportunities"]}) % {unprotected_stride} = 0
                               THEN i % {sizes["opportunities"]} ELSE 1 + i % {sizes["opportunities"]} END)))),
                   now()
            FROM generate_series(1, {reviews}) i WHERE i % {unprotected_stride} <> 0""",
        ),
        (
            "alert_channels",
            """INSERT INTO alert_channels (org_id, kind, target, label, enabled)
            VALUES (1, 'email', 'benchmark@example.invalid', 'Synthetic benchmark only', false)""",
        ),
        (
            "notifications",
            f"""INSERT INTO notifications
                (org_id, channel_id, kind, dedupe_key, payload, status, attempts)
            SELECT 1, (SELECT id FROM alert_channels WHERE label = 'Synthetic benchmark only'),
                   'digest', 'benchmark:notification:' || i,
                   jsonb_build_object('benchmark_only', true,
                       'items', jsonb_build_array(jsonb_build_object('opportunity_id', i))),
                   'sent', 1
            FROM generate_series(1, {notifications}) i WHERE i % {unprotected_stride} <> 0""",
        ),
        (
            "ingest_runs",
            f"""INSERT INTO ingest_runs
                (source_id, status, started_at, finished_at, fetched, created, updated,
                 skipped, stats)
            SELECT (SELECT id FROM sources WHERE key = 'bench'),
                   {_arr(("succeeded", "partial", "failed"))}[1 + i % 3],
                   now() - i * interval '1 minute',
                   now() - i * interval '1 minute' + interval '10 seconds',
                   10, 5, 2, 3, '{{"benchmark_only": true}}'::jsonb
            FROM generate_series(1, {ingest_runs}) i""",
        ),
        (
            "link_reconciliation_states",
            """INSERT INTO link_reconciliation_states
                (institution_code, generation, reconciled_generation, recommendations_generation)
            SELECT code, 3,
                   CASE WHEN n % 3 = 0 THEN 2 ELSE 3 END,
                   CASE WHEN n % 3 = 2 THEN 3 ELSE 2 END
            FROM (SELECT code, row_number() OVER (ORDER BY code) AS n FROM institutions) i
            ON CONFLICT (institution_code) DO UPDATE SET
                generation = EXCLUDED.generation,
                reconciled_generation = EXCLUDED.reconciled_generation,
                recommendations_generation = EXCLUDED.recommendations_generation""",
        ),
    ]
    for name, sql in steps:
        await conn.execute(sql)
        counts[name] = int(await conn.fetchval(f"SELECT count(*) FROM {name}"))
        log(f"  loaded head-only {name}: {counts[name]:,}")
    # The baseline maps stages periodically, so default memberships have no mixed stages.
    # Retype two accepted members in up to 1,000 groups after baseline measurement to
    # exercise a real 51-row audit page, without adding or deleting opportunities/signals.
    retyped = await conn.fetchval(
        """WITH eligible AS (
            SELECT os.opportunity_id FROM opportunity_signals os
            JOIN signals s ON s.id = os.signal_id
            WHERE s.verdict = 'accepted' AND NOT os.tentative
            GROUP BY os.opportunity_id HAVING count(*) >= 2
            ORDER BY os.opportunity_id LIMIT 1000
        ), numbered AS (
            SELECT s.id, row_number() OVER (
                PARTITION BY os.opportunity_id ORDER BY s.id
            ) AS n FROM eligible e
            JOIN opportunity_signals os ON os.opportunity_id = e.opportunity_id
            JOIN signals s ON s.id = os.signal_id
            WHERE s.verdict = 'accepted' AND NOT os.tentative
        ), changed AS (
            UPDATE signals s SET stage = CASE WHEN n.n = 1 THEN 'budget_line' ELSE 'bid_notice' END
            FROM numbered n WHERE s.id = n.id AND n.n <= 2 RETURNING s.id
        ) SELECT count(*) FROM changed"""
    )
    counts["signals_retyped_for_mixed_audit"] = int(retyped)
    counts["mixed_groups_seeded"] = int(retyped) // 2
    counts["coverage_documents"] = int(await conn.fetchval("SELECT count(*) FROM documents"))
    counts["coverage_sources"] = int(await conn.fetchval("SELECT count(*) FROM sources"))
    # The original fixture's signal embeddings/chunk FKs are null. Populate the complete
    # probed institution plus group 1 only, after baseline timings, so the new projection
    # actually reads vectors and source locations without rewriting 400k rows unnecessarily.
    # In _load, the first chunk of document d is d-1 (or documents for document 1).
    counts["reconciliation_payload_signals"] = int(
        await conn.fetchval(
            f"""WITH changed AS (
            UPDATE signals s SET embedding = o.embedding, chunk_id = c.id, verdict = 'accepted'
            FROM opportunity_signals os
            JOIN opportunities o ON o.id = os.opportunity_id,
                 document_chunks c
            WHERE os.signal_id = s.id
              AND (s.institution_code = 'B-0001' OR os.opportunity_id = 1)
              AND c.document_id = s.document_id
              AND c.id = CASE WHEN s.document_id = 1 THEN {sizes["documents"]}
                              ELSE s.document_id - 1 END
            RETURNING s.id
        ) SELECT count(*) FROM changed"""
        )
    )
    counts["reconciliation_scope_signals"] = int(
        await conn.fetchval("SELECT count(*) FROM signals WHERE institution_code = 'B-0001'")
    )
    counts["reconciliation_summary_tie_signals"] = int(
        await conn.fetchval(
            """WITH changed AS (
            UPDATE signals s SET observed_at = date '2026-01-01'
            FROM opportunity_signals os
            WHERE os.signal_id = s.id AND os.opportunity_id = 1 RETURNING s.id
        ) SELECT count(*) FROM changed"""
        )
    )
    counts["reconciliation_dirty_states"] = int(
        await conn.fetchval(
            "SELECT count(*) FROM link_reconciliation_states WHERE generation > reconciled_generation"
        )
    )
    counts["reconciliation_pending_dispatch_states"] = int(
        await conn.fetchval(
            "SELECT count(*) FROM link_reconciliation_states WHERE generation > reconciled_generation "
            "OR reconciled_generation > recommendations_generation"
        )
    )
    # Separate manual, review-history-only, notification-only and relation-history-only
    # anchors make the protected query exercise later EXISTS branches, not just unsafe
    # members/live endpoints. Do not reduce or LIMIT the 400-identity query scope.
    await conn.execute(
        "UPDATE opportunity_signals SET method = 'manual' WHERE opportunity_id = $1",
        unprotected_stride,
    )
    if sizes["opportunities"] >= 2 * unprotected_stride:
        await conn.execute(
            """INSERT INTO review_items (signal_id, reasons, status, resolution, resolved_at)
            VALUES ($1, ARRAY['synthetic_query_volume'], 'edited',
                    jsonb_build_object('benchmark_only', true, 'history', jsonb_build_array(
                        jsonb_build_object('previous_link', jsonb_build_object('opportunity_id', $2::bigint)))), now())""",
            sizes["signals"] - 1,
            2 * unprotected_stride,
        )
    if sizes["opportunities"] >= 3 * unprotected_stride:
        await conn.execute(
            """INSERT INTO notifications
                (org_id, channel_id, kind, dedupe_key, payload, status, attempts)
            SELECT 1, id, 'digest', 'benchmark:notification:history-only',
                   jsonb_build_object('benchmark_only', true, 'items', jsonb_build_array(
                       jsonb_build_object('opportunity_id', $1::bigint))), 'sent', 1
            FROM alert_channels WHERE label = 'Synthetic benchmark only'""",
            3 * unprotected_stride,
        )
    if sizes["opportunities"] >= 4 * unprotected_stride:
        await conn.execute(
            """UPDATE opportunity_relation_events SET evidence_snapshot = evidence_snapshot ||
                jsonb_build_array(jsonb_build_object('opportunity_id', $1::bigint))
            WHERE id = (SELECT min(id) FROM opportunity_relation_events)""",
            4 * unprotected_stride,
        )
    # Populate a real-size core table without turning every unprotected probe identity
    # into a customer identity. The first two of each group's original members are the
    # immutable core; later members still appear in the complete reconciliation scope.
    await conn.execute(
        f"""INSERT INTO opportunity_customer_anchors (opportunity_id, signal_ids)
        SELECT os.opportunity_id, (array_agg(os.signal_id ORDER BY os.signal_id))[1:2]
        FROM opportunity_signals os
        WHERE os.opportunity_id % 5 = 1
           OR (os.opportunity_id % {unprotected_stride} = 0
               AND (os.opportunity_id / {unprotected_stride}) % 3 = 0)
        GROUP BY os.opportunity_id"""
    )
    if sizes["opportunities"] >= 5 * unprotected_stride:
        # A retained but missing core member must exercise strict protection's correlated
        # eligible-member count. Array IDs are snapshots, deliberately not cascading FKs.
        await conn.execute(
            "INSERT INTO opportunity_customer_anchors (opportunity_id, signal_ids) "
            "VALUES ($1, ARRAY[$1::bigint, -1::bigint])",
            5 * unprotected_stride,
        )
    await conn.execute(
        """INSERT INTO notifications
            (org_id, channel_id, kind, dedupe_key, payload, status, attempts)
        SELECT 1, c.id, 'digest', 'benchmark:customer-core:' || a.opportunity_id,
               jsonb_build_object('benchmark_only', true, 'items', jsonb_build_array(
                   jsonb_build_object('opportunity_id', a.opportunity_id))), 'sent', 1
        FROM opportunity_customer_anchors a JOIN opportunities o ON o.id = a.opportunity_id
        CROSS JOIN alert_channels c
        WHERE o.institution_code = 'B-0001' AND a.opportunity_id <> $1
          AND c.label = 'Synthetic benchmark only'""",
        3 * unprotected_stride,
    )
    counts["opportunity_customer_anchors"] = int(
        await conn.fetchval("SELECT count(*) FROM opportunity_customer_anchors")
    )
    for name in ("review_items", "notifications"):
        counts[name] = int(await conn.fetchval(f"SELECT count(*) FROM {name}"))
    await conn.execute("ANALYZE")
    head_queries = {q.key: q.after for q in additional_queries(sizes)}
    protected_sql = head_queries["reconciliation_protected_opportunities"]
    strict_sql = head_queries["reconciliation_strict_opportunities"]
    anchor_sql = head_queries["reconciliation_customer_anchor_scope"]
    counts["reconciliation_scope_opportunities"] = int(
        await conn.fetchval("SELECT count(*) FROM opportunities WHERE institution_code = 'B-0001'")
    )
    counts["reconciliation_protected_opportunities"] = int(
        await conn.fetchval(f"SELECT count(*) FROM ({protected_sql}) protected")
    )
    counts["reconciliation_strict_opportunities"] = int(
        await conn.fetchval(f"SELECT count(*) FROM ({strict_sql}) strict")
    )
    counts["reconciliation_customer_only_opportunities"] = (
        counts["reconciliation_protected_opportunities"]
        - counts["reconciliation_strict_opportunities"]
    )
    counts["reconciliation_scope_customer_anchors"] = int(
        await conn.fetchval(f"SELECT count(*) FROM ({anchor_sql}) anchors")
    )
    counts["reconciliation_unprotected_opportunities"] = (
        counts["reconciliation_scope_opportunities"]
        - counts["reconciliation_protected_opportunities"]
    )
    if (
        not 0
        < counts["reconciliation_strict_opportunities"]
        < counts["reconciliation_protected_opportunities"]
        < counts["reconciliation_scope_opportunities"]
    ):
        raise RuntimeError(
            "Reconciliation benchmark fixture must contain strict, customer-only and unprotected identities"
        )
    await conn.execute("VACUUM ANALYZE")
    return counts


async def _params(conn: asyncpg.Connection) -> dict[str, str]:
    # A company profile close to one project type (e.g. a 스마트쉘터 maker).
    vec = await conn.fetchval(
        f"""SELECT (SELECT array_agg(v[d] + (random() - 0.5) * {NOISE} ORDER BY d)
                    FROM generate_series(1, {DIM}) d)::vector::text
            FROM bench_topics WHERE topic = 0"""
    )
    inst = await conn.fetchval(
        "SELECT o.institution_code FROM opportunities o WHERE EXISTS "
        "(SELECT 1 FROM signals s WHERE s.institution_code = o.institution_code "
        "AND s.verdict = 'accepted' AND s.external_refs ? 'order_plan_no') "
        "GROUP BY 1 ORDER BY count(*) DESC, o.institution_code LIMIT 1"
    )
    hit = await conn.fetchval(
        "SELECT external_refs->>'order_plan_no' FROM signals "
        "WHERE external_refs ? 'order_plan_no' AND institution_code = $1 "
        "AND verdict = 'accepted' ORDER BY id DESC LIMIT 1",
        inst,
    )
    return {"vec": vec, "inst": str(inst), "ref_hit": str(hit), "ref_miss": "R-NOT-SEEN-YET"}


async def run_bench(sizes: dict[str, int], report: Path, log: Any = print) -> dict[str, Any]:
    db = _bench_db()
    log(f"bench database: {db}")
    await _recreate(db)
    await asyncio.to_thread(_alembic, "0001", db)
    conn = await asyncpg.connect(_dsn(db))
    try:
        log("loading (initial schema 0001) …")
        await _load(conn, sizes, log)
        p = await _params(conn)
        qs = queries(p["vec"], p["inst"], p["ref_hit"], p["ref_miss"])
        before = {q.key: await run_query(conn, q, "before") for q in qs}
        log("migrating to head …")
    finally:
        await conn.close()
    started = datetime.now(UTC)
    await asyncio.to_thread(_alembic, "head", db)
    migrate_s = (datetime.now(UTC) - started).total_seconds()
    conn = await asyncpg.connect(_dsn(db))
    try:
        await conn.execute("ANALYZE")
        after = {q.key: await run_query(conn, q, "after") for q in qs}
        log("loading head-only synthetic query-volume fixtures …")
        supplemental_sizes = await _load_additional(conn, sizes, log)
        for q in additional_queries(sizes, vec=p["vec"]):
            qs.append(q)
            before[q.key] = None
            after[q.key] = await run_query(conn, q, "after")
        version = await conn.fetchval("SELECT version()")
        vector_v = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname='vector'")
    finally:
        await conn.close()
    results = {
        "sizes": sizes,
        "supplemental_sizes": supplemental_sizes,
        "supplemental_scope": "synthetic_query_volume_only_after_baseline_comparison",
        "benchmark_source_adapter": "benchmark",
        "migrate_seconds": round(migrate_s, 1),
        "postgres": version,
        "pgvector": vector_v,
        "machine": f"{platform.machine()}, {os.cpu_count()} CPU",
        "queries": [
            {
                "key": q.key,
                "title": q.title,
                "before": _res(before[q.key]),
                "after": _res(after[q.key]),
                "sql_before": q.before,
                "sql_after": q.after,
                "setup_after": list(q.setup_after),
            }
            for q in qs
        ],
    }
    await asyncio.to_thread(report.write_text, render(results), encoding="utf-8")
    return results


def _res(r: Result | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {"ms": round(r.ms, 2), "rows": r.rows, "plan": r.plan, "recall": r.recall}


def _ms(ms: float) -> str:
    return f"{ms:.2f} ms" if ms < 1 else f"{ms:.1f} ms"


def _plan(steps: list[str]) -> str:
    return " → ".join(dict.fromkeys(steps))  # de-duplicated, execution tree order


def render(r: dict[str, Any]) -> str:
    s = r["sizes"]
    lines = [
        "# 대용량 쿼리 성능 (자동 생성: `manage bench`)",
        "",
        f"데이터: 공고 {s['opportunities']:,} · 신호 {s['signals']:,} · 문서 {s['documents']:,} · "
        f"청크 {s['chunks']:,} · 추천 {s['organizations'] * RECS_PER_ORG:,}"
        f" (회사 {s['organizations']:,} × {RECS_PER_ORG}) · 작업 기록 {s['job_runs']:,}",
        "",
        f"환경: {r['postgres'].split(',')[0]}, pgvector {r['pgvector']}, {r['machine']}. "
        "시간은 따뜻한 캐시에서 5회 실행의 중앙값이며 기계마다 다릅니다. "
        "실행 계획과 반환 행 수·재현율이 읽어야 할 부분입니다.",
        "",
        "시간은 PostgreSQL EXPLAIN ANALYZE의 서버 실행 시간입니다. 네트워크 전송, 벡터 디코딩, "
        "ORM 객체 생성과 Python 연결 점수 계산은 포함하지 않으므로 전체 연결 시간은 replay로 따로 측정합니다.",
        "",
        f"벡터는 사업 유형 {len(_TITLES)}개를 중심으로 뭉친 {DIM}차원 합성 임베딩입니다(실제 사업 설명 "
        "임베딩처럼). 재현율은 같은 쿼리를 인덱스 없이 전체 정렬한 정확한 결과와 비교한 값입니다.",
        "",
        "| 쿼리 | 전 | 후 | 전: 행 / 재현율 | 후: 행 / 재현율 |",
        "|---|---:|---:|---|---|",
    ]
    if extra := r.get("supplemental_sizes"):
        lines[4:4] = [
            "신규 쿼리는 기존 전후 비교가 끝난 뒤 head 전용 합성 fixture를 추가하여 측정했습니다. "
            f"관계 {extra['opportunity_relations']:,} · 관계 이력 "
            f"{extra['opportunity_relation_events']:,} · 리뷰 {extra['review_items']:,} · "
            f"수집 실행 {extra['ingest_runs']:,}. 혼합 감사 페이지용으로 "
            f"기존 {extra['mixed_groups_seeded']:,}개 그룹의 승인 신호 "
            f"{extra['signals_retyped_for_mixed_audit']:,}건 단계만 이 추가 측정 전에 바꿨습니다.",
            "",
            f"커버리지 SQL 입력은 문서 {extra['coverage_documents']:,}건·"
            f"수집원 {extra['coverage_sources']:,}개이며, fixture 제외 조건을 통과시키는 "
            "비활성 벤치 전용 adapter `benchmark`를 사용합니다. 원문·기관별 실제 수집·사람 검토 "
            "정답·사업/계약 관계의 의미적 정확성을 검증하는 데이터가 아닙니다. "
            "파일 읽기·원문 유효성 검사·보고서 Python 집계 비용도 SQL 시간에 포함하지 않습니다. "
            "신규 기능의 전 결과는 미구현으로 표시하며 속도 개선율을 계산하지 않습니다.",
            "",
        ]
        if "link_reconciliation_states" in extra:
            lines[4:4] = [
                f"재정합 입력은 기관 상태 {extra['link_reconciliation_states']:,}개 "
                f"(미정합 {extra['reconciliation_dirty_states']:,}, 미정합 또는 추천 전달 대기 "
                f"{extra['reconciliation_pending_dispatch_states']:,})입니다. "
                f"B-0001 기관 전체 {extra['reconciliation_scope_signals']:,}신호를 조회하며, "
                f"이 기관과 요약 동률 그룹의 합계 {extra['reconciliation_payload_signals']:,}신호에만 "
                f"512차원 벡터와 청크 FK를 head 측정 전에 채웠습니다. 요약 동률 조회는 "
                f"{extra['reconciliation_summary_tie_signals']:,}신호의 원문 위치를 읽습니다. "
                "기관 밖 나머지 신호의 벡터·청크 FK는 기존 합성 fixture처럼 비어 있습니다. "
                "완료 세대 필터를 사용하는 추천·알림도 head 전용 쿼리로 별도 측정합니다.",
                "기존 기관/검토 주기의 우연한 일치를 피하도록 B-0001 신호는 head에서만 전부 "
                "승인 상태로 바꿉니다. 이 기관의 매 네 번째 기회에는 일괄 생성하는 보호 근거를 "
                "붙이지 않고, 그중 네 기회에 수동 연결·검토 이력·알림 이력·관계 이력을 각각 "
                f"추가했습니다. 전체 {extra['reconciliation_scope_opportunities']:,}개 기회 중 "
                f"보호 {extra['reconciliation_protected_opportunities']:,}개·비보호 "
                f"{extra['reconciliation_unprotected_opportunities']:,}개는 실제 공용 보호 SQL로 "
                "센 값입니다. 조회 범위·보호 조건을 줄이지 않았습니다.",
                f"고객 원본 근거는 전체 {extra['opportunity_customer_anchors']:,}행이며 이 기관에 "
                f"{extra['reconciliation_scope_customer_anchors']:,}행이 있습니다. 기회별 첫 두 신호만 "
                "보존하고 이후 신호는 전체 조회에 남깁니다. 이 기관에는 고객 이력만 있는 기회와 "
                "원본 근거 하나가 누락된 기회를 따로 넣었습니다. 공용 SQL로 엄격 보호 "
                f"{extra['reconciliation_strict_opportunities']:,}개·고객 이력만 있는 "
                f"{extra['reconciliation_customer_only_opportunities']:,}개·비보호 "
                f"{extra['reconciliation_unprotected_opportunities']:,}개를 확인하며, 세 집단이 "
                "모두 존재하지 않으면 벤치를 실패시킵니다.",
                f"과거 알림 보호용 합성 payload {extra['notifications']:,}건은 비활성 채널에만 "
                "저장합니다. 검토 resolution에도 이전 기회 ID를 채웁니다. 실제 알림을 보내지 "
                "않으며, 보호 전체 조회의 OR 가지가 다른 근거로 단축될 수 있어 검토·알림 "
                "JSONB 포함 조건의 hit/miss도 각각 따로 측정합니다.",
                "",
            ]
    for q in r["queries"]:
        b, a = q["before"], q["after"]

        def rr(x: dict[str, Any] | None) -> str:
            if x is None:
                return "—"
            rec = f" / {x['recall'] * 100:.0f}%" if x["recall"] is not None else ""
            return f"{x['rows']}{rec}"

        before_ms = _ms(b["ms"]) if b is not None else "미구현"
        lines.append(f"| {q['title']} | {before_ms} | {_ms(a['ms'])} | {rr(b)} | {rr(a)} |")
    lines += ["", "## 실행 계획", ""]
    for q in r["queries"]:
        lines += [
            f"### {q['title']}",
            "",
            f"- 전: `{_plan(q['before']['plan'])}`" if q["before"] is not None else "- 전: 미구현",
            f"- 후: `{_plan(q['after']['plan'])}`"
            + (f" (세션 설정: `{'; '.join(q['setup_after'])}`)" if q["setup_after"] else ""),
            "",
        ]
    lines += [
        f"마이그레이션 0001 → head 적용 시간(데이터가 있는 상태): {r['migrate_seconds']}초",
        "",
    ]
    return "\n".join(lines)
