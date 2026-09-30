"""The static demo's file names (the browser builds the same ones, apps/web/src/lib/demo/fetch.ts)."""

from app.demo.snapshot import file_key


def test_example_document_exports_complete_text_without_internal_fields(tmp_path) -> None:
    import json
    from datetime import date

    from app.db.models import Document
    from app.demo import snapshot

    doc = Document(
        id=12,
        title="예시 회의록",
        text="앞부분\n예산 근거\n발췌에 없는 다음 안건",
        doc_type="council_minutes",
        published_at=date(2026, 9, 30),
        structured={"truth_id": "must-not-be-published"},
        raw_uri="private://raw",
    )
    snapshot.write_example_document(tmp_path, doc)
    result = json.loads((tmp_path / "documents/12.json").read_text())
    assert result["text"] == "앞부분\n예산 근거\n발췌에 없는 다음 안건"
    assert result["synthetic"] is True
    assert "structured" not in result
    assert "raw_uri" not in result


def test_showcase_requires_a_complete_document_chain_and_does_not_depend_on_fixed_ids() -> None:
    from app.demo import snapshot

    def detail(opp_id, title, stages):
        return {
            "id": opp_id,
            "title": title,
            "status": "open",
            "tender_out": False,
            "signals": [{"stage": s, "document": {"id": i}} for i, s in enumerate(stages)],
        }

    short = detail(1, "디지털트윈 단일 신호", ["council_mention"])
    complete = detail(
        991, "디지털트윈 기반 도시관리", ["council_mention", "budget_line", "order_plan", "prespec"]
    )
    fallback = detail(2, "다른 연결 사업", ["council_mention", "budget_line", "order_plan"])
    assert snapshot.choose_showcase([short, fallback, complete])["id"] == 991
    assert snapshot.choose_showcase([short, fallback])["id"] == 2
    assert snapshot.choose_showcase([short]) is None
    published = {**complete, "id": 1, "tender_out": True}
    closed = {**complete, "id": 2, "status": "closed"}
    assert snapshot.choose_showcase([published, closed, complete])["id"] == 991
    assert snapshot.choose_showcase([published, closed]) is None
    repeated = {**complete, "signals": [*complete["signals"], complete["signals"][0]]}
    assert snapshot.choose_showcase([repeated])["document_count"] == 4


def test_file_key_matches_the_browser_side() -> None:
    assert file_key("/api/me", {}) == "me"
    assert (
        file_key("/api/admin/jobs", {"status": "failed", "limit": 100})
        == "admin_jobs_limit_100_status_failed"
    )
    assert file_key("/api/admin/llm/usage", {"days": 7}) == "admin_llm_usage_days_7"
