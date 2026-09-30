# AGENTS.md — 이 저장소에서 일하는 코딩 에이전트를 위한 안내

## 구조
- `apps/api` — Python 3.11, FastAPI, SQLAlchemy 2 async, arq, Alembic. 패키지 `app`, 관리 명령 `manage`.
- `apps/web` — Next.js 16 App Router, React 19, TypeScript, Tailwind v4, TanStack Query. 이 디렉터리의 `AGENTS.md`(Next.js 제공)를 먼저 읽을 것: 버전별 API가 학습 데이터와 다릅니다.
- `apps/orchestrator` — Node 22, TypeScript, Hono. 작업 API(API 키·스코프·사용량)와 다운로드 워커. 큐는 PostgreSQL이고, 스키마와 큐 함수(`jobq_*`)는 `apps/api`의 Alembic 마이그레이션이 소유합니다(ADR 0013).
- `infra/terraform` — GCP. `docs/` — ADR, 아키텍처, 평가, 런북.

## 자주 쓰는 명령
```bash
make infra                      # PostgreSQL(pgvector)·Redis·Mailpit
cd apps/api && uv sync && uv run pytest          # 통합 테스트는 위 인프라 필요
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run manage seed && uv run manage demo run && uv run manage eval all
cd apps/web && pnpm install && pnpm typecheck && pnpm lint && pnpm test && pnpm build
cd apps/orchestrator && pnpm install && pnpm typecheck && pnpm lint && DATABASE_URL=… pnpm test
make gen-api                    # FastAPI 스키마가 바뀌면 반드시 실행하고 결과를 커밋
```

## 규칙
- 파이프라인·파서·프롬프트·연결·랭킹을 바꾸면 `manage eval all` 전후 수치를 PR에 적습니다. CI 품질 게이트(추출·연결 P/R ≥ 0.95 등)를 낮추지 마세요.
- **정답 누수 금지**: 합성 레코드의 `structured["truth_id"]` 등 평가용 필드는 파이프라인 코드에서 읽지 않습니다.
- 쿼리·인덱스를 바꾸면 `make bench`로 운영 규모에서 실행 계획을 확인하고 `docs/performance.md`를 갱신합니다. 데모 규모에서는 모든 쿼리가 순차 스캔이라 문제가 보이지 않습니다.
- 크롤러는 robots.txt와 호스트별 속도 제한을 우회하지 않습니다.
- LLM 출력은 반드시 `domain/grounding.py` 검증을 거칩니다. 검증을 우회하는 경로를 만들지 마세요.
- 프롬프트·스키마·모델 설정을 바꾸면 `make eval-llm`으로 전후를 비교합니다. 실제 API 비용이 들므로 에이전트는 `--dry-run` 추정까지만 하고, 실행(로컬 또는 GitHub Actions **LLM eval** 워크플로)은 사람이 키와 `--max-usd`를 정해 승인합니다. 수기 세트(`eval/golden/realistic.jsonl`)의 정답을 모델 출력에 맞춰 고치지 않습니다.
- 시간은 `app.clock`(`now_utc`, `today_kst`, `KST`)으로. `date.today()`·naive datetime 금지.
- 금액은 원 단위 `int`. 파싱은 `domain/krw.py`만 사용.
- 과금·알림처럼 외부 효과가 있는 코드는 멱등 키를 가집니다. 새 경로를 만들면 중복 호출 테스트를 추가합니다.
- 테스트를 건너뛰거나(`skip`, `xfail`) 기준을 낮춰 CI를 통과시키지 않습니다.
- 커밋은 Conventional Commits(`feat(api): …`, `fix(web): …`). 비밀값·`.env`는 커밋하지 않습니다.
