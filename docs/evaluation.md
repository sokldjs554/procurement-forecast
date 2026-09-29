# 평가 결과 (자동 생성: `manage eval all --report`)

이 문서 본문의 수치는 PR #49 병합 기준입니다. 후속 [PR #50](https://github.com/sokldjs554/procurement-forecast/pull/50)의
동일 조건 전후 수치와 최종 검증 실행 링크는 해당 PR에 기록합니다. 각 실행의 `integrity-audit`
아티팩트에는 `baseline.json`, `changed.json`, 전체 생성 보고서 및 저장 원문 재검증 결과가
포함됩니다. 이전 결과를 후속 코드의 실측값으로 간주하지 않습니다.

> 합성 세계(synthetic world) 결과는 파이프라인이 설계대로 동작하는지 보여줄 뿐, 실제 데이터에서의
> 정확도를 주장하지 않습니다. 실제 문장에 가까운 수기 작성 세트(realistic)를 따로 둔 이유입니다.

조건: 기준일 2026-09-25 · 시드 7 · 규모 1.0 · 스캔 비율 기본값 · 추출기 `heuristic`

## 추출 (합성 정답 대비)
- 정밀도 100.0% · 재현율 100.0% (정답 106건, 예측 106건; 검토 대기 포함)
- 자동 승인 103건 · 검토 대기 3건. 저장 재현율은 자동 승인 재현율이 아닙니다.
- 필드 정확도: budget 100.0%, expected_year 100.0%, commitment 100.0%, category 97.2%, institution 100.0%
- 트리아지: 청크 807개 중 59.4%를 LLM 호출 없이 건너뜀, 그 상태에서 정답 신호 재현율 99.1%

## 기회 연결 (linking)
- 쌍(pairwise) 정밀도 100.0% · 재현율 98.5% · F1 0.992
- 한 기회로 온전히 묶인 실제 사업 비율 98.6%, 순수한 기회 비율 100.0%

## OCR (스캔 예산서)
- 문서 6건 · CER 0.0113 → 보정 후 0.0099
- 금액 토큰 정확도 100.0% → 100.0%

## 수기 작성 세트 (extractor: heuristic-v2, 54건)
- 정밀도 92.0% · 재현율 46.0% (기대 50건, 예측 25건)
- 필드 정확도: category 82.6%, commitment 82.6%, budget 78.3%, expected_year 91.3%
- 검증기 통과 후(저장되는 값): 정밀도 92.0% · 재현율 46.0% · category 82.6%, commitment 82.6%, budget 78.3%, expected_year 91.3% · 근거를 찾지 못해 버린 신호 0건
- 모델별 비교는 `manage eval llm` → [evaluation-llm.md](evaluation-llm.md)

## 백테스트 (현재 연결의 회고적 진단)
- 방법: `public-date-cohort-v2` · 기준일 2026-09-25 · 관측기간 540일
- 관측기간 미충족 기회 35건 · 공개일 불확실 제외 신호 0건
- 합성 데이터 실행 수치는 실제 예측 성능이 아닙니다. 현재 연결을 사용하므로 과거 시점 예측 재현도 아닙니다.
- 입찰공고 63건 중 54.0%가 공고 이전에 공개 신호를 가짐
- 선행 기간 중앙값 273.5일 (p25 192.75, p75 347.75)
- 첫 신호 유형별 입찰 전환율:
  - `budget_line:*` n=9 → 77.8%
  - `council_mention:*` n=25 → 56.0%
  - `budget_line:committed` n=9 → 77.8%
  - `council_mention:planned` n=3 → 100.0%
  - `council_mention:declined` n=2 → 0.0%
  - `council_mention:committed` n=11 → 81.8%
  - `council_mention:reviewing` n=9 → 22.2%
