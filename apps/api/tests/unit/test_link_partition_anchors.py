"""Customer history anchors its original evidence while lifecycle evidence can grow."""

from datetime import date, timedelta
from itertools import permutations

import pytest

from app.db.models import Signal
from app.pipeline.link_partition import PartitionAnchor, plan_partition

TODAY = date(2026, 9, 29)


def signal(number, *, refs=None, stage="order_plan", day=0, title="청사 냉난방기 교체"):
    return Signal(
        id=number,
        document_id=number,
        institution_code="A",
        title=title,
        summary="공공청사 시설개선",
        stage=stage,
        category="facility",
        observed_at=TODAY + timedelta(days=day),
        verdict="accepted",
        budget_krw=100_000_000,
        embedding=[1.0, 0.0],
        external_refs=refs or {},
        keywords=["청사"],
        evidence=[],
    )


def plan(rows, anchors, keys=None):
    universe = [*rows, *(member for anchor in anchors for member in anchor.members)]
    keys = keys if keys is not None else {row.id: f"key-{row.id:03}" for row in universe}
    return plan_partition(rows, keys, anchors=anchors, threshold=0.6, review_band=0.08, today=TODAY)


def partitions(groups):
    return sorted(sorted(row.id for row in group.members) for group in groups)


def test_customer_anchor_receives_bids_revisions_and_awards_without_rewriting_core():
    core = signal(1, refs={"order_plan_no": "P"}, day=-2)
    following = [
        signal(2, refs={"order_plan_no": "P", "bid_notice_no": "B1"}, stage="bid_notice"),
        signal(3, refs={"bid_notice_no": "B1", "bid_notice_ord": "001"}, stage="bid_notice", day=1),
        signal(4, refs={"bid_notice_no": "B1"}, stage="award", day=2),
    ]
    for arrival in permutations(following):
        groups = plan(arrival, [PartitionAnchor(70, [core])])
        assert partitions(groups) == [[1, 2, 3, 4]]
        group = groups[0]
        assert group.anchored_opportunity_id == 70
        assert group.anchor_signal_ids == frozenset({1})
        assert set(group.decisions) == {2, 3, 4}
        assert group.summary.stage == "award" and group.summary.signal_count == 4


def test_later_competitor_can_detach_an_automatic_append_but_never_the_core():
    core = signal(1, refs={"order_plan_no": "P", "bid_notice_no": "B1"}, day=-2)
    competitor = signal(2, refs={"prespec_no": "S", "bid_notice_no": "B2"}, day=-1)
    append = signal(3, refs={"order_plan_no": "P", "prespec_no": "S"})
    anchor = PartitionAnchor(70, [core])
    assert partitions(plan([append], [anchor])) == [[1, 3]]
    for arrival in permutations([competitor, append]):
        groups = plan(arrival, [anchor])
        assert partitions(groups) == [[1], [2], [3]]
        assert groups[0].anchored_opportunity_id == 70
        assert groups[0].anchor_signal_ids == frozenset({1})
        assert groups[0].decisions == {}


def test_anchors_never_merge_and_input_anchor_order_does_not_choose_identity():
    first = signal(1, refs={"order_plan_no": "P", "bid_notice_no": "B1"})
    second = signal(2, refs={"order_plan_no": "P", "bid_notice_no": "B2"})
    cancel = signal(
        3, refs={"order_plan_no": "P", "cancels_bid_notice_no": "B1"}, stage="bid_notice", day=1
    )
    anchors = [PartitionAnchor(900, [first]), PartitionAnchor(20, [second])]
    for order in permutations(anchors):
        groups = plan([cancel], order)
        assert [
            (group.anchored_opportunity_id, sorted(row.id for row in group.members))
            for group in groups
        ] == [(900, [1, 3]), (20, [2])]


def test_anchor_and_automatic_members_share_the_same_stable_summary_order():
    core = signal(1, refs={"order_plan_no": "P"}, title="청사 냉난방기 교체 1")
    append = signal(2, refs={"order_plan_no": "P"}, title="청사 냉난방기 교체 2")
    group = plan([append], [PartitionAnchor(70, [core])], {1: "z", 2: "a"})[0]
    assert [row.id for row in group.members] == [2, 1]
    assert group.summary.title == core.title


@pytest.mark.parametrize(
    "defect",
    [
        "auto_overlap",
        "core_overlap",
        "empty",
        "same_opportunity",
        "missing_key",
        "unaccepted",
        "mixed_institutions",
    ],
)
def test_ambiguous_or_invalid_anchor_universes_are_rejected(defect):
    first, second = signal(1), signal(2)
    rows = [second]
    anchors = [PartitionAnchor(70, [first])]
    keys = {1: "first", 2: "second"}
    if defect == "auto_overlap":
        rows.append(first)
    elif defect == "core_overlap":
        anchors.append(PartitionAnchor(80, [first]))
    elif defect == "empty":
        anchors = [PartitionAnchor(70, [])]
    elif defect == "same_opportunity":
        rows = []
        anchors.append(PartitionAnchor(70, [second]))
    elif defect == "missing_key":
        del keys[1]
    elif defect == "unaccepted":
        first.verdict = "rejected"
    else:
        rows = []
        second.institution_code = "B"
        anchors = [PartitionAnchor(70, [first, second])]
    message = {
        "auto_overlap": "duplicate signal ID",
        "core_overlap": "duplicate signal ID",
        "empty": "anchor requires",
        "same_opportunity": "duplicate anchored opportunity",
        "missing_key": "missing stable key",
        "unaccepted": "only accepted",
        "mixed_institutions": "one institution",
    }[defect]
    with pytest.raises(ValueError, match=message):
        plan(rows, anchors, keys)
