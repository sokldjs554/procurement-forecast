# orchestrator — 작업 API와 다운로드 워커 (TypeScript · Node 22 · Hono)

고객 시스템이 API 키로 회의 영상 작업을 맡기고, 진행률·비용·결과를 조회하는 서비스입니다. 큐는 PostgreSQL 하나이고, 큐의 의미(가져가기·리스·하트비트·백오프·회수·취소)는 `apps/api` 마이그레이션 0005의 SQL 함수(`jobq_*`)에 있습니다. 그래서 이 서비스와 Python 단계 워커가 같은 정의를 씁니다([ADR 0013](../../docs/adr/0013-postgres-job-queue.md)).

```
고객 ──POST /v1/media-jobs──▶ Hono API ──jobq_enqueue──▶ jobs (media.fetch)
                                            │
       TypeScript 다운로드 워커 ◀──jobq_claim──┘   (스트리밍 해시, Range 재개, 허용 호스트만)
                │ 파일 저장 후 자식 작업
                ▼
       jobs (media.transcribe) ──▶ Python 워커 (`manage queue worker`)
                                    STT · 자막 OCR · 추출 · 근거 검증 · 연결 · 사용량 기록
```

## API
모든 `/v1` 요청에는 `Authorization: Bearer pfk_xxxxxxxx.<secret>`이 필요합니다. 키는 `manage apikey create --org <id> --name <용도> --scope …`로 만들고, 만들 때 한 번만 출력됩니다. 저장되는 것은 비밀값의 SHA-256뿐입니다. 요청은 키의 조직으로 `SET LOCAL ROLE app_tenant` 아래에서 실행되고, 행 수준 보안이 다른 조직의 행을 막습니다. 오류는 RFC 9457 problem 문서로 돌려줍니다.

| 메서드 · 경로 | 스코프 | 설명 |
|---|---|---|
| `POST /v1/media-jobs` | `jobs:write` | 영상 URL과 회의 정보(`title`, `meeting_date`, 선택 `publisher`·`institution`·`published_at`·`budget_usd`)를 받습니다. 같은 URL(또는 같은 `Idempotency-Key`)이면 기존 작업을 돌려줍니다. 새 작업은 202, 기존 작업은 200입니다. |
| `GET /v1/jobs/{id}` | `jobs:read` | 작업과 그 아래 단계(`stages`): 상태, 진행률, 시도 횟수, 비용, 결과(신호와 영상 초). |
| `GET /v1/jobs?status=&limit=&before=` | `jobs:read` | 최근 작업부터 페이지로 돌려줍니다. |
| `POST /v1/jobs/{id}/cancel` | `jobs:write` | 작업과 하위 단계를 취소합니다. 대기 중이면 즉시(200), 실행 중이면 다음 하트비트에(202). |
| `GET /v1/usage?month=YYYY-MM` | `usage:read` | 한국 시간 기준 월의 미터별 사용량·금액과 조직의 월 한도. |
| `GET /healthz` | — | DB 연결 확인. |

## 다운로드 단계(`media.fetch`)의 방어
- `FETCH_ALLOWED_HOSTS`에 있는 호스트만 받습니다. 리다이렉트도 한 번씩 다시 검사합니다. 기본값은 빈 목록이라 아무것도 받지 않습니다.
- https만 허용합니다(`FETCH_ALLOW_INSECURE_HTTP`는 테스트용). URL 안의 계정 정보는 거부합니다.
- 크기 상한(`FETCH_MAX_BYTES`)을 넘거나 HTML·JSON 응답이 오면 재시도 없이 실패합니다.
- 끊긴 다운로드는 다음 시도에서 `Range`로 이어 받습니다. 서버가 Range를 무시하면 처음부터 다시 받습니다. 해시는 파일 전체의 SHA-256이고, 전사 단계의 중복 제거와 체크포인트 키로 씁니다.

## 설정
| 변수 | 기본값 | |
|---|---|---|
| `DATABASE_URL` | (필수) | `postgres://…` — API 마이그레이션이 적용된 DB |
| `ROLE` | `all` | `api` · `worker` · `all` |
| `PORT` | `8787` | |
| `MEDIA_STORAGE_DIR` | `./.data/media-in` | 받은 영상 (`org<id>/<sha256>.<ext>`) |
| `FETCH_ALLOWED_HOSTS` | (빈 값) | 쉼표로 구분 |
| `FETCH_MAX_BYTES` | 4 GiB | |
| `STT_SPEC` | `faster-whisper` | 전사 단계가 쓸 엔진. 배포가 정하고, 고객 입력으로는 바뀌지 않습니다. |
| `LEASE_SECONDS` · `HEARTBEAT_SECONDS` · `POLL_SECONDS` · `REAP_SECONDS` | 60 · 15 · 5 · 30 | |

## 개발
```bash
pnpm install
pnpm typecheck && pnpm lint
# 테스트 DB: 스키마는 API의 Alembic 마이그레이션이 만듭니다
(cd ../api && APP_DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/orchestrator_test uv run manage db upgrade)
DATABASE_URL=postgres://app:app@localhost:5432/orchestrator_test pnpm test
# 두 언어를 잇는 E2E (시드된 DB, ffmpeg, tesseract 필요)
pnpm build && APP_DATABASE_URL=… bash ../../scripts/e2e-media-queue.sh
```
