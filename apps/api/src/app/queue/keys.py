"""API keys for the job API: ``pfk_<8 hex>.<secret>``. The prefix is stored and shown (it is
how a key is found and named in logs); only the SHA-256 of the secret is stored, so a leaked
database does not leak working keys. Keys are shown once, at creation."""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SCOPES = ("jobs:read", "jobs:write", "usage:read")


def new_key() -> tuple[str, str, str]:
    """(full key, prefix, sha256 of the secret)."""
    prefix = f"pfk_{secrets.token_hex(4)}"
    secret = secrets.token_urlsafe(32)
    return f"{prefix}.{secret}", prefix, hashlib.sha256(secret.encode()).hexdigest()


async def create_api_key(
    session: AsyncSession, *, org_id: int, name: str, scopes: list[str]
) -> str:
    unknown = sorted(set(scopes) - set(SCOPES))
    if unknown or not scopes:
        raise ValueError(f"scopes must be some of {SCOPES}; got {scopes}")
    key, prefix, digest = new_key()
    await session.execute(
        text(
            "INSERT INTO api_keys (org_id, name, prefix, secret_sha256, scopes)"
            " VALUES (:org, :name, :prefix, :digest, :scopes)"
        ),
        {"org": org_id, "name": name, "prefix": prefix, "digest": digest, "scopes": scopes},
    )
    return key


async def revoke_api_key(session: AsyncSession, prefix: str) -> bool:
    row = await session.scalar(
        text(
            "UPDATE api_keys SET revoked_at = now()"
            " WHERE prefix = :prefix AND revoked_at IS NULL RETURNING id"
        ),
        {"prefix": prefix},
    )
    return row is not None
