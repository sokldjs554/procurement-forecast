# 대용량 쿼리 성능 (자동 생성: `manage bench`)

2026-09-29 [검증 실행](https://github.com/sokldjs554/procurement-forecast/actions/runs/36557807882)의 결과입니다.
[PR #50](https://github.com/sokldjs554/procurement-forecast/pull/50)의 검증 병합 커밋은
`153f2bc042ee259ebd67c4fdff37c8d4bcade945`이며, main에 반영된 `1519ec9`의 파일 트리와 같습니다.
아래 표의 전/후는 각 벤치에 정의된 쿼리·인덱스 비교입니다. PR 전체의 속도 개선율이 아닙니다.
"가까운 기회 12건" 항목은 이전 경로의 비교용 측정이며 현재 연결기는 전체 적격 후보를 검사합니다.
Render 운영 환경의 측정값은 아닙니다.

데이터: 공고 100,000 · 신호 400,000 · 문서 150,000 · 청크 600,000 · 추천 300,000 (회사 2,000 × 150) · 작업 기록 100,000

환경: PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2) on x86_64-pc-linux-gnu, pgvector 0.8.6, x86_64, 4 CPU. 시간은 따뜻한 캐시에서 5회 실행의 중앙값이며 기계마다 다릅니다. 실행 계획과 반환 행 수·재현율이 읽어야 할 부분입니다.

시간은 PostgreSQL EXPLAIN ANALYZE의 서버 실행 시간입니다. 네트워크 전송, 벡터 디코딩, ORM 객체 생성과 Python 연결 점수 계산은 포함하지 않으므로 전체 연결 시간은 replay로 따로 측정합니다.

벡터는 사업 유형 20개를 중심으로 뭉친 512차원 합성 임베딩입니다(실제 사업 설명 임베딩처럼). 재현율은 같은 쿼리를 인덱스 없이 전체 정렬한 정확한 결과와 비교한 값입니다.

| 쿼리 | 전 | 후 | 전: 행 / 재현율 | 후: 행 / 재현율 |
|---|---:|---:|---|---|
| 기회 연결: 기관·기간 내 전체 후보 (전: 벡터 포함, 후: 메타데이터만) | 0.84 ms | 0.96 ms | 132 / 100% | 132 / 100% |
| 기회 연결: 구조 필터를 통과한 후보의 벡터 일괄 조회 (4건 예시) | 0.01 ms | 0.01 ms | 4 | 4 |
| 기회 연결: 기관·승인·확정 조건의 모든 번호 일치 대상 (모호성 검사) | 1.7 ms | 0.20 ms | 1 | 1 |
| 기회 연결: 전체 적격 후보의 승인된 확정 번호 (계약 충돌 검사) | 2.1 ms | 2.4 ms | 528 | 528 |
| 저장 신호 재검증: 수집원·문서 유형·승인 조건의 ID 커서 1,001건 | 4.4 ms | 4.5 ms | 1001 | 1001 |
| 백테스트: 승인된 확정 연결의 공개일·첫 신호 조회 | 978.8 ms | 388.4 ms | 400000 | 392000 |
| 추천 후보: 회사 소개와 가까운 진행 중 공고 300건 (벡터) | 0.66 ms | 50.2 ms | 12 / 4% | 300 / 100% |
| 추천 후보: 관심 키워드가 제목·키워드에 있는 진행 중 공고 | 4.1 ms | 5.1 ms | 300 | 300 |
| 기회 연결: 처음 보는 발주계획번호로 기존 기회 찾기 (없음) | 81.9 ms | 0.01 ms | 0 | 0 |
| 기회 연결: 이미 있는 발주계획번호로 기존 기회 찾기 | 0.13 ms | 0.02 ms | 1 | 1 |
| 기회 연결: 같은 기관·기간의 가까운 기회 12건 (벡터) | 0.68 ms | 0.70 ms | 12 / 100% | 12 / 100% |
| 기회 연결: 후보 기회 12건이 가진 번호 (다른 번호면 후보에서 제외) | 0.09 ms | 0.08 ms | 48 | 48 |
| 기회 연결: 후보 기회 12건에 같은 예산서의 다른 행이 있는지 (있으면 제외) | 0.09 ms | 0.08 ms | 0 | 0 |
| 고객 피드 첫 페이지 (점수순 21건) | 0.28 ms | 0.35 ms | 21 | 21 |
| 고객 피드 첫 페이지 (입찰이 가까운 순 21건) | 0.36 ms | 0.42 ms | 21 | 21 |
| 고객 피드 첫 페이지 (새 소식 순 21건) | 0.41 ms | 0.34 ms | 21 | 21 |
| 고객 피드 단계별 건수 (칩·머리말, GROUP BY) | 0.35 ms | 0.38 ms | 5 | 5 |
| 운영 개요: 파이프라인 퍼널 집계 (5개 COUNT) | 178.0 ms | 124.8 ms | 1 | 1 |
| 배치: 처리 대기 문서 200건 (10분마다) | 0.12 ms | 0.14 ms | 200 | 200 |

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
- 후: `Nested Loop → Aggregate → Sort → Bitmap Heap Scan → Bitmap Index Scan (ix_opportunities_institution) → Index Scan (opportunity_signals_pkey) → Index Scan (signals_pkey)`

### 저장 신호 재검증: 수집원·문서 유형·승인 조건의 ID 커서 1,001건

- 전: `Limit → Nested Loop → Index Scan (signals_pkey) → Memoize → Index Scan (documents_pkey) → Materialize → Seq Scan (sources)`
- 후: `Limit → Nested Loop → Index Scan (signals_pkey) → Memoize → Index Scan (documents_pkey) → Materialize → Seq Scan (sources)`

### 백테스트: 승인된 확정 연결의 공개일·첫 신호 조회

- 전: `Gather Merge → Incremental Sort → Merge Join → Nested Loop → Index Only Scan (opportunity_signals_pkey) → Index Scan (signals_pkey) → Index Scan (opportunities_pkey)`
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

마이그레이션 0002 적용 시간(데이터가 있는 상태): 4.1초

## 실제 아카이브 재연결 시간과 한계

같은 CI 환경에서 보존된 신호 2,404건 중 2,399건을 연결했습니다. 각 실행은 독립된 새 DB를 사용합니다.

| 코드 | 첫 실행 | 두 번째 실행 | 그룹 수 |
| --- | ---: | ---: | ---: |
| 기준 `043abdd`, 상위 12개 제한 | 29초 | 29초 | 1,387 |
| 변경 `6470244`, 전체 적격 후보 | 134초 | 132초 | 1,323 |

각 코드에서 두 번의 전체 그룹 JSON은 완전히 같았습니다. 제목 캐시 적용 전후의 변경 코드 그룹도 같습니다.
그러나 전체 후보를 검사하는 변경 경로는 기준보다 느립니다. 아래의 개별 함수 CPU 개선을 전체 연결 속도
개선이라고 주장하지 않습니다. 더 많은 후보를 검토하는 비용이며 실제 운영 처리량·메모리 측정은 남아 있습니다.
정답이 없는 그룹 변화와 반복 재현성은 실데이터 정확도가 향상됐다는 증거가 아닙니다.

이전 감사에서는 첫 replay가 157초에 끝난 뒤, 롤백한 같은 DB의 두 번째 replay가 24분 이상 지연되어
30분 제한으로 종료됐습니다. 롤백은 물리적 테이블/인덱스 상태와 통계를 초기화하지 않으므로,
감사에서 매회 새 DB를 초기화하도록 바꿨습니다. 위 두 실행이 모두 약 2분에 끝난 결과가 그 검증입니다.
운영 DB의 통계·vacuum·쓰기 부하까지 해결됐다는 뜻은 아닙니다.

## 제목 비교의 반복 계산 제거: 로컬 CPU 측정

동일한 Python 3.11 작업 컨테이너에서 보존 파일
`docs/data/seongnam-link-signals.jsonl.gz`의 예산 신호 2,329건을 파일 순서대로 읽고,
`islice(combinations(titles, 2), 10000)`으로 첫 10,000쌍을 비교했습니다. 이 쌍에는
서로 다른 제목 934개가 들어 있습니다. DB·네트워크·임베딩 조회 없이 함수 호출만
각각 3회 측정했으며, 캐시는 매 실행 전에 비웠습니다.

| 함수 | 변경 전 중앙값 | 캐시 적용 후 중앙값 |
|---|---:|---:|
| `budget_names_agree` | 1.770초 | 0.0847초 |
| `title_similarity` | 1.111초 | 0.0884초 |

10,000쌍의 이름 일치 여부, 유사도 실수값, 용어 충돌 여부는 변경 전과 모두 같았습니다.
별도 프로파일에서는 이 비교에 정규식 치환 약 396만 회가 발생하여, 같은 제목의 정규화가
반복되는 것이 주된 CPU 비용임을 확인했습니다. 위 수치는 전체 replay나 운영 처리량의
개선율이 아닙니다. 실제 전체 연결 시간은 CI replay 결과로 별도 확인합니다.

제목별 정규화 문자열·2/3글자 묶음·원제목의 용어 집합을 변경 불가능한 값으로 보관합니다.
프로세스당 LRU 항목은 최대 2,048개이며 256자를 넘는 제목은 저장하지 않고 계산합니다.
후보 제한, 점수식, 판정 기준은 바꾸지 않았습니다. 원제목의 용어와 정규화한 제목의
글자 묶음을 구분하여 `[CCTV]` 같은 머리표의 기존 의미도 그대로 유지합니다.
