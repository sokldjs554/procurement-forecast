import hashlib
import json
from pathlib import Path

import pytest

from app.eval.holdout import load_holdout, score_cases
from app.eval.realistic import Prediction


def pred(title: str, budget: int | None = 1000) -> Prediction:
    return Prediction(title, (), "public_sw", "committed", budget, 2026)


def test_one_prediction_cannot_satisfy_two_projects_and_missing_fields_count() -> None:
    expected = [
        {"title_keywords": ["홈페이지"], "budget_krw": 1000},
        {"title_keywords": ["홈페이지 개선"], "budget_krw": 1000},
    ]
    result = score_cases([({"id": "a", "expected": expected}, [pred("홈페이지 개선")])])
    assert result["matched"] == 1
    assert result["recall"] == 0.5
    assert result["fields"]["budget_krw"]["ambiguous_identity_excluded"] == 2
    assert result["fields"]["budget_krw"]["accuracy_when_matched"] is None


def test_maximum_matching_is_order_independent_and_keywords_do_not_match() -> None:
    case = {
        "id": "a",
        "expected": [{"title_keywords": ["홈페이지"]}, {"title_keywords": ["홈페이지 개선"]}],
    }
    result = score_cases([(case, [pred("홈페이지 개선"), pred("홈페이지 구축")])])
    assert result["matched"] == 2
    unrelated = Prediction("청사 신축", ("홈페이지",), "facility", "planned", None, None)
    assert score_cases([(case, [unrelated])])["matched"] == 0


def test_negative_false_positives_and_zero_denominators_remain_visible() -> None:
    result = score_cases([({"id": "negative", "expected": []}, [pred("홈페이지")])])
    assert result["precision"] == 0.0
    assert result["recall"] is None
    assert result["negative_cases"] == 1
    assert result["negative_cases_with_predictions"] == 1
    assert result["fields"]["budget_krw"]["accuracy_when_matched"] is None


def test_null_gold_amount_means_no_amount_not_unscorable() -> None:
    result = score_cases(
        [
            (
                {"id": "a", "expected": [{"title_keywords": ["홈페이지"], "budget_krw": None}]},
                [pred("홈페이지", 1000)],
            )
        ]
    )
    assert result["fields"]["budget_krw"]["correct"] == 0
    assert result["fields"]["category"]["all_expected_eligible"] == 0


def write_manifest(tmp_path: Path) -> Path:
    source = "원문 예산서 홈페이지 신규 구축 1,000천원"
    (tmp_path / "source.txt").write_text(source)
    case = {
        "id": "a",
        "source_id": "doc",
        "institution": "기관",
        "doc_type": "budget_book",
        "fiscal_year": 2026,
        "date": "2026-01-01",
        "date_basis": "fiscal_year_context_only",
        "text": "홈페이지 신규 구축 1,000천원",
        "expected": [],
    }
    cases = (json.dumps(case, ensure_ascii=False) + "\n").encode()
    (tmp_path / "cases.jsonl").write_bytes(cases)
    manifest = {
        "schema_version": "source-holdout-v1",
        "cases_path": "cases.jsonl",
        "cases_sha256": hashlib.sha256(cases).hexdigest(),
        "label_origin": "source_grounded_ai_blind",
        "frozen_before_scoring": True,
        "sources": [
            {
                "id": "doc",
                "path": "source.txt",
                "url": "https://example.go.kr/doc",
                "sha256": hashlib.sha256(source.encode()).hexdigest(),
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def test_source_and_label_hashes_are_verified_before_model_call(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path)
    assert len(load_holdout(manifest)[1]) == 1
    (tmp_path / "source.txt").write_text("tampered")
    with pytest.raises(ValueError, match="source hash"):
        load_holdout(manifest)


def test_changed_gold_rejected(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path)
    with (tmp_path / "cases.jsonl").open("a") as f:
        f.write("\n")
    with pytest.raises(ValueError, match="cases hash"):
        load_holdout(manifest)


def test_excerpt_must_be_in_source_even_if_label_hash_is_updated(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path)
    value = json.loads(manifest.read_text())
    path = tmp_path / "cases.jsonl"
    case = json.loads(path.read_text())
    case["text"] = "없는 문장"
    content = json.dumps(case).encode()
    path.write_bytes(content)
    value["cases_sha256"] = hashlib.sha256(content).hexdigest()
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="excerpt"):
        load_holdout(manifest)


def test_scalar_title_keywords_cannot_match_individual_characters(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path)
    value = json.loads(manifest.read_text())
    path = tmp_path / "cases.jsonl"
    case = json.loads(path.read_text())
    case["expected"] = [{"title_keywords": "홈페이지"}]
    content = json.dumps(case).encode()
    path.write_bytes(content)
    value["cases_sha256"] = hashlib.sha256(content).hexdigest()
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="title phrases"):
        load_holdout(manifest)


def test_missing_unambiguous_project_reduces_end_to_end_field_recall() -> None:
    expected = [
        {"title_keywords": ["홈페이지"], "budget_krw": 1000},
        {"title_keywords": ["공유자전거"], "budget_krw": 2000},
    ]
    result = score_cases([({"id": "a", "expected": expected}, [pred("홈페이지")])])
    field = result["fields"]["budget_krw"]
    assert field["accuracy_when_matched"] == 1.0
    assert field["end_to_end_recall"] == 0.5


def test_ambiguous_identity_never_makes_field_scores_depend_on_prediction_order() -> None:
    expected = [
        {"title_keywords": ["홈페이지"], "budget_krw": 1000},
        {"title_keywords": ["홈페이지 개선"], "budget_krw": 2000},
    ]
    predictions = [pred("홈페이지 개선 A", 1000), pred("홈페이지 개선 B", 2000)]
    scores = [
        score_cases([({"id": "a", "expected": expected}, ps)])
        for ps in (predictions, list(reversed(predictions)))
    ]
    assert scores[0]["fields"] == scores[1]["fields"]
    assert scores[0]["fields"]["budget_krw"]["ambiguous_identity_excluded"] == 2
    assert scores[0]["fields"]["budget_krw"]["accuracy_when_matched"] is None
