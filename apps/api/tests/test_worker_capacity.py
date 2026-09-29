"""Bounded concurrency configuration protects small production workers."""

import pytest
from pydantic import ValidationError

from app.settings import Settings


@pytest.mark.parametrize("capacity", [0, -1, 65])
def test_rejects_invalid_worker_capacity(capacity: int) -> None:
    with pytest.raises(ValidationError, match="worker_max_jobs"):
        Settings(env="test", worker_max_jobs=capacity)


def test_accepts_worker_capacity_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_WORKER_MAX_JOBS", "2")
    assert Settings(env="test").worker_max_jobs == 2
