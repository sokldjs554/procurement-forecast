"""Render adapter: validate production configuration, migrate once, and start a role.

No resource creation, demo seeding, credential output, paid model calls, or notifications.
Run from /app in the Render image. ``check`` performs read-only dependency checks.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit


def configure() -> None:
    """Adapt provider-owned connection strings before importing application settings."""
    database_url = os.environ.get("APP_DATABASE_URL", "")
    for prefix in ("postgres://", "postgresql://"):
        if database_url.startswith(prefix):
            os.environ["APP_DATABASE_URL"] = "postgresql+asyncpg://" + database_url[len(prefix) :]
            break

    # Render generates standard base64; Fernet requires the URL-safe alphabet.
    key = os.environ.get("APP_BILLING_KEY_ENCRYPTION_KEY", "")
    if key:
        os.environ["APP_BILLING_KEY_ENCRYPTION_KEY"] = key.replace("+", "-").replace("/", "_")

    # A Render secret file is preferred. JSON env input allows a single initial Blueprint form.
    credential_json = os.environ.get("RENDER_GCP_CREDENTIALS_JSON")
    if credential_json and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        try:
            info = json.loads(credential_json)
            if info.get("type") != "service_account":
                raise ValueError("a GCS service-account credential is required")
            from google.oauth2.service_account import Credentials

            Credentials.from_service_account_info(info)
        except Exception:
            raise ValueError(
                "RENDER_GCP_CREDENTIALS_JSON is not a valid service-account key"
            ) from None
        fd, name = tempfile.mkstemp(prefix="render-gcs-", suffix=".json")
        with os.fdopen(fd, "w") as handle:
            handle.write(credential_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = name
        os.environ.pop("RENDER_GCP_CREDENTIALS_JSON", None)

    public_url = os.environ.get("APP_PUBLIC_WEB_URL", "")
    os.environ["APP_CORS_ORIGINS"] = json.dumps([public_url.rstrip("/")])


def validate() -> None:
    from cryptography.fernet import Fernet

    from app.settings import get_settings

    required = (
        "APP_DATABASE_URL",
        "APP_REDIS_URL",
        "APP_JWT_SECRET",
        "APP_BILLING_KEY_ENCRYPTION_KEY",
        "APP_STORAGE_URL",
        "APP_PUBLIC_WEB_URL",
        "APP_DATA_GO_KR_SERVICE_KEY",
        "APP_TOSS_CLIENT_KEY",
        "APP_TOSS_SECRET_KEY",
        "APP_SMTP_HOST",
        "APP_SMTP_USERNAME",
        "APP_SMTP_PASSWORD",
        "APP_MAIL_FROM",
    )
    missing = [key for key in required if not os.environ.get(key, "").strip()]
    if missing:
        raise ValueError("Missing required production settings: " + ", ".join(missing))
    try:
        settings = get_settings()
    except ValueError:
        # Pydantic error details can include the supplied settings dictionary (and secrets).
        raise ValueError(
            "Invalid application settings; check required production variables"
        ) from None
    if settings.env != "production" or not settings.cookie_secure:
        raise ValueError("Render requires APP_ENV=production and APP_COOKIE_SECURE=true")
    if len(settings.jwt_secret.get_secret_value()) < 32:
        raise ValueError("APP_JWT_SECRET must contain at least 32 characters")
    Fernet(settings.billing_key_encryption_key.get_secret_value())
    database = urlsplit(settings.database_url)
    redis = urlsplit(settings.redis_url)
    if database.scheme != "postgresql+asyncpg" or not database.hostname:
        raise ValueError("APP_DATABASE_URL must be a PostgreSQL asyncpg URL")
    if redis.scheme not in {"redis", "rediss"} or not redis.hostname:
        raise ValueError("APP_REDIS_URL must be a Redis URL")
    storage = urlsplit(settings.storage_url)
    if storage.scheme != "gs" or not storage.netloc:
        raise ValueError("APP_STORAGE_URL must use shared durable gs:// storage on Render")
    credentials = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not credentials or not Path(credentials).is_file():
        raise ValueError("Provide a GCS secret file or RENDER_GCP_CREDENTIALS_JSON")
    public = urlsplit(settings.public_web_url)
    if (
        public.scheme != "https"
        or not public.hostname
        or public.hostname in {"localhost", "127.0.0.1"}
    ):
        raise ValueError("APP_PUBLIC_WEB_URL must be the real HTTPS frontend origin")
    if public.path not in {"", "/"} or public.query or public.fragment or public.username:
        raise ValueError("APP_PUBLIC_WEB_URL must contain only the HTTPS origin")
    if settings.payment_provider != "toss":
        raise ValueError("Production cannot use the fake payment provider")
    if settings.smtp_host in {"localhost", "127.0.0.1", "mailpit"} or not settings.smtp_use_tls:
        raise ValueError("Production requires a real SMTP relay with STARTTLS")
    if "example.com" in settings.mail_from:
        raise ValueError("APP_MAIL_FROM must use a verified sender domain")
    if settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
        raise ValueError("APP_ANTHROPIC_API_KEY is required when enabling Anthropic")
    if settings.embedding_provider == "voyage" and not settings.voyage_api_key:
        raise ValueError("APP_VOYAGE_API_KEY is required when enabling Voyage")


def check_storage() -> None:
    """Read-only IAM check: do not create probe objects or expose credential errors."""
    from google.cloud import storage

    from app.settings import get_settings

    bucket_name = urlsplit(get_settings().storage_url).netloc
    needed = {"storage.objects.get", "storage.objects.create"}
    try:
        granted = set(storage.Client().bucket(bucket_name).test_iam_permissions(sorted(needed)))
    except Exception:
        raise RuntimeError(
            "GCS permission check failed; verify bucket and service account"
        ) from None
    if not needed.issubset(granted):
        raise RuntimeError(
            "GCS service account requires storage.objects.get and storage.objects.create"
        )


async def check_dependencies(*, wait_for_schema: bool = True) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from redis.asyncio import Redis
    from sqlalchemy import text

    from app.db.session import create_engine
    from app.settings import get_settings

    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=10, socket_timeout=10)
    try:
        await redis.ping()
    finally:
        await redis.aclose()
    await asyncio.to_thread(check_storage)
    if not wait_for_schema:
        return
    heads = set(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
    engine = create_engine(settings)
    deadline = time.monotonic() + 600
    try:
        while True:
            ready = False
            try:
                async with engine.connect() as connection:
                    versions = set(
                        (
                            await connection.execute(
                                text("SELECT version_num FROM alembic_version")
                            )
                        ).scalars()
                    )
                    ready = versions == heads
            except Exception:
                ready = False  # Initial services may start before API migrations finish.
            if ready:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Database did not reach this image's Alembic head within 600 seconds"
                )
            await asyncio.sleep(5)
    finally:
        await engine.dispose()


async def migrate() -> None:
    """Serialize migrations across retries, then bootstrap real reference data only."""
    import asyncpg
    from sqlalchemy.dialects.postgresql import insert

    from app.db.models import Source
    from app.db.session import dispose_engine, session_scope
    from app.demo.seed import seed_institutions
    from app.runtime import build_runtime
    from app.settings import get_settings
    from app.sources.registry import SOURCE_CATALOG

    settings = get_settings()
    connection = await asyncpg.connect(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    )
    try:
        await asyncio.wait_for(
            connection.execute("SELECT pg_advisory_lock(735912608)"), timeout=600
        )
        process = await asyncio.create_subprocess_exec("manage", "db", "upgrade")
        if await process.wait() != 0:
            raise RuntimeError("Database migration failed")
        runtime = build_runtime(settings)
        enabled = {
            "g2b": bool(settings.data_go_kr_service_key),
            "clik": bool(settings.clik_api_key),
            "lofin": bool(settings.lofin_api_key),
            "crawler": False,
        }
        async with session_scope() as session:
            await seed_institutions(session, runtime)
            for source in SOURCE_CATALOG:
                statement = insert(Source).values(
                    **source, enabled=enabled[source["adapter"]], config={}
                )
                await session.execute(statement.on_conflict_do_nothing(index_elements=["key"]))
        # Never seed fixture sources, organizations, accounts, subscriptions, or paid calls.
    finally:
        await connection.close()  # Also releases the session-level advisory lock.
        await dispose_engine()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in {"api", "worker", "migrate", "check", "exec"}:
        raise SystemExit("Usage: render-runtime.py api|worker|migrate|check|exec [command ...]")
    role = sys.argv[1]
    configure()
    validate()
    asyncio.run(check_dependencies(wait_for_schema=role != "migrate"))
    if role == "migrate":
        asyncio.run(migrate())
        print("Production migrations and real reference data are ready.")
        return
    if role == "check":
        print("Production configuration, Postgres schema, Redis, and GCS permissions verified.")
        return
    if role == "api":
        command = [
            "uvicorn",
            "app.api.app:create_app",
            "--factory",
            "--host",
            "0.0.0.0",  # noqa: S104 - required by Render's service router
            "--port",
            os.environ.get("PORT", "10000"),
            "--proxy-headers",
            "--forwarded-allow-ips=*",
            "--no-server-header",
        ]
    elif role == "worker":
        command = ["manage", "worker"]
    else:
        command = sys.argv[2:]
        if not command:
            raise SystemExit("exec requires a command")
    os.execvp(command[0], command)  # noqa: S606 - fixed roles or an operator's explicit exec command


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print(f"Render startup refused: {error}", file=sys.stderr)
        raise SystemExit(1) from None
