# 성남시의회 회의록 첫 실데이터 (2026-09-28)

> 작업 중인 문서입니다. 수치는 모두 이 세션에서 직접 본 것이고, 계산으로 낸 값은 "(계산)"으로 표시합니다.

국회도서관 지방의정포털(CLIK, `sources/clik.py`)의 키가 아직 승인되지 않아, 회의록 소스는 실데이터를 한 번도 보지 못했습니다. 그 대신 성남시의회 누리집(`www.sncouncil.go.kr`)의 회의록을 게시판 크롤러(`sources/crawler.py`)로 받아, 의회 발언 → 성남시 예산서 행([`real-data-budget.md` §8](real-data-budget.md#8-게시판에서-예산서-받기-강남구성남시-2026-09-27)의 6권) → 조달청 공고로 이어지는 실제 사례를 찾습니다.

- `APP_ANTHROPIC_API_KEY`가 이 환경에 없어 추출은 **규칙 기반 추출기**(`llm/providers/heuristic.py`)가 합니다. LLM 비용은 0입니다.
- 이 컨테이너에는 Docker가 없어 PostgreSQL 16(pgvector)과 Redis를 직접 띄웠고, CI와 같게 `tesseract-ocr`·`tesseract-ocr-kor`·`fonts-nanum`을 설치했습니다.

## 1. 접속: HTTPS는 끊기고 HTTP로 받음

`www.sncouncil.go.kr`은 이 환경의 허용 도메인에 들어 있습니다. 프록시는 CONNECT에 `200 Connection Established`로 답했고, 프록시 상태(`/__agentproxy/status`)의 `recentRelayFailures`도 비어 있어 **정책 차단(connect_rejected)이 아닙니다.** 끊기는 곳은 그 뒤 TLS 단계입니다.

| 시도 | 횟수 | 결과 |
|---|---:|---|
| `curl https://www.sncouncil.go.kr/robots.txt` (기본 설정, 간격 3~10초) | 14 | 성공 1, 나머지 13은 ClientHello 직후 `Connection reset by peer` |
| `openssl s_client -connect www.sncouncil.go.kr:443 -servername …` (프록시 경유, 기본·`-tls1_3`·`-tls1_2`) | 7 | 7번 모두 ServerHello 전에 `errno=104`(reset). 서버가 받는 TLS 버전·암호는 **확인하지 못함** |
| `curl https://www.seongnam.go.kr/robots.txt` (어제 §8에서 받았던 곳) | 3 | 3번 모두 같은 reset |
| `curl http://www.sncouncil.go.kr/…` (평문, 같은 프록시 경유) | 3 | 3번 모두 200 |

- 어제 받았던 성남시청도 지금 같은 증상이라, 이 서버의 암호 설정보다는 **컨테이너 → 한국 공공기관 서버 사이의 경로 문제**일 가능성이 큽니다. openssl이 한 번도 ServerHello를 받지 못해 §8.1처럼 "오래된 암호만 받는 서버"인지는 가를 수 없었고, 그래서 `legacy_tls_hosts`는 쓰지 않았습니다.
- 사이트는 HTTP 요청을 HTTPS로 넘기지 않고 그대로 제공합니다(첫 화면 `200`, EUC-KR, `/kr/main.do`로 JS 이동).
- **그래서 이번 회의록은 HTTP(평문)로 받았습니다.** 프록시는 그대로 거쳤고, 인증서 검증을 끈 것이 아니라 TLS 없이 받은 것입니다. 전송 중 변조되지 않았다는 보장이 없으므로 받은 파일마다 SHA-256을 남기고, HTTPS가 붙을 때 몇 개를 다시 받아 해시를 비교합니다(아래). 크롤러 코드에 https → http 자동 전환은 넣지 않았고, 설정에서 이 호스트의 게시판 주소를 `http://`로 적은 것뿐입니다.
- robots.txt(HTTP로 읽음)는 `User-agent: *` / `Allow:/` 두 줄입니다. 막힌 경로는 없습니다.
