# 성남시 연결 신호 (`seongnam-link-signals.jsonl.gz`)

성남시 예산서 6권과 성남시의회 회의록 34건을 파이프라인에 돌린 실제 실행 A의 신호입니다. `manage link replay`로 연결 규칙을 로컬에서 다시 재기 위한 자료입니다. 방법은 [`real-data-minutes.md` §9.1](../real-data-minutes.md#91-방법)과 같습니다.

- 만든 날짜: 2026-09-29
- 코드: `d6450e0` 위에 `e8024ea`(`fix(api): link export reads named columns, not whole documents`)를 cherry-pick한 것입니다. 연결기는 `d6450e0` 그대로이고, 바뀐 것은 `export_signals`의 쿼리뿐입니다. 이 커밋 없이는 `link export`가 `statement_timeout`(30 s)으로 두 번 실패했습니다. 신호 행마다 문서 본문이 함께 읽혀, 예산서 쪽만 4,717,298,150바이트였습니다(SQL로 계산).
- 파일: `seongnam-link-signals.jsonl.gz`, 204,420바이트
  - SHA-256 `fc62e037e3b017dacad4bd94f2320e1c43043a8f682e4f0789fbba8a894fac1b`
- 추출은 규칙 기반(`APP_LLM_PROVIDER=heuristic`)만 썼습니다. 모든 명령에서 `APP_ANTHROPIC_API_KEY`를 환경에서 뺐습니다.
- 쓴 DB 7개(기준, A, B, replay 4) 모두 `llm_calls`가 0건입니다.

## 실행한 명령

PostgreSQL 16 + pgvector 0.6.0, Redis 7.0.15, tesseract-ocr·tesseract-ocr-kor·fonts-nanum을 쓴 컨테이너에서 돌렸습니다(Docker 없음). 실행마다 DB와 Redis db 번호를 따로 썼습니다.

1. 기준 DB: `db upgrade` → `seed --anchor 2026-09-25` → [`real-data-budget.md` §8.3](../real-data-budget.md#83-설정과-실행)과 [`real-data-minutes.md` §3](../real-data-minutes.md#3-설정과-실행)의 SQL 그대로 두 수집원을 켰습니다. 이어서 두 적재를 돌렸습니다.
   - `sources ingest -s budget_boards --days 400 --until 2026-09-27`
     - 첫 실행은 성남시청 `ConnectTimeout` 3번 뒤 0권이었습니다. 회로 차단기는 열리지 않았고 지우지 않았습니다.
     - 같은 명령을 한 번 더 돌려 요청 10번, 31.2 s에 6권을 적재했습니다.
   - `sources ingest -s minutes_boards --days 393 --until 2026-09-28`: 요청 88번, 135.8 s, 34건.
   - 40개 파일의 SHA-256이 §9.2(§1·§8.2 표)와 같습니다. 예산서의 바이트 수, `published_at`, `published_from`도 같습니다.
2. A: `createdb -T` 복사본에서 `pipeline run`을 한 번 돌렸습니다. 이어서 `link export docs/data/seongnam-link-signals.jsonl.gz`를 실행했습니다(기본 doc-type).
3. B: 기준 DB의 새 복사본에서 다음 순서로 돌렸습니다.
   - 회의록 34건을 SQL로 `'skipped'` → `pipeline run` → `'pending'` → `pipeline run`
   - `link export /tmp/b.jsonl.gz`: SHA-256 `5ecacc9d055ec0dc77a27258e1b6610d7ac0599a84c08257c4f84c38f0e85192`, 커밋하지 않음
4. replay: `db upgrade` + `seed --anchor 2026-09-25`만 한 새 DB에서 각각 돌렸습니다.

## 신호

| 문서 종류 | stage | verdict | 신호 |
|---|---|---|---:|
| budget_book | budget_line | accepted | 2,327 |
| budget_book | budget_line | needs_review | 2 |
| council_minutes | council_mention | accepted | 72 |
| council_minutes | council_mention | needs_review | 3 |
| 합계 | | | 2,404 |

- 신호가 나온 문서는 20개입니다(계산).
- 두 실행의 파일은 신호 키 집합이 같습니다. `opportunity`를 뺀 모든 필드도 2,404건 모두 같습니다(계산).

## 기회

| | A (한 번에) | B (예산서 먼저) |
|---|---:|---:|
| 기회 | 1,251 | 1,250 |
| 기회에 붙은 신호 | 2,399 | 2,399 |
| 회의록 신호가 든 기회 (계산) | 54 | 55 |
| 회의록 + 예산서 사슬 (계산) | 18 | 19 |

A·B의 기회 수와 사슬 수는 §9.3의 브랜치 A·B와 같습니다.

## 재현 확인

| 확인 | replay 기회 | 기록된 기회 | `same_as_recorded` | 결과 |
|---|---:|---:|---:|---|
| 1. A 파일 replay | 1,250 | 1,251 | 1,243 | **다름** |
| 2. B 파일 `--first budget_book` | 1,250 | 1,250 | 1,244 | **다름** |
| 3. A 파일 `--first budget_book` vs 실제 B | 1,250 | 1,250 | 1,244 | **다름** (신호 키 기준 비교) |

- **1**: 기록과 다른 기회는 기록 쪽 8개(신호 19)와 replay 쪽 7개(신호 19)입니다(계산).
  - 모두 예산서 신호만 가진 기회입니다. 사업은 "산모신생아건강관리지원사업(전환사업)", "고독사예방 및 관리사업", "…종사자 처우개선비·웰빙보조비", "사회복지 시책추진비"입니다.
  - 이름이 같은 행들이 어느 기회에 들어가는지가 바뀌었습니다.
  - 같은 파일을 새 DB에서 한 번 더 replay하니 `--out` 파일이 바이트까지 같았습니다. replay 자체는 결정적입니다.
- **2**: 다른 기회는 양쪽 6개씩(신호 14)입니다(계산). 역시 모두 예산서 신호만 가진 기회입니다. 사업은 "AI·IoT기반 어르신 건강관리사업", "소규모 정비공사"·"분당동 101번지 정비공사", "고독사예방 및 관리사업"입니다.
- **3**: 결과는 2와 같습니다(1,244개 같음, 양쪽 6개씩 다름).
  - A 파일 `--first` replay의 `--out`과 B 파일 replay의 `--out`이 바이트까지 같습니다.
  - 따라서 A 파일 하나로 B 순서의 replay를 그대로 낼 수 있습니다. 다만 그 replay가 실제 B와 다른 것은 2와 같습니다.
- 세 경우 모두 회의록 신호가 든 기회(사슬 포함)는 실제 실행과 모두 같았습니다(계산). 달라진 것은 예산 행끼리의 묶음뿐입니다.
- 코드는 고치지 않았습니다. 원인은 조사하지 않았습니다.

## 걸린 시간

| 단계 | 벽시계 | 처리 / 연결 |
|---|---:|---:|
| 예산서 적재 (두 번째, 보고서 `seconds`) | 32.2 s | 31.2 s |
| 회의록 적재 (보고서 `seconds`) | 137.1 s | 135.8 s |
| A `pipeline run` | 601.2 s | 561.2 s / 38.9 s |
| A `link export` | 1.9 s | |
| B 1차 (예산서 6권) | 619.2 s | 577.6 s / 40.4 s |
| B 2차 (회의록 34건) | 6.0 s | 3.3 s / 1.7 s |
| replay (A · A `--first` · B `--first`) | 48.5 s · 48.4 s · 51.2 s | |

- A 파일 replay 세 번(재현 확인 1·3과 결정성 확인)은 B 1차와 겹쳐 돌렸습니다. 그래서 B 1차 시간은 그만큼 부풀었을 수 있습니다.
- A 실행 뒤, export 전에 컨테이너가 한 번 다시 시작됐습니다. DB는 남아 있었고 A DB의 신호 2,404 / 기회 1,251을 확인한 뒤 export했습니다.
- B는 처음 시작한 실행을 1차 도중(97.8 s)에 중단했고, 기준 DB를 새로 복사해 처음부터 다시 돌렸습니다. 위 수치는 다시 돌린 실행입니다.
