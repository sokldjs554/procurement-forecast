"""Record what the web app reads from the API, for the static public demo (`manage demo snapshot`).

The public demo is the real Next.js app exported to static files (GitHub Pages), with no server
behind it. In place of the API it reads these files: what the real API answered for the seeded
demo world, one file per request the pages make. The feed is recorded whole, once per sort order,
and filtered in the browser the way the API filters it (apps/web/src/lib/demo/fetch.ts). Briefs
are made here, one per opportunity in the demo company's feed, so "영업 브리핑 만들기" has a real
answer to show; they are made last, so the recorded balance and brief lists are the untouched
ones.

Run against a running API over a database that went through `seed` and `demo run`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from app.billing.ledger import apply_credits
from app.db.models import User
from app.db.session import session_scope

USERS = {
    "demo": ("demo@example.com", "demo-pass-1234"),
    "admin": ("admin@example.com", "admin-pass-1234"),
}
SORTS = ("score", "soon", "recent")
STATUSES = ("open", "bid_open", "closed", "dormant")
APP_GETS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("/api/me", {}),
    ("/api/profile", {}),
    ("/api/institutions", {}),
    ("/api/categories", {}),
    ("/api/alerts/rule", {}),
    ("/api/alerts/channels", {}),
    ("/api/alerts/notifications", {}),
    ("/api/billing", {}),
)
ADMIN_GETS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("/api/admin/overview", {}),
    ("/api/admin/sources", {}),
    ("/api/admin/jobs", {"limit": 100}),
    ("/api/admin/jobs", {"limit": 100, "status": "failed"}),
    ("/api/admin/jobs", {"limit": 100, "status": "retrying"}),
    *(("/api/admin/review", {"status": s}) for s in ("open", "approved", "edited", "rejected")),
    *(("/api/admin/llm/usage", {"days": d}) for d in (7, 14, 30)),
    ("/api/admin/evals", {}),
)


def file_key(path: str, query: dict[str, Any]) -> str:
    """`/api/admin/jobs` + {limit: 100, status: failed} → `admin_jobs_limit_100_status_failed`.
    The browser side builds the same name from the request (keep the two in step)."""
    raw = path.removeprefix("/api/")
    if query:
        raw += "?" + urlencode(sorted((k, str(v)) for k, v in query.items()))
    return re.sub(r"[^A-Za-z0-9-]", "_", raw)


def _write(out: Path, rel: str, data: Any) -> None:
    target = out / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


async def _get(client: httpx.AsyncClient, path: str, query: dict[str, Any] | list[Any]) -> Any:
    resp = await client.get(path, params=query)
    resp.raise_for_status()
    return resp.json()


async def _feed(client: httpx.AsyncClient, sort: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    first: dict[str, Any] | None = None
    while True:
        params: list[tuple[str, Any]] = [("status", s) for s in STATUSES]
        params += [("sort", sort), ("limit", 100)]
        if cursor:
            params.append(("cursor", cursor))
        page = await _get(client, "/api/opportunities", params)
        first = first or page
        items += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert first is not None
    return {"items": items, "total": len(items), "stage_counts": first["stage_counts"]}


async def _grant(email: str, credits: int) -> None:
    async with session_scope() as s:
        user = await s.scalar(select(User).where(User.email == email))
        assert user is not None, email
        await apply_credits(
            s,
            org_id=user.org_id,
            delta=credits,
            reason="adjustment",
            idempotency_key=f"demo-snapshot:{user.org_id}:{credits}",
        )


async def record_snapshot(base_url: str, out: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    ids_by_user: dict[str, list[int]] = {}
    for user, (email, password) in USERS.items():
        async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
            login = await client.post(
                "/api/auth/login", json={"email": email, "password": password}
            )
            login.raise_for_status()
            for path, query in APP_GETS + (ADMIN_GETS if user == "admin" else ()):
                _write(out, f"{user}/{file_key(path, query)}.json", await _get(client, path, query))
            ids: list[int] = []
            for sort in SORTS:
                feed = await _feed(client, sort)
                _write(out, f"{user}/feed.{sort}.json", feed)
                ids += [item["id"] for item in feed["items"] if item["id"] not in ids]
            for opp_id in ids:
                detail = await _get(client, f"/api/opportunities/{opp_id}", {})
                _write(out, f"{user}/opportunities/{opp_id}.json", detail)
            ids_by_user[user] = ids
            counts[f"{user}_opportunities"] = len(ids)

    # Last, so the balance and brief lists recorded above are the ones the seed left.
    email, password = USERS["demo"]
    ids = ids_by_user["demo"]
    await _grant(email, 3 * len(ids) + 30)
    async with httpx.AsyncClient(base_url=base_url, timeout=120) as client:
        login = await client.post("/api/auth/login", json={"email": email, "password": password})
        login.raise_for_status()
        for opp_id in ids:
            resp = await client.post(
                f"/api/opportunities/{opp_id}/briefs",
                headers={"Idempotency-Key": f"demo-snapshot-{opp_id}"},
            )
            resp.raise_for_status()
            _write(out, f"demo/briefs/{opp_id}.json", resp.json())
    counts["briefs"] = len(ids)
    _write(out, "ids.json", sorted({i for ids in ids_by_user.values() for i in ids}))
    return counts
