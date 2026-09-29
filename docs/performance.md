# 대용량 쿼리 성능 (자동 생성: `manage bench`)

데이터: 공고 100,000 · 신호 400,000 · 문서 150,000 · 청크 600,000 · 추천 300,000 (회사 2,000 × 150) · 작업 기록 100,000

환경: PostgreSQL 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1) on x86_64-pc-linux-gnu, pgvector 0.6.0, x86_64, 4 CPU. 시간은 따뜻한 캐시에서 5회 실행의 중앙값이며 기계마다 다릅니다. 실행 계획과 반환 행 수·재현율이 읽어야 할 부분입니다.

벡터는 사업 유형 20개를 중심으로 뭉친 512차원 합성 임베딩입니다(실제 사업 설명 임베딩처럼). 재현율은 같은 쿼리를 인덱스 없이 전체 정렬한 정확한 결과와 비교한 값입니다.

| 쿼리 | 전 | 후 | 전: 행 / 재현율 | 후: 행 / 재현율 |
|---|---:|---:|---|---|
| 추천 후보: 회사 소개와 가까운 진행 중 공고 300건 (벡터) | 1.2 ms | 2.6 ms | 13 / 4% | 300 / 100% |
| 추천 후보: 관심 키워드가 제목·키워드에 있는 진행 중 공고 | 4.7 ms | 4.0 ms | 300 | 300 |
| 기회 연결: 처음 보는 발주계획번호로 기존 기회 찾기 (없음) | 99.5 ms | 0.02 ms | 0 | 0 |
| 기회 연결: 이미 있는 발주계획번호로 기존 기회 찾기 | 0.07 ms | 0.03 ms | 1 | 1 |
| 기회 연결: 같은 기관·기간의 가까운 기회 12건 (벡터) | 0.83 ms | 0.94 ms | 12 / 100% | 12 / 100% |
| 기회 연결: 후보 기회 12건이 가진 번호 (다른 번호면 후보에서 제외) | 0.10 ms | 0.10 ms | 48 | 48 |
| 기회 연결: 후보 기회 12건에 같은 예산서의 다른 행이 있는지 (있으면 제외) | 0.09 ms | 0.09 ms | 0 | 0 |
| 고객 피드 첫 페이지 (점수순 21건) | 0.36 ms | 0.34 ms | 21 | 21 |
| 고객 피드 첫 페이지 (입찰이 가까운 순 21건) | 0.48 ms | 0.45 ms | 21 | 21 |
| 고객 피드 첫 페이지 (새 소식 순 21건) | 0.44 ms | 0.44 ms | 21 | 21 |
| 고객 피드 단계별 건수 (칩·머리말, GROUP BY) | 0.45 ms | 0.41 ms | 5 | 5 |
| 운영 개요: 파이프라인 퍼널 집계 (5개 COUNT) | 197.3 ms | 130.2 ms | 1 | 1 |
| 배치: 처리 대기 문서 200건 (10분마다) | 0.20 ms | 0.18 ms | 200 | 200 |

## 실행 계획

### 추천 후보: 회사 소개와 가까운 진행 중 공고 300건 (벡터)

- 전: `Limit → Index Scan (ix_opportunities_embedding_hnsw)`
- 후: `Limit → Index Scan (ix_opportunities_open_embedding_hnsw)` (세션 설정: `SET LOCAL hnsw.ef_search = 300`)

### 추천 후보: 관심 키워드가 제목·키워드에 있는 진행 중 공고

- 전: `Limit → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_status_window)`
- 후: `Limit → Bitmap Heap Scan → BitmapOr → Bitmap Index Scan (ix_opportunities_title_trgm) → Bitmap Index Scan (ix_opportunities_keywords_gin)`

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

마이그레이션 0002 적용 시간(데이터가 있는 상태): 4.3초

## 2026-09-29: 백테스트 공개일 조회 검증 추가

[PR #49](https://github.com/sokldjs554/procurement-forecast/pull/49)에서 `make bench`에
`backtest_public_dates` 비교를 추가했습니다. 위 표의 기존 측정과 구분합니다.

- 전: 기회·신호의 전체 ORM 행을 읽고 신호 관측일로 정렬.
- 후: 문서를 조인해 공개일을 읽고, 승인 신호·확정 연결만 선택. 기회 ID와 신호 ID·단계·관측일·발언 강도·참조번호·문서 공개일·공개일 출처만 조회.
- 문서의 원문과 기회/신호의 임베딩은 전송하지 않습니다. 같은 긴 회의록을 신호 수만큼 반복 전송하지 않기 위해 필요한 열만 명시했습니다.
- `EXPLAIN (ANALYZE, BUFFERS)` 비교는 기존 기본 규모(문서 15만, 신호 40만, 기회 10만, 청크 60만, 추천 30만)를 유지합니다. 필터와 결과 열이 바뀌었으므로 단순 인덱스 추가에 따른 속도 향상으로 해석하지 않습니다.

재현: `make bench`. [Integrity audit 워크플로](https://github.com/sokldjs554/procurement-forecast/actions/workflows/integrity-audit.yml)의
`integrity-audit` 아티팩트에 실행 머신, 행 수, 전후 시간과 실행 계획을 담은 `performance.md`를 보관합니다.
최초 CI 시도는 HNSW 인덱스를 만드는 동안 Docker 기본 공유 메모리 한도를 초과했습니다.
검증 컨테이너에 `--shm-size=2g`를 지정했으며, 데이터 규모나 테스트 기준은 줄이지 않았습니다.
