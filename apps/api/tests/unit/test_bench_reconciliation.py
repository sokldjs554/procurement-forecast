"""Benchmark the operational reconciliation scope, not a cheap ID-only substitute."""

from app.bench import SIZES, additional_queries


def test_reconciliation_scope_keeps_vectors_and_source_locations_without_truncation() -> None:
    queries = {q.key: q for q in additional_queries()}
    scope = queries["reconciliation_institution_signals"]
    assert scope.before is None
    assert "signals.embedding" in scope.after
    assert "sources.key" in scope.after and "documents.external_id" in scope.after
    assert "document_chunks.seq" in scope.after and "document_chunks.labels" in scope.after
    assert "LEFT OUTER JOIN document_chunks" in scope.after
    assert "UNION" in scope.after  # includes foreign-owner legacy memberships
    assert "ORDER BY signals.id" in scope.after
    assert "LIMIT" not in scope.after
    assert "documents.text" not in scope.after and "document_chunks.text" not in scope.after


def test_relation_event_protection_measures_current_member_containment_hit_and_miss() -> None:
    queries = {q.key: q for q in additional_queries()}
    for key in ("reconciliation_evidence_contains_hit", "reconciliation_evidence_contains_miss"):
        query = queries[key]
        assert query.before is None
        assert "SELECT opportunity_relation_events.id" in query.after
        assert "@> CAST(ARRAY[" in query.after and "AS BIGINT[])" in query.after
        assert "LIMIT 1" in query.after
    assert "ARRAY[1]" in queries["reconciliation_evidence_contains_hit"].after
    assert "ARRAY[0]" in queries["reconciliation_evidence_contains_miss"].after


def test_reconciliation_generation_queries_cover_dirty_and_recommendation_outbox() -> None:
    queries = {q.key: q for q in additional_queries()}
    dirty = queries["reconciliation_dirty_sweep"].after
    outbox = queries["reconciliation_pending_dispatch"].after
    assert "generation > link_reconciliation_states.reconciled_generation" in dirty
    assert "LIMIT" not in dirty
    assert "reconciled_generation > link_reconciliation_states.recommendations_generation" in outbox
    assert " OR " in outbox and "LIMIT 200" in outbox
    assert "ORDER BY link_reconciliation_states.institution_code" in outbox


def test_summary_tie_metadata_is_a_four_member_projection_without_vectors_or_text() -> None:
    queries = {q.key: q for q in additional_queries()}
    sql = queries["link_summary_tie_metadata"].after
    assert "signals.id IN (1, 100001, 200001, 300001)" in sql
    assert "signals.embedding" not in sql and "signals.summary" not in sql
    assert "document_chunks.text" not in sql and "documents.text" not in sql
    assert "sources.key" in sql and "document_chunks.labels" in sql


def test_small_bench_uses_its_actual_scope_size_and_member_ids() -> None:
    sizes = {**SIZES, "institutions": 2, "signals": 8, "opportunities": 4}
    queries = {q.key: q for q in additional_queries(sizes)}
    assert "signals.id IN (1, 5)" in queries["link_summary_tie_metadata"].after


def test_recommendation_and_notification_queries_use_actual_unsettled_generation_gate() -> None:
    queries = {q.key: q for q in additional_queries()}
    for key in (
        "recommend_semantic_settled",
        "recommend_keywords_settled",
        "recommend_category_settled",
        "notify_candidates_settled",
    ):
        query = queries[key]
        assert query.before is None
        assert "NOT (EXISTS" in query.after
        assert (
            "link_reconciliation_states.institution_code = opportunities.institution_code"
            in query.after
        )
        assert "generation > link_reconciliation_states.reconciled_generation" in query.after
        assert "opportunities.embedding" in query.after
    vector = queries["recommend_semantic_settled"]
    assert "<=>" in vector.after and "LIMIT 300" in vector.after
    assert vector.setup_after == ("SET LOCAL hnsw.ef_search = 300",)
    assert vector.exact == vector.after
    assert "recommendations.org_id = 7" in queries["notify_candidates_settled"].after
    assert "LIMIT" not in queries["notify_candidates_settled"].after


def test_history_protection_uses_shared_scope_and_nonempty_json_index_probes() -> None:
    queries = {q.key: q for q in additional_queries()}
    protected = queries["reconciliation_protected_opportunities"]
    assert protected.before is None
    assert "B-0001" in protected.after
    assert "EXISTS" in protected.after and "review_items" in protected.after
    assert "opportunity_relation_events" in protected.after
    assert "notifications" in protected.after
    assert "LIMIT" not in protected.after
    for kind in ("review_history", "notification_history"):
        for outcome in ("hit", "miss"):
            query = queries[f"reconciliation_{kind}_{outcome}"]
            assert query.before is None
            assert "@>" in query.after
            assert "LIMIT 1" in query.after


def test_customer_anchor_scope_load_and_strict_queries_follow_current_core_validation() -> None:
    queries = {q.key: q for q in additional_queries()}
    anchors = queries["reconciliation_customer_anchor_scope"].after
    assert "opportunity_customer_anchors.signal_ids" in anchors
    assert "opportunity_customer_anchors.created_at" in anchors
    assert "UNION" in anchors and "LIMIT" not in anchors
    strict = queries["reconciliation_strict_opportunities"].after
    assert "cardinality(opportunity_customer_anchors.signal_ids)" in strict
    assert "count(signals.id)" in strict
    assert "ANY (opportunity_customer_anchors.signal_ids)" in strict
    assert "notifications" not in strict and "recommendations" not in strict
    for key, target in (
        ("reconciliation_strict_target_customer", 3000),
        ("reconciliation_strict_target_invalid_core", 5000),
    ):
        sql = queries[key].after
        assert f"opportunities.id IN ({target})" in sql
        assert f"opportunity_signals.opportunity_id IN ({target})" in sql
        assert "LIMIT 1" in sql


def test_customer_anchor_reprocessing_uses_document_signal_ids_and_typed_overlap() -> None:
    queries = {q.key: q for q in additional_queries()}
    hit = queries["customer_anchor_document_protection_hit"].after
    miss = queries["customer_anchor_document_protection_miss"].after
    assert "opportunity_customer_anchors.opportunity_id" in hit
    assert "&& CAST(ARRAY[1, 150001, 300001] AS BIGINT[])" in hit
    assert "LIMIT 1" in hit and "LIMIT 1" in miss
    assert "ARRAY[-400001, -550001, -700001]" in miss
