import pytest
from pydantic import ValidationError

from app.api.schemas import ReviewDecisionIn


def test_review_accepts_explicit_category_correction():
    body = ReviewDecisionIn.model_validate({"action": "edit", "category": "welfare_care"})
    assert body.model_dump().get("category") == "welfare_care"


def test_review_rejects_unknown_category_instead_of_silently_ignoring_it():
    with pytest.raises(ValidationError):
        ReviewDecisionIn.model_validate({"action": "edit", "category": "invented"})
