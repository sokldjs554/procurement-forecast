from typing import Any

from app.bench import SIZES, Query, render, run_query


def test_report_labels_unimplemented_before_without_fabricating_a_timing() -> None:
    report: dict[str, Any] = {
        "sizes": SIZES,
        "postgres": "PostgreSQL test",
        "pgvector": "test",
        "machine": "test",
        "migrate_seconds": 1,
        "queries": [
            {
                "title": "신규 관계 조회",
                "before": None,
                "after": {"ms": 2.5, "rows": 51, "plan": ["Index Scan"], "recall": None},
                "setup_after": [],
            }
        ],
    }
    rendered = render(report)
    assert "| 신규 관계 조회 | 미구현 | 2.5 ms | — | 51 |" in rendered
    assert "- 전: 미구현" in rendered
    assert "0.00 ms" not in rendered


async def test_after_only_query_does_not_execute_against_initial_schema() -> None:
    class InitialSchemaConnection:
        def transaction(self) -> None:
            raise AssertionError("An unimplemented query must not execute")

    result = await run_query(
        InitialSchemaConnection(),  # type: ignore[arg-type]
        Query("new", "new", None, "SELECT actual_new_table FROM new_table"),
        "before",
    )
    assert result is None


def test_new_queries_use_narrow_projections_and_real_bounded_query_shapes() -> None:
    from app.bench import additional_queries

    queries = {query.key: query for query in additional_queries()}
    assert all(query.before is None for query in queries.values())
    coverage = queries["coverage_documents"].after
    assert "documents.raw_uri" in coverage
    assert "documents.content_hash" not in coverage
    assert "documents.structured," not in coverage
    assert "DISTINCT ON (ingest_runs.source_id)" in queries["coverage_latest_runs"].after
    relation = queries["relation_endpoint_page"].after
    assert (
        "opportunity_relations.project_id = 1 OR opportunity_relations.contract_id = 1" in relation
    )
    assert "opportunity_relations.id > 0" in relation
    assert "LIMIT 51" in relation
    assert "@>" in queries["relation_evidence_protection_hit"].after
    mixed = queries["relation_mixed_audit"].after
    assert "HAVING" in mixed and "budget_line" in mixed and "bid_notice" in mixed
    assert "LIMIT 51" in mixed
    review = queries["review_export_page"].after
    assert "signals.embedding" not in review
    assert "signals.summary" in review and "document_chunks.text" in review
    assert "LIMIT 501" in review
