"""Calls to the ``jobq_*`` SQL functions. Each takes the caller's session; the caller decides
the transaction, because a job's result and its usage rows must commit together."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import session_scope

Heartbeat = Literal["ok", "cancel", "lost"]
FailOutcome = Literal["retry", "failed", "cancelled", "lost"]


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    org_id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    budget_usd: Decimal | None
    parent_id: int | None
    dedupe_key: str | None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def enqueue(
    session: AsyncSession,
    *,
    org_id: int,
    kind: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
    priority: int = 0,
    max_attempts: int = 5,
    budget_usd: Decimal | float | None = None,
    parent_id: int | None = None,
) -> tuple[int, bool]:
    """(job id, created). Same (tenant, kind, dedupe key) while queued, running or done →
    the existing job and ``False``."""
    row = (
        await session.execute(
            text(
                "SELECT job_id, created FROM jobq_enqueue(:org, :kind, CAST(:payload AS jsonb),"
                " :dedupe, CAST(:priority AS smallint), :max_attempts, :budget, :parent)"
            ),
            {
                "org": org_id,
                "kind": kind,
                "payload": _json(payload),
                "dedupe": dedupe_key,
                "priority": priority,
                "max_attempts": max_attempts,
                "budget": budget_usd,
                "parent": parent_id,
            },
        )
    ).one()
    return int(row.job_id), bool(row.created)


async def claim(
    session: AsyncSession, worker: str, kinds: list[str], *, lease_seconds: int
) -> Job | None:
    row = (
        await session.execute(
            text(
                "SELECT id, org_id, kind, payload, attempts, max_attempts, budget_usd,"
                " parent_id, dedupe_key FROM jobq_claim(:worker, :kinds, :lease)"
            ),
            {"worker": worker, "kinds": kinds, "lease": lease_seconds},
        )
    ).one_or_none()
    if row is None:
        return None
    return Job(
        id=row.id,
        org_id=row.org_id,
        kind=row.kind,
        payload=row.payload,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        budget_usd=row.budget_usd,
        parent_id=row.parent_id,
        dedupe_key=row.dedupe_key,
    )


async def heartbeat(
    session: AsyncSession,
    job_id: int,
    worker: str,
    *,
    lease_seconds: int,
    progress: dict[str, Any] | None = None,
) -> Heartbeat:
    state = await session.scalar(
        text("SELECT jobq_heartbeat(:job, :worker, :lease, CAST(:progress AS jsonb))"),
        {
            "job": job_id,
            "worker": worker,
            "lease": lease_seconds,
            "progress": _json(progress) if progress is not None else None,
        },
    )
    return state  # type: ignore[no-any-return]


async def complete(
    session: AsyncSession, job_id: int, worker: str, result: dict[str, Any], cost: dict[str, Any]
) -> bool:
    """False when the lease was lost: another worker owns the job now; roll back."""
    done = await session.scalar(
        text("SELECT jobq_complete(:job, :worker, CAST(:result AS jsonb), CAST(:cost AS jsonb))"),
        {"job": job_id, "worker": worker, "result": _json(result), "cost": _json(cost)},
    )
    return bool(done)


async def fail(
    session: AsyncSession,
    job_id: int,
    worker: str,
    error: str,
    *,
    retryable: bool,
    cost: dict[str, Any] | None = None,
    base_seconds: int = 15,
    cap_seconds: int = 3600,
) -> FailOutcome:
    outcome = await session.scalar(
        text(
            "SELECT jobq_fail(:job, :worker, :error, :retryable, :base, :cap, CAST(:cost AS jsonb))"
        ),
        {
            "job": job_id,
            "worker": worker,
            "error": error[:4000],
            "retryable": retryable,
            "base": base_seconds,
            "cap": cap_seconds,
            "cost": _json(cost) if cost is not None else None,
        },
    )
    return outcome  # type: ignore[no-any-return]


async def reap(session: AsyncSession, *, base_seconds: int = 15, cap_seconds: int = 3600) -> int:
    return int(
        await session.scalar(
            text("SELECT jobq_reap(:base, :cap)"), {"base": base_seconds, "cap": cap_seconds}
        )
        or 0
    )


async def cancel(session: AsyncSession, job_id: int) -> str:
    return str(await session.scalar(text("SELECT jobq_cancel(:job)"), {"job": job_id}))


async def record_usage(
    session: AsyncSession,
    *,
    org_id: int,
    job_id: int,
    meter: str,
    quantity: float,
    usd: float = 0.0,
) -> None:
    """One row per (job, meter); a second write for the same job is ignored."""
    await session.execute(
        text(
            "INSERT INTO usage_events (org_id, job_id, meter, quantity, usd, idempotency_key)"
            " VALUES (:org, :job, :meter, :quantity, :usd, :key)"
            " ON CONFLICT (idempotency_key) DO NOTHING"
        ),
        {
            "org": org_id,
            "job": job_id,
            "meter": meter,
            "quantity": quantity,
            "usd": usd,
            "key": f"job:{job_id}:{meter}",
        },
    )


async def month_spend_usd(session: AsyncSession, org_id: int) -> tuple[Decimal, Decimal | None]:
    """(spent this calendar month in KST — the month customers are billed by — and the
    organisation's monthly cap or None)."""
    row = (
        await session.execute(
            text(
                "SELECT coalesce((SELECT sum(usd) FROM usage_events WHERE org_id = :org"
                "   AND created_at >= date_trunc('month', now() AT TIME ZONE 'Asia/Seoul')"
                "   AT TIME ZONE 'Asia/Seoul'), 0) AS spent,"
                " (SELECT usage_cap_usd FROM organizations WHERE id = :org) AS cap"
            ),
            {"org": org_id},
        )
    ).one()
    return Decimal(row.spent), row.cap


class PgCheckpoints:
    """The media job's checkpoint store in Postgres, keyed by tenant and by what the work is
    about (a video's content hash) — not by job, so a new job for the same video resumes.
    Each write commits on its own: a checkpoint must survive the job failing right after."""

    def __init__(self, org_id: int, scope: str) -> None:
        self._org = org_id
        self._scope = scope

    async def get(self, key: str) -> Any | None:
        async with session_scope() as s:
            return await s.scalar(
                text(
                    "SELECT value FROM job_checkpoints"
                    " WHERE org_id = :org AND scope = :scope AND key = :key"
                ),
                {"org": self._org, "scope": self._scope, "key": key},
            )

    async def put(self, key: str, value: Any) -> None:
        async with session_scope() as s:
            await s.execute(
                text(
                    "INSERT INTO job_checkpoints (org_id, scope, key, value)"
                    " VALUES (:org, :scope, :key, CAST(:value AS jsonb))"
                    " ON CONFLICT (org_id, scope, key)"
                    " DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
                ),
                {"org": self._org, "scope": self._scope, "key": key, "value": _json(value)},
            )
