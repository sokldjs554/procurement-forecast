# 대용량 쿼리 성능 (자동 생성: `manage bench`)

2026-09-30 [PR #54 운영 규모 벤치](https://github.com/sokldjs554/procurement-forecast/actions/runs/36666923259), 코드 `9834b6a`의 결과입니다.
아래 전/후는 벤치에 정의된 쿼리·인덱스 비교이며 PR 전체의 속도 개선율이 아닙니다.
가까운 후보 12건은 이전 경로의 비교용이고, 현재 연결기는 전체 적격 후보를 검사합니다.
실제 배포 환경의 부하·메모리·네트워크·큐 대기시간을 측정한 결과는 아닙니다.

데이터: 공고 100,000 · 신호 400,000 · 문서 150,000 · 청크 600,000 · 추천 300,000 (회사 2,000 × 150) · 작업 기록 100,000

재정합 입력은 기관 상태 250개 (미정합 83, 미정합 또는 추천 전달 대기 167)입니다. B-0001 기관 전체 1,600신호를 조회하며, 이 기관과 요약 동률 그룹의 합계 1,604신호에만 512차원 벡터와 청크 FK를 head 측정 전에 채웠습니다. 요약 동률 조회는 4신호의 원문 위치를 읽습니다. 기관 밖 나머지 신호의 벡터·청크 FK는 기존 합성 fixture처럼 비어 있습니다. 완료 세대 필터를 사용하는 추천·알림도 head 전용 쿼리로 별도 측정합니다.
기존 기관/검토 주기의 우연한 일치를 피하도록 B-0001 신호는 head에서만 전부 승인 상태로 바꿉니다. 이 기관의 매 네 번째 기회에는 일괄 생성하는 보호 근거를 붙이지 않고, 그중 네 기회에 수동 연결·검토 이력·알림 이력·관계 이력을 각각 추가했습니다. 전체 400개 기회 중 보호 337개·비보호 63개는 실제 공용 보호 SQL로 센 값입니다. 조회 범위·보호 조건을 줄이지 않았습니다.
고객 원본 근거는 전체 20,034행이며 이 기관에 34행이 있습니다. 기회별 첫 두 신호만 보존하고 이후 신호는 전체 조회에 남깁니다. 이 기관에는 고객 이력만 있는 기회와 원본 근거 하나가 누락된 기회를 따로 넣었습니다. 공용 SQL로 엄격 보호 304개·고객 이력만 있는 33개·비보호 63개를 확인하며, 세 집단이 모두 존재하지 않으면 벤치를 실패시킵니다.
과거 알림 보호용 합성 payload 49,984건은 비활성 채널에만 저장합니다. 검토 resolution에도 이전 기회 ID를 채웁니다. 실제 알림을 보내지 않으며, 보호 전체 조회의 OR 가지가 다른 근거로 단축될 수 있어 검토·알림 JSONB 포함 조건의 hit/miss도 각각 따로 측정합니다.

신규 쿼리는 기존 전후 비교가 끝난 뒤 head 전용 합성 fixture를 추가하여 측정했습니다. 관계 49,950 · 관계 이력 99,900 · 리뷰 19,981 · 수집 실행 10,000. 혼합 감사 페이지용으로 기존 1,000개 그룹의 승인 신호 2,000건 단계만 이 추가 측정 전에 바꿨습니다.

커버리지 SQL 입력은 문서 150,000건·수집원 1개이며, fixture 제외 조건을 통과시키는 비활성 벤치 전용 adapter `benchmark`를 사용합니다. 원문·기관별 실제 수집·사람 검토 정답·사업/계약 관계의 의미적 정확성을 검증하는 데이터가 아닙니다. 파일 읽기·원문 유효성 검사·보고서 Python 집계 비용도 SQL 시간에 포함하지 않습니다. 신규 기능의 전 결과는 미구현으로 표시하며 속도 개선율을 계산하지 않습니다.

환경: PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2) on x86_64-pc-linux-gnu, pgvector 0.8.6, x86_64, 4 CPU. 시간은 따뜻한 캐시에서 5회 실행의 중앙값이며 기계마다 다릅니다. 실행 계획과 반환 행 수·재현율이 읽어야 할 부분입니다.

시간은 PostgreSQL EXPLAIN ANALYZE의 서버 실행 시간입니다. 네트워크 전송, 벡터 디코딩, ORM 객체 생성과 Python 연결 점수 계산은 포함하지 않으므로 전체 연결 시간은 replay로 따로 측정합니다.

벡터는 사업 유형 20개를 중심으로 뭉친 512차원 합성 임베딩입니다(실제 사업 설명 임베딩처럼). 재현율은 같은 쿼리를 인덱스 없이 전체 정렬한 정확한 결과와 비교한 값입니다.

| 쿼리 | 전 | 후 | 전: 행 / 재현율 | 후: 행 / 재현율 |
|---|---:|---:|---|---|
| 기회 연결: 기관·기간 내 전체 후보 (전: 벡터 포함, 후: 메타데이터만) | 0.82 ms | 0.81 ms | 132 / 100% | 132 / 100% |
| 기회 연결: 구조 필터를 통과한 후보의 벡터 일괄 조회 (4건 예시) | 0.02 ms | 0.02 ms | 4 | 4 |
| 기회 연결: 기관·승인·확정 조건의 모든 번호 일치 대상 (모호성 검사) | 1.6 ms | 0.19 ms | 1 | 1 |
| 기회 연결: 전체 적격 후보의 승인된 확정 번호 (계약 충돌 검사) | 2.2 ms | 2.0 ms | 528 | 528 |
| 저장 신호 재검증: 수집원·문서 유형·승인 조건의 ID 커서 1,001건 | 5.2 ms | 5.0 ms | 1001 | 1001 |
| 백테스트: 승인된 확정 연결의 공개일·첫 신호 조회 | 1124.5 ms | 434.7 ms | 400000 | 392000 |
| 추천 후보: 회사 소개와 가까운 진행 중 공고 300건 (벡터) | 0.65 ms | 57.7 ms | 13 / 4% | 300 / 100% |
| 추천 후보: 관심 키워드가 제목·키워드에 있는 진행 중 공고 | 4.2 ms | 5.3 ms | 300 | 300 |
| 기회 연결: 처음 보는 발주계획번호로 기존 기회 찾기 (없음) | 94.7 ms | 0.02 ms | 0 | 0 |
| 기회 연결: 이미 있는 발주계획번호로 기존 기회 찾기 | 0.15 ms | 0.03 ms | 1 | 1 |
| 기회 연결: 같은 기관·기간의 가까운 기회 12건 (벡터) | 0.79 ms | 0.62 ms | 12 / 100% | 12 / 100% |
| 기회 연결: 후보 기회 12건이 가진 번호 (다른 번호면 후보에서 제외) | 0.10 ms | 0.10 ms | 48 | 48 |
| 기회 연결: 후보 기회 12건에 같은 예산서의 다른 행이 있는지 (있으면 제외) | 0.09 ms | 0.09 ms | 0 | 0 |
| 고객 피드 첫 페이지 (점수순 21건) | 0.29 ms | 0.25 ms | 21 | 21 |
| 고객 피드 첫 페이지 (입찰이 가까운 순 21건) | 0.38 ms | 0.36 ms | 21 | 21 |
| 고객 피드 첫 페이지 (새 소식 순 21건) | 0.35 ms | 0.35 ms | 21 | 21 |
| 고객 피드 단계별 건수 (칩·머리말, GROUP BY) | 0.35 ms | 0.35 ms | 5 | 5 |
| 운영 개요: 파이프라인 퍼널 집계 (5개 COUNT) | 199.0 ms | 138.1 ms | 1 | 1 |
| 배치: 처리 대기 문서 200건 (10분마다) | 0.13 ms | 0.16 ms | 200 | 200 |
| 추천 후보: 미정합 기관 제외 + 벡터 최근접 300건 (전체 기회 투영) | 미구현 | 49.0 ms | — | 300 / 100% |
| 추천 후보: 미정합 기관 제외 + 제목·키워드 300건 (전체 기회 투영) | 미구현 | 2.5 ms | — | 300 |
| 추천 후보: 미정합 기관 제외 + 분야 300건 (전체 기회 투영) | 미구현 | 1.2 ms | — | 300 |
| 알림 후보: 회사 7·점수 0.5 이상·미정합 기관 제외 (추천·기회 투영) | 미구현 | 0.27 ms | — | 9 |
| 수집 현황: 전체 문서의 좁은 메타데이터 투영 | 미구현 | 59.6 ms | — | 150000 |
| 수집 현황: 수집원별 최신 실행 기록 | 미구현 | 2.6 ms | — | 1 |
| 자동 연결 재정합: 기관 전체 신호·연결·벡터·원문 위치 | 미구현 | 90.4 ms | — | 1600 |
| 자동 연결 재정합: 기관 범위의 사람 결정·고객 이력 전체 보호 조회 | 미구현 | 533.1 ms | — | 337 |
| 자동 연결 재정합: 엄격 보호 전체 조회 (고객 원본 근거 유효성 포함) | 미구현 | 439.3 ms | — | 304 |
| 자동 연결 재정합: 기관 전체 고객 원본 근거 ID·생성 시각 | 미구현 | 21.0 ms | — | 34 |
| 자동 연결 재정합: 미정합 세대가 있는 기관 전체 조회 | 미구현 | 0.05 ms | — | 83 |
| 자동 연결 재정합: 미정합 또는 추천 미전달 세대 200기관 | 미구현 | 0.10 ms | — | 167 |
| 기회 요약: 날짜가 같은 구성 신호의 원문 위치 일괄 조회 | 미구현 | 0.05 ms | — | 4 |
| 사업·계약 관계: 양쪽 ID + 커서 51건 | 미구현 | 1.3 ms | — | 51 |
| 사업·계약 관계: 양쪽 ID + 확정 상태 + 커서 51건 | 미구현 | 0.09 ms | — | 51 |
| 사업·계약 관계: 제안 상태 + ID 커서 51건 | 미구현 | 0.04 ms | — | 51 |
| 자동 연결: 잠근 단일 후보의 최신 엄격 보호 확인 (customer) | 미구현 | 0.22 ms | — | 0 |
| 자동 연결: 잠근 단일 후보의 최신 엄격 보호 확인 (invalid_core) | 미구현 | 0.18 ms | — | 1 |
| 문서 재처리 보호: 문서 신호 ID와 고객 원본 근거 겹침 (hit) | 미구현 | 0.05 ms | — | 1 |
| 문서 재처리 보호: 문서 신호 ID와 고객 원본 근거 겹침 (miss) | 미구현 | 0.05 ms | — | 0 |
| 자동 연결 보호: 구성 신호 ID를 보존한 관계 이력 (hit) | 미구현 | 0.02 ms | — | 1 |
| 자동 연결 보호: 구성 신호 ID를 보존한 관계 이력 (miss) | 미구현 | 0.01 ms | — | 0 |
| 관계 이력: 문서 근거 재처리 보호 JSONB (hit) | 미구현 | 0.02 ms | — | 1 |
| 관계 이력: 문서 근거 재처리 보호 JSONB (miss) | 미구현 | 0.01 ms | — | 0 |
| 자동 연결 보호: 사람 검토의 이전 기회 JSONB 근거 (hit) | 미구현 | 0.02 ms | — | 1 |
| 자동 연결 보호: 보존된 알림의 기회 JSONB 근거 (hit) | 미구현 | 0.01 ms | — | 1 |
| 자동 연결 보호: 사람 검토의 이전 기회 JSONB 근거 (miss) | 미구현 | 0.01 ms | — | 0 |
| 자동 연결 보호: 보존된 알림의 기회 JSONB 근거 (miss) | 미구현 | 0.01 ms | — | 0 |
| 기존 혼합 그룹 감사: 사업·계약 단계 HAVING + 커서 51건 | 미구현 | 0.43 ms | — | 51 |
| 사람 검토 내보내기: 리뷰·신호·문서·청크 첫 501건 (벡터 제외) | 미구현 | 1.8 ms | — | 501 |

## 실행 계획

### 기회 연결: 기관·기간 내 전체 후보 (전: 벡터 포함, 후: 메타데이터만)

- 전: `Sort → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution) → Index Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`
- 후: `Sort → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution) → Index Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`

### 기회 연결: 구조 필터를 통과한 후보의 벡터 일괄 조회 (4건 예시)

- 전: `Index Scan (opportunities_pkey)`
- 후: `Index Scan (opportunities_pkey)`

### 기회 연결: 기관·승인·확정 조건의 모든 번호 일치 대상 (모호성 검사)

- 전: `Unique → Sort → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_signals_institution_observed) → Index Scan (opportunity_signals_signal_id_key) → Index Scan (opportunities_pkey)`
- 후: `Unique → Sort → Nested Loop → Bitmap Heap Scan → BitmapAnd → Bitmap Index Scan (ix_signals_institution_observed) → BitmapOr → Bitmap Index Scan (ix_signals_external_refs_gin) → Index Scan (opportunity_signals_signal_id_key) → Index Scan (opportunities_pkey)`

### 기회 연결: 전체 적격 후보의 승인된 확정 번호 (계약 충돌 검사)

- 전: `Nested Loop → Aggregate → Sort → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution) → Index Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`
- 후: `Nested Loop → Unique → Sort → Subquery Scan → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution) → Index Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`

### 저장 신호 재검증: 수집원·문서 유형·승인 조건의 ID 커서 1,001건

- 전: `Limit → Nested Loop → Index Scan (signals_pkey) → Memoize → Index Scan (documents_pkey) → Materialize → Seq Scan (sources)`
- 후: `Limit → Nested Loop → Index Scan (signals_pkey) → Memoize → Index Scan (documents_pkey) → Materialize → Seq Scan (sources)`

### 백테스트: 승인된 확정 연결의 공개일·첫 신호 조회

- 전: `Gather Merge → Incremental Sort → Nested Loop → Merge Join → Index Only Scan (opportunity_signals_pkey) → Index Scan (opportunities_pkey) → Index Scan (signals_pkey)`
- 후: `Gather Merge → Sort → Hash Join → Seq Scan (signals) → Hash → Seq Scan (opportunity_signals) → Index Only Scan (opportunities_pkey) → Seq Scan (documents)`

### 추천 후보: 회사 소개와 가까운 진행 중 공고 300건 (벡터)

- 전: `Limit → Index Scan (ix_opportunities_embedding_hnsw)`
- 후: `Limit → Sort → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_status_window)` (세션 설정: `SET LOCAL hnsw.ef_search = 300`)

### 추천 후보: 관심 키워드가 제목·키워드에 있는 진행 중 공고

- 전: `Limit → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_status_window)`
- 후: `Limit → Seq Scan (opportunities)`

### 기회 연결: 처음 보는 발주계획번호로 기존 기회 찾기 (없음)

- 전: `Limit → Nested Loop → Seq Scan (signals) → Index Scan (opportunity_signals_signal_id_key)`
- 후: `Limit → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_signals_external_refs_gin) → Index Scan (opportunity_signals_signal_id_key)`

### 기회 연결: 이미 있는 발주계획번호로 기존 기회 찾기

- 전: `Limit → Nested Loop → Seq Scan (signals) → Index Scan (opportunity_signals_signal_id_key)`
- 후: `Limit → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_signals_external_refs_gin) → Index Scan (opportunity_signals_signal_id_key)`

### 기회 연결: 같은 기관·기간의 가까운 기회 12건 (벡터)

- 전: `Limit → Sort → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution)`
- 후: `Limit → Sort → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution)`

### 기회 연결: 후보 기회 12건이 가진 번호 (다른 번호면 후보에서 제외)

- 전: `Nested Loop → Index Only Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`
- 후: `Nested Loop → Index Only Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`

### 기회 연결: 후보 기회 12건에 같은 예산서의 다른 행이 있는지 (있으면 제외)

- 전: `Nested Loop → Index Only Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`
- 후: `Nested Loop → Index Only Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`

### 고객 피드 첫 페이지 (점수순 21건)

- 전: `Limit → Incremental Sort → Nested Loop → Index Scan (ix_recommendations_org_score) → Index Scan (opportunities_pkey)`
- 후: `Limit → Incremental Sort → Nested Loop → Index Scan (ix_recommendations_org_score) → Index Scan (opportunities_pkey)`

### 고객 피드 첫 페이지 (입찰이 가까운 순 21건)

- 전: `Limit → Sort → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_recommendations_org_score) → Index Scan (opportunities_pkey)`
- 후: `Limit → Sort → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_recommendations_org_score) → Index Scan (opportunities_pkey)`

### 고객 피드 첫 페이지 (새 소식 순 21건)

- 전: `Limit → Sort → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_recommendations_org_score) → Index Scan (opportunities_pkey)`
- 후: `Limit → Sort → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_recommendations_org_score) → Index Scan (opportunities_pkey)`

### 고객 피드 단계별 건수 (칩·머리말, GROUP BY)

- 전: `Aggregate → Sort → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_recommendations_org_score) → Index Scan (opportunities_pkey)`
- 후: `Aggregate → Sort → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_recommendations_org_score) → Index Scan (opportunities_pkey)`

### 운영 개요: 파이프라인 퍼널 집계 (5개 COUNT)

- 전: `Result → Aggregate → Index Only Scan (ix_documents_type_published) → Gather → Index Only Scan (document_chunks_pkey) → Seq Scan (document_chunks) → Index Only Scan (ix_signals_verdict) → Index Only Scan (ix_opportunities_institution)`
- 후: `Subquery Scan → Aggregate → Index Only Scan (ix_documents_type_published) → Gather → Index Only Scan (ix_signals_verdict) → Index Only Scan (ix_opportunities_institution) → Seq Scan (document_chunks)`

### 배치: 처리 대기 문서 200건 (10분마다)

- 전: `Limit → Sort → Index Scan (ix_documents_pending)`
- 후: `Limit → Sort → Index Scan (ix_documents_pending)`

### 추천 후보: 미정합 기관 제외 + 벡터 최근접 300건 (전체 기회 투영)

- 전: 미구현
- 후: `Limit → Sort → Hash Join → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_status_window) → Hash → Seq Scan (link_reconciliation_states)` (세션 설정: `SET LOCAL hnsw.ef_search = 300`)

### 추천 후보: 미정합 기관 제외 + 제목·키워드 300건 (전체 기회 투영)

- 전: 미구현
- 후: `Limit → Hash Join → Bitmap Heap Scan → BitmapAnd → BitmapOr → Bitmap Index Scan (ix_opportunities_title_trgm) → Bitmap Index Scan (ix_opportunities_keywords_gin) → Bitmap Index Scan (ix_opportunities_status_window) → Hash → Seq Scan (link_reconciliation_states)`

### 추천 후보: 미정합 기관 제외 + 분야 300건 (전체 기회 투영)

- 전: 미구현
- 후: `Limit → Hash Join → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_status_window) → Hash → Seq Scan (link_reconciliation_states)`

### 알림 후보: 회사 7·점수 0.5 이상·미정합 기관 제외 (추천·기회 투영)

- 전: 미구현
- 후: `Sort → Nested Loop → Bitmap Heap Scan → Bitmap Index Scan (ix_recommendations_org_score) → Index Scan (opportunities_pkey) → Index Scan (link_reconciliation_states_pkey)`

### 수집 현황: 전체 문서의 좁은 메타데이터 투영

- 전: 미구현
- 후: `Nested Loop → Seq Scan (sources) → Seq Scan (documents)`

### 수집 현황: 수집원별 최신 실행 기록

- 전: 미구현
- 후: `Unique → Sort → Seq Scan (ingest_runs)`

### 자동 연결 재정합: 기관 전체 신호·연결·벡터·원문 위치

- 전: 미구현
- 후: `Sort → Nested Loop → Seq Scan (sources) → Hash Join → Seq Scan (opportunity_signals) → Hash → Aggregate → Append → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution) → Gather → Bitmap Index Scan (ix_signals_institution_observed) → Index Scan (opportunity_signals_signal_id_key) → Index Scan (signals_pkey) → Index Scan (documents_pkey) → Memoize → Index Scan (document_chunks_pkey)`

### 자동 연결 재정합: 기관 범위의 사람 결정·고객 이력 전체 보호 조회

- 전: 미구현
- 후: `Sort → Aggregate → Append → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution) → Gather → Nested Loop → Bitmap Index Scan (ix_signals_institution_observed) → Index Scan (opportunity_signals_signal_id_key) → CTE Scan → Index Scan (opportunities_pkey) → Index Scan (opportunity_signals_pkey) → Index Scan (signals_pkey) → Index Only Scan (opportunity_signals_pkey) → Index Scan (review_items_signal_id_key) → BitmapOr → Bitmap Index Scan (ix_review_resolution) → Bitmap Index Scan (ix_relations_project_status) → Bitmap Index Scan (ix_relations_contract_status) → Bitmap Index Scan (ix_relation_events_evidence) → Bitmap Index Scan (ix_relation_events_signal_ids) → Index Scan (opportunity_customer_anchors_pkey) → Bitmap Index Scan (ix_recommendations_opportunity) → Seq Scan (briefs) → Bitmap Index Scan (ix_notification_payload)`

### 자동 연결 재정합: 엄격 보호 전체 조회 (고객 원본 근거 유효성 포함)

- 전: 미구현
- 후: `Sort → Aggregate → Append → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution) → Gather → Nested Loop → Bitmap Index Scan (ix_signals_institution_observed) → Index Scan (opportunity_signals_signal_id_key) → CTE Scan → Index Scan (opportunities_pkey) → Index Scan (opportunity_signals_pkey) → Index Scan (signals_pkey) → Index Only Scan (opportunity_signals_pkey) → Index Scan (review_items_signal_id_key) → BitmapOr → Bitmap Index Scan (ix_review_resolution) → Bitmap Index Scan (ix_relations_project_status) → Bitmap Index Scan (ix_relations_contract_status) → Bitmap Index Scan (ix_relation_events_evidence) → Bitmap Index Scan (ix_relation_events_signal_ids) → Index Scan (opportunity_customer_anchors_pkey)`

### 자동 연결 재정합: 기관 전체 고객 원본 근거 ID·생성 시각

- 전: 미구현
- 후: `Hash Join → Seq Scan (opportunity_customer_anchors) → Hash → Aggregate → Append → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution) → Gather → Nested Loop → Bitmap Index Scan (ix_signals_institution_observed) → Index Scan (opportunity_signals_signal_id_key)`

### 자동 연결 재정합: 미정합 세대가 있는 기관 전체 조회

- 전: 미구현
- 후: `Sort → Seq Scan (link_reconciliation_states)`

### 자동 연결 재정합: 미정합 또는 추천 미전달 세대 200기관

- 전: 미구현
- 후: `Limit → Sort → Seq Scan (link_reconciliation_states)`

### 기회 요약: 날짜가 같은 구성 신호의 원문 위치 일괄 조회

- 전: 미구현
- 후: `Nested Loop → Seq Scan (sources) → Index Scan (signals_pkey) → Index Scan (documents_pkey) → Index Scan (document_chunks_pkey)`

### 사업·계약 관계: 양쪽 ID + 커서 51건

- 전: 미구현
- 후: `Limit → Index Scan (opportunity_relations_pkey)`

### 사업·계약 관계: 양쪽 ID + 확정 상태 + 커서 51건

- 전: 미구현
- 후: `Limit → Sort → Bitmap Heap Scan → BitmapOr → Bitmap Index Scan (ix_relations_project_status) → Bitmap Index Scan (uq_relation_confirmed_contract)`

### 사업·계약 관계: 제안 상태 + ID 커서 51건

- 전: 미구현
- 후: `Limit → Index Scan (opportunity_relations_pkey)`

### 자동 연결: 잠근 단일 후보의 최신 엄격 보호 확인 (customer)

- 전: 미구현
- 후: `Limit → Unique → Sort → Append → Index Scan (opportunities_pkey) → Nested Loop → Index Only Scan (opportunity_signals_pkey) → Index Scan (signals_pkey) → CTE Scan → Index Scan (opportunity_signals_pkey) → Index Scan (review_items_signal_id_key) → Aggregate → Bitmap Heap Scan → BitmapOr → Bitmap Index Scan (ix_review_resolution) → Bitmap Index Scan (ix_relations_project_status) → Bitmap Index Scan (ix_relations_contract_status) → Bitmap Index Scan (ix_relation_events_evidence) → Bitmap Index Scan (ix_relation_events_signal_ids) → Index Scan (opportunity_customer_anchors_pkey) → Index Scan (opportunity_signals_signal_id_key)`

### 자동 연결: 잠근 단일 후보의 최신 엄격 보호 확인 (invalid_core)

- 전: 미구현
- 후: `Limit → Unique → Sort → Append → Index Scan (opportunities_pkey) → Nested Loop → Index Only Scan (opportunity_signals_pkey) → Index Scan (signals_pkey) → CTE Scan → Index Scan (opportunity_signals_pkey) → Index Scan (review_items_signal_id_key) → Aggregate → Bitmap Heap Scan → BitmapOr → Bitmap Index Scan (ix_review_resolution) → Bitmap Index Scan (ix_relations_project_status) → Bitmap Index Scan (ix_relations_contract_status) → Bitmap Index Scan (ix_relation_events_evidence) → Bitmap Index Scan (ix_relation_events_signal_ids) → Index Scan (opportunity_customer_anchors_pkey) → Index Scan (opportunity_signals_signal_id_key)`

### 문서 재처리 보호: 문서 신호 ID와 고객 원본 근거 겹침 (hit)

- 전: 미구현
- 후: `Limit → Append → Bitmap Heap Scan → Bitmap Index Scan (ix_customer_anchor_signals) → Nested Loop → Aggregate → Index Scan (opportunity_signals_signal_id_key) → Index Only Scan (opportunities_pkey) → Bitmap Index Scan (ix_recommendations_opportunity) → Seq Scan (briefs) → Bitmap Index Scan (ix_notification_payload) → Index Only Scan (opportunity_customer_anchors_pkey)`

### 문서 재처리 보호: 문서 신호 ID와 고객 원본 근거 겹침 (miss)

- 전: 미구현
- 후: `Limit → Append → Bitmap Heap Scan → Bitmap Index Scan (ix_customer_anchor_signals) → Nested Loop → Aggregate → Index Scan (opportunity_signals_signal_id_key) → Index Only Scan (opportunities_pkey) → Bitmap Index Scan (ix_recommendations_opportunity) → Seq Scan (briefs) → Bitmap Index Scan (ix_notification_payload) → Index Only Scan (opportunity_customer_anchors_pkey)`

### 자동 연결 보호: 구성 신호 ID를 보존한 관계 이력 (hit)

- 전: 미구현
- 후: `Limit → Bitmap Heap Scan → Bitmap Index Scan (ix_relation_events_signal_ids)`

### 자동 연결 보호: 구성 신호 ID를 보존한 관계 이력 (miss)

- 전: 미구현
- 후: `Limit → Bitmap Heap Scan → Bitmap Index Scan (ix_relation_events_signal_ids)`

### 관계 이력: 문서 근거 재처리 보호 JSONB (hit)

- 전: 미구현
- 후: `Limit → Bitmap Heap Scan → Bitmap Index Scan (ix_relation_events_evidence)`

### 관계 이력: 문서 근거 재처리 보호 JSONB (miss)

- 전: 미구현
- 후: `Limit → Bitmap Heap Scan → Bitmap Index Scan (ix_relation_events_evidence)`

### 자동 연결 보호: 사람 검토의 이전 기회 JSONB 근거 (hit)

- 전: 미구현
- 후: `Limit → Bitmap Heap Scan → BitmapOr → Bitmap Index Scan (ix_review_resolution)`

### 자동 연결 보호: 보존된 알림의 기회 JSONB 근거 (hit)

- 전: 미구현
- 후: `Limit → Bitmap Heap Scan → Bitmap Index Scan (ix_notification_payload)`

### 자동 연결 보호: 사람 검토의 이전 기회 JSONB 근거 (miss)

- 전: 미구현
- 후: `Limit → Bitmap Heap Scan → BitmapOr → Bitmap Index Scan (ix_review_resolution)`

### 자동 연결 보호: 보존된 알림의 기회 JSONB 근거 (miss)

- 전: 미구현
- 후: `Limit → Bitmap Heap Scan → Bitmap Index Scan (ix_notification_payload)`

### 기존 혼합 그룹 감사: 사업·계약 단계 HAVING + 커서 51건

- 전: 미구현
- 후: `Limit → Aggregate → Nested Loop → Index Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`

### 사람 검토 내보내기: 리뷰·신호·문서·청크 첫 501건 (벡터 제외)

- 전: 미구현
- 후: `Limit → Nested Loop → Index Scan (review_items_pkey) → Index Scan (signals_pkey) → Index Scan (documents_pkey) → Memoize → Index Scan (document_chunks_pkey)`

마이그레이션 0001 → head 적용 시간(데이터가 있는 상태): 4.6초

## 보호 조회 병목과 교정

첫 완성본 `8e242f0`의 [감사](https://github.com/sokldjs554/procurement-forecast/actions/runs/36663512346)는
기능을 통과했지만, 대용량 이력의 희소한 일치를 `EXISTS`로 찾는 조회가 기관마다 전체
이력 테이블을 반복 순차 조회했습니다. 이 결과를 확인한 뒤 병합을 보류했습니다.
인덱스로 일치 건수를 세는 형태로 바꾸고 추천·브리프의 기회 ID 인덱스를 추가했습니다.
새 실행 계획은 검토·관계·알림의 GIN/B-tree 인덱스를 사용합니다. 빈 브리프 표의 순차
조회는 그대로 허용하며, PostgreSQL의 순차 조회를 전역으로 끄지 않았습니다.

| 보호 조회 | 이전 서버 시간 | 교정 후 서버 시간 | 반환 수 |
| --- | ---: | ---: | ---: |
| 기관 전체 보호 | 71,737.3ms | 533.14ms | 337 → 337 |
| 기관의 엄격 보호 | 64,492.6ms | 439.32ms | 304 → 304 |
| 고객 기회 단일 대상 | 326.6ms | 0.22ms | 0 → 0 |
| 원본 근거가 누락된 단일 대상 | 326.4ms | 0.18ms | 1 → 1 |

기관의 400기회·1,600신호와 전체 이력 범위를 줄이지 않았습니다. 엄격 보호 304개,
고객 이력만 있는 33개, 비보호 63개가 모두 존재합니다. 원본 근거 표도 20,034행을
채운 상태입니다. 무조건 보호되는 작은 표본만 재서 빠르다고 보고하지 않습니다.
문서 보호 miss 입력도 누락 근거용 fixture의 `-1`과 겹치지 않는 값으로 고쳤으며,
현재 hit 1행 / miss 0행을 각각 확인합니다.

`Integrity audit`의 독립 benchmark 작업은 같은 기본 규모에서 기관 보호 조회 2초,
단일 대상/문서 보호 조회 100ms 이내와 hit/miss 반환 수를 검사합니다. 자료 수나
보호 조건을 줄여 통과시키지 않습니다. 이 기준은 CI 서버의 따뜻한 SQL 실행시간
회귀 기준이며 실제 API 응답시간 보장은 아닙니다.

## 보존 실신호의 전체 재연결 비용과 순서 수렴

같은 [최종 감사 실행](https://github.com/sokldjs554/procurement-forecast/actions/runs/36666923259)에서
기준 `a764922`와 변경 `9834b6a`를 각각 독립된 새 DB 두 곳에 재연결했습니다.
입력은 보존된 2,404신호이며 digest는
`c568e5edacc785fe676bfada96b36c08daded0944517d71a93e1d0a609f8b8ab`입니다.
각 코드의 두 묶음 파일은 바이트까지 같았습니다. 전체 원문 재추출이나 독립 정답 평가가 아닙니다.

| 공개일순 결과 | 기준 | 변경 |
| --- | ---: | ---: |
| 연결 신호 | 2,399 | 2,399 |
| 묶음 / 복수 신호 묶음 | 1,324 / 776 | 1,323 / 780 |
| 기록된 1,251묶음과 동일 | 839 | 832 |
| 두 실행 시간 | 111 / 115초 | 138 / 131초 |

기준 대비 35신호의 소속 집합이 바뀌었습니다. 기록과의 일치는 정확도가 아닙니다.
재조정은 기존 자동 결정을 전체 근거에서 다시 계산하므로 공개일순 전체 처리도
이번 실행에서 16~27초 늘었습니다. 이 비용은 SQL 조회 시간과 별도로 봐야 합니다.

| 변경 코드의 도착 순서 | 완료 후 묶음 | 공개일순 대비 소속 집합이 다른 신호 |
| --- | ---: | ---: |
| 공개일순 | 1,323 | 0 |
| 예산서 우선 | 1,323 | 0 |
| 역순(건별 입력) | 1,323 | 0 |

이전 예산우선 6개·역순 89개의 차이가 각각 0개가 됐습니다. 입력을 먼저 정렬한 뒤
비교한 것이 아니라 실제 도착 순서대로 임시 연결한 후 동일한 재조정 단계를 완료했습니다.
보호된 사람 판단과 고객 원본 근거는 조건으로 고정합니다. 모든 순서가 같은 결과를
낸다는 사실은 정답이라는 뜻이 아니며, 원문과 독립 판정으로 정확도를 따로 검증해야 합니다.
