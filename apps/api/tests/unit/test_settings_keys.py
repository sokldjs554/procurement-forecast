"""Provider keys pasted into a secrets form arrive with stray whitespace or quotes."""

from __future__ import annotations

import pytest

from app.settings import Settings


@pytest.mark.parametrize(
    "pasted",
    ["abc123\n", "  abc123  ", '"abc123"', "'abc123'\n", '" abc123 "'],
)
def test_a_pasted_key_is_trimmed(monkeypatch: pytest.MonkeyPatch, pasted: str) -> None:
    monkeypatch.setenv("APP_CLIK_API_KEY", pasted)
    monkeypatch.setenv("APP_DATA_GO_KR_SERVICE_KEY", pasted)
    settings = Settings()
    assert settings.clik_api_key is not None
    assert settings.clik_api_key.get_secret_value() == "abc123"
    assert settings.data_go_kr_service_key is not None
    assert settings.data_go_kr_service_key.get_secret_value() == "abc123"


def test_an_empty_secret_still_means_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_CLIK_API_KEY", "  \n")
    assert not Settings().clik_api_key


def test_a_key_with_inner_symbols_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_DATA_GO_KR_SERVICE_KEY", "a+b/c==")
    key = Settings().data_go_kr_service_key
    assert key is not None and key.get_secret_value() == "a+b/c=="
