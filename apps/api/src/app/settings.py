"""Runtime configuration.

Every knob is an environment variable prefixed with ``APP_``. Defaults are chosen so that
``docker compose up`` works offline: the heuristic extractor replaces the LLM, a hashing embedder
replaces the embedding API, and the fake payment provider replaces Toss Payments.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]

_DEV_JWT_SECRET = "dev-only-change-me-dev-only-change-me"  # noqa: S105 - refused outside local
_DEV_FERNET_KEY = "q3a1tq8ib5zU4DPAqE9L3eY1s8r9Oq9w2m4YkB2yX0c="


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["local", "test", "staging", "production"] = "local"
    service_name: str = "app-api"
    public_web_url: str = "http://localhost:3000"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    db_pool_size: int = 10
    db_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"
    storage_url: str = "file://./.data/raw"
    worker_max_jobs: int = Field(default=8, ge=1, le=64)

    jwt_secret: SecretStr = SecretStr(_DEV_JWT_SECRET)
    jwt_ttl_minutes: int = 60 * 12
    cookie_secure: bool = False

    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.1
    log_json: bool = True
    log_level: str = "INFO"

    # --- LLM -------------------------------------------------------------------------------
    llm_provider: Literal["anthropic", "heuristic"] = "heuristic"
    anthropic_api_key: SecretStr | None = None
    llm_extract_model: str = "claude-opus-5"
    llm_extract_effort: Effort = "low"
    llm_brief_model: str = "claude-opus-5"
    llm_brief_effort: Effort = "medium"
    llm_timeout_seconds: float = 90.0
    llm_max_retries: int = 2
    llm_daily_budget_usd: float = 25.0
    llm_prompt_cache: bool = True
    llm_server_side_fallback: bool = True

    # --- embeddings --------------------------------------------------------------------------
    embedding_provider: Literal["hashing", "voyage"] = "hashing"
    embedding_dim: int = 512
    voyage_api_key: SecretStr | None = None
    voyage_model: str = "voyage-3.5"

    # --- pipeline -----------------------------------------------------------------------------
    triage_threshold: float = 0.35
    link_threshold: float = 0.60
    link_review_band: float = 0.08
    grounding_min_score: float = 88.0
    ocr_provider: Literal["tesseract", "none"] = "tesseract"
    ocr_languages: str = "kor+eng"
    ocr_min_text_chars_per_page: int = 40

    # --- council video (app/media) ------------------------------------------------------------
    stt_model: str = "small"
    stt_model_dir: str | None = None  # a local copy, for workers without access to the model hub
    stt_compute_type: str = "int8"
    stt_language: str = "ko"
    media_workdir: str = "./.data/media"
    media_window_seconds: float = 600.0
    media_window_search_seconds: float = 30.0
    # Where broadcasts print the speaker: x, y, width, height as fractions of the frame.
    media_caption_region: tuple[float, float, float, float] = (0.0, 0.70, 1.0, 0.30)
    media_caption_every_seconds: float = 2.0
    media_caption_scene: float = 0.12

    # --- public data sources -------------------------------------------------------------------
    clik_api_key: SecretStr | None = None
    clik_daily_quota: int = 1000
    data_go_kr_service_key: SecretStr | None = None
    data_go_kr_daily_quota: int = 1000
    lofin_api_key: SecretStr | None = None
    source_http_timeout_seconds: float = 20.0
    source_max_attempts: int = 4
    circuit_failure_threshold: int = 5
    circuit_cooldown_seconds: int = 600

    # --- billing --------------------------------------------------------------------------------
    payment_provider: Literal["toss", "fake"] = "fake"
    toss_secret_key: SecretStr | None = None
    toss_client_key: str | None = None
    # Fernet key for local development only. Production reads it from Secret Manager.
    billing_key_encryption_key: SecretStr = SecretStr(_DEV_FERNET_KEY)

    # --- notifications ------------------------------------------------------------------------
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = False
    mail_from: str = "발주 예측 알림 <alerts@example.com>"
    solapi_api_key: SecretStr | None = None
    solapi_api_secret: SecretStr | None = None
    kakao_pf_id: str | None = None
    kakao_template_id: str | None = None
    kakao_sender_number: str | None = None
    notify_quiet_hours: tuple[int, int] = (22, 8)

    @property
    def is_test(self) -> bool:
        return self.env == "test"

    @model_validator(mode="after")
    def _refuse_dev_secrets_outside_local(self) -> Settings:
        if self.env in ("staging", "production"):
            if self.jwt_secret.get_secret_value() == _DEV_JWT_SECRET:
                raise ValueError("APP_JWT_SECRET must be set outside local/test")
            if self.billing_key_encryption_key.get_secret_value() == _DEV_FERNET_KEY:
                raise ValueError("APP_BILLING_KEY_ENCRYPTION_KEY must be set outside local/test")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
