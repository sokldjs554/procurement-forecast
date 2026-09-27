# 데이터 소스

| 키 | 제공처 | 문서 유형 | 주기 (KST) | 어댑터 |
|---|---|---|---|---|
| `clik_minutes` | 국회도서관 지방의정포털 Open API | 지방의회 회의록 | 매일 03:10 | `sources/clik.py` |
| `lofin_budget` | 행정안전부 지방재정365 (`www.lofin365.go.kr/lf/hub/<데이터코드>`) | 세출예산서(PDF·스캔 PDF·HWP/HWPX) | 매주 일 02:40 | `sources/lofin.py` |
| `g2b_order_plan` | 조달청 발주계획현황서비스 | 발주계획 | 매시 7분 | `sources/g2b.py` |
| `g2b_prespec` | 조달청 사전규격정보서비스 | 사전규격 | 매시 7분 | `sources/g2b.py` |
| `g2b_bid` | 조달청 입찰공고정보서비스 | 입찰공고 | 매시 7분 | `sources/g2b.py` |
| (기관별 설정) | 지자체 누리집 "예산 공개"·"고시·공고" 게시판 | 예산서·사업설명서 첨부(HWP·PDF) | 수집원 설정 | `sources/crawler.py` |
| `fixture_*` | 합성 세계 (`demo/synth.py`) — 예산서는 합성 누리집을 **크롤링**해서 수집 | 위 여섯 가지 | 수동/데모 | `sources/registry.py`, `demo/sites.py` |

## 검증 상태 — 먼저 읽어 주세요

처음 작성할 때는 개발 컨테이너의 네트워크 정책 때문에 `clik.nanet.go.kr`, `data.go.kr`, `lofin.mois.go.kr`에 직접 접속하지 못했습니다. 그래서 세 어댑터의 **엔드포인트 경로와 필드명은 공개 명세·검색 결과·공개 저장소를 근거로 작성**했고, `tests/unit/test_sources.py`의 계약 픽스처로만 검증했습니다. 실제 키로 처음 돌릴 때는 운영 콘솔 *수집원* 화면에서 첫 실행 결과(가져온 수·신규·오류)를 확인하세요.

**2026-09-26 첫 실호출(키 미승인)** ([`source-check.md`](source-check.md)): `apis.data.go.kr`의 `ad`/`ao` 경로 9개는 모두 존재하고(접두어 없는 옛 경로는 오류 12 "서비스 없음"), 키 전달 방식도 게이트웨이까지 정상입니다. 다만 그 키가 세 서비스 모두 활용신청 전이라 전부 오류 30으로 거절돼, **필드명·채움 비율은 아직 검증하지 못했습니다.** 이때 게이트웨이가 오류를 HTTP 200이 아니라 **HTTP 403 + JSON `OpenAPI_ServiceResponse`**로 보낸다는 것을 확인해 `http.py`가 이 본문의 코드로 분류하도록 고쳤습니다. 해외 경로에서는 TLS 연결이 가끔 끊기거나(`ConnectTimeout`, `Connection reset`) 느려서 점검 명령은 오퍼레이션마다 세 번까지 시도합니다.

**2026-09-26 첫 실데이터 점검** (세 서비스 활용신청 후, 최근 7일 첫 페이지): 9개 오퍼레이션 모두 성공했고, 받은 항목은 전부 레코드로 변환됐습니다. id·제목·날짜 필드명은 명세 그대로였습니다.

| 유형 | 용역 | 물품 | 공사 | 고친 것 |
|---|---:|---:|---:|---|
| 발주계획 | 1,152 | 941 | 921 | `orderBgnYm`·`orderEndYm`(발주년월)이 필수 — 없으면 HTTP 200 + `nkoneps.com.response.ResponseError` 오류 08이 오는데, 이걸 빈 페이지(0건 "성공")로 읽고 있었음. 창 앞뒤 1년씩 넣고, 이 오류 본문과 나라장터 코드 06~08을 즉시 실패로 분류 |
| 사전규격 | 883 | 724 | 43 | 없음 |
| 입찰공고 | 2,010 | 1,756 | 1,416 | 공사는 `asignBdgtAmt` 대신 `bdgtAmt`로 예산을 줌 → 금액 매핑에 추가(전에는 부가세 빠진 `presmptPrce`로 떨어짐) |

**같은 날 30일치 적재** ([`real-data-run.md`](real-data-run.md)): 57,514건을 91회 호출로 받았고(한 번에 999건), 같은 기간을 다시 적재하면 전부 건너뜁니다. 여기서 1만 원 미만 자리표시 금액, 발주계획의 `bidNtceNoList`(차수 세 자리가 붙음), 사전규격에 부서 필드가 없다는 것을 확인했습니다.

**2026-09-27 지방재정365 예산서 첫 실호출** ([`real-data-budget.md`](real-data-budget.md)): 발급받은 키로 시도했지만 **API에 닿지 못했습니다.** 기본값 `lofin.mois.go.kr/HUB/BGTBOOK`은 TLS 단계에서 매번 끊겼고(목록 호출 3번 포함), 새 사이트 `www.lofin365.go.kr`은 컨테이너 네트워크 정책이 막았습니다(프록시 CONNECT 403). `lofin365.go.kr`은 가끔 연결되지만 `www`로 301을 돌려줍니다. 그래서 예산서 어댑터의 경로·필드명·파일 호스트는 **여전히 확인되지 않은 추정값**입니다. 대신 어댑터가 파일을 받기 전에 회계연도·수집 창·`institutions` 목록으로 행을 거르도록 바꿨고, 호스트도 `base_url`로 바꿀 수 있게 했습니다. 전체 실행에는 최소한 `www.lofin365.go.kr` 허용이 필요하고, 파일 호스트는 목록을 받아 봐야 압니다.

채움 비율이 낮은 필드는 필드명 문제가 아니라 원래 비어 있는 값입니다. 사전규격 응답에는 `orderPlanUntyNo` 필드 자체가 없어 발주계획번호가 0%이고(사전규격→발주계획 연결은 번호가 아니라 유사도로), 공사 입찰공고의 `bfSpecRgstNo`는 1,000건 중 19건만 차 있습니다(공사 사전규격 자체가 주 43건). 수의계약 공고 일부는 `bidClseDt`가 비어 있습니다.

키를 처음 넣었을 때는 파이프라인을 돌리기 전에 점검 명령부터 실행합니다. 데이터베이스 없이 조달청 오퍼레이션 9개(발주계획·사전규격·입찰공고 × 용역·물품·공사)를 최근 7일로 한 번씩 호출하고, 결과를 `docs/source-check.md`에 씁니다.

```bash
APP_DATA_GO_KR_SERVICE_KEY=... make check-sources
```

| 보는 것 | 뜻 |
|---|---|
| 결과가 `실패: ... 30 SERVICE_KEY_IS_NOT_REGISTERED_ERROR` | 그 서비스에 활용신청이 안 됐거나 승인 전입니다(승인 직후에는 반영까지 시간이 걸릴 수 있음). 공공데이터포털에서 서비스마다 신청합니다. 보고서의 "활용신청이 필요한 서비스"에 서비스명과 링크가 나옵니다 |
| 받은 항목 > 레코드로 변환 | id·제목·날짜 필드명이 명세와 다릅니다. 보고서의 "버려진 항목의 실제 필드"를 보고 `g2b.py`의 `map_item`에 새 이름을 추가합니다 |
| 금액·발주계획번호·사전규격번호 채움 비율이 낮음 | 기회 연결과 랭킹이 약해집니다. 필드명을 확인합니다 |

- 포털이 보여 주는 일반 인증키는 "Encoding"(`%2B` 등)과 "Decoding"(`+` 등) 두 가지인데 어느 쪽을 넣어도 됩니다. Encoding 키는 한 번 풀어서 보냅니다(`g2b.normalize_service_key`). 그대로 보내면 두 번 인코딩돼 모든 호출이 오류 30으로 실패합니다.
- 키는 출력·로그·Sentry에 남지 않습니다. 로그는 `app/log.py`의 처리기가 모든 줄(트레이스백 포함)에서 `serviceKey=`·`key=`·`Key=` 값을 가리고, Sentry는 오류·성능 트랜잭션·스택 프레임 변수 모두 스크럽합니다(`observability.py`).
- 조달청 필드명은 설정이 아니라 코드(`g2b.py`의 `map_item`)에서 고칩니다. 아래 `sources.config` 덮어쓰기는 CLIK·지방재정365 어댑터에 해당합니다.

어긋나는 부분이 있으면 코드 수정 없이 `sources.config`(DB, JSON)에서 덮어쓸 수 있습니다.

```sql
UPDATE sources SET config = config || '{"overrides": {"list_path": "/openapi/minutes.do", "date_fields": ["MTG_DE"]}}'
WHERE key = 'clik_minutes';
```

지방재정365는 여기에 더해 API 호스트(`base_url`)와, 파일을 받을 기관 목록(`institutions`)을 설정합니다. 목록이 있으면 그 기관의 예산서만 내려받습니다. 이름은 띄어쓰기를 무시하고, 기관명만으로도 "지역명 + 기관명"으로도 맞춥니다.

```sql
UPDATE sources SET config = config || '{"base_url": "https://www.lofin365.go.kr",
  "institutions": ["서울특별시 강남구", "부산광역시 해운대구"]}'
WHERE key = 'lofin_budget';
```

## 호출 제약과 대응

| 제약 | 대응 | 코드 |
|---|---|---|
| CLIK: 키당 하루 1,000회, 호출당 100건 | 회의 날짜로 페이지 이동, 저장하지 않은 회의록만 본문 호출 | `clik.py` |
| data.go.kr: 개발 키 오퍼레이션당 하루 1,000회, 조회 기간 제한 | 7일 창으로 쪼개 페이지네이션, KST 일일 한도 카운터 | `g2b.py`, `resilience.py` |
| 한도 초과·오류를 **HTTP 200 + 오류 본문**(XML·JSON), 키 오류는 **HTTP 401/403 + JSON `OpenAPI_ServiceResponse`**로 응답 | 상태 코드보다 본문의 `resultCode`/`returnReasonCode`로 분류: 한도(22) → KST 자정 이후로 재예약, 키·파라미터(06~08, 10~33) → 즉시 실패(운영자 확인), 일시 오류 → 재시도 | `http.py` |
| 429/5xx/타임아웃 | 지수 백오프 + full jitter, `Retry-After` 존중 | `http.py` |
| 제공처 장애 | 수집원별 서킷 브레이커(연속 5회 실패 → 10분 차단 → 1회 탐침) | `resilience.py` |
| 워커 여러 대가 한도 공유 | 토큰 버킷·일일 카운터·서킷 상태 모두 Redis(Lua로 원자적 처리) | `resilience.py` |

## 조달청 오퍼레이션과 매핑

업무구분(용역 `Servc` / 물품 `Thng` / 공사 `Cnstwk`)마다 오퍼레이션이 따로 있어 세 개를 모두 호출합니다. 경로는 `g2b.py`의 `OPERATIONS`.

| 유형 | 경로 | 외부 ID | 연결에 쓰는 필드 |
|---|---|---|---|
| 발주계획 | `/ao/OrderPlanSttusService/getOrderPlanSttusList{Servc,Thng,Cnstwk}` | `orderPlanUntyNo` | `bizNm`, `sumOrderAmt`, `orderYear`·`orderMnth`, `orderInsttNm`, `deptNm`, `bidNtceNoList`(차수 붙음) |
| 사전규격 | `/ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfo{Servc,Thng,Cnstwk}` | `bfSpecRgstNo` | `prdctClsfcNoNm`, `asignBdgtAmt`, `bidNtceNoList`, `rlDminsttNm` (`orderPlanUntyNo`는 응답에 없음) |
| 입찰공고 | `/ad/BidPublicInfoService/getBidPblancListInfo{Servc,Thng,Cnstwk}` | `bidNtceNo`-`bidNtceOrd` | `bidNtceNm`, `asignBdgtAmt`(공사는 `bdgtAmt`)/`presmptPrce`, `bfSpecRgstNo`, `orderPlanUntyNo`, `dminsttNm` |

- 참조번호(`orderPlanUntyNo`, `bfSpecRgstNo`, 공고번호)가 있으면 **유사도보다 먼저** 그 번호로 기회를 잇습니다. 같은 공고번호의 다른 차수(변경·재공고·취소)도 번호로 같은 기회에 묶습니다.
- 공고 종류(`ntceKindNm`)가 `취소공고`면 그 공고번호의 입찰을 거둬들인 것으로 봅니다. 기회는 남은 신호로 단계와 상태를 다시 계산합니다.
- 기관은 먼저 이름으로 우리 기관 사전(지자체 전체)에 맞춥니다. 사전으로 풀리지 않으면 레코드의 수요기관코드(`orderInsttCd`·`rlDminsttCd`·`dminsttCd`)로 기관을 새로 만들고(`G2B-<코드>`), 다음부터는 이름을 보지 않고 코드로 찾습니다. 학교·병원·공사·공단 대부분이 이 경로입니다([ADR-0011](adr/0011-provider-codes-for-institutions.md)).
- 지자체처럼 생긴 이름이 사전에 없거나, "중구청"처럼 광역시가 빠져 모호하면 새 기관을 만들지 않고 검토 대기열로 보냅니다.

## 게시판 크롤러

API가 없는 기관 누리집 게시판은 범용 크롤러가 목록 → 상세 → 첨부 순으로 따라갑니다([ADR-0009](adr/0009-polite-board-crawler.md)). 기관 추가는 설정만으로 합니다.

```json
{"boards": [{"url": "https://www.example.go.kr/board/B_000052/list.do",
             "institution_code": "LG-11680", "publisher": "서울특별시 강남구"}],
 "doc_type": "budget_book", "title_keywords": ["예산서", "사업명세서"],
 "detail_pattern": "view\\.do", "attachment_pattern": "download|fileDown|atchFile",
 "id_param": "nttId", "page_param": "pageIndex", "max_pages": 20,
 "delay_seconds": 1.0, "max_file_mb": 50}
```

| 지키는 것 | 방법 |
|---|---|
| robots.txt | 호스트마다 한 번 받아 따름. 4xx면 규칙 없음, 5xx·접속 불가면 이번 실행에서 그 호스트 전체 금지 (RFC 9309) |
| 요청 속도 | `<수집원>@<호스트>` 공유 토큰 버킷 + 호스트별 최소 간격 |
| 증분 | 최신순 목록에서 가장 오래된 글이 수집 창보다 이전이면 페이지 이동 중단 |
| 파일 | 스트리밍 중 크기 상한 초과 시 중단, 형식은 앞부분 바이트로 판별(PDF·HWP5·HWPX), 이미지·오류 페이지는 버림 |
| 같은 글 | 세션·메뉴 파라미터를 뺀 정규 URL과 게시글 ID로 식별 |

데모에서는 기관마다 `*.gov.example` 합성 누리집이 있고(robots.txt 금지 경로, 예산과 무관한 공지, 이미지 첨부, `application/octet-stream` 다운로드 포함), 크롤러가 여기서 예산서 25건을 모두 원본과 동일하게 수집합니다.

## 원문 파일

- 원문 바이트는 SHA-256 콘텐츠 주소로 저장합니다(`storage.py`: 로컬 `file://`, 프로덕션 `gs://`). 같은 파일은 한 번만 저장·처리됩니다.
- 예산서 PDF: 페이지별로 텍스트층 글자 수를 보고, 부족한 페이지만 OCR합니다(스캔본과 텍스트본이 섞인 권도 처리).
- HWP 5.x: OLE 컨테이너의 `BodyText/Section*` 레코드를 풀어 `PARA_TEXT`(태그 67)에서 글자를 읽고 제어 문자를 제거합니다. HWPX: ZIP 안의 섹션 XML.

## 합성 세계

`manage seed`가 `fixture_*` 수집원을 만들고, 어댑터는 시드·기준일·규모로 결정되는 합성 세계를 실제 제공처처럼 내어 줍니다. 21개 사업 원형, 동명 기관, 스캔 예산서, 약한 발언, 공고로 이어지지 않는 사업, 무관한 공고가 섞여 있고, 정답은 평가에만 쓰입니다([ADR-0007](adr/0007-synthetic-world-evaluation.md)).
