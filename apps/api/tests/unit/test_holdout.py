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


def test_speaker_context_comes_from_hashed_source_never_gold_role(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path)
    value = json.loads(manifest.read_text())
    source = (
        "○위원 김유진 홈페이지 도입은 어떻습니까?\n○정보과장 이민호 홈페이지 구축을 검토하겠습니다."
    )
    (tmp_path / "source.txt").write_text(source)
    value["sources"][0]["sha256"] = hashlib.sha256(source.encode()).hexdigest()
    case = json.loads((tmp_path / "cases.jsonl").read_text())
    case.update(
        doc_type="council_minutes",
        text="홈페이지 구축을 검토하겠습니다.",
        labels=["위원 김유진"],
        speaker_role="member",
    )
    content = json.dumps(case).encode()
    (tmp_path / "cases.jsonl").write_bytes(content)
    value["cases_sha256"] = hashlib.sha256(content).hexdigest()
    manifest.write_text(json.dumps(value))
    _, cases = load_holdout(manifest)
    assert cases[0]["_source_labels"] == ["정보과장 이민호"]
    assert cases[0]["_source_text"][cases[0]["_char_start"] :] == case["text"]


def _two_case_manifest(tmp_path: Path) -> Path:
    source = "○정보과장 이민호  내년 본예산에 홈페이지 전면 개편 2억 원을 반영하겠습니다.\n○위원 김유진  수당 30만 원은 그대로입니까?"
    (tmp_path / "source.txt").write_text(source)
    turns = source.split("\n")
    cases = [
        {
            "id": f"c{n}",
            "source_id": "doc",
            "institution": "기관",
            "doc_type": "council_minutes",
            "date": "2026-09-10",
            "date_basis": "meeting_date",
            "text": text,
            "expected": expected,
        }
        for n, (text, expected) in enumerate(
            [
                (turns[0], [{"title_keywords": ["홈페이지"], "budget_krw": 200_000_000}]),
                (turns[1], []),
            ]
        )
    ]
    content = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases).encode()
    (tmp_path / "cases.jsonl").write_bytes(content)
    manifest = {
        "schema_version": "source-holdout-v1",
        "cases_path": "cases.jsonl",
        "cases_sha256": hashlib.sha256(content).hexdigest(),
        "label_origin": "test",
        "frozen_before_scoring": True,
        "sources": [
            {
                "id": "doc",
                "path": "source.txt",
                "sha256": hashlib.sha256(source.encode()).hexdigest(),
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


class _Claude:
    """Stands in for the Anthropic provider: one signal per call, a fixed token bill."""

    name = "anthropic"
    extract_model = "claude-opus-5"
    extract_effort = "low"

    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, ctx):  # type: ignore[no-untyped-def]
        from app.llm.schemas import ExtractedSignal, ExtractionOutput
        from app.llm.types import LLMResult, Usage

        self.calls += 1
        signal = ExtractedSignal(
            title="홈페이지 전면 개편",
            summary="홈페이지를 개편한다",
            category="public_sw",
            institution_mention=None,
            department="정보과",
            budget_text="2억 원",
            budget_krw=200_000_000,
            timing_text="내년 본예산에",
            expected_year=2027,
            expected_half=None,
            commitment="committed",
            procurement_type="service",
            keywords=["홈페이지"],
            evidence=["홈페이지 전면 개편 2억 원을 반영하겠습니다"],
            confidence=0.9,
        )
        usage = Usage(input_tokens=2_000, output_tokens=800)  # $0.03 at $5 / $25 per Mtok
        return LLMResult(
            ExtractionOutput(signals=[signal]), "anthropic", self.extract_model, "t", usage
        )


async def test_a_claude_run_stops_at_its_cap_and_never_scores_uncalled_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.eval.holdout import evaluate_holdout

    fake = _Claude()
    monkeypatch.setattr("app.eval.llm_compare.build_provider", lambda *a, **k: fake)
    manifest = _two_case_manifest(tmp_path)
    result = await evaluate_holdout(
        manifest, code_revision="t", extractor="claude-opus-5:low", max_usd=0.05
    )
    assert fake.calls == 1  # the second call's estimate would pass $0.05
    assert result["extractor"] == "claude-opus-5 · low"
    assert result["api_cost_usd"] == pytest.approx(0.03)
    assert result["skipped_cases"] == [{"case": "c1", "why": "spend cap $0.05 reached"}]
    assert result["raw"]["cases"] == 1  # the skipped case is not an empty "correct negative"
    assert result["after_verifier"]["matched"] == 1


async def test_a_saved_run_is_scored_again_for_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.eval.holdout import evaluate_holdout

    fake = _Claude()
    monkeypatch.setattr("app.eval.llm_compare.build_provider", lambda *a, **k: fake)
    manifest = _two_case_manifest(tmp_path)
    first = await evaluate_holdout(
        manifest, code_revision="t", extractor="claude-opus-5:low", max_usd=1
    )
    saved = tmp_path / "run.json"
    saved.write_text(json.dumps(first, ensure_ascii=False))
    again = await evaluate_holdout(manifest, code_revision="t", replay=saved)
    assert fake.calls == 2  # only the first run called the model
    assert again["api_cost_usd"] == 0
    assert again["extractor"] == "replay of claude-opus-5 · low"
    assert again["after_verifier"] == first["after_verifier"]
    assert again["raw"] == first["raw"]


def test_a_prediction_of_an_ambiguous_project_is_set_aside_not_scored() -> None:
    case = {
        "id": "a",
        "expected": [{"title_keywords": ["카페골목"], "budget_krw": 420_000_000}],
        "ambiguous": ["지중화"],
    }
    predictions = [pred("방배카페골목 보행환경 개선", 420_000_000), pred("동광로 지중화사업")]
    result = score_cases([(case, predictions)])
    assert result["matched"] == 1
    assert result["predicted"] == 1 and result["precision"] == 1.0
    assert result["ambiguous_predictions_set_aside"] == 1
    assert not any("unexpected" in f for f in result["failures"])
    # A missed ambiguous project is not a miss either.
    assert (
        score_cases([({"id": "b", "expected": [], "ambiguous": ["지중화"]}, [])])["recall"] is None
    )
