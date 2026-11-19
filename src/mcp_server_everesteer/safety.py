"""Preflight safety checks and journaled write execution."""

import hashlib
import math
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast

from mcp_server_everesteer.client.endpoints import Endpoint
from mcp_server_everesteer.config import Limit
from mcp_server_everesteer.errors import (
    DeadlineExceeded,
    EveresteerError,
    InvalidRequest,
    NetworkError,
    NotSent,
    Refused,
    ServerError,
)
from mcp_server_everesteer.models import WriteResult, as_dict

if TYPE_CHECKING:
    from mcp_server_everesteer.ops.registry import Call


def check_usdc_limit(limit: Limit, amount: Decimal, what: str, setting: str) -> None:
    """Enforce per-call USDC ceiling unless `limit == 'unlimited'`."""
    if limit == "unlimited":
        return
    if limit == 0:
        raise Refused(f"{what} is disabled ({setting}=0).")
    if amount > limit:
        raise Refused(
            f"{what} of ${amount:.2f} exceeds {setting}=${limit}.",
            hint=f"Use a smaller amount or ask the user to raise {setting}.",
        )


def check_daily_usdc(call: "Call", amount: Decimal, kind: str) -> None:
    """Enforce UTC daily USDC cap for `kind` ('staking' or 'withdrawals')."""
    limit = call.settings.max_daily_usdc
    if limit == "unlimited":
        return
    spent = call.runtime.journal.spent_today(kind)
    if spent + amount > limit:
        raise Refused(
            f"This would bring today's {kind} total to ${spent + amount:.2f}, above "
            + f"EVERESTEER_MAX_DAILY_USDC=${limit}.",
            hint="Wait until the next UTC day or ask the user to raise EVERESTEER_MAX_DAILY_USDC.",
        )


def check_prediction_range(
    values: Mapping[str, float], *, lo: float = 0.0, hi: float = 1.0
) -> None:
    """Fail fast locally when any prediction is non-finite or outside `[lo, hi]`."""
    bad: list[str] = []
    for key, val in values.items():
        if isinstance(val, bool):
            bad.append(f"{key}={val!r}")
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            bad.append(f"{key}={val!r}")
            continue
        if not math.isfinite(num) or num < lo or num > hi:
            bad.append(f"{key}={val}")
    if bad:
        preview = ", ".join(bad[:5])
        more = f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""
        raise InvalidRequest(
            f"{len(bad)} prediction(s) must be finite and within [{lo}, {hi}]: {preview}{more}",
            hint="Rank or rescale predictions into the required interval before submitting.",
        )


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal | int | float) and not isinstance(value, bool):
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("0x"):
            return text.lower()
        try:
            return format(Decimal(text).normalize(), "f")
        except InvalidOperation:
            return text
    if isinstance(value, dict):
        items = sorted(as_dict(value).items(), key=lambda kv: kv[0])
        return {k: _canonical(v) for k, v in items}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in cast(Iterable[Any], value)]
    return value


def fingerprint(*parts: Any) -> str:
    """Canonical fingerprint to catch duplicate writes after an unknown network outcome."""
    return hashlib.sha256(repr(_canonical(list(parts))).encode()).hexdigest()[:32]


def block_duplicate(call: "Call", print_: str) -> None:
    """Refuse an identical write while a previous attempt is pending or unknown."""
    previous = call.runtime.journal.pending_duplicate(print_)
    if previous is not None:
        state = "is still in flight" if previous.status == "pending" else "has an unknown outcome"
        raise Refused(
            f"An identical write ({previous.id}) {state}.",
            hint="Inspect account state (`get_status` / `get_wallet_status` / "
            + "`list_unconfirmed_writes`) before retrying.",
        )


async def send_journaled_write(
    call: "Call",
    *,
    tool_key: str,
    endpoint: Endpoint,
    summary: str,
    usdc: Decimal | None = None,
    print_: str | None = None,
    path_args: dict[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[Any, WriteResult | None]:
    """Record write intent in SQLite, send once, and degrade unknown network outcomes cleanly."""
    if print_ is not None:
        block_duplicate(call, print_)
    journal = call.runtime.journal
    entry_id = journal.record(
        tool=tool_key,
        endpoint=endpoint.key,
        summary=summary,
        usdc=usdc,
        fingerprint=print_,
    )
    try:
        data = await call.json(
            endpoint,
            path_args=path_args,
            params=params,
            body=body,
            headers=headers,
        )
    except NotSent as exc:
        journal.finish(entry_id, "failed", {"error": exc.message})
        raise
    except (NetworkError, ServerError, DeadlineExceeded) as exc:
        journal.finish(entry_id, "unknown", {"error": exc.message})
        return None, WriteResult(
            status="unknown",
            request_id=entry_id,
            message=f"The outcome is unknown: {exc.message}",
            next_step=(
                "Do not automatically repeat this write. Check account/staking state first "
                "or call `list_unconfirmed_writes`."
            ),
        )
    except EveresteerError as exc:
        journal.finish(entry_id, "rejected", {"error": exc.message})
        raise
    except BaseException as exc:
        journal.finish(entry_id, "unknown", {"error": f"interrupted: {type(exc).__name__}"})
        raise
    journal.finish(entry_id, "sent", data)
    return data, None
