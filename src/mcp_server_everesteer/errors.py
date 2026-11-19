"""Structured, agent-actionable error hierarchy for mcp-server-everesteer."""

from typing import Any


class EveresteerError(Exception):
    """Base error class. `hint` tells an agent what concrete next step to take."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: Any = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail
        self.hint = hint

    def for_agent(self) -> str:
        return f"{self.message} Next step: {self.hint}" if self.hint else self.message


class ConfigError(EveresteerError):
    """Invalid local configuration, missing credentials, or unsafe setting."""


class AuthError(EveresteerError):
    """Missing, invalid, or expired API key / authentication."""


class PermissionDenied(EveresteerError):
    """Valid key lacks scope or ownership for the target resource (HTTP 403)."""


class EmailVerificationRequired(PermissionDenied):
    """Account email is not verified yet; staking/payouts/withdrawals are blocked."""


class RateLimited(EveresteerError):
    """Platform rate limit hit (HTTP 429)."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        hint = (
            f"Wait about {int(retry_after)} seconds and retry."
            if retry_after is not None
            else "Wait briefly and retry."
        )
        kwargs.setdefault("hint", hint)
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class InvalidRequest(EveresteerError):
    """Client/API parameter validation failure (HTTP 400/422 or local preflight)."""


class ConflictError(EveresteerError):
    """State conflict such as closed window, locked allocation, or immutable record (HTTP 409)."""


class NotFound(EveresteerError):
    """Requested resource does not exist or is hidden in the current scope (HTTP 404)."""


class ServerError(EveresteerError):
    """Upstream Everesteer server failure (HTTP 5xx)."""


class NetworkError(EveresteerError):
    """Transport error after the request may have reached Everesteer."""


class NotSent(NetworkError):
    """Request definitely never reached Everesteer (safe to retry)."""


class DeadlineExceeded(EveresteerError):
    """Call ran out of the MCP client's tool execution budget."""


class Refused(EveresteerError):
    """Refused locally by a safety gate, USDC limit, or duplicate-write guard before sending."""


class CloudflareAccessError(AuthError):
    """Response was intercepted by a Cloudflare Access edge challenge instead of API JSON."""
