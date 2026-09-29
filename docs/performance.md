# 대용량 쿼리 성능 (자동 생성: `manage bench`)

2026-09-29 [PR #53 검증 실행](https://github.com/sokldjs554/procurement-forecast/actions/runs/36567195391)의 결과입니다.
검증 병합 `be55e15cd99e016e85346ff3b574ecaaea2f123a`와 main의 `dfec29a2fe44cc197f259ad309f5974b44d0b7a9`는 같은 파일 트리입니다.
아래 전/후는 벤치가 정의한 쿼리·인덱스 비교이며 PR 전체의 속도 개선율이 아닙니다.
가까운 후보 12건은 이전 경로의 비교용 측정입니다. 현재 연결기는 전체 적격 후보를 검사합니다. 실제 배포 환경 측정은 아닙니다.

데이터: 공고 100,000 · 신호 400,000 · 문서 150,000 · 청크 600,000 · 추천 300,000 (회사 2,000 × 150) · 작업 기록 100,000

신규 쿼리는 기존 전후 비교가 끝난 뒤 head 전용 합성 fixture를 추가하여 측정했습니다. 관계 50,000 · 관계 이력 100,000 · 리뷰 20,000 · 수집 실행 10,000. 혼합 감사 페이지용으로 기존 1,000개 그룹의 승인 신호 2,000건 단계만 이 추가 측정 전에 바꿨습니다.

커버리지 SQL 입력은 문서 150,000건·수집원 1개이며, fixture 제외 조건을 통과시키는 비활성 벤치 전용 adapter `benchmark`를 사용합니다. 원문·기관별 실제 수집·사람 검토 정답·사업/계약 관계의 의미적 정확성을 검증하는 데이터가 아닙니다. 파일 읽기·원문 유효성 검사·보고서 Python 집계 비용도 SQL 시간에 포함하지 않습니다. 신규 기능의 전 결과는 미구현으로 표시하며 속도 개선율을 계산하지 않습니다.

환경: PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2) on x86_64-pc-linux-gnu, pgvector 0.8.6, x86_64, 4 CPU. 시간은 따뜻한 캐시에서 5회 실행의 중앙값이며 기계마다 다릅니다. 실행 계획과 반환 행 수·재현율이 읽어야 할 부분입니다.

시간은 PostgreSQL EXPLAIN ANALYZE의 서버 실행 시간입니다. 네트워크 전송, 벡터 디코딩, ORM 객체 생성과 Python 연결 점수 계산은 포함하지 않으므로 전체 연결 시간은 replay로 따로 측정합니다.

벡터는 사업 유형 20개를 중심으로 뭉친 512차원 합성 임베딩입니다(실제 사업 설명 임베딩처럼). 재현율은 같은 쿼리를 인덱스 없이 전체 정렬한 정확한 결과와 비교한 값입니다.

| 쿼리 | 전 | 후 | 전: 행 / 재현율 | 후: 행 / 재현율 |
|---|---:|---:|---|---|
| 기회 연결: 기관·기간 내 전체 후보 (전: 벡터 포함, 후: 메타데이터만) | 0.78 ms | 0.79 ms | 132 / 100% | 132 / 100% |
| 기회 연결: 구조 필터를 통과한 후보의 벡터 일괄 조회 (4건 예시) | 0.02 ms | 0.02 ms | 4 | 4 |
| 기회 연결: 기관·승인·확정 조건의 모든 번호 일치 대상 (모호성 검사) | 1.8 ms | 0.20 ms | 1 | 1 |
| 기회 연결: 전체 적격 후보의 승인된 확정 번호 (계약 충돌 검사) | 2.9 ms | 2.1 ms | 528 | 528 |
| 저장 신호 재검증: 수집원·문서 유형·승인 조건의 ID 커서 1,001건 | 5.1 ms | 5.0 ms | 1001 | 1001 |
| 백테스트: 승인된 확정 연결의 공개일·첫 신호 조회 | 1129.5 ms | 451.6 ms | 400000 | 392000 |
| 추천 후보: 회사 소개와 가까운 진행 중 공고 300건 (벡터) | 0.78 ms | 56.4 ms | 11 / 4% | 300 / 100% |
| 추천 후보: 관심 키워드가 제목·키워드에 있는 진행 중 공고 | 4.3 ms | 5.3 ms | 300 | 300 |
| 기회 연결: 처음 보는 발주계획번호로 기존 기회 찾기 (없음) | 94.5 ms | 0.02 ms | 0 | 0 |
| 기회 연결: 이미 있는 발주계획번호로 기존 기회 찾기 | 0.15 ms | 0.02 ms | 1 | 1 |
| 기회 연결: 같은 기관·기간의 가까운 기회 12건 (벡터) | 0.77 ms | 0.63 ms | 12 / 100% | 12 / 100% |
| 기회 연결: 후보 기회 12건이 가진 번호 (다른 번호면 후보에서 제외) | 0.10 ms | 0.10 ms | 48 | 48 |
| 기회 연결: 후보 기회 12건에 같은 예산서의 다른 행이 있는지 (있으면 제외) | 0.09 ms | 0.09 ms | 0 | 0 |
| 고객 피드 첫 페이지 (점수순 21건) | 0.25 ms | 0.26 ms | 21 | 21 |
| 고객 피드 첫 페이지 (입찰이 가까운 순 21건) | 0.35 ms | 0.36 ms | 21 | 21 |
| 고객 피드 첫 페이지 (새 소식 순 21건) | 0.36 ms | 0.35 ms | 21 | 21 |
| 고객 피드 단계별 건수 (칩·머리말, GROUP BY) | 0.36 ms | 0.35 ms | 5 | 5 |
| 운영 개요: 파이프라인 퍼널 집계 (5개 COUNT) | 203.0 ms | 138.9 ms | 1 | 1 |
| 배치: 처리 대기 문서 200건 (10분마다) | 0.13 ms | 0.15 ms | 200 | 200 |
| 수집 현황: 전체 문서의 좁은 메타데이터 투영 | 미구현 | 59.6 ms | — | 150000 |
| 수집 현황: 수집원별 최신 실행 기록 | 미구현 | 2.6 ms | — | 1 |
| 사업·계약 관계: 양쪽 ID + 커서 51건 | 미구현 | 1.3 ms | — | 51 |
| 사업·계약 관계: 양쪽 ID + 확정 상태 + 커서 51건 | 미구현 | 0.08 ms | — | 51 |
| 사업·계약 관계: 제안 상태 + ID 커서 51건 | 미구현 | 0.04 ms | — | 51 |
| 관계 이력: 문서 근거 재처리 보호 JSONB (hit) | 미구현 | 0.02 ms | — | 1 |
| 관계 이력: 문서 근거 재처리 보호 JSONB (miss) | 미구현 | 0.01 ms | — | 0 |
| 기존 혼합 그룹 감사: 사업·계약 단계 HAVING + 커서 51건 | 미구현 | 0.53 ms | — | 51 |
| 사람 검토 내보내기: 리뷰·신호·문서·청크 첫 501건 (벡터 제외) | 미구현 | 1.9 ms | — | 501 |

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

- 전: `Nested Loop → Unique → Sort → Subquery Scan → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution) → Index Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`
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

### 수집 현황: 전체 문서의 좁은 메타데이터 투영

- 전: 미구현
- 후: `Nested Loop → Seq Scan (sources) → Seq Scan (documents)`

### 수집 현황: 수집원별 최신 실행 기록

- 전: 미구현
- 후: `Unique → Sort → Seq Scan (ingest_runs)`

### 사업·계약 관계: 양쪽 ID + 커서 51건

- 전: 미구현
- 후: `Limit → Index Scan (opportunity_relations_pkey)`

### 사업·계약 관계: 양쪽 ID + 확정 상태 + 커서 51건

- 전: 미구현
- 후: `Limit → Sort → Bitmap Heap Scan → BitmapOr → Bitmap Index Scan (ix_relations_project_status) → Bitmap Index Scan (uq_relation_confirmed_contract)`

### 사업·계약 관계: 제안 상태 + ID 커서 51건

- 전: 미구현
- 후: `Limit → Index Scan (opportunity_relations_pkey)`

### 관계 이력: 문서 근거 재처리 보호 JSONB (hit)

- 전: 미구현
- 후: `Limit → Bitmap Heap Scan → Bitmap Index Scan (ix_relation_events_evidence)`

### 관계 이력: 문서 근거 재처리 보호 JSONB (miss)

- 전: 미구현
- 후: `Limit → Bitmap Heap Scan → Bitmap Index Scan (ix_relation_events_evidence)`

### 기존 혼합 그룹 감사: 사업·계약 단계 HAVING + 커서 51건

- 전: 미구현
- 후: `Limit → Aggregate → Nested Loop → Index Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`

### 사람 검토 내보내기: 리뷰·신호·문서·청크 첫 501건 (벡터 제외)

- 전: 미구현
- 후: `Limit → Nested Loop → Index Scan (review_items_pkey) → Index Scan (signals_pkey) → Index Scan (documents_pkey) → Index Scan (document_chunks_pkey)`

마이그레이션 0001 → head 적용 시간(데이터가 있는 상태): 5.0초

## 보존 실신호의 전체 재연결 비용과 순서 민감도

같은 아카이브 2,404신호를 기준 `2d3e7bb`와 변경 `d4a32b4`에서 각각 독립된 새 DB 두 곳에 재연결했습니다. 두 실행씩의 묶음 파일은 각각 바이트까지 같습니다. 전체 원문 재추출이나 정답 정확도 검사는 아닙니다.

| 공개일순 결과 | 기준 | 변경 |
|---|---:|---:|
| 연결 신호 | 2,399 | 2,399 |
| 묶음 / 복수 신호 묶음 | 1,323 / 775 | 1,324 / 776 |
| 기록된 1,251묶음과 동일 | 844 | 839 |
| 두 실행 시간 | 152 / 152초 | 166 / 159초 |

기준 대비 21신호의 소속 집합이 달라졌습니다. 기록과의 일치는 정답률이 아닙니다. 관할 구 충돌 제약이 추가됐으며 전체 실행시간도 늘었습니다.

| 변경 코드의 순서 | 묶음 | 공개일순 대비 소속 집합이 다른 신호 |
|---|---:|---:|
| 공개일순 | 1,324 | 0 |
| 예산서 우선 | 1,326 | 6 |
| 역순(건별 입력) | 1,322 | 89 |

순서 비교의 분모는 연결된 2,399신호이고 입력 digest는 세 경우 같습니다. **반복 재현성은 확인했지만 도착 순서 독립성은 성립하지 않습니다.** 차이가 난 사례는 원문을 대조해 판정해야 하며, 임의로 기존 검토 기록을 지우거나 합쳐 동일 결과를 만들지 않았습니다. 서비스 요청 전체의 메모리·네트워크·큐 대기시간은 이 SQL 벤치에서 측정하지 않았습니다.
