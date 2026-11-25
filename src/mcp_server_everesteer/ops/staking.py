"""USDC staking, Strategy contracts, per-model stake plans, event allocations, and withdrawals."""

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.errors import InvalidRequest
from mcp_server_everesteer.models import (
    ApiPayload,
    UnconfirmedWriteEntry,
    UnconfirmedWritesResult,
    WriteResult,
)
from mcp_server_everesteer.money import (
    format_micro_usdc,
    micro_to_decimal,
    parse_usdc_to_micro,
    resolve_usdc_micro,
)
from mcp_server_everesteer.ops.registry import Call, ModelId, operation
from mcp_server_everesteer.redaction import clean_label
from mcp_server_everesteer.safety import (
    Idempotency,
    JournaledWrite,
    check_usdc_limit,
    fingerprint,
    preflight_write,
    send_write,
)

WalletAddress = Annotated[
    str,
    Field(
        pattern=r"^0x[0-9a-fA-F]{40}$",
        description="Ethereum/Base wallet address (0x + 40 hex).",
    ),
]
UsdcAmount = Annotated[
    str | int,
    Field(
        description=(
            "Exact USDC amount as a decimal STRING (e.g. '25.5') or integer (e.g. 25). "
            "Floats are refused to prevent rounding errors."
        )
    ),
]


# -- Read Operations ---------------------------------------------------------


@operation("Get model stake balance", toolset="staking", requires=("api_key",), read_only=True)
async def get_stake_balance(call: Call, model_id: ModelId) -> ApiPayload:
    """Get active stake, pending payouts, and total earned USDC for a model."""
    return ApiPayload.from_raw(
        await call.json(ep.STAKING_BALANCE, path_args={"model_id": model_id})
    )


@operation("Get staking history", toolset="staking", requires=("api_key",), read_only=True)
async def get_staking_history(call: Call, model_id: ModelId) -> ApiPayload:
    """Get stake, unstake, and claim transaction history for a model."""
    return ApiPayload.from_raw(
        await call.json(ep.STAKING_HISTORY, path_args={"model_id": model_id})
    )


@operation("Get staking strategy status", toolset="staking", requires=("api_key",), read_only=True)
async def get_staking_strategy(call: Call) -> ApiPayload:
    """Inspect your linked wallet's personal staking Strategy contract, balances, and caps."""
    return ApiPayload.from_raw(await call.json(ep.STAKING_STRATEGY))


@operation("List model stake plans", toolset="staking", requires=("api_key",), read_only=True)
async def get_staking_plans(call: Call) -> ApiPayload:
    """List your active and configured per-model Staking v2 plans."""
    return ApiPayload.from_raw(await call.json(ep.STAKING_PLANS))


@operation("List staking positions", toolset="staking", requires=("api_key",), read_only=True)
async def get_staking_positions(call: Call) -> ApiPayload:
    """List active and unresolved Staking v2 round positions across your models."""
    return ApiPayload.from_raw(await call.json(ep.STAKING_POSITIONS))


@operation("Get wallet & cooling status", toolset="staking", requires=("api_key",), read_only=True)
async def get_wallet_status(call: Call) -> ApiPayload:
    """Check withdrawal destination, verified wallets, 24h cooling timer, and freeze flag."""
    return ApiPayload.from_raw(await call.json(ep.STAKING_WALLETS))


@operation("Get event staking summary", toolset="staking", requires=("api_key",), read_only=True)
async def get_event_staking(call: Call) -> ApiPayload:
    """Read your hackathon event staking position (all money fields are integer micro-USDC)."""
    return ApiPayload.from_raw(await call.json(ep.EVENT_STAKING_SUMMARY))


@operation("List unconfirmed local writes", toolset="staking", read_only=True)
async def list_unconfirmed_writes(
    call: Call,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> UnconfirmedWritesResult:
    """Inspect local journal entries whose network outcome was ambiguous (`unknown`)."""
    rows = call.runtime.journal.unknown(limit=limit)
    entries = [
        UnconfirmedWriteEntry(
            id=row.id,
            tool=row.tool,
            endpoint=row.endpoint,
            summary=row.summary,
            usdc=row.usdc,
            status=row.status,
            created=row.created,
        )
        for row in rows
    ]
    return UnconfirmedWritesResult(entries=entries, count=len(entries))


# -- Staking Writes (gate="staking") -----------------------------------------
# `describe` runs the write's local checks before the user is asked to confirm it.


def _stake_write(
    call: Call, model_id: str, amount_usdc: str | int, wallet_address: str
) -> tuple[int, JournaledWrite]:
    micro = parse_usdc_to_micro(amount_usdc)
    usd = micro_to_decimal(micro)
    check_usdc_limit(call.settings.max_stake_usdc, usd, "Stake", "EVERESTEER_MAX_STAKE_USDC")
    return micro, JournaledWrite(
        tool_key="staking:stake_on_model",
        endpoint=ep.STAKING_STAKE,
        summary=f"stake {format_micro_usdc(micro)} USDC on {model_id}",
        usdc=usd,
        print_=fingerprint("staking:stake", model_id, micro, wallet_address.lower()),
        body={
            "model_id": model_id,
            "amount_usdc": float(usd),
            "wallet_address": wallet_address,
        },
    )


async def _describe_stake(
    call: Call, model_id: str, amount_usdc: str | int, wallet_address: str
) -> str:
    micro, write = _stake_write(call, model_id, amount_usdc, wallet_address)
    preflight_write(call, write)
    return (
        f"Stake {format_micro_usdc(micro)} USDC on model '{clean_label(model_id)}' "
        f"from wallet {wallet_address}?"
    )


@operation(
    "Stake USDC on model",
    toolset="staking",
    requires=("api_key",),
    gate="staking",
    describe=_describe_stake,
)
async def stake_on_model(
    call: Call,
    model_id: ModelId,
    amount_usdc: UsdcAmount,
    wallet_address: WalletAddress,
) -> WriteResult:
    """Initiate a USDC stake on a model (requires ALLOW_STAKING + confirmation)."""
    _, write = _stake_write(call, model_id, amount_usdc, wallet_address)
    data, unknown = await send_write(call, write)
    return unknown or WriteResult.from_raw(data, status="ok", message=f"Recorded: {write.summary}.")


@operation("Confirm on-chain stake", toolset="staking", requires=("api_key",), gate="staking")
async def confirm_stake(
    call: Call,
    stake_id: Annotated[str, Field(min_length=1)],
    txn_hash: Annotated[
        str, Field(pattern=r"^0x[0-9a-fA-F]{64}$", description="On-chain tx hash.")
    ],
) -> WriteResult:
    """Attach an on-chain transaction hash to confirm a pending stake."""
    res = await call.json(ep.STAKING_CONFIRM, body={"stake_id": stake_id, "txn_hash": txn_hash})
    return WriteResult.from_raw(res, status="ok", message=f"Stake {stake_id} confirmed.")


def _unstake_write(stake_id: str) -> JournaledWrite:
    return JournaledWrite(
        tool_key="staking:unstake_from_model",
        endpoint=ep.STAKING_UNSTAKE,
        summary=f"unstake {stake_id}",
        path_args={"stake_id": stake_id},
        print_=fingerprint("staking:unstake", stake_id),
    )


async def _describe_unstake(call: Call, stake_id: str) -> str:
    preflight_write(call, _unstake_write(stake_id))
    return f"Request unstake for stake '{clean_label(stake_id)}'?"


@operation(
    "Unstake USDC from model",
    toolset="staking",
    requires=("api_key",),
    gate="staking",
    describe=_describe_unstake,
)
async def unstake_from_model(
    call: Call, stake_id: Annotated[str, Field(min_length=1)]
) -> WriteResult:
    """Initiate unstaking USDC for a stake ID."""
    data, unknown = await send_write(call, _unstake_write(stake_id))
    return unknown or WriteResult.from_raw(data, status="ok", message=f"Unstaked {stake_id}.")


def _claim_write(model_id: str, round_id: str) -> JournaledWrite:
    return JournaledWrite(
        tool_key="staking:claim_payout",
        endpoint=ep.STAKING_CLAIM,
        summary=f"claim payout {model_id} round {round_id}",
        body={"model_id": model_id, "round_id": round_id},
        print_=fingerprint("staking:claim", model_id, round_id),
    )


async def _describe_claim(call: Call, model_id: str, round_id: str) -> str:
    preflight_write(call, _claim_write(model_id, round_id))
    return f"Claim resolved payout for model '{clean_label(model_id)}' on round {round_id}?"


@operation(
    "Claim resolved payout",
    toolset="staking",
    requires=("api_key",),
    gate="staking",
    describe=_describe_claim,
)
async def claim_payout(
    call: Call,
    model_id: ModelId,
    round_id: Annotated[str, Field(min_length=1)],
) -> WriteResult:
    """Claim resolved USDC payout for a model and round."""
    data, unknown = await send_write(call, _claim_write(model_id, round_id))
    return unknown or WriteResult.from_raw(data, status="ok", message="Payout claimed.")


def _relay_charge(settings: Settings) -> Decimal | None:
    """USDC a relay counts toward the daily staking cap, since only the forwarder knows it.

    A relay counts as the largest stake one call may move (`EVERESTEER_MAX_STAKE_USDC`), or as
    the whole daily cap when that limit is `unlimited`. A limit of 0 refuses relays like stakes.
    """
    per_call = settings.max_stake_usdc
    check_usdc_limit(per_call, Decimal(0), "Relaying a stake", "EVERESTEER_MAX_STAKE_USDC")
    if per_call != "unlimited":
        return per_call
    daily = settings.max_daily_usdc
    if daily == "unlimited":
        return None
    check_usdc_limit(daily, Decimal(0), "Relaying a stake", "EVERESTEER_MAX_DAILY_USDC")
    return daily


def _relay_stake_write(call: Call, model_id: str) -> JournaledWrite:
    charge = _relay_charge(call.settings)
    counted = "" if charge is None else f" (counted as {charge:.2f} USDC)"
    return JournaledWrite(
        tool_key="staking:relay_stake",
        endpoint=ep.STAKING_RELAY_STAKE,
        summary=f"relay stake for {model_id}{counted}",
        usdc=charge,
        body={"model_id": model_id},
        print_=fingerprint("staking:relay_stake", model_id),
    )


async def _describe_relay_stake(call: Call, model_id: str) -> str:
    write = _relay_stake_write(call, model_id)
    preflight_write(call, write)
    counted = (
        ""
        if write.usdc is None
        else f" The amount is only known on-chain, so this counts as {write.usdc:.2f} USDC "
        + "toward today's staking cap."
    )
    return (
        f"Relay forwarder USDC balance into the staking contract for model "
        f"'{clean_label(model_id)}'?{counted}"
    )


@operation(
    "Relay forwarder balance to stake",
    toolset="staking",
    requires=("api_key",),
    gate="staking",
    describe=_describe_relay_stake,
)
async def relay_stake(call: Call, model_id: ModelId) -> WriteResult:
    """Forward deposited USDC from your CREATE2 forwarder to the staking contract."""
    data, unknown = await send_write(call, _relay_stake_write(call, model_id))
    return unknown or WriteResult.from_raw(data, status="ok", message="Stake relayed.")


def _relay_claim_write(round_id: int) -> JournaledWrite:
    return JournaledWrite(
        tool_key="staking:relay_claim",
        endpoint=ep.STAKING_RELAY_CLAIM,
        summary=f"relay claim round {round_id}",
        body={"round_id": round_id},
        print_=fingerprint("staking:relay_claim", round_id),
    )


async def _describe_relay_claim(call: Call, round_id: int) -> str:
    preflight_write(call, _relay_claim_write(round_id))
    return f"Relay claim payout for resolved round {round_id}?"


@operation(
    "Relay claim payout",
    toolset="staking",
    requires=("api_key",),
    gate="staking",
    describe=_describe_relay_claim,
)
async def relay_claim(call: Call, round_id: Annotated[int, Field(ge=1)]) -> WriteResult:
    """Relay payout claim from a resolved round via the CREATE2 forwarder."""
    data, unknown = await send_write(call, _relay_claim_write(round_id))
    return unknown or WriteResult.from_raw(data, status="ok", message="Claim relayed.")


def _resolve_plan_amounts(
    *,
    commitment_micro: int | None,
    commitment_usdc: str | int | None,
    per_round_stake_micro: int | None,
    per_round_stake_usdc: str | int | None,
    enabled: bool,
) -> tuple[int | None, int | None, int]:
    c_micro = (
        resolve_usdc_micro(amount_micro=commitment_micro, amount_usdc=commitment_usdc)
        if commitment_micro is not None or commitment_usdc is not None
        else None
    )
    s_micro = (
        resolve_usdc_micro(amount_micro=per_round_stake_micro, amount_usdc=per_round_stake_usdc)
        if per_round_stake_micro is not None or per_round_stake_usdc is not None
        else None
    )
    if c_micro is not None and s_micro is not None:
        raise InvalidRequest(
            "Pass either commitment (`commitment_micro`/`commitment_usdc`) or per-round stake "
            + "(`per_round_stake_micro`/`per_round_stake_usdc`), not both."
        )
    if enabled and c_micro is None and s_micro is None:
        raise InvalidRequest(
            "Enabling a stake plan requires commitment (`commitment_usdc`/`commitment_micro`) "
            + "or per-round stake (`per_round_stake_usdc`/`per_round_stake_micro`)."
        )
    return c_micro, s_micro, c_micro or s_micro or 0


def _plan_write(
    call: Call,
    *,
    model_id: str,
    mode: str,
    enabled: bool,
    commitment_micro: int | None,
    commitment_usdc: str | int | None,
    per_round_stake_micro: int | None,
    per_round_stake_usdc: str | int | None,
) -> tuple[str, JournaledWrite]:
    """Build a plan write and its prompt detail; a plan without an amount moves no USDC."""
    c_micro, s_micro, effective_micro = _resolve_plan_amounts(
        commitment_micro=commitment_micro,
        commitment_usdc=commitment_usdc,
        per_round_stake_micro=per_round_stake_micro,
        per_round_stake_usdc=per_round_stake_usdc,
        enabled=enabled,
    )
    usd = micro_to_decimal(effective_micro) if effective_micro > 0 else None
    if usd is not None:
        check_usdc_limit(
            call.settings.max_stake_usdc, usd, "Stake plan", "EVERESTEER_MAX_STAKE_USDC"
        )
    body: dict[str, Any] = {"mode": mode, "enabled": enabled}
    if c_micro is not None:
        body["commitment_micro"] = c_micro
        detail = f"{format_micro_usdc(c_micro)} USDC total commitment"
    elif s_micro is not None:
        body["per_round_stake_micro"] = s_micro
        detail = f"{format_micro_usdc(s_micro)} USDC/round"
    else:
        detail = "0 USDC (disable)"
    write = JournaledWrite(
        tool_key="staking:set_staking_plan",
        endpoint=ep.STAKING_PLAN_SET,
        path_args={"model_id": model_id},
        summary=(
            f"plan {model_id} enabled={enabled} mode={mode} "
            f"({format_micro_usdc(effective_micro)} USDC)"
        ),
        usdc=usd,
        print_=fingerprint("staking:plan", model_id, mode, enabled, c_micro, s_micro),
        body=body,
        plan_key=model_id,
        per_round=enabled and s_micro is not None,
    )
    return detail, write


async def _describe_staking_plan(
    call: Call,
    model_id: str,
    mode: str = "compound",
    enabled: bool = True,
    commitment_micro: int | None = None,
    commitment_usdc: str | int | None = None,
    per_round_stake_micro: int | None = None,
    per_round_stake_usdc: str | int | None = None,
) -> str:
    detail, write = _plan_write(
        call,
        model_id=model_id,
        mode=mode,
        enabled=enabled,
        commitment_micro=commitment_micro,
        commitment_usdc=commitment_usdc,
        per_round_stake_micro=per_round_stake_micro,
        per_round_stake_usdc=per_round_stake_usdc,
    )
    preflight_write(call, write)
    action = "Enable" if enabled else "Disable"
    recurring = (
        " Its per-round stake counts toward the daily staking cap on every UTC day while enabled."
        if write.per_round
        else ""
    )
    return (
        f"{action} staking plan for model '{clean_label(model_id)}' ({detail}, mode={mode})?"
        f"{recurring}"
    )


@operation(
    "Configure model stake plan",
    toolset="staking",
    requires=("api_key",),
    gate="staking",
    idempotent=True,
    describe=_describe_staking_plan,
)
async def set_staking_plan(
    call: Call,
    model_id: ModelId,
    mode: Literal["constant", "compound"] = "compound",
    enabled: bool = True,
    commitment_micro: Annotated[
        int | None,
        Field(gt=0, description="Total commitment in micro-USDC (spread across active ladder)."),
    ] = None,
    commitment_usdc: Annotated[
        str | int | None,
        Field(description="Total commitment in USDC as string (e.g. '250') or integer."),
    ] = None,
    per_round_stake_micro: Annotated[
        int | None,
        Field(gt=0, description="Exact per-round stake in micro-USDC (alternative to commitment)."),
    ] = None,
    per_round_stake_usdc: Annotated[
        str | int | None,
        Field(description="Exact per-round stake in USDC as string (e.g. '15') or integer."),
    ] = None,
) -> WriteResult:
    """Create or update a per-model Staking v2 plan (`constant` or `compound`)."""
    _, write = _plan_write(
        call,
        model_id=model_id,
        mode=mode,
        enabled=enabled,
        commitment_micro=commitment_micro,
        commitment_usdc=commitment_usdc,
        per_round_stake_micro=per_round_stake_micro,
        per_round_stake_usdc=per_round_stake_usdc,
    )
    data, unknown = await send_write(call, write)
    return unknown or WriteResult.from_raw(
        data,
        status="accepted" if enabled else "ok",
        message=f"Stake plan updated for model '{model_id}'.",
    )


def _allocation_write(
    call: Call,
    model: str,
    amount_micro: int | None,
    amount_usdc: str | int | None,
    window: str | None,
) -> tuple[int, JournaledWrite]:
    micro = resolve_usdc_micro(amount_micro=amount_micro, amount_usdc=amount_usdc)
    usd = micro_to_decimal(micro)
    check_usdc_limit(
        call.settings.max_stake_usdc, usd, "Event stake draft", "EVERESTEER_MAX_STAKE_USDC"
    )
    body: dict[str, Any] = {"amount_micro": micro}
    if window is not None:
        body["window"] = window
    return micro, JournaledWrite(
        tool_key="staking:set_stake_allocation",
        endpoint=ep.EVENT_STAKING_ALLOC_SET,
        path_args={"model": model},
        summary=f"draft {format_micro_usdc(micro)} USDC on {model}",
        usdc=usd,
        print_=fingerprint("event_alloc", model, micro, window),
        body=body,
    )


async def _describe_event_alloc(
    call: Call,
    model: str,
    amount_micro: int | None = None,
    amount_usdc: str | int | None = None,
    window: str | None = None,
) -> str:
    micro, write = _allocation_write(call, model, amount_micro, amount_usdc, window)
    preflight_write(call, write)
    win = f" in window '{clean_label(window)}'" if window else ""
    return (
        f"Draft event stake allocation of {format_micro_usdc(micro)} USDC ({micro} micro-USDC) "
        f"for model '{clean_label(model)}'{win}? (Locked automatically at round open; immutable.)"
    )


@operation(
    "Draft event stake allocation",
    toolset="staking",
    requires=("api_key",),
    gate="staking",
    idempotent=True,
    describe=_describe_event_alloc,
)
async def set_stake_allocation(
    call: Call,
    model: ModelId,
    amount_micro: Annotated[
        int | None,
        Field(gt=0, description="Exact integer micro-USDC (1 USDC = 1_000_000). Preferred."),
    ] = None,
    amount_usdc: Annotated[
        str | int | None,
        Field(description="Exact decimal USDC string (e.g. '2.5'). Floats are refused."),
    ] = None,
    window: Annotated[
        str | None, Field(description="Optional guard matching draft_window (e.g. 'round_1').")
    ] = None,
) -> WriteResult:
    """Create or replace an off-chain event stake draft (locked immutably when round opens)."""
    _, write = _allocation_write(call, model, amount_micro, amount_usdc, window)
    data, unknown = await send_write(call, write)
    return unknown or WriteResult.from_raw(
        data, status="ok", message=f"Event draft saved: {write.summary}."
    )


@operation(
    "Drop event stake draft",
    toolset="staking",
    requires=("api_key",),
    gate="staking",
    idempotent=True,
)
async def withdraw_stake_allocation(
    call: Call,
    model: ModelId,
    window: Annotated[
        str | None, Field(description="Optional window guard (e.g. 'round_1').")
    ] = None,
) -> WriteResult:
    """Remove an unlocked event stake allocation draft (refused with 409 once locked)."""
    params = {"window": window} if window is not None else None
    res = await call.json(ep.EVENT_STAKING_ALLOC_DEL, path_args={"model": model}, params=params)
    return WriteResult.from_raw(res, status="ok", message="Event stake draft removed.")


# -- Withdrawals (gate="withdrawals", mandatory human confirmation) ----------


def _withdraw_write(
    call: Call, to_address: str, amount_usdc: str | int, idempotency_key: str | None
) -> tuple[int, JournaledWrite]:
    micro = parse_usdc_to_micro(amount_usdc)
    usd = micro_to_decimal(micro)
    check_usdc_limit(
        call.settings.max_withdraw_usdc,
        usd,
        "Withdrawal",
        "EVERESTEER_MAX_WITHDRAW_USDC",
    )
    return micro, JournaledWrite(
        tool_key="withdrawals:withdraw_usdc",
        endpoint=ep.STAKING_WITHDRAW,
        summary=f"withdraw {format_micro_usdc(micro)} USDC to {to_address}",
        usdc=usd,
        print_=fingerprint("withdraw", to_address.lower(), micro),
        body={"to_address": to_address, "amount_usdc": float(usd)},
        idempotency=Idempotency(requested=idempotency_key or None),
    )


async def _describe_withdraw(
    call: Call,
    to_address: str,
    amount_usdc: str | int,
    idempotency_key: str | None = None,
) -> str:
    micro, write = _withdraw_write(call, to_address, amount_usdc, idempotency_key)
    preflight_write(call, write)
    return (
        f"WITHDRAW {format_micro_usdc(micro)} USDC to verified wallet {to_address}? "
        "This moves funds on-chain and cannot be undone."
    )


@operation(
    "Withdraw USDC to verified wallet",
    toolset="staking",
    requires=("api_key",),
    gate="withdrawals",
    destructive=True,
    describe=_describe_withdraw,
    mandatory_confirm=True,
)
async def withdraw_usdc(
    call: Call,
    to_address: WalletAddress,
    amount_usdc: UsdcAmount,
    idempotency_key: Annotated[
        str | None,
        Field(
            description=(
                "Optional Idempotency-Key header. If omitted, a retry of an identical withdrawal "
                "whose outcome is unknown reuses that attempt's key; otherwise a fresh UUID4."
            )
        ),
    ] = None,
) -> WriteResult:
    """Withdraw USDC to verified destination wallet (requires ALLOW_WITHDRAWALS + confirmation)."""
    _, write = _withdraw_write(call, to_address, amount_usdc, idempotency_key)
    data, unknown = await send_write(call, write)
    return unknown or WriteResult.from_raw(
        data, status="ok", message=f"Withdrawal submitted: {write.summary}."
    )
