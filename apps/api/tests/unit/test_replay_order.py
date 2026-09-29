"""Audit order must reach the linker, whose own batch sort otherwise hides it."""

from typing import Any

import pytest

from app.pipeline import link_replay


def rows() -> list[dict[str, Any]]:
    return [
        {"key": "b", "document": {"published_at": "2026-02-01", "doc_type": "budget_book"}},
        {"key": "a", "document": {"published_at": "2026-01-01", "doc_type": "council_minutes"}},
        {"key": "c", "document": {"published_at": "2026-03-01", "doc_type": "bid_notice"}},
    ]


def test_reverse_is_single_signal_batches_so_linker_cannot_resort_it() -> None:
    assert hasattr(link_replay, "replay_batches"), "replay audit needs explicit batches"
    assert link_replay.replay_batches(rows(), order="reverse") == [["c"], ["b"], ["a"]]


def test_publication_order_and_budget_first_are_explicit_and_stable() -> None:
    assert hasattr(link_replay, "replay_batches"), "replay audit needs explicit batches"
    assert link_replay.replay_batches(rows()) == [["a", "b", "c"]]
    assert link_replay.replay_batches(rows(), first=["budget_book"]) == [["b"], ["a", "c"]]


def test_replay_rejects_mixed_order_requests_and_duplicate_signal_keys() -> None:
    assert hasattr(link_replay, "replay_batches"), "replay audit needs input validation"
    with pytest.raises(ValueError, match="cannot combine"):
        link_replay.replay_batches(rows(), first=["budget_book"], order="reverse")
    with pytest.raises(ValueError, match="duplicate"):
        link_replay.replay_batches(rows() + rows()[:1])


def test_replay_result_cannot_be_misread_as_historical_accuracy() -> None:
    result = link_replay.ReplayResult(signals=3, linked=3, groups=[["a", "b"], ["c"]])
    assert result.summary().get("scope") == "retrospective_link_replay"
    assert result.summary().get("accuracy_evaluated") is False
