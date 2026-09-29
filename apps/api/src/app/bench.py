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
5. writes the comparison to ``docs/performance.md``.

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

from app.settings import get_settings

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
    before: str
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
            "기회 연결: 승인된 확정 신호가 있는 기관·기간 내 전체 후보 (제한 없음)",
            eligible_candidates,
            eligible_candidates,
            [inst],
            exact=eligible_candidates,
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


async def run_query(conn: asyncpg.Connection, q: Query, phase: str, repeats: int = 5) -> Result:
    sql = q.before if phase == "before" else q.after
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
            "INSERT INTO sources (key, name, adapter) VALUES ('bench', 'bench', 'fixture')",
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
        version = await conn.fetchval("SELECT version()")
        vector_v = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname='vector'")
    finally:
        await conn.close()
    results = {
        "sizes": sizes,
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


def _res(r: Result) -> dict[str, Any]:
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
        f"벡터는 사업 유형 {len(_TITLES)}개를 중심으로 뭉친 {DIM}차원 합성 임베딩입니다(실제 사업 설명 "
        "임베딩처럼). 재현율은 같은 쿼리를 인덱스 없이 전체 정렬한 정확한 결과와 비교한 값입니다.",
        "",
        "| 쿼리 | 전 | 후 | 전: 행 / 재현율 | 후: 행 / 재현율 |",
        "|---|---:|---:|---|---|",
    ]
    for q in r["queries"]:
        b, a = q["before"], q["after"]

        def rr(x: dict[str, Any]) -> str:
            rec = f" / {x['recall'] * 100:.0f}%" if x["recall"] is not None else ""
            return f"{x['rows']}{rec}"

        lines.append(f"| {q['title']} | {_ms(b['ms'])} | {_ms(a['ms'])} | {rr(b)} | {rr(a)} |")
    lines += ["", "## 실행 계획", ""]
    for q in r["queries"]:
        lines += [
            f"### {q['title']}",
            "",
            f"- 전: `{_plan(q['before']['plan'])}`",
            f"- 후: `{_plan(q['after']['plan'])}`"
            + (f" (세션 설정: `{'; '.join(q['setup_after'])}`)" if q["setup_after"] else ""),
            "",
        ]
    lines += [f"마이그레이션 0002 적용 시간(데이터가 있는 상태): {r['migrate_seconds']}초", ""]
    return "\n".join(lines)
