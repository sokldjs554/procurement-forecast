# 무료 로컬 모델 추출

외부 API 과금 없이 CPU에서 언어모델을 실행하는 **선택 기능**이다. 공개 데모나 상시 서버 배포가 아니다. 기본 규칙 추출기는 그대로이며 `APP_LLM_PROVIDER=local_llama`로 선택한다. 원문 근거 검증을 적용하고, 모든 로컬 모델 신호는 기존 검토함으로 보낸다. 재검증으로 자동 승인되지 않는다.

## 검증한 모델과 범위

- Qwen3-4B Q4_K_M, 모델 저장소 revision `bc640142c66e1fdd12af0bd68f40445458f3869b`.
- GGUF SHA256 `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`.
- llama.cpp b11308 / commit `feb9a3d6debb3a8544052b04c84fa1f445fd77f5`.
- 검증 환경: Linux x86_64, RAM 9.7 GiB, CPU 6스레드, context 8192, GPU 미사용.
- API 비용은 0달러다. 호스트 장비·전력 비용을 측정한 것은 아니다. 토큰 사용량은 별도로 기록한다.
- 기존 `extract-v3` 프롬프트·`signal-v3` 스키마, thinking 비활성, seed 42를 사용한다. 수치·단위와 근거는 운영 검증기로 다시 확인한다.

첫 실문서 진단 표본은 정답 20개 중 14개를 식별했으나 정밀도 58.33%, 음성 사례 오탐 5/8이었다. 이는 추출 후보를 넓힐 수 있다는 결과이며 정확도 해결이나 자동 승인 적합성의 증거가 아니다. 전체 요청·응답·실패 및 모델 선택 경위는 [후속 검증 기록](data/followup-2026-10-01/README.md)에 보존한다. 독립 전문가 정답 또는 전국 대표 표본은 아니다.

## 설정

```dotenv
APP_LLM_PROVIDER=local_llama
APP_LLM_LOCAL_BASE_URL=http://127.0.0.1:18080
APP_LLM_LOCAL_TIMEOUT_SECONDS=480
APP_LLM_DAILY_BUDGET_USD=0
APP_WORKER_MAX_JOBS=1
APP_EMBEDDING_PROVIDER=hashing
```

`APP_LLM_EXTRACT_MODEL` 및 Claude effort 설정은 이 제공자에 적용되지 않는다. 모델과 샘플링 프로파일은 코드로 고정한다. 브리프는 기존 사실 기반 템플릿을 사용한다. 외부 호스트·DNS 호스트명·프록시·HTTP 리다이렉트는 거부하며 literal loopback 주소만 연결한다. API/worker와 모델 서버가 같은 호스트 네트워크에서 실행되어야 한다. Docker 컨테이너의 `127.0.0.1`은 호스트가 아니므로, 기존 Docker 데모만 실행했다고 로컬 모델이 연결되지는 않는다.

## 재현

검증한 원본 다운로드 URL과 SHA는 [runtime-proof.json](data/followup-2026-10-01/local-runtime-proof/runtime-proof.json)에 있다. 모델 가중치(약 2.50 GB)와 실행 바이너리는 저장소에 포함하지 않는다. Linux CPU 릴리스와 모델을 해당 파일의 경로에 준비한 뒤, 다음 래퍼는 모델 SHA와 런타임 revision, 기존 포트 점유, 서버 프로세스 생존을 검사하고 서버와 평가를 함께 실행한다.

```sh
python docs/data/followup-2026-10-01/local-runtime-proof/run-with-server.py -- \
  uv run --project apps/api manage eval llm \
  --model heuristic --model local-qwen3-4b-q4_k_m-7485fe6f \
  --max-usd 0 --concurrency 1
```

이 명령의 수기 세트는 알려진 개발 회귀 자료다. 원문에서 먼저 고정한 실문서 표본은 별도 매니페스트·전체 요청·응답과 함께 평가한다. 진단 결과를 보고 바꾼 모델을 같은 표본에서 다시 실행한 결과를 신규 독립 검증으로 부르지 않는다.

검증된 HTTP alias는 가중치의 암호학적 증명이 아니다. 위 래퍼의 실제 파일 해시 검사가 별도로 필요하다. 연결 실패·서버 과부하는 기존 작업 재시도 경로로, 잘못된 JSON·잘린 출력·거부는 오류 기록과 검토 가능한 대체 경로로 전달한다. 다른 모델 alias는 설정 오류로 작업을 중단한다. API 금액 가드는 이전 유료 호출의 지출이 있어도 API 비용 0인 추출을 차단하지 않는다.
