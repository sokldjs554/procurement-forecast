from app.pipeline.link_reconcile import assign_existing_ids


def test_unchanged_groups_keep_their_ids_before_partial_overlaps():
    groups = [{1, 2}, {3}, {4, 5}]
    old = {20: {1, 2}, 10: {3, 4}, 30: {5}}
    assert assign_existing_ids(groups, old) == [20, 10, 30]


def test_splits_and_merges_never_assign_an_old_id_twice():
    result = assign_existing_ids([{1}, {2}, {3, 4}], {9: {1, 2}, 8: {3}, 7: {4}})
    assert result == [9, None, 7]
    assert len([x for x in result if x]) == len({x for x in result if x})


def test_reuse_never_relabels_an_unrelated_old_identity():
    assert assign_existing_ids([{10}, {11}], {1: {1}, 2: {2}}) == [None, None]
