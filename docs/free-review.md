# 비용 없이 실행하고 면접 전에 점검하기

이 경로는 **PC에서 실제 API·PostgreSQL·Redis·worker·웹을 실행하는 구성**이다.
공개 데모 URL을 배포하는 명령이 아니며, 공개 데모는 기존 담당 범위를 유지한다.
Render나 GCP 자원을 생성하지 않고 유료 모델 API를 호출하지 않는다.
기존 `render.yaml`은 유료 운영용 선택 구성이다. 이 실행에는 사용하지 않는다.

## 실행

Docker와 Docker Compose v2, Python 3가 설치된 PC에서 저장소 루트로 이동한다.
호스트에 Node.js, PostgreSQL, Redis, uv를 따로 설치할 필요는 없다.
첫 실행에는 이미지와 의존성을 내려받을 인터넷 연결이 필요하다.

```bash
python3 scripts/free-review.py start
```

Windows에서는 `python scripts/free-review.py start`를 사용한다.

| 확인할 곳 | 주소 |
| --- | --- |
| 실제 웹 앱 | http://localhost:13000 |
| API 명세 | http://localhost:18000/docs |
| 로컬 알림 메일함 | http://localhost:18025 |

`demo@example.com` / `demo-pass-1234`로 피드·원문 근거·브리핑을 확인한다.
`admin@example.com` / `admin-pass-1234`로 운영 콘솔과 실제 작업 실행 이력을 확인한다.
이 계정은 합성 데이터 확인용이며 외부에 운영 계정으로 노출하지 않는다.

시작 명령은 마이그레이션 → 최초 합성 자료 준비 → 전체 파이프라인 실행 →
API·worker·웹 시작 → 로그인·권한·피드·원문 읽기·메일·실제 큐 처리 점검 순서로 동작한다.
API 응답을 녹화해서 대신 돌려주는 방식이 아니다. 추출은 규칙 기반이고 임베딩은
hashing, 결제는 모의 공급자를 사용한다. 외부 모델의 정확도나 실결제 검증으로 해석하지 않는다.

## 면접 전과 종료 후

```bash
python3 scripts/free-review.py check
python3 scripts/free-review.py stop
python3 scripts/free-review.py start
```

`check`는 로그인·데이터 조회 외에 합성 데이터의 backtest 작업 한 건을 실제 Redis 큐에
넣고 worker 완료 이력을 PostgreSQL에서 확인한다. 실제 사용자에게 메일을 보내지 않는다.
알림은 Mailpit에만 저장한다. `stop`은 이 프로젝트의 컨테이너만 종료하고 볼륨을 보존한다.
다시 시작할 때 이미 준비된 자료를 재시드하거나 브리핑·피드백을 초기화하지 않는다.
첫 실행일에 고정한 합성 자료의 날짜도 유지한다. 컴퓨터가 꺼져 있으면 앱은 실행되지 않는다.

`procurement-forecast-review`라는 별도 Compose 프로젝트와 전용 데이터 볼륨을 쓴다.
기존 개발 DB, 다른 프로젝트의 Render 무료 DB·Redis에는 접속하지 않는다.
호스트에는 `127.0.0.1` 포트만 열고, API와 worker는 외부 인터넷 경로가 없는 네트워크에서 실행한다.
사용자의 `.env`나 외부 API 키를 읽지 않는다. 웹·API·메일의 호스트 접속은 고정된 내부 주소로만
전달하는 Nginx를 거친다. 운영용 설정 검증은 그대로 유지한다.

볼륨 삭제, Docker 초기화, PC 디스크 손실까지 막는 백업은 아니다.
기존 `docker compose down -v` 같은 볼륨 삭제 명령을 이 프로젝트에 실행하면 저장 자료가 사라진다.

## 자동 검증 범위

[Free review stack](https://github.com/sokldjs554/procurement-forecast/actions/workflows/free-review.yml)은
공개 저장소의 표준 Linux runner에서 다음을 실행하고 결과를 남긴다.

- 전체 Docker 구성과 실제 호스트 포트 확인
- API·worker의 외부 연결 차단
- 기존 브라우저 시나리오: 로그인, 피드, 사업 상세, 브리핑 작성, 운영 콘솔
- 컨테이너를 두 번 내렸다 다시 올린 뒤 원문 해시·신호 연결·피드백·저장 브리핑 비교
- 매 시작마다 실제 Redis 작업 → 별도 worker 실행 → PostgreSQL 완료 기록 확인

이는 임시 CI 실행과 로컬 구성을 검증한다. CI를 웹 호스팅 서버로 쓰지 않는다.
실제 공공 데이터의 지속 수집, 독립 정답으로 측정한 정확도, 유료 LLM 응답,
외부 서비스의 상시 운영 완료는 별도 근거가 필요하다.
