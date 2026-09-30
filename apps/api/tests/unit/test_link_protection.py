import sqlite3
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect


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


def test_strict_protection_does_not_freeze_customer_lifecycle() -> None:
    from app.pipeline.link_state import protected_opportunities_query

    sql = str(protected_opportunities_query({"LG-1"}, include_customer=False))
    assert "review_items" in sql and "opportunity_relation_events" in sql
    assert "notifications" not in sql and "recommendations" not in sql and "briefs" not in sql


def test_strict_protection_rejects_incomplete_or_empty_persisted_customer_cores() -> None:
    from app.pipeline.link_state import protected_opportunities_query

    sql = str(
        protected_opportunities_query({"LG-1"}, include_customer=False).compile(dialect=PGDialect())
    )
    assert "opportunity_customer_anchors" in sql
    assert "cardinality(opportunity_customer_anchors.signal_ids)" in sql
    assert "ANY (opportunity_customer_anchors.signal_ids)" in sql
    assert "signals.institution_code = opportunities.institution_code" in sql


def test_reprocessing_checks_legacy_customer_history_before_first_core_capture() -> None:
    from app.pipeline.link_state import customer_reprocessing_query

    sql = str(
        customer_reprocessing_query([41, 42]).compile(
            dialect=PGDialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "opportunity_customer_anchors.signal_ids &&" in sql
    assert "opportunity_signals.signal_id IN (41, 42)" in sql
    assert "notifications" in sql and "recommendations" in sql and "briefs" in sql
    assert "NOT (EXISTS" in sql  # Later additions to an already captured core remain replaceable.
    assert "LIMIT 1" in sql


class AnchorSession:
    def __init__(self, connection: sqlite3.Connection, *, history: bool = False) -> None:
        self.connection = connection
        self.history = history
        self.anchor: Any = None
        self.membership_reads = 0

    async def get(self, model: Any, opportunity_id: int, **kwargs: Any) -> Any:
        assert opportunity_id == 7
        return self.anchor

    async def scalar(self, query: Any) -> int | None:
        sql = str(query)
        assert "notifications" in sql and "recommendations" in sql and "briefs" in sql
        return 7 if self.history else None

    async def scalars(self, query: Any) -> Any:
        self.membership_reads += 1
        sql = str(query.compile(dialect=sqlite_dialect(), compile_kwargs={"literal_binds": True}))
        ids = [row[0] for row in self.connection.execute(sql).fetchall()]
        return SimpleNamespace(all=lambda: ids)

    async def execute(self, query: Any) -> None:
        compiled = query.compile(dialect=PGDialect())
        assert "ON CONFLICT (opportunity_id) DO NOTHING" in str(compiled)
        if self.anchor is None:
            self.anchor = SimpleNamespace(
                opportunity_id=compiled.params["opportunity_id"],
                signal_ids=compiled.params["signal_ids"],
            )


def _members(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE signals (id INTEGER, verdict TEXT)")
    connection.execute(
        "CREATE TABLE opportunity_signals (opportunity_id INTEGER, signal_id INTEGER, tentative BOOL)"
    )
    connection.executemany(
        "INSERT INTO signals VALUES (?, ?)",
        [(i, "accepted") for i in range(1, 1006)] + [(1006, "rejected")],
    )
    connection.executemany(
        "INSERT INTO opportunity_signals VALUES (?, ?, ?)",
        [(7, i, False) for i in range(1, 1003)]
        + [(7, 1003, True), (8, 1004, False), (7, 1006, False)],
    )


async def test_first_customer_action_captures_all_eligible_members_and_keeps_original_core() -> (
    None
):
    from app.pipeline.link_state import capture_customer_anchor

    with sqlite3.connect(":memory:") as connection:
        _members(connection)
        session = AnchorSession(connection)
        anchor = await capture_customer_anchor(session, 7)
        assert anchor.signal_ids == list(range(1, 1003))
        # A new accepted arrival must not silently become part of the original customer core.
        connection.execute("INSERT INTO opportunity_signals VALUES (7, 1005, false)")
        again = await capture_customer_anchor(session, 7)
        assert again is anchor
        assert 1005 not in again.signal_ids
        assert session.membership_reads == 1


async def test_legacy_customer_anchor_is_created_only_when_history_exists() -> None:
    from app.pipeline.link_state import ensure_customer_anchor

    with sqlite3.connect(":memory:") as connection:
        _members(connection)
        session = AnchorSession(connection)
        assert await ensure_customer_anchor(session, 7) is None
        assert session.membership_reads == 0
        session.history = True
        anchor = await ensure_customer_anchor(session, 7)
        assert anchor is not None and anchor.signal_ids == list(range(1, 1003))
        session.history = False  # Immutable core survives even if a recommendation is removed.
        assert await ensure_customer_anchor(session, 7) is anchor
        assert session.membership_reads == 1
