"""Production adapter must fail closed without contacting any external provider."""

import base64
import runpy
from pathlib import Path

import pytest

from app.settings import get_settings

RUNTIME = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/render-runtime.py"))


@pytest.fixture
def production_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    credentials = tmp_path / "gcs.json"
    credentials.write_text("{}")  # Only presence is validated here; IAM checks run at startup.
    environment = {
        "APP_ENV": "production",
        "APP_COOKIE_SECURE": "true",
        "APP_DATABASE_URL": "postgres://user:encoded%40password@db.internal:5432/app",
        "APP_REDIS_URL": "redis://queue.internal:6379",
        "APP_JWT_SECRET": "unit-test-only-" * 4,
        "APP_BILLING_KEY_ENCRYPTION_KEY": base64.b64encode(bytes(range(32))).decode(),
        "APP_STORAGE_URL": "gs://test-raw-documents/raw",
        "GOOGLE_APPLICATION_CREDENTIALS": str(credentials),
        "APP_PUBLIC_WEB_URL": "https://procurement.test",
        "APP_DATA_GO_KR_SERVICE_KEY": "test-source-key",
        "APP_PAYMENT_PROVIDER": "toss",
        "APP_TOSS_CLIENT_KEY": "test-client-key",
        "APP_TOSS_SECRET_KEY": "test-secret-key",
        "APP_SMTP_HOST": "smtp.provider.test",
        "APP_SMTP_PORT": "587",
        "APP_SMTP_USE_TLS": "true",
        "APP_SMTP_USERNAME": "test-user",
        "APP_SMTP_PASSWORD": "test-password",
        "APP_MAIL_FROM": "alerts@procurement.test",
        "APP_LLM_PROVIDER": "heuristic",
        "APP_EMBEDDING_PROVIDER": "hashing",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_normalizes_render_postgres_without_changing_encoded_credentials(
    production_env: None,
) -> None:
    RUNTIME["configure"]()
    RUNTIME["validate"]()
    settings = get_settings()
    assert (
        settings.database_url == "postgresql+asyncpg://user:encoded%40password@db.internal:5432/app"
    )
    assert settings.cors_origins == ["https://procurement.test"]


def test_render_generated_256_bit_base64_is_a_usable_fernet_key(
    production_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet

    raw_key = b"\xfb" * 32
    standard_base64 = base64.b64encode(raw_key).decode()
    assert "+" in standard_base64 and "/" in standard_base64
    monkeypatch.setenv("APP_BILLING_KEY_ENCRYPTION_KEY", standard_base64)
    RUNTIME["configure"]()
    RUNTIME["validate"]()
    key = get_settings().billing_key_encryption_key.get_secret_value()
    assert key == base64.urlsafe_b64encode(raw_key).decode()
    cipher = Fernet(key)
    assert cipher.decrypt(cipher.encrypt(b"test-billing-key")) == b"test-billing-key"


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("APP_STORAGE_URL", "file:///tmp/raw", "shared durable"),
        ("APP_PAYMENT_PROVIDER", "fake", "fake payment"),
        ("APP_SMTP_HOST", "localhost", "real SMTP"),
        ("APP_COOKIE_SECURE", "false", "COOKIE_SECURE"),
        ("APP_PUBLIC_WEB_URL", "http://localhost:3000", "real HTTPS"),
        ("APP_DATA_GO_KR_SERVICE_KEY", "", "APP_DATA_GO_KR_SERVICE_KEY"),
    ],
)
def test_refuses_incomplete_production(
    production_env: None,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv(key, value)
    RUNTIME["configure"]()
    with pytest.raises(ValueError, match=message):
        RUNTIME["validate"]()


def test_settings_errors_do_not_disclose_secrets(
    production_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_DB_POOL_SIZE", "invalid")
    RUNTIME["configure"]()
    with pytest.raises(ValueError, match="Invalid application settings") as error:
        RUNTIME["validate"]()
    assert "test-password" not in str(error.value)
    assert "test-secret-key" not in str(error.value)
