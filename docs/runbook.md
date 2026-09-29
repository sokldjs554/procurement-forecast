# 운영 런북

증상 → 확인할 곳 → 조치 순서입니다. 대부분은 운영 콘솔(`/admin`)에서 시작합니다.

## 수집

**수집원이 "서킷 열림" / 연속 실패**
1. *수집원* 화면의 마지막 오류를 봅니다. `FatalSourceError`(키·파라미터 오류 코드 10~33)는 재시도로 해결되지 않습니다: 키 만료, 서비스 신청 누락(조달청은 오퍼레이션별 활용 신청), 필드·경로 변경을 의심합니다.
2. 경로·필드가 바뀌었으면 `sources.config`에서 덮어쓰고 *지금 수집*으로 확인합니다([data-sources.md](data-sources.md)).
3. 제공처 장애면 기다립니다. 서킷은 쿨다운(기본 10분) 후 탐침 한 번으로 스스로 닫힙니다.

**"daily quota exhausted"**
- 정상 동작입니다. 작업이 KST 자정 이후로 재예약됩니다. 백필 중이라면 `window` 범위를 줄여 나눠 돌리고, 운영 키(트래픽 상향)를 신청합니다.

## 추출·LLM

**LLM 비용이 일 한도에 닿음 / 강등(degraded) 비율 상승**
1. *LLM 비용* 화면에서 작업·모델·프롬프트 버전별 비용과 캐시 적중률을 확인합니다. 캐시 적중률이 갑자기 떨어졌다면 시스템 프롬프트가 바뀌어 캐시 접두가 깨진 것입니다.
2. 트리아지 통과율이 올랐는지 확인합니다(새 수집원이 절차 발언이 많은 회의록을 대량으로 넣는 경우).
3. 필요하면 `APP_LLM_DAILY_BUDGET_USD`를 올립니다. 한도 초과 동안의 문서는 규칙 기반 추출기로 처리되어 `degraded:budget_exceeded` 사유로 검토 대기열에 들어가 있습니다.

**작업이 `LLMSetupError`로 실패 / `manage pipeline run`이 `stopped`로 끝남**
1. 오류 문구로 원인을 봅니다. `auth`는 키(`APP_ANTHROPIC_API_KEY`)나 권한, `out of credit`은 Anthropic 계정 잔액, `not found`는 모델 이름(`APP_LLM_EXTRACT_MODEL`)입니다.
2. 고치는 동안 문서는 `pending`으로 남고 규칙 기반으로 처리되지 않습니다. 새 신호가 늦어질 뿐, 품질이 낮은 신호가 쌓이지는 않습니다. 멈추기 전에 이미 비용을 낸 응답은 캐시(`llm_cache`)와 호출 기록(`llm_calls`)에 남아, 다시 돌릴 때 같은 청크에는 비용이 들지 않습니다.
3. 고친 뒤 `manage pipeline run`을 돌리거나 *작업 로그*에서 재실행합니다. 워커는 실패한 작업 결과를 한 시간 보관한 뒤 `sweep_pending_documents`가 같은 문서를 다시 넣습니다. 규칙 기반으로라도 먼저 돌려야 하면 `APP_LLM_PROVIDER=heuristic`으로 돌립니다.

**측정·임시 DB를 버리기 전**
- `manage llm-cache export <파일>.jsonl.gz`로 비용을 낸 추출 응답을 파일로 빼 둡니다. 새 DB에서는 `manage llm-cache import <파일>`로 넣으면 같은 청크가 캐시로 처리됩니다. 파일에는 캐시 키와 응답만 들어가고, 키가 모델·effort·프롬프트·청크 글자를 묶고 있어 설정이 달라지면 쓰이지 않을 뿐 틀린 응답이 나가지는 않습니다.

**검토 대기열이 쌓임**
- 사유별로 봅니다. `institution_unresolved`가 대부분이면 기관 사전(`domain/data/institutions.csv`)에 별칭을 추가하거나, 행정구역이 바뀌었다면 `scripts/build_institutions.py`로 사전을 다시 만듭니다. 배포 뒤 `manage pipeline reresolve` → `manage pipeline run`으로 이미 쌓인 문서의 기관을 다시 풀고 처리합니다(다시 받지 않음). `year_unverified`가 대부분이면 시점 해석기(`domain/timing.py`)에 표현을 추가하고 수기 평가 세트에 사례를 넣습니다.
- 승인/수정하면 해당 신호의 연결 작업이 다시 돕니다.

## 작업 큐

**워커 헬스체크 503 / 인스턴스 재시작 반복**
- `/healthz`는 arq의 Redis 하트비트가 끊기면 503입니다. Redis 연결(Memorystore 상태, AUTH 문자열 시크릿)과 워커 로그의 시작 오류를 확인합니다.

**Redis 재시작 후 작업 유실**
- 문서는 PostgreSQL에서 `pending`으로 남아 있으므로 10분마다 도는 `sweep_pending_documents`가 다시 넣습니다. 급하면 *작업 로그*에서 해당 작업을 재실행합니다.

## 결제

**결제 승인 응답을 받지 못함(타임아웃)**
- 결제 행은 호출 전에 `orderId`로 먼저 만들어지고, 승인 호출은 `orderId`를 `Idempotency-Key`로 씁니다. 같은 주문을 다시 호출해도 이중 청구되지 않습니다.
- 토스의 결제 상태 변경 웹훅(`/api/webhooks/toss`, `PAYMENT_STATUS_CHANGED`)을 받으면 `orderId`로 토스 API를 조회해 결제 행과 크레딧을 맞춥니다. 웹훅이 오지 않은 건은 토스 개발자센터에서 웹훅 재전송을 요청합니다.

**정기결제 갱신 실패**
- 1·3·7일 후 자동 재시도(`past_due`), 모두 실패하면 구독을 해지하고 무료 플랜으로 내립니다. 카드 교체는 고객이 *요금·크레딧* 화면에서 합니다.

## 알림

**채널이 "꺼짐"으로 바뀜**
- 영구 오류(폐기된 Slack 웹훅, 잘못된 번호)로 자동 비활성화된 것입니다. 발송 기록의 오류를 고객에게 안내하고, 고객이 채널을 다시 등록합니다.

## 배포·롤백

- 배포 순서: 이미지 → 마이그레이션 Job → 워커 → API → 웹. 마이그레이션은 **확장 → 코드 배포 → 축소** 두 단계로 나눠, 직전 리비전이 새 스키마에서도 돌게 작성합니다.
- 롤백: `gcloud run services update-traffic app-api --to-revisions <이전 리비전>=100` (웹·워커도 동일). 스키마는 되돌리지 않습니다.
- DB 복구: Cloud SQL PITR(7일)로 새 인스턴스를 만든 뒤 `database-url` 시크릿의 새 버전을 추가하고 서비스를 재배포합니다.

## 비밀값 교체

```bash
printf '%s' "$NEW_VALUE" | gcloud secrets versions add <secret-id> --data-file=-
gcloud run services update app-api --region asia-northeast3 --update-labels rotated=$(date +%s)
```

`APP_BILLING_KEY_ENCRYPTION_KEY`는 교체 전에 저장된 빌링키를 새 키로 재암호화해야 합니다(`MultiFernet` 이행 스크립트가 필요, 아직 없음).
