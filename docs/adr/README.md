# Architecture Decision Records

결정 하나에 문서 하나. 형식: 맥락 → 결정 → 결과(얻은 것 / 치른 비용) → 다시 볼 조건.

| # | 결정 | 상태 |
|---|---|---|
| [0001](0001-pre-tender-signals.md) | 주제: 공고가 아니라 공고 이전 신호를 판다 | 채택 |
| [0002](0002-grounded-extraction.md) | LLM 추출은 원문 근거 검증을 통과해야만 쓴다 | 채택 |
| [0003](0003-arq-and-in-worker-cron.md) | 작업 큐는 arq(Redis), 스케줄은 워커 안의 cron | 채택 |
| [0004](0004-postgres-only-search.md) | 검색은 PostgreSQL 하나로: pgvector + pg_trgm | 채택 |
| [0005](0005-credit-ledger.md) | 크레딧은 추가 전용 원장 + 멱등 키 | 채택 |
| [0006](0006-one-model-low-effort.md) | 추출 모델: 가장 좋은 모델 하나를 낮은 effort로 | 채택 |
| [0007](0007-synthetic-world-evaluation.md) | 평가는 정답을 심은 합성 세계 + 수기 세트 + 백테스트 | 채택 |
| [0008](0008-bff-and-typed-client.md) | 웹은 BFF 프록시 + httpOnly 쿠키 + OpenAPI 생성 타입 | 채택 |
| [0009](0009-polite-board-crawler.md) | 지자체 누리집 게시판은 예의 바른 크롤러로 수집한다 | 채택 |
| [0010](0010-measure-at-volume.md) | 쿼리는 운영 규모 데이터에서 측정하고 고친다 | 채택 |
| [0011](0011-provider-codes-for-institutions.md) | 조달청 문서의 기관은 조달청 코드로 식별한다 | 채택 |
| [0012](0012-meeting-video-as-minutes.md) | 회의 영상은 회의록 모양의 전사로 바꿔 같은 파이프라인에 넣는다 | 채택 |
| [0013](0013-postgres-job-queue.md) | 긴 작업은 Postgres 큐로: 의미는 SQL 함수에, 테넌트 격리는 RLS로 | 채택 |
