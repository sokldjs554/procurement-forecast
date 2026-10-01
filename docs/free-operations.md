# 무료 상시 운영: GitHub Actions + 무료 PostgreSQL

수집과 처리가 테스트할 때만이 아니라 계속 돌게 하는 가장 싼 구성입니다. 비용은 0원이고, 대신 상주 서버가 없습니다. 유료 구성(상주 API·웹·worker)은 [Render 배포](render-deployment.md)를 참고하세요.

## 무엇이 도는가

[`ops-schedule.yml`](../.github/workflows/ops-schedule.yml)이 매시 23분에 GitHub Actions 러너를 하나 띄워 아래를 차례로 하고 끝납니다.

1. 호스팅된 PostgreSQL에 마이그레이션을 적용합니다(`manage db upgrade`).
2. 실제 기준 데이터만 넣습니다(`manage ops bootstrap`). 기관 사전과 키가 설정된 실데이터 수집원뿐이고, 데모 수집원·데모 계정은 넣지 않습니다. 데모 seed가 들어간 DB면 거절합니다.
3. worker의 정기 작업을 한 번 실행합니다(`manage ops tick`).
   - 상주 worker(arq)와 **같은 cron 정의와 같은 작업 함수**를 씁니다. 지난 실행 이후 실행 시각이 된 작업만 돌립니다. 나라장터는 매시 7분, 회의록은 매일 03:10, 예산서는 일요일 02:40, 처리 대기 문서·연결 정리는 수시로 돕니다.
   - 그 작업들이 이어서 만드는 작업(문서 처리 → 신호 연결 → 기관별 재정합 → 추천 → 알림)을 이 프로세스 안에서 끝까지 처리합니다. Redis 큐 대신 쓰는 큐도 arq처럼 같은 작업 ID를 한 번만 받습니다.
   - 실행 기록은 `job_runs`에 `ops_tick`으로 남습니다(운영 콘솔 → 작업 로그). 다음 실행은 이 기록이 끝난 시점부터 이어서 봅니다. 실행이 밀리거나 건너뛰어도, 그 사이 실행됐어야 할 작업을 한 번씩 따라잡습니다.
   - 같은 DB에서 두 실행이 겹치면 PostgreSQL advisory lock으로 나중 실행이 아무것도 하지 않고 끝납니다. 워크플로 `concurrency`로도 겹치지 않게 막습니다.
4. 실행 요약을 Actions 실행 화면에 남깁니다. 실행한 작업, 성공·다음 실행으로 넘김·실패 수, 처리 대기 문서 수, DB 크기가 들어갑니다.

**유료 모델은 호출하지 않습니다.** 추출은 규칙 기반(`APP_LLM_PROVIDER=heuristic`), 임베딩은 해싱으로 고정합니다. 워크플로는 Anthropic 키를 참조하지 않습니다.

## 상주 worker와 다른 점

| | 상주 worker (Render 등) | 이 구성 |
|---|---|---|
| 지연 | 매분 | 최대 1시간. GitHub 예약 실행 자체도 몇 분~수십 분 밀릴 수 있음 |
| API·웹 | 상시 제공 | 없음. 결과는 DB, 실행 요약, `job_runs`로 확인 |
| 원문 파일 | 보관(gs://) | 처리한 원문은 실행 끝에 지움. 처리 대기 문서의 원문만 Actions 캐시로 다음 실행에 넘김 |
| 재시도 | arq가 지수 백오프로 재시도 | 이번 실행에서는 기록만 하고 다음 실행의 cron·정리 작업이 다시 집음 |
| 일일 호출 한도 | Redis로 하루 단위 집계 | 실행마다 따로 집계 |

- 원문을 지우므로 파서·OCR을 고친 뒤 옛 문서를 다시 파싱하려면 제공처에서 다시 받아야 합니다. 추출 텍스트와 근거 위치는 DB에 남습니다.
- 하루 호출 수는 30일 실측(나라장터 57,514건: 발주계획 17,707 · 사전규격 9,285 · 입찰공고 30,522)으로 추정했습니다.
  - 매 실행은 직전 수집일의 3일 전부터 오늘까지(약 4일치)를 다시 받습니다. 입찰공고는 약 4,000건입니다.
  - 어댑터 기본값(한 번에 100건)이면 실행당 약 40회, 하루 900회를 넘어 기본 한도 1,000회에 가깝습니다.
  - 그래서 bootstrap이 새로 만드는 나라장터 수집원은 한 번에 999건씩 받게 합니다. 30일 실측 때와 같은 값입니다. 이러면 실행당 4~5회, 하루 약 100회입니다. 실측이 아니라 추정입니다.
- **같은 DB에 상주 worker를 함께 돌리지 마세요.** 정기 작업이 두 번 실행됩니다.

## 무료 DB 용량

나라장터만 30일에 약 5만 7천 건이 들어오고, 문서마다 텍스트·청크·512차원 벡터가 저장됩니다. 무료 PostgreSQL의 저장 한도는 보통 수백 MB~수 GB라 몇 주에서 몇 달 안에 찰 수 있습니다. 그래서 두 가지를 둡니다.

- **첫 수집은 7일치**만 받습니다. 상주 worker의 기본값은 30일입니다. 수동 실행 때 `first_window_days`로 바꿀 수 있습니다.
- **용량 보호**: 저장소 변수 `OPS_DB_LIMIT_MB`에 요금제 한도(MB)를 넣으면, DB가 그 90%에 이르는 순간 새 수집만 멈춥니다. 이미 받은 문서의 처리·연결·추천은 계속됩니다. 실행 요약에 "fetching paused (storage guard)"가 보이면 요금제를 올리거나 데이터 범위를 줄일 때입니다.

무료 한도는 제공자마다 다르고 자주 바뀌므로, 가입 화면에서 확인한 값을 그대로 넣으세요. 이 문서는 특정 수치를 보장하지 않습니다.

## 설정 순서

1. **무료 PostgreSQL 만들기.** pgvector와 pg_trgm 확장을 지원하는 곳이어야 합니다(예: Neon, Supabase). **새 DB**를 쓰세요. 데모 seed가 들어간 DB는 bootstrap이 거절합니다.
2. **저장소 Secrets 등록**(Settings → Secrets and variables → Actions → *Secrets*). 값은 채팅이나 코드에 붙이지 말고 여기에만 넣습니다.
   | 이름 | 필수 | 내용 |
   |---|---|---|
   | `OPS_DATABASE_URL` | 필수 | 제공자가 주는 연결 문자열 그대로. `postgresql://…?sslmode=require` 형식은 워크플로가 asyncpg 형식으로 바꿉니다 |
   | `APP_DATA_GO_KR_SERVICE_KEY` | 사실상 필수 | 공공데이터포털 인증키. 없으면 나라장터 3종이 꺼집니다 |
   | `APP_CLIK_API_KEY` | 선택 | 지방의정포털(CLIK) 회의록 |
   | `APP_LOFIN_API_KEY` | 선택 | 지방재정365 예산서 |
   | `APP_BILLING_KEY_ENCRYPTION_KEY`, `APP_JWT_SECRET` | 선택 | 나중에 같은 DB로 API를 운영할 때만 그쪽과 같은 값을 넣습니다. 없으면 실행마다 임시값을 씁니다(이 실행은 토큰·결제키를 쓰지 않음) |
3. **저장소 Variables 등록**(같은 화면의 *Variables*).
   - `OPS_ENABLED` = `true`: 이 값이 있어야 예약 실행이 돕니다. 지우면 멈춥니다.
   - `OPS_DB_LIMIT_MB` = 요금제 저장 한도(MB). 예: 512
4. **첫 실행.** Actions 탭 → *Scheduled operations (free tier)* → *Run workflow*. 실행 요약에서 수집·처리 수와 DB 크기를 확인합니다. 그다음부터는 매시간 저절로 돕니다.

## 로컬에서 같은 흐름 확인

```bash
cd apps/api
export APP_DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/ops_free   # 새 DB
uv run manage db upgrade && uv run manage ops bootstrap
uv run manage ops tick --report tick.json          # 지난 실행 이후 실행 시각이 된 작업
uv run manage ops tick --since 2026-10-01T16:00:00+09:00   # 특정 시각부터 다시 보기
```

`--prune-raw`는 원문 파일을 지우므로 로컬 저장소에는 쓰지 마세요. 워크플로처럼 러너가 끝나면 사라지는 저장소용입니다.

## 확인한 것

- `tests/unit/test_ops.py`
  - 실행 창별로 실행 시각이 된 작업: 매시·매일·매주, 긴 공백 뒤 한 번씩 따라잡기, UTC→KST 변환
  - worker의 모든 작업을 이름으로 실행할 수 있는지
  - 같은 작업 ID를 한 번만 받는지
  - 원문 정리가 처리 대기 문서의 원문만 남기는지
- `tests/integration/test_ops_tick.py`(실제 PostgreSQL)
  - 처리 대기 문서가 한 번의 실행에서 처리되고 `job_runs`에 기록되는지
  - 다음 실행이 그 시점부터 이어가는지
  - 실행이 겹치면 나중 실행이 아무것도 하지 않는지
  - 데모 DB를 거절하는지
- 새 로컬 DB에서 운영 설정(`APP_ENV=production`)으로 마이그레이션 → bootstrap → tick → 두 번째 tick까지 실행했습니다. 기관 517곳이 들어갔고, 두 번째 실행은 첫 실행이 끝난 시점부터 이어갔습니다. 인증키를 넣으면 나라장터 3종이 켜지고, 처음 수집 창이 7일로 잡히는 것도 확인했습니다. 다만 이 개발 환경은 외부 접속이 막혀 있어(403) 실제 수집은 첫 Actions 실행에서 확인해야 합니다.
