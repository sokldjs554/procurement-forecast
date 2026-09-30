# 스텝에이아이 AI 엔지니어 공고와 현재 구현

2026-09-30 공유된 새 공고 전문과 `7408eca`(PR #55까지 반영)를 대조했다. 후속 미디어 무결성 수정은 [PR #57](https://github.com/sokldjs554/procurement-forecast/pull/57)에 기록한다. 공고 항목과 유사한 코드가 있다는 사실을 실제 고객 서비스 운영 경험이나 실데이터 정확도로 바꿔 표현하지 않는다.

## 달라진 업무 범위

기존 [데이터 수집 직무](https://groupby.kr/positions/12297)는 Python/FastAPI, Redis 작업 큐, 비정형 문서·OCR, 추천·알림이 중심이었다. 새 공고는 TypeScript/Node API·워커와 Python AI 처리의 역할을 나누고, STT·비전, ffmpeg, 외부 플랫폼 발행, 미디어 저장소와 장시간 작업의 재개를 요구한다. 우대사항의 표현만 바뀐 것으로 보기는 어렵다. [회사 채용 목록](https://groupby.kr/startups/1877)에도 데이터 수집과 미디어 처리 직무가 별도로 표시된다.

서비스 성장과 신규 프로젝트 확장에 따른 증원이라는 설명은 두 직무에 모두 있는 회사 측 설명이다. 이 문장만으로 인원수, 특정 프로젝트 배치, 합격 가능성을 추정하지 않는다.

## 코드로 보여 줄 수 있는 범위

| 새 공고의 요구 | 확인한 근거 | 현재 범위와 남은 부분 |
| --- | --- | --- |
| Python 멀티스테이지 처리 | [문서 처리](../apps/api/src/app/pipeline/process.py), [영상 입력](../apps/api/src/app/media/ingest.py) | 수집·파싱·구조화·근거 검증·연결을 구현했다. 영상은 전사와 발언자 자막을 기존 처리 경로에 넣는다. |
| 구조화 출력·결과 검증·품질 평가 | [LLM 서비스](../apps/api/src/app/llm/service.py), [평가](evaluation.md) | 스키마·원문 근거·화자·부정/조건을 검사한다. 합성 및 수기 세트 결과를 독립 실데이터 정확도로 주장하지 않는다. |
| 실패 후 재개·진행 상태 | [기존 워커](../apps/api/src/app/worker/tasks.py), [미디어 체크포인트](../apps/api/src/app/media/job.py) | 기존 작업은 Redis/ARQ와 DB 작업 이력, 영상 CLI는 단계·창별 체크포인트를 사용한다. 원본·언어·추론 설정이 다르면 이전 결과를 재사용하지 않도록 보완했다. |
| 호출 원가와 재시도 비용 | [LLM 호출 기록·캐시](../apps/api/src/app/llm/service.py), [예산 제한](../apps/api/src/app/llm/budget.py) | 호출별 사용량·비용, 일일 제한, 완료 응답 재사용을 구현했다. 미디어 비용 기록 자체를 동시 실행에서의 엄격한 건당 상한으로 설명하지 않는다. |
| ffmpeg·STT·비전 결과 정규화 | [ffmpeg](../apps/api/src/app/media/ffmpeg.py), [STT](../apps/api/src/app/media/stt.py), [자막 OCR](../apps/api/src/app/media/captions.py) | FLAC 변환·무음 기반 분할·프레임 추출·전사·발언자 자막을 처리한다. 실제 회의 STT 오류율과 화자 오귀속률은 아직 측정하지 않았다. 영상 오버레이·자동 편집 템플릿·VLM은 이 코드로 입증하지 못한다. |
| TypeScript/Node 22 API·워커와 DB 큐 | [별도 PR #56](https://github.com/sokldjs554/procurement-forecast/pull/56) | 대조 시점에는 Node/Hono, PostgreSQL 큐·리스·하트비트·취소가 별도 PR에 있다. 현재 FastAPI/ARQ 구현을 Node/DB 큐 경험으로 대체 표기하지 않는다. 해당 PR의 병합·통합 검증은 별도로 확인해야 한다. |
| Next.js·엄격한 타입·운영 콘솔 | [웹 앱](../apps/web/src/app), [작업 현황](../apps/web/src/app/admin/jobs/page.tsx), [LLM 비용](../apps/web/src/app/admin/llm/page.tsx) | 운영 콘솔과 API 연동, 정적 타입 검사와 브라우저 검증이 있다. 타임라인 편집기·실시간 영상 미리보기는 별도 요구다. |
| PostgreSQL 설계·최적화 | [모델](../apps/api/src/app/db/models.py), [대용량 실행 계획](performance.md) | 마이그레이션·보호 이력·재조정 세대, 10만 기회/40만 신호 규모 SQL을 검증했다. 실제 서비스 부하 지연과는 구분한다. |
| 검색·RAG·하이브리드 랭킹 | [검색 설계](adr/0004-postgres-only-search.md) | pgvector 기반 검색·랭킹 설계를 보여 준다. 기본 hashing 임베딩의 데모 결과를 실제 임베딩 모델의 검색 품질로 주장하지 않는다. |
| 멀티테넌시·API 키·사용량 과금 | [현재 인증](../apps/api/src/app/auth/security.py), [결제](../apps/api/src/app/billing), [별도 PR #56](https://github.com/sokldjs554/procurement-forecast/pull/56) | 기존 조직 접근 제어·구독/크레딧과 새 RLS/키 스코프/작업 원장은 서로 다른 검증 대상이다. 실제 결제 운영과 모든 테이블의 RLS 격리를 완료했다고 쓰지 않는다. |
| 외부 OAuth·발행·성과 API | 현재 이메일·메신저 알림은 [알림 모듈](../apps/api/src/app/notify) | 알림 전송은 소셜 OAuth·콘텐츠 발행·토큰 갱신·성과 조회를 대신하지 않는다. 이 부분은 별도 미충족 항목이다. |
| GCP/AWS·파일 수명주기·장애 대응 | [Terraform](../infra/terraform), [원문 저장소](../apps/api/src/app/storage.py), [운영 상태](production-status.md) | 컨테이너와 구성 검증, 원문 보존 경로가 있다. 실제 클라우드 적용, 대용량 미디어 업로드/보관 정책, GPU/spot 운영은 별도 검증이 필요하다. |

## 면접에서 설명할 핵심

이 프로젝트의 가장 강한 근거는 **불확실한 AI 출력을 근거와 함께 검증하고, 실패해도 완료한 작업과 고객의 판단을 보존하는 처리 흐름**이다. 같은 2,399신호를 다른 순서로 넣었을 때 소속 차이를 0으로 맞춘 검증, 최초 고객 근거를 보존한 채 후속 신호를 연결하는 설계, 중단 후 완료 응답을 다시 호출하지 않는 구조를 코드와 실행 기록으로 설명할 수 있다.

새 공고에는 TypeScript 서버와 Python AI 파이프라인 중 한쪽에서 자립할 수 있으면 된다고 명시돼 있다. 따라서 Python 처리·검증·재개 경험을 중심으로 설명할 수 있지만, STT/미디어 실측과 외부 발행 경험의 빈칸이 자동으로 없어지는 것은 아니다.

## 지원 자격과 제출

공유된 공고는 이어드림스쿨·KDT·폴리텍 하이테크 중 하나의 수료와 고용24 **직업훈련이력 확인원** 제출을 필수로 제시한다. 이는 코드 완성도와 별개다. 일반 부트캠프 수료만으로 충족한다고 추정하지 않으며, 실제 이력의 과정 종류를 확인해야 한다. 공유된 마감일은 **2026-10-10**이다.

공개 데모는 사용자가 담당한다. 독립 실데이터 판정·장기간 관측·실제 운영 적용이 확인되지 않은 부분은 [운영 상태](production-status.md)의 미완료 항목으로 유지한다.
