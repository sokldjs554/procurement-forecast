from typing import Any

from sqlalchemy.dialects.postgresql.base import PGDialect


async def test_empty_institution_scope_never_reads_global_protection_history() -> None:
    from app.pipeline.link_state import protected_opportunity_ids

    class UnusedSession:
        async def scalars(self, statement: Any) -> Any:
            raise AssertionError("An empty scope must not query historical customer actions")

    assert await protected_opportunity_ids(UnusedSession(), set()) == set()


def test_protection_query_projects_only_ids_and_binds_institution_scope() -> None:
    from app.pipeline.link_state import protected_opportunities_query

    code = "scope'); DELETE FROM notifications; --"
    query = protected_opportunities_query({code})
    compiled = query.compile(dialect=PGDialect())
    sql = str(compiled)
    assert list(query.selected_columns.keys()) == ["id"]
    assert code not in sql
    assert any(code in value for value in compiled.params.values() if isinstance(value, list))
    assert "MATERIALIZED" in sql
    assert "signals.embedding" not in sql
    assert "opportunities.embedding" not in sql
    assert "documents.text" not in sql


def test_legacy_json_anchors_are_compared_without_casting_or_expanding_untrusted_values() -> None:
    from app.pipeline.link_state import protected_opportunities_query

    compiled = protected_opportunities_query({"LG-1"}).compile(
        dialect=PGDialect(paramstyle="named"), compile_kwargs={"literal_binds": True}
    )
    sql = str(compiled)
    # Nested containment tolerates nulls, strings and unexpected arrays in legacy JSON.
    # It also leaves the JSONB/array predicates eligible for their GIN indexes.
    assert "@>" in sql and "jsonb_build_object" in sql
    assert "'previous_link'" in sql and "'history'" in sql and "'items'" in sql
    assert "opportunity_relation_events.evidence_signal_ids @> ARRAY[" in sql
    assert "opportunity_relation_events.evidence_snapshot @>" in sql
    assert "CAST(review_items.resolution" not in sql
    assert "CAST(notifications.payload" not in sql
    assert "jsonb_array_elements" not in sql
    assert "jsonb_to_record" not in sql
