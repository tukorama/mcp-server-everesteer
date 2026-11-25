"""Preflight safety checks and journaled write execution."""

import hashlib
import math
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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
from mcp_server_everesteer.journal import DUPLICATE_TTL_SECONDS, JournalEntry, WriteState
from mcp_server_everesteer.models import WriteResult, as_dict

if TYPE_CHECKING:
    from mcp_server_everesteer.ops.registry import Call

_KEYED_REPEAT_STATES: frozenset[WriteState] = frozenset({"pending", "unknown", "sent"})


@dataclass(frozen=True)
class Idempotency:
    """Send a write with an Idempotency-Key, reusing the key of an identical attempt in doubt."""

    requested: str | None = None


@dataclass(frozen=True)
class JournaledWrite:
    """Everything needed to check, journal, and send one guarded write."""

    tool_key: str
    endpoint: Endpoint
    summary: str
    usdc: Decimal | None = None
    print_: str | None = None
    path_args: dict[str, str] | None = None
    params: Mapping[str, Any] | None = None
    body: Any = None
    headers: dict[str, str] | None = None
    idempotency: Idempotency | None = None
    plan_key: str | None = None
    per_round: bool = False

    @property
    def kind(self) -> str:
        """Daily cap bucket ('staking' or 'withdrawals'): the prefix of `tool_key`."""
        return self.tool_key.partition(":")[0]


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


def check_daily_usdc(
    call: "Call", amount: Decimal, kind: str, *, idempotency_key: str | None = None
) -> None:
    """Enforce UTC daily USDC cap for `kind` ('staking' or 'withdrawals')."""
    limit = call.settings.max_daily_usdc
    if limit == "unlimited":
        return
    total = call.runtime.journal.projected_spend(kind, amount, idempotency_key)
    if total > limit:
        raise Refused(
            f"This would bring today's {kind} total to ${total:.2f}, above "
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


def _in_doubt(entry: JournalEntry) -> str:
    return "is still in flight" if entry.status == "pending" else "has an unknown outcome"


def block_duplicate(call: "Call", print_: str) -> None:
    """Refuse an identical write while a previous attempt is pending or unknown."""
    previous = call.runtime.journal.pending_duplicate(print_)
    if previous is not None:
        raise Refused(
            f"An identical write ({previous.id}) {_in_doubt(previous)}.",
            hint="Inspect account state (`get_status` / `get_wallet_status` / "
            + "`list_unconfirmed_writes`) before retrying.",
        )


def claim_idempotency_key(call: "Call", print_: str, requested: str | None) -> str:
    """Pick the Idempotency-Key for an identical-write-sensitive call such as a withdrawal.

    An identical attempt that is still pending or unknown is retried under its own key, so
    Everesteer can deduplicate it. One that already succeeded inside the window is refused.
    """
    previous = call.runtime.journal.latest_duplicate(print_, _KEYED_REPEAT_STATES)
    if previous is None:
        return requested or str(uuid.uuid4())
    if previous.status == "sent":
        raise Refused(
            f"An identical write ({previous.id}) already succeeded in the last "
            + f"{DUPLICATE_TTL_SECONDS // 60} minutes.",
            hint="Check `get_wallet_status` / `get_status` before repeating it. To send it again "
            + "on purpose, wait until that window has passed.",
        )
    stored = previous.idempotency_key
    if stored is None or requested not in (None, stored):
        retry = f"Retry with idempotency_key='{stored}' or without one" if stored else "Retry"
        raise Refused(
            f"An identical write ({previous.id}) {_in_doubt(previous)} under a different "
            + "Idempotency-Key.",
            hint=f"{retry} only after inspecting account state (`get_wallet_status` / "
            + "`list_unconfirmed_writes`).",
        )
    return stored


def _admit(call: "Call", write: JournaledWrite) -> str | None:
    """Run the local journal checks for `write`; return the Idempotency-Key to send, if any."""
    write.endpoint.render(write.path_args)
    key: str | None = None
    if write.idempotency is not None:
        if write.print_ is None:
            raise ValueError(
                f"{write.tool_key} sends an Idempotency-Key, so it needs a fingerprint."
            )
        key = claim_idempotency_key(call, write.print_, write.idempotency.requested)
    elif write.print_ is not None:
        block_duplicate(call, write.print_)
    if write.usdc is not None:
        check_daily_usdc(call, write.usdc, write.kind, idempotency_key=key)
    return key


def preflight_write(call: "Call", write: JournaledWrite) -> None:
    """Run the local checks of `send_write` early, e.g. before asking the user to confirm."""
    _admit(call, write)


def _unknown_next_step(key: str | None) -> str:
    if key is None:
        return (
            "Do not automatically repeat this write. Check account/staking state first "
            "or call `list_unconfirmed_writes`."
        )
    return (
        "Check account state (`get_wallet_status` / `list_unconfirmed_writes`) first. Repeating "
        f"the identical write reuses Idempotency-Key {key}, so Everesteer can deduplicate it."
    )


async def send_write(call: "Call", write: JournaledWrite) -> tuple[Any, WriteResult | None]:
    """Check and record write intent atomically in SQLite, send once, degrade unknown outcomes.

    The duplicate guard, daily cap check, and journal insert share one `BEGIN IMMEDIATE`
    transaction, so concurrent server processes on the same journal cannot overshoot the cap.
    """
    journal = call.runtime.journal
    with journal.locked():
        key = _admit(call, write)
        entry_id = journal.record(
            tool=write.tool_key,
            endpoint=write.endpoint.key,
            summary=write.summary,
            usdc=write.usdc,
            fingerprint=write.print_,
            idempotency_key=key,
            plan_key=write.plan_key,
            per_round=write.per_round,
        )
    headers = dict(write.headers or {})
    if key is not None:
        headers["Idempotency-Key"] = key
    try:
        data = await call.json(
            write.endpoint,
            path_args=write.path_args,
            params=write.params,
            body=write.body,
            headers=headers or None,
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
            next_step=_unknown_next_step(key),
        )
    except EveresteerError as exc:
        journal.finish(entry_id, "rejected", {"error": exc.message})
        raise
    except BaseException as exc:
        journal.finish(entry_id, "unknown", {"error": f"interrupted: {type(exc).__name__}"})
        raise
    journal.finish(entry_id, "sent", data)
    return data, None


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
    """Keyword form of `send_write`: record write intent, send once, degrade unknown outcomes."""
    return await send_write(
        call,
        JournaledWrite(
            tool_key=tool_key,
            endpoint=endpoint,
            summary=summary,
            usdc=usdc,
            print_=print_,
            path_args=path_args,
            params=params,
            body=body,
            headers=headers,
        ),
    )
