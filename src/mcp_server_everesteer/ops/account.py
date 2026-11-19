"""Agent account, profile, API keys, notifications, badges, registration, and freeze."""

from typing import Annotated, Any

from pydantic import Field

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.models import ApiPayload, WriteResult
from mcp_server_everesteer.ops.registry import Call, operation
from mcp_server_everesteer.redaction import clean_label

# -- Read Operations ---------------------------------------------------------


@operation("Get agent profile", toolset="account", requires=("api_key",), read_only=True)
async def get_profile(call: Call) -> ApiPayload:
    """Get your agent profile, `email_verified_at`, wallet address, and model count."""
    return ApiPayload.from_raw(await call.json(ep.AGENT_PROFILE))


@operation("Get notifications", toolset="account", requires=("api_key",), read_only=True)
async def get_notifications(
    call: Call,
    unread_only: bool = True,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> ApiPayload:
    """List badge awards, altitude changes, and round score notifications."""
    params: dict[str, Any] = {"limit": limit}
    if unread_only:
        params["unread_only"] = True
    return ApiPayload.from_raw(await call.json(ep.NOTIFICATIONS_LIST, params=params))


@operation("Get earned badges", toolset="account", requires=("api_key",), read_only=True)
async def get_badges(
    call: Call,
    model_id: Annotated[
        str | None, Field(description="Optional model id to filter badges.")
    ] = None,
) -> ApiPayload:
    """Get achievement badges across your agent or for one specific model."""
    if model_id is not None:
        return ApiPayload.from_raw(
            await call.json(ep.MODEL_BADGES, path_args={"model_id": model_id})
        )
    return ApiPayload.from_raw(await call.json(ep.BADGES_LIST))


@operation("List API keys", toolset="account", requires=("api_key",), read_only=True)
async def list_api_keys(call: Call) -> ApiPayload:
    """List API keys configured on your agent account (masked prefixes only)."""
    return ApiPayload.from_raw(await call.json(ep.API_KEYS_LIST))


# -- Write Operations (gate="writes") ----------------------------------------


async def _describe_register(_call: Call, name: str, email: str) -> str:
    return (
        f"Register a brand-new Everesteer agent named '{clean_label(name)}' "
        f"with email '{clean_label(email)}'? "
        "Note: the returned `api_key` and `webhook_secret` are shown only once."
    )


@operation(
    "Register new agent",
    toolset="account",
    gate="writes",
    describe=_describe_register,
)
async def register_agent(
    call: Call,
    name: Annotated[str, Field(min_length=1, description="Globally unique agent name.")],
    email: Annotated[
        str, Field(min_length=3, description="Contact email for verification & login.")
    ],
) -> WriteResult:
    """Register a new Everesteer agent account (does not require an existing API key)."""
    res = await call.json(ep.AGENT_REGISTER, body={"name": name, "email": email})
    return WriteResult.from_raw(
        res,
        status="ok",
        message="Agent registered. Save the returned api_key and verify your email address.",
    )


@operation(
    "Rename agent",
    toolset="account",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def rename_agent(
    call: Call,
    name: Annotated[str, Field(min_length=1, description="Globally unique agent name.")],
) -> WriteResult:
    """Change your agent's public display name on the cross-agent leaderboard."""
    res = await call.json(ep.AGENT_RENAME, body={"name": name})
    return WriteResult.from_raw(res, status="ok", message=f"Agent renamed to {name}.")


@operation(
    "Resend verification email",
    toolset="account",
    requires=("api_key",),
    gate="writes",
)
async def resend_email_verification(call: Call) -> WriteResult:
    """Re-send the verification link to your account email (rate-limited to 1/min)."""
    res = await call.json(ep.AGENT_EMAIL_VERIFY)
    return WriteResult.from_raw(res, status="ok", message="Verification email check completed.")


@operation(
    "Mark notifications read",
    toolset="account",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def mark_notifications_read(
    call: Call,
    notification_id: Annotated[
        str | None, Field(description="Notification ID to mark read; omit to mark ALL read.")
    ] = None,
) -> WriteResult:
    """Mark one notification or all unread notifications as read."""
    if notification_id is not None:
        res = await call.json(ep.NOTIFICATION_READ, path_args={"notification_id": notification_id})
        return WriteResult.from_raw(res, status="ok", message="Notification marked read.")
    res = await call.json(ep.NOTIFICATIONS_READ_ALL)
    return WriteResult.from_raw(res, status="ok", message="All notifications marked read.")


async def _describe_create_key(_call: Call, name: str = "additional") -> str:
    return f"Generate a new Everesteer API key named '{clean_label(name)}' on your account?"


@operation(
    "Create API key",
    toolset="account",
    requires=("api_key",),
    gate="writes",
    describe=_describe_create_key,
)
async def create_api_key(
    call: Call,
    name: Annotated[str, Field(min_length=1)] = "additional",
) -> WriteResult:
    """Generate an additional API key on your agent account."""
    res = await call.json(ep.API_KEYS_CREATE, params={"name": name})
    return WriteResult.from_raw(res, status="ok", message="API key created.")


# -- Destructive / Emergency Operations (gate="withdrawals", mandatory confirm)


async def _describe_revoke_key(_call: Call, key_id: str) -> str:
    return (
        f"REVOKE API key '{clean_label(key_id)}'? "
        "Any client using this key will immediately lose access."
    )


@operation(
    "Revoke API key",
    toolset="account",
    requires=("api_key",),
    gate="withdrawals",
    destructive=True,
    describe=_describe_revoke_key,
    mandatory_confirm=True,
)
async def revoke_api_key(call: Call, key_id: Annotated[str, Field(min_length=1)]) -> WriteResult:
    """Permanently revoke one API key by its key ID."""
    res = await call.json(ep.API_KEYS_REVOKE, path_args={"key_id": key_id})
    return WriteResult.from_raw(res, status="ok", message=f"API key {key_id} revoked.")


async def _describe_freeze(_call: Call) -> str:
    return (
        "EMERGENCY FREEZE: Freeze all USDC withdrawals AND immediately REVOKE EVERY API KEY "
        "on your account (including the current key)? Unfreezing requires browser login on the "
        "Security page."
    )


@operation(
    "Emergency freeze withdrawals & keys",
    toolset="account",
    requires=("api_key",),
    gate="withdrawals",
    destructive=True,
    describe=_describe_freeze,
    mandatory_confirm=True,
)
async def freeze_withdrawals(call: Call) -> WriteResult:
    """Emergency stop: freeze all withdrawals and revoke EVERY API key on the account."""
    res = await call.json(ep.SECURITY_FREEZE)
    return WriteResult.from_raw(
        res,
        status="ok",
        message="Withdrawals frozen and all API keys revoked.",
        next_step="Sign in via browser on the Everesteer Security page to unfreeze.",
    )
