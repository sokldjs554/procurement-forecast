"""HTTP client for flaky public-data APIs.

Retry policy (per request):

* Timeouts, connection errors, 429 and 5xx → retry with exponential backoff and full jitter,
  honouring ``Retry-After`` when present.
* Provider "soft errors" — data.go.kr answers *HTTP 200* with an XML ``OpenAPI_ServiceResponse``
  or a JSON ``resultCode != "00"``, and its gateway answers *HTTP 401/403* with a JSON
  ``OpenAPI_ServiceResponse`` (seen live 2026-09-26: 403 + code 30 for a service the key was not
  applied for), and 나라장터 itself answers a bad request with *HTTP 200* and
  ``{"nkoneps.com.response.ResponseError": {"header": {"resultCode": "08", …}}}`` (seen live
  2026-09-26 for a missing parameter; ``_items`` would have read it as an empty page) — are
  classified by the provider's code, not the status: quota/traffic codes raise
  :class:`QuotaExhaustedError` (reschedule, do not retry), key/parameter codes raise
  :class:`FatalSourceError` (page an operator), transient codes retry.
* Every attempt goes through the shared rate limiter; every outcome feeds the circuit breaker.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import ssl
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from app.log import get_logger, redact_secrets
from app.sources.resilience import (
    Breaker,
    Limiter,
    QuotaExhaustedError,
    next_kst_midnight,
)

log = get_logger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


# data.go.kr common error codes (공공데이터포털 OpenAPI 에러코드 표)
_DGK_QUOTA_CODES = {"22"}  # LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR
_DGK_TRANSIENT_CODES = {"01", "02", "03", "04", "05", "99"}  # app/db/http/timeout/unknown
# params / key problems; 06–08 are 나라장터's own (날짜 형식, 입력 범위 초과, 필수값 누락)
_DGK_FATAL_CODES = {"06", "07", "08", "10", "11", "12", "20", "30", "31", "32", "33"}


class FatalSourceError(Exception):
    """Misconfiguration (bad key, bad parameter) or a 4xx. Retrying will not help."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ResponseTooLargeError(Exception):
    """The body exceeded the caller's size cap; nothing past the cap was buffered."""


class TransientSourceError(Exception):
    pass


def _describe(exc: Exception) -> str:
    """httpx raises ``ConnectTimeout('')``/``ReadError('')`` — keep at least the type."""
    return str(exc) or type(exc).__name__


def _kind(exc: Exception) -> str:
    """Short, key-free label for counting failures: ``ConnectTimeout``, ``HTTP 503``,
    ``provider 22``…"""
    if isinstance(exc, QuotaExhaustedError):
        return "quota"
    m = re.search(r"(HTTP \d{3}|provider error \d+)", str(exc))
    if m and isinstance(exc, (FatalSourceError, TransientSourceError)):
        return m.group(1).replace("provider error", "provider")
    return type(exc).__name__


def _classify_soft_error(source: str, body: str, *, status: int | None = None) -> None:
    code: str | None = None
    message = ""
    if body.lstrip().startswith("<"):
        m = re.search(r"<returnReasonCode>\s*(\d+)\s*</returnReasonCode>", body)
        if m:
            code = m.group(1)
            err = re.search(r"<errMsg>\s*([^<]+)</errMsg>", body)
            auth = re.search(r"<returnAuthMsg>\s*([^<]+)</returnAuthMsg>", body)
            message = " ".join(x.group(1).strip() for x in (err, auth) if x)
        else:
            m = re.search(r"<resultCode>\s*(\d+)\s*</resultCode>", body)
            if m and m.group(1) not in {"00", "0"}:
                code = m.group(1)
    else:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return
        if not isinstance(parsed, dict):
            return
        header = (parsed.get("response") or {}).get("header")
        if header is None:  # 나라장터's error wrapper: {"nkoneps.com.response.ResponseError": …}
            wrapper = next(
                (
                    v
                    for k, v in parsed.items()
                    if k.endswith("ResponseError") and isinstance(v, dict)
                ),
                {},
            )
            header = wrapper.get("header")
        gateway = (parsed.get("OpenAPI_ServiceResponse") or {}).get("cmmMsgHeader")
        if isinstance(gateway, dict) and gateway.get("returnReasonCode") is not None:
            code = str(gateway["returnReasonCode"])
            message = " ".join(
                str(gateway[k]) for k in ("errMsg", "returnAuthMsg") if gateway.get(k)
            )
        elif isinstance(header, dict):
            rc = str(header.get("resultCode", "00"))
            if rc not in {"00", "0"}:
                code, message = rc, str(header.get("resultMsg", ""))
    if code is None:
        return
    if code in _DGK_QUOTA_CODES:
        raise QuotaExhaustedError(source, next_kst_midnight())
    if code in _DGK_FATAL_CODES:
        where = f" (HTTP {status})" if status else ""
        raise FatalSourceError(
            f"{source}: provider error {code} {message}".strip() + where, status=status
        )
    raise TransientSourceError(f"{source}: provider error {code} {message}".strip())


def legacy_cipher_context() -> ssl.SSLContext:
    """Certificates and host names are verified exactly as by default; only the cipher list is
    OpenSSL's ``DEFAULT`` instead of Python's forward-secret-only list. Some 지자체 servers
    (www.seongnam.go.kr, 2026-09) offer nothing but TLS 1.2 ``AES128-SHA``, so Python's default
    handshake fails with ``SSLV3_ALERT_HANDSHAKE_FAILURE`` where curl succeeds."""
    ctx = httpx.create_ssl_context()  # honours SSL_CERT_FILE like the default client
    ctx.set_ciphers("DEFAULT")
    return ctx


class ResilientClient:
    def __init__(
        self,
        source: str,
        *,
        base_url: str,
        limiter: Limiter,
        breaker: Breaker,
        timeout: float = 20.0,
        max_attempts: int = 4,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        verify: ssl.SSLContext | None = None,
    ) -> None:
        self.source = source
        self._limiter = limiter
        self._breaker = breaker
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        # What this client did, for ingest reports: ``attempts`` (every try, including ones
        # that never reached the provider), ``retries``, and one ``error:<kind>`` per failure.
        self.stats: Counter[str] = Counter()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
            transport=transport,
            verify=verify if verify is not None else True,
            headers={
                "User-Agent": "procurement-forecast-collector/0.1 (+https://github.com/sokldjs554/procurement-forecast)"
            },
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self._max_delay)
            except ValueError:
                pass
        # Full jitter (AWS architecture blog): uniform(0, min(cap, base * 2^attempt)).
        return random.uniform(0, min(self._max_delay, self._base_delay * 2**attempt))  # noqa: S311

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        soft_errors: bool = True,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_attempts):
            await self._breaker.before_call(self.source)
            await self._limiter.acquire(self.source)
            self.stats["attempts"] += 1
            retry_after: str | None = None
            try:
                if max_bytes is None:
                    resp = await self._client.request(method, url, params=params)
                else:
                    resp = await self._read_capped(method, url, params, max_bytes)
                if soft_errors and resp.status_code >= 400 and len(resp.content) < 65_536:
                    # The gateway's 401/403 bodies say *why* (code 30: not applied for); a bare
                    # "HTTP 403" would hide it. A transient code leaves the status to decide.
                    try:
                        _classify_soft_error(self.source, resp.text, status=resp.status_code)
                    except TransientSourceError:
                        pass
                    except (FatalSourceError, QuotaExhaustedError):
                        await self._breaker.record_success(self.source)  # provider is up
                        raise
                if resp.status_code in RETRYABLE_STATUS:
                    retry_after = resp.headers.get("Retry-After")
                    raise TransientSourceError(f"{self.source}: HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    await self._breaker.record_success(self.source)  # provider is up; we're wrong
                    raise FatalSourceError(
                        f"{self.source}: HTTP {resp.status_code} for {url}",
                        status=resp.status_code,
                    )
                # Error envelopes are small; don't re-parse multi-MB data pages to look for one.
                if soft_errors and (
                    len(resp.content) < 65_536 or resp.content.lstrip()[:1] == b"<"
                ):
                    _classify_soft_error(self.source, resp.text)
                await self._breaker.record_success(self.source)
                return resp
            except (QuotaExhaustedError, FatalSourceError, ResponseTooLargeError) as exc:
                self.stats[f"error:{_kind(exc)}"] += 1
                raise
            except (httpx.TimeoutException, httpx.TransportError, TransientSourceError) as exc:
                last_exc = exc
                self.stats[f"error:{_kind(exc)}"] += 1
                await self._breaker.record_failure(self.source)
                if attempt + 1 >= self._max_attempts:
                    break
                self.stats["retries"] += 1
                delay = self._backoff(attempt, retry_after)
                log.warning(
                    "source.retry",
                    source=self.source,
                    attempt=attempt + 1,
                    delay=round(delay, 2),
                    error=_describe(exc),  # app.log redacts credentials in every log line
                )
                await self._sleep(delay)
        assert last_exc is not None
        raise TransientSourceError(
            redact_secrets(
                f"{self.source}: gave up after {self._max_attempts} attempts: {_describe(last_exc)}"
            )
        ) from last_exc

    async def _read_capped(
        self, method: str, url: str, params: Mapping[str, Any] | None, max_bytes: int
    ) -> httpx.Response:
        """Stream the body and stop at ``max_bytes`` (a declared Content-Length is checked first),
        so a mislabelled multi-GB attachment can't exhaust worker memory."""
        req = self._client.build_request(method, url, params=params)
        resp = await self._client.send(req, stream=True)
        try:
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ResponseTooLargeError(f"{url}: {declared} bytes > {max_bytes}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ResponseTooLargeError(f"{url}: more than {max_bytes} bytes")
                chunks.append(chunk)
        finally:
            await resp.aclose()
        return httpx.Response(
            resp.status_code, headers=resp.headers, content=b"".join(chunks), request=req
        )

    async def get_json(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        resp = await self.request("GET", url, params=params)
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise TransientSourceError(f"{self.source}: non-JSON body") from exc

    async def get_bytes(self, url: str) -> bytes:
        resp = await self.request("GET", url, soft_errors=False)
        return resp.content
