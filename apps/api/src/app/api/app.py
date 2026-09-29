"""FastAPI application factory — run with ``uvicorn app.api.app:create_app --factory``."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app import __version__
from app.api.routers import admin, alerts, auth, billing, opportunities, profile, relations
from app.db.session import dispose_engine, get_engine
from app.log import bind_contextvars, clear_contextvars, configure_logging, get_logger
from app.observability import init_sentry
from app.runtime import build_runtime
from app.settings import Settings, get_settings
from app.worker.queue import create_queue

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        json=settings.log_json, level=settings.log_level, service=settings.service_name
    )
    init_sentry(settings, component="api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        get_engine()
        redis = Redis.from_url(settings.redis_url)
        app.state.runtime = build_runtime(settings, redis=redis)
        app.state.queue = await create_queue(settings)
        log.info("api.started", version=__version__, extractor=app.state.runtime.extractor_mode)
        try:
            yield
        finally:
            await app.state.queue.aclose()
            await redis.aclose()
            await dispose_engine()

    app = FastAPI(
        title="발주 예측 API",
        version=__version__,
        description="B2G pre-procurement signal intelligence.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key", "Authorization", "X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        clear_contextvars()
        bind_contextvars(request_id=request_id, path=request.url.path, method=request.method)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request.unhandled")
            response = JSONResponse(
                {
                    "detail": "잠깐 문제가 생겼어요. 잠시 후 다시 시도해 주세요",
                    "request_id": request_id,
                },
                status_code=500,
            )
        response.headers["X-Request-ID"] = request_id
        if not request.url.path.startswith(("/healthz", "/readyz")):
            log.info(
                "request.done",
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        return response

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["health"])
    async def readyz(request: Request) -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as exc:  # pragma: no cover - exercised in ops, not tests
            checks["postgres"] = f"error: {type(exc).__name__}"
        try:
            await request.app.state.runtime.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # pragma: no cover
            checks["redis"] = f"error: {type(exc).__name__}"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"status": "ok" if ok else "degraded", **checks}, status_code=200 if ok else 503
        )

    for module in (auth, profile, opportunities, alerts, billing, admin, relations):
        app.include_router(module.router)
    app.include_router(auth.me_router)
    app.include_router(billing.webhook_router)
    return app
