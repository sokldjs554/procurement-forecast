"""Different district budgets are not a department rename."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import Opportunity, Signal
from app.pipeline import link


@pytest.mark.parametrize(
    ("left", "right", "conflict"),
    [
        ("수정구보건소 건강증진과", "중원구보건소 건강증진과", True),
        ("분당구 건설과", "중원구 건설과", True),
        ("분당구 건설과", "분당구청 도로관리과", False),
        ("문화관광과", "문화예술과", False),
        (None, "분당구 건설과", False),
        ("도시재생과", "도시균형발전과", False),
    ],
)
def test_explicit_district_conflict(left, right, conflict):
    assert hasattr(link, "budget_jurisdictions_conflict"), "district veto missing"
    assert link.budget_jurisdictions_conflict(left, right) is conflict


async def test_first_same_name_row_cannot_join_another_district():
    """A single candidate must be rejected even before duplicate names reveal ambiguity."""
    signal = Signal(
        id=5,
        document_id=20,
        title="정신건강 AI체험관 마인드 피트니스 사업",
        department="중원구보건소 건강증진과",
        stage="budget_line",
    )
    candidate = Opportunity(id=1)
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                all=lambda: [(1, 10, signal.title, "수정구보건소 건강증진과")]
            )
        )
    )
    assert await link._without_other_budget_rows(session, signal, [candidate]) == []
