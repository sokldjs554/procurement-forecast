"""Provider-neutral result and error types for LLM tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Generic, TypeVar

T = TypeVar("T")

# A reserved local provider identity, including the measured quantized model's weight hash.
# Zero is API spend only; host hardware/electricity are not measured here.
LOCAL_QWEN_MODEL_ID = "local-qwen3-4b-q4_k_m-7485fe6f"

# $ per million tokens (Anthropic first-party list prices). Cache writes (5-minute TTL) bill at
# 1.25x input, cache reads at 0.1x input. Used for cost accounting and the daily budget guard.
PRICING_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    LOCAL_QWEN_MODEL_ID: (Decimal(0), Decimal(0)),
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-8": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}
_DEFAULT_PRICE = (Decimal("5.00"), Decimal("25.00"))


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def cost_usd(self, model: str) -> Decimal:
        price_in, price_out = PRICING_PER_MTOK.get(model, _DEFAULT_PRICE)
        mtok = Decimal(1_000_000)
        return (
            Decimal(self.input_tokens) * price_in
            + Decimal(self.cache_write_tokens) * price_in * Decimal("1.25")
            + Decimal(self.cache_read_tokens) * price_in * Decimal("0.1")
            + Decimal(self.output_tokens) * price_out
        ) / mtok


@dataclass(slots=True)
class LLMResult(Generic[T]):
    value: T
    provider: str
    model: str
    prompt_version: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    served_by: str | None = None
    request_id: str | None = None
    cached: bool = False


class LLMError(Exception):
    """Base class. ``retryable`` tells the job runner whether to back off and try again."""

    retryable = False


class LLMUnavailableError(LLMError):
    """Rate limits, overload, 5xx, timeouts — after the SDK's own retries."""

    retryable = True


class LLMConfigError(LLMError):
    """Bad key, unknown model, invalid request. Needs a human; do not retry."""


class LLMSetupError(LLMConfigError):
    """No call can succeed until a person fixes the account or the settings: missing or bad
    credentials, no permission, an unknown model, no credit left. Degrading chunk by chunk
    would quietly finish the whole batch on the rule-based extractor, so extraction stops
    instead: the job fails and its documents stay pending for the next run."""


class LLMRefusedError(LLMError):
    """stop_reason == "refusal" even after server-side fallback."""


class LLMInvalidOutputError(LLMError):
    """Output truncated (max_tokens) or failed client-side validation."""


class LLMBudgetExceededError(LLMError):
    """Daily spend cap reached — degrade to the heuristic extractor until midnight KST."""
