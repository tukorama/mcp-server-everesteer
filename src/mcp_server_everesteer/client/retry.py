"""Retry policies: exponential backoff for safe reads, zero auto-retry for writes."""

import random
from dataclasses import dataclass

import httpx2


@dataclass(frozen=True)
class RetryPolicy:
    rate_limit_attempts: int = 4
    server_attempts: int = 3
    network_attempts: int = 3
    retry_ambiguous_timeouts: bool = True


RETRY_SAFE = RetryPolicy()
# Never automatically repeat a write that may have reached Everesteer.
RETRY_UNSAFE = RetryPolicy(
    rate_limit_attempts=1,
    server_attempts=1,
    network_attempts=1,
    retry_ambiguous_timeouts=False,
)


def backoff(attempt: int) -> float:
    """Jittered exponential backoff capped at 30 seconds."""
    jitter = random.SystemRandom().uniform(0.0, 0.5)
    return float(min(1.5 * (1.8**attempt), 30.0)) + jitter


def parse_retry_after(response: httpx2.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))
