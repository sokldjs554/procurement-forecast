# 예산 분류 회귀 검증 — 2026-09-29

예산 추출기는 편성목·산출기초·옆 행까지 포함한 청크 전체에서 분야를 골랐습니다. 이제 사업명의 구매 대상과 용도를 먼저 분류합니다. `classify_category(text)`와 기존 정답 파일은 변경하지 않았습니다. 예산 전용 `classify_budget_category(title, details)`를 추가했고, 규칙 기반 추출기의 버전은 `heuristic-v3`입니다. 유료 LLM·수집 API 호출과 기존 저장 신호의 일괄 수정은 없습니다.

## 자료와 측정 범위

- 비교 코드: `2d3e7bb`의 예산 추출 함수와 이번 변경.
- 역사 보고서: [`real-data-budget.md` §8.5](real-data-budget.md#85-예산-사업-20건-수기-대조).
- 보존 자료: [`data/seongnam-link-signals.jsonl.gz`](data/seongnam-link-signals.jsonl.gz). SHA-256은 `fc62e037e3b017dacad4bd94f2320e1c43043a8f682e4f0789fbba8a894fac1b`입니다. 전체 2,404신호 중 예산 신호 2,329건, 서로 다른 예산 제목 934개를 사용했습니다.
- 보존 파일에는 원문 청크·근거 인용·원본 PDF가 없습니다. 따라서 아래 비교는 **보존 제목의 분류 회귀·변동량**이며 원문 6권을 다시 추출한 결과가 아닙니다.
- 기존 보고서는 20건 중 11건 오분류라는 집계와 일부 사업명만 남겼습니다. 전체 20건의 독립 정답을 복원하거나 새 정확도·11건 전체 해결률을 계산하지 않았습니다.

## 변경한 판단

1. 건물 신축·리모델링, 홈페이지·정보시스템처럼 구매 대상이 명확하면 사업명에서 판단합니다. 물놀이장·보행교·교량 등 토목시설 대상 바로 뒤 설치·조성·개설 등의 공사 행위가 이어지는 제목도 시설로 분류합니다. 일반어 `공사`만으로 분류를 확정하지 않습니다. 도서관 리모델링은 시설, 노인복지관 홈페이지 개편은 공공 SW입니다.
2. 돌봄·건강증진·침수감시·관광·교육처럼 용도가 명확하면 AI·IoT 같은 구현 기술이 분야를 바꾸지 않습니다. 청소년 교향악 행사는 문화, 구강보건 사업은 복지·돌봄입니다.
3. 제목이 구체적이면 산출기초를 이용해 다른 분야로 덮어쓰지 않습니다. `장비 구입` 같은 일반 제목만 명시적인 산출기초 기호의 내용을 제한적으로 참고합니다. 편성목·부서명은 분류 근거에서 제외하고 다음 사업 행을 만나면 중단합니다.
4. 용도가 없거나 서로 충돌하면 `other`로 남깁니다. 추출 신뢰도 0.35를 사용해 기존 grounding의 `low_confidence` 검토 경로로 보냅니다. 이전처럼 모르는 분류의 행을 조용히 버리거나 높은 신뢰도로 승인하지 않습니다.
5. 예산 키워드도 사업명에서만 뽑습니다. 옆 행의 홈페이지·XR 등을 다른 사업의 연결·추천 근거로 차용하지 않습니다.
6. 청크 분할기와 추출기가 같은 사업 행 판별기를 사용합니다. 명시적인 세부사업 행, 세부사업 열이 있는 Markdown 표, 편성목이 이어지는 일반 표를 구분하고 연도 머리글·편성목·통계목은 사업으로 만들지 않습니다. 여러 사업은 각자의 금액·근거·부서를 갖습니다. 원문을 변경하지 않아 문자 오프셋이 유지됩니다.
7. `근로자/직원/공무원/인력 보수`처럼 임금이 명시된 제목은 운영 경비로 제외합니다. `근로자 복지관 보수공사`와 같은 시설 보수는 이 조건에 해당하지 않습니다. 이는 이전경비·보조금 전체를 판별하는 정책은 아닙니다.

## 이름으로 확인 가능한 역사 사례

`기존 저장값`은 보존 파일에 남은 과거 청크 전체 분류입니다. `기존 제목만`은 기준 코드의 일반 분류기를 같은 제목에 적용한 값이고, `변경 후 제목만`은 새 예산 분류기 결과입니다. 같은 이름의 여러 행은 서로 독립적인 정답 사례로 세지 않습니다.

| 사업명 | 보존 행 수 | 기존 저장값 | 기존 제목만 | 변경 후 제목만 |
|---|---:|---|---|---|
| 개별주택가격 조사·산정 | 6 | facility | other | other |
| 전국동시지방선거 추진 | 50 | facility | other | other |
| 성남시 청소년 교향악 페스티벌 | 2 | education | education | tourism_culture |
| 지역사회 통합건강증진사업(구강보건) | 6 | education 3 / facility 3 | other | welfare_care |
| 정자1동 복합청사 신축 | 2 | facility | facility | facility |
| 도로명 안내표지판 정비공사 | 2 | facility | facility | facility |
| 가로수 및 녹지대 병해충 방제 | 3 | facility | other | energy_env |
| 다문화사회 이해교육 | 4 | education | education | education |
| AI·IoT기반 어르신 건강관리사업 | 8 | welfare_care | welfare_care | welfare_care |

첫 네 제목은 과거 보고서에서 오분류 사례로 명시한 것입니다. 마지막 건강관리 제목은 같은 보존 파일의 기술 단어 회귀 확인용입니다. 이 표에 정답 판정을 새로 확대해 전체 정확도라고 부르지 않습니다. `other`는 확정 분류가 아니라 분류 유보입니다.

| 2,329개 보존 제목의 진단 | 실측 |
|---|---:|
| 과거 저장 분야와 새 제목 분야가 다른 행 | 1,053 |
| 기준 코드의 제목 분야와 새 제목 분야가 다른 행 | 537 |
| 기준 코드에서 제목만으로 other인 행 | 568 |
| 새 제목 분류에서 other인 행 | 969 |

변동은 개선 건수나 정답률이 아닙니다. 특히 `other` 증가로 새 추출 시 검토 대기가 늘 수 있습니다. 보존 파일에는 제목으로 분류할 수 없으나 상세 근거로 분류 가능한 행도 있을 수 있어, 이 969건을 실제 재처리 후 검토 건수라고 주장하지 않습니다.

## 테스트와 기존 평가

- 최초 분류 회귀 26개: 기준 예산 추출 함수를 같은 테스트에 적용하면 **20실패 / 6통과**, 변경 후 **26통과**입니다. 처음 24개를 구현 전에 실행해 19개 실패를 확인했고, 메타버스 안의 `버스` 중복 해석과 키워드 오염도 각각 실패를 확인한 뒤 수정했습니다.
- 테스트의 역사 사업명은 보고서에서 가져왔지만, 오염을 만드는 편성목·산출기초 줄은 **구성한 테스트 입력**입니다. 원문 인용이라고 표시하지 않습니다.
- 표 경계 회귀는 다른 제목·숫자·형식으로 구성했습니다. 최초 10개 중 **8실패 / 2통과**를 확인한 뒤 구현했고, 임금과 직원 시설 보수를 구분하는 추가 실패 사례를 보완했습니다. 이후 통합 CI에서 시설공사 분류 유보로 기존 연결 테스트 세 건이 실패했습니다. 실제 예산 표를 재현해 `물놀이장 설치공사`, `보행교 설치공사`가 other/0.35/needs_review로 바뀐 원인을 확인했습니다. 명시적 시설 대상과 공사 행위를 함께 인정하는 규칙을 추가했습니다. 기존 실패 제목 세 개와 독립 제목·용도 보존 통제를 포함한 10개 회귀에서 **5실패**를 먼저 확인했습니다. 현재 새 테스트 **47개 통과**, 기존 파싱·LLM 테스트를 함께 실행하면 **103개 통과**입니다. 기존 통합 테스트의 단언은 변경하지 않았습니다.
- 기존 합성 archetype의 예산 제목 24개: 분류 일치 **22/24 → 24/24**. 이는 알려진 합성 제목 회귀이고 문서 추출·연결 P/R 평가를 대신하지 않습니다.
- 최종 PostgreSQL/Redis CI에서 **685개 모두 통과**했습니다([실행](https://github.com/sokldjs554/procurement-forecast/actions/runs/36567195440)). 중간에 드러난 export 두 결함과 시설 연결 회귀 세 건은 수정했고 기존 검증 단언을 낮추지 않았습니다. 로컬은 **580통과 / DB 환경 skip 105개**, 웹은 **61통과**입니다.
- 수정 Python 파일의 Ruff, 포맷, mypy 및 `git diff --check` 통과. 시설 대상 보완 후 보존 제목·archetype·realistic 수치를 재측정했으며 아래 표는 그 최종 값입니다.
- 전체 합성 추출·연결 P/R ≥ 0.95 게이트도 같은 DB 기반 CI에서 통과했습니다. 기준과 변경본의 전후 비교는 별도의 무결성 감사 기록에 남깁니다.

기존 `golden/realistic.jsonl` 54사례도 동일한 원문·정답·평가 함수로 무료 heuristic 비교했습니다. 저장 전과 grounding 후 수치가 각각 동일했습니다.

| 기존 realistic 진단 | 기준 | 제목·키워드만 보완한 중간 단계 | 표 경계 포함 최종 |
|---|---:|---:|---:|
| 정답 / 예측 | 50 / 25 | 50 / 25 | 50 / 28 |
| 매칭 | 23 | 21 | 26 |
| Precision / Recall | 0.920 / 0.460 | 0.840 / 0.420 | 0.929 / 0.520 |
| F1 | 0.613 | 0.560 | 0.667 |
| 매칭된 항목의 category 일치 | 0.826 | 0.905 | 0.923 |
| 매칭된 항목의 commitment 일치 | 0.826 | 0.810 | 0.846 |
| 매칭된 항목의 budget 일치 | 0.783 | 0.857 | 0.885 |
| 매칭된 항목의 expected_year 일치 | 0.913 | 0.905 | 0.923 |

중간 하락을 숨기거나 정답·평가 코드를 변경하지 않았습니다. 기존 두 매칭은 옆 행 키워드에 의존했습니다. r38은 제목이 `|`인 신호가 옆 행 `가로등`으로, r42는 `기간제 근로자 보수` 신호가 옆 행 `XR`로 정답 사업에 매칭되었습니다. 키워드 오염 제거로 이 매칭이 없어졌습니다. 이어서 표 구조를 일반 규칙으로 읽도록 보완하여 r38은 실제 가로등 제목·250,000,000원, r42는 실제 XR 제작 제목·430,000,000원으로 추출됩니다. 연도 머리글을 예산으로 읽지 않고 임금 행과 다음 사업의 경계도 지킵니다.

r13의 OCR 간격이 있는 디지털트윈 제목, r18의 이안류 감시 목적은 각각 smart_city와 safety_cctv로 바뀌었습니다. 단위 머리글 때문에 놓치던 r19·r27·r47도 읽습니다. 이 가운데 r47은 기존 grounding에서 여전히 needs_review입니다. 최종 수치도 이미 알려진 수기 세트의 회귀이며 새 독립 실데이터 정확도가 아닙니다. 특히 Recall 0.520은 아직 많은 수기 사례를 놓친다는 뜻입니다.

## 보존 제목 진단 재현

저장 자료를 수정하지 않고 `apps/api`에서 실행합니다. 기준 모듈은 별도 메모리 이름으로 읽으므로 현재 파일을 되돌리지 않습니다.

```bash
uv run python - <<'PY'
import gzip, hashlib, json, subprocess, sys, types
from collections import Counter
from pathlib import Path
from app.domain.taxonomy import classify_budget_category

path = Path('../../docs/data/seongnam-link-signals.jsonl.gz')
rows = [r for line in gzip.open(path, 'rt')
        if (r := json.loads(line))['stage'] == 'budget_line']
old = types.ModuleType('baseline_budget_taxonomy')
sys.modules[old.__name__] = old
exec(subprocess.check_output([
    'git', 'show', '2d3e7bb:apps/api/src/app/domain/taxonomy.py'
], text=True), old.__dict__)
before = [old.classify_category(r['title'])[0] for r in rows]
after = [classify_budget_category(r['title'])[0] for r in rows]
print('sha256', hashlib.sha256(path.read_bytes()).hexdigest())
print('rows/titles', len(rows), len({r['title'] for r in rows}))
print('stored/new changes', sum(r['category'] != c for r, c in zip(rows, after)))
print('title-only changes', sum(a != b for a, b in zip(before, after)))
print('other before/after', before.count('other'), after.count('other'))
for title in ['개별주택가격 조사·산정', '전국동시지방선거 추진',
              '성남시 청소년 교향악 페스티벌', '지역사회 통합건강증진사업(구강보건)']:
    saved = Counter(r['category'] for r in rows if r['title'] == title)
    print(title, dict(saved), old.classify_category(title), classify_budget_category(title))
PY
uv run pytest tests/unit/test_budget_classification.py tests/unit/test_budget_table_boundaries.py tests/unit/test_parsing.py tests/unit/test_llm.py -q
uv run ruff check src/app/domain/taxonomy.py src/app/llm/providers/heuristic.py src/app/parsing/chunking.py tests/unit/test_budget_classification.py tests/unit/test_budget_table_boundaries.py
uv run mypy src/app/domain/taxonomy.py src/app/llm/providers/heuristic.py src/app/parsing/chunking.py
```

## 남은 범위

이 규칙은 예산 분야의 보수적 분류기입니다. 보조금·이전경비인지 실제 구매 예산인지 판별하는 전체 정책, 새로운 기관의 표 구조, 독립 수기 정답 확대를 해결했다고 주장하지 않습니다. 약한 제목과 복합 사업을 사람이 검토하는 비용이 남습니다. 기존 데이터는 검토 기록을 보존하는 별도 절차 없이 재추출하지 않았습니다.
