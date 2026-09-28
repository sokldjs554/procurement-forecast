"""Source adapter contract.

An adapter turns one provider (CLIK minutes, 나라장터 사전규격, 지방재정365 예산서, …) into a
stream of :class:`RawRecord` s. Adapters do *no* interpretation beyond field mapping: parsing,
institution resolution and extraction happen downstream so they can be re-run over stored
documents when the logic improves, without re-fetching (and without spending API quota).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol, runtime_checkable

DocType = Literal["council_minutes", "budget_book", "order_plan", "prespec", "bid_notice", "award"]


@dataclass(slots=True)
class RawRecord:
    external_id: str
    doc_type: DocType
    title: str
    published_at: date
    mime: str
    publisher_raw: str | None = None
    institution_code_hint: str | None = None  # a code of our institution table
    provider_institution_code: str | None = None  # the provider's own (조달청 수요기관코드)
    sido_hint: str | None = None
    url: str | None = None
    content: bytes | None = None
    structured: dict[str, Any] = field(default_factory=dict)

    def content_hash(self) -> str:
        h = hashlib.sha256()
        h.update(self.title.encode())
        if self.content:
            h.update(self.content)
        if self.structured:
            import json

            h.update(json.dumps(self.structured, sort_keys=True, ensure_ascii=False).encode())
        return h.hexdigest()


@dataclass(slots=True)
class FetchWindow:
    since: date
    until: date


class SourceAdapter(Protocol):
    key: str
    doc_type: DocType

    def fetch(self, window: FetchWindow) -> AsyncIterator[RawRecord]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class SkipsStored(Protocol):
    """An adapter whose second call per record costs quota (a detail call) asks which of its
    external ids are stored already; ``run_ingest`` sets the lookup before fetching."""

    known_external_ids: Callable[[list[str]], Awaitable[set[str]]] | None


def pick(record: dict[str, Any], *names: str) -> Any:
    """First non-empty field among ``names`` — providers rename fields between API versions."""
    for name in names:
        value = record.get(name)
        if value not in (None, "", " "):
            return value
    return None


def parse_compact_date(value: Any) -> date | None:
    """'20260315', '2026-03-15', '2026-03-15 10:00:00', '202603151000' → date."""
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 8:
        if len(digits) == 6:  # YYYYMM (발주시기)
            return date(int(digits[:4]), int(digits[4:6]), 1)
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    digits = str(value).replace(",", "").split(".")[0].strip()
    return int(digits) if digits.lstrip("-").isdigit() else None
