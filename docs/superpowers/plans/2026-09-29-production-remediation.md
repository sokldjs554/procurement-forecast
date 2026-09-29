# Production Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 근거·연결·검토·재검증의 확인된 결함을 수정하고 실제 운영 배포에 필요한 증거와 장애를 명시한다.

**Architecture:** 기존 데이터 모델과 품질 게이트를 유지한다. 근거 검증과 안전한 데이터 정정은 분리하고, 운영 배포는 검증된 동일 커밋으로 수행한다.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, PostgreSQL/pgvector, Redis/arq, Next.js, Render.

**Spec:** docs/production-remediation-spec.md

## Global Constraints

- 추출·연결 P/R ≥ 0.95 유지. 평가 정답·테스트 기준을 낮추지 않는다.
- 비밀값을 커밋하지 않는다. 날짜는 app.clock 사용.
- 운영 데이터 삭제·전체 재생성을 하지 않는다.
- 실제 운영 확인 전 배포 완료라고 표시하지 않는다.

## Review Focus

- 조건을 생략한 짧은 인용도 원문 문장에서 확인한다 — commitment evidence tests.
- 검토와 재추출의 경합으로 승인 기록을 잃지 않는다 — protected reprocessing tests.
- 공유 발주계획을 가진 서로 다른 공고를 합치지 않는다 — reference safety tests.
- dry-run 뒤 입력 변경을 적용하지 않는다 — stored revalidation digest tests.
- 거절된 근거가 기존 추천에 남지 않는다 — review reconciliation tests.

### Task 1: Semantic evidence and protected reprocessing

Files: domain/grounding.py, pipeline/process.py; new commitment evidence and reprocessing tests.

- [ ] 실패 회귀: 부정/조건부 발언을 committed로 추출하면 needs_review; 보호 문서 재추출 시 기존 행 보존.
- [ ] 원문 문장 의미 검사와 ReprocessingProtectedError 구현.
- [ ] 단위 검사 및 DB 통합 검사 통과.

### Task 2: Reference and candidate integrity

Files: pipeline/link.py; new link safety integration tests; bench.py, docs/performance.md.

- [ ] 실패 회귀: 타 기관·복수 충돌·공유 계획의 서로 다른 공고·유효 13번째 후보.
- [ ] 참조 모호함 확인, 기관/기간 전체 후보 검사, accepted/non-tentative 요약.
- [ ] 기존 평가와 신규 회귀 및 운영 규모 benchmark 확인.

### Task 3: Stored evidence revalidation

Files: pipeline/revalidate.py, cli.py; stored revalidation tests.

Interface: revalidate_signals(session, runtime, apply=False, expected_digest=None, limit=1000, after_id=0, source_keys=None, doc_types=None, today=None).

- [ ] dry-run 무변경, digest 불일치 실패, 검토/식별자/피드백 보존 회귀.
- [ ] 범위 제한·dry-run 기본 CLI와 명시적 apply 연결.
- [ ] 실제 운영 원문 존재 시 dry-run 결과를 검토하고 같은 digest로 적용.

### Task 4: Review reconciliation

Files: pipeline/review.py, api/routers/admin.py, worker/tasks.py; new review reconciliation tests.

Interface: reconcile_reviewed_signal(session, runtime, signal, today=None) -> list[int]. Caller commits.

- [ ] 거절 시 요약/추천 제거, 수정 후 기관 재연결, 중복 호출 회귀.
- [ ] 검토와 연결 변경을 같은 트랜잭션에서 처리하고 이력을 보존.
- [ ] 기존 대기 relink 작업도 현재 verdict를 반영하도록 변경.

### Task 5: Deploy and evidence

Files: render.yaml, scripts/render-*, docs/render-deployment.md, docs/production-status.md.

- [ ] 유료 자원·필수 비밀값·공유 저장소 요구를 확인하고 배포 전 검사 구현.
- [ ] CI 전체, 전후 eval, 실제 아카이브 replay, benchmark, 독립 리뷰 통과.
- [ ] 검증된 커밋 반영 후 지정 환경에 배포·마이그레이션·smoke 확인.
- [ ] 실데이터/권한/예산으로 미해결된 항목은 완료로 표시하지 않고 근거와 함께 기록.
