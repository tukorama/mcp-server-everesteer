"""USDC staking, CREATE2 forwarder, agent-owned vaults, event allocations, and withdrawals."""

import uuid
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_everesteer.client import endpoints as ep
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
    check_daily_usdc,
    check_usdc_limit,
    fingerprint,
    send_journaled_write,
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


@operation("Get CREATE2 deposit address", toolset="staking", requires=("api_key",), read_only=True)
async def get_deposit_address(call: Call) -> ApiPayload:
    """Get your deterministic CREATE2 USDC forwarder deposit address."""
    return ApiPayload.from_raw(await call.json(ep.STAKING_DEPOSIT_ADDRESS))


@operation("Get forwarder balance", toolset="staking", requires=("api_key",), read_only=True)
async def get_forwarder_balance(call: Call) -> ApiPayload:
    """Read spendable USDC sitting on your CREATE2 forwarder contract."""
    return ApiPayload.from_raw(await call.json(ep.STAKING_FORWARDER_BALANCE))


@operation("Get staking vault status", toolset="staking", requires=("api_key",), read_only=True)
async def get_staking_vault(
    call: Call,
    models: Annotated[
        list[str] | None, Field(description="Optional model ids whose on-chain policy to inspect.")
    ] = None,
) -> ApiPayload:
    """Inspect your deterministic vault address, balance, and whether automation is enabled."""
    params = {"models": ",".join(models)} if models else None
    return ApiPayload.from_raw(await call.json(ep.STAKING_VAULT, params=params))


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


async def _describe_stake(
    _call: Call, model_id: str, amount_usdc: str | int, wallet_address: str
) -> str:
    micro = parse_usdc_to_micro(amount_usdc)
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
    micro = parse_usdc_to_micro(amount_usdc)
    usd = micro_to_decimal(micro)
    check_usdc_limit(call.settings.max_stake_usdc, usd, "Stake", "EVERESTEER_MAX_STAKE_USDC")
    check_daily_usdc(call, usd, "staking")
    summary = f"stake {format_micro_usdc(micro)} USDC on {model_id}"
    data, unknown = await send_journaled_write(
        call,
        tool_key="staking:stake_on_model",
        endpoint=ep.STAKING_STAKE,
        summary=summary,
        usdc=usd,
        print_=fingerprint("staking:stake", model_id, micro, wallet_address.lower()),
        body={
            "model_id": model_id,
            "amount_usdc": float(usd),
            "wallet_address": wallet_address,
        },
    )
    return unknown or WriteResult.from_raw(data, status="ok", message=f"Recorded: {summary}.")


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


async def _describe_unstake(_call: Call, stake_id: str) -> str:
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
    data, unknown = await send_journaled_write(
        call,
        tool_key="staking:unstake_from_model",
        endpoint=ep.STAKING_UNSTAKE,
        summary=f"unstake {stake_id}",
        path_args={"stake_id": stake_id},
        print_=fingerprint("staking:unstake", stake_id),
    )
    return unknown or WriteResult.from_raw(data, status="ok", message=f"Unstaked {stake_id}.")


async def _describe_claim(_call: Call, model_id: str, round_id: str) -> str:
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
    data, unknown = await send_journaled_write(
        call,
        tool_key="staking:claim_payout",
        endpoint=ep.STAKING_CLAIM,
        summary=f"claim payout {model_id} round {round_id}",
        body={"model_id": model_id, "round_id": round_id},
        print_=fingerprint("staking:claim", model_id, round_id),
    )
    return unknown or WriteResult.from_raw(data, status="ok", message="Payout claimed.")


async def _describe_relay_stake(_call: Call, model_id: str) -> str:
    return (
        f"Relay forwarder USDC balance into the staking contract for model "
        f"'{clean_label(model_id)}'?"
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
    data, unknown = await send_journaled_write(
        call,
        tool_key="staking:relay_stake",
        endpoint=ep.STAKING_RELAY_STAKE,
        summary=f"relay stake for {model_id}",
        body={"model_id": model_id},
        print_=fingerprint("staking:relay_stake", model_id),
    )
    return unknown or WriteResult.from_raw(data, status="ok", message="Stake relayed.")


async def _describe_relay_claim(_call: Call, round_id: int) -> str:
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
    data, unknown = await send_journaled_write(
        call,
        tool_key="staking:relay_claim",
        endpoint=ep.STAKING_RELAY_CLAIM,
        summary=f"relay claim round {round_id}",
        body={"round_id": round_id},
        print_=fingerprint("staking:relay_claim", round_id),
    )
    return unknown or WriteResult.from_raw(data, status="ok", message="Claim relayed.")


async def _describe_vault_tx(
    _call: Call,
    model_id: str,
    per_round_stake_usdc: str | int,
    mode: str = "compound",
    enabled: bool = True,
) -> str:
    micro = parse_usdc_to_micro(per_round_stake_usdc)
    return (
        f"Build unsigned vault config calldata for model '{clean_label(model_id)}' "
        f"({format_micro_usdc(micro)} USDC/round, mode={mode}, enabled={enabled})?"
    )


@operation(
    "Build vault config transaction",
    toolset="staking",
    requires=("api_key",),
    gate="staking",
    describe=_describe_vault_tx,
)
async def build_vault_config_tx(
    call: Call,
    model_id: ModelId,
    per_round_stake_usdc: UsdcAmount,
    mode: Literal["compound", "constant"] = "compound",
    enabled: bool = True,
) -> WriteResult:
    """Build UNSIGNED `setModelConfig` calldata for your owner wallet to sign and broadcast."""
    micro = parse_usdc_to_micro(per_round_stake_usdc)
    usd = micro_to_decimal(micro)
    check_usdc_limit(
        call.settings.max_stake_usdc, usd, "Per-round vault stake", "EVERESTEER_MAX_STAKE_USDC"
    )
    res = await call.json(
        ep.STAKING_VAULT_CONFIG_TX,
        body={
            "model_id": model_id,
            "per_round_stake_usdc": float(usd),
            "mode": mode,
            "enabled": enabled,
        },
    )
    return WriteResult.from_raw(
        res,
        status="ok",
        message="Unsigned vault config built. Sign and submit from your verified owner wallet.",
    )


async def _describe_event_alloc(
    _call: Call,
    model: str,
    amount_micro: int | None = None,
    amount_usdc: str | int | None = None,
    window: str | None = None,
) -> str:
    micro = resolve_usdc_micro(amount_micro=amount_micro, amount_usdc=amount_usdc)
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
    micro = resolve_usdc_micro(amount_micro=amount_micro, amount_usdc=amount_usdc)
    usd = micro_to_decimal(micro)
    check_usdc_limit(
        call.settings.max_stake_usdc, usd, "Event stake draft", "EVERESTEER_MAX_STAKE_USDC"
    )
    check_daily_usdc(call, usd, "staking")

    body: dict[str, Any] = {"amount_micro": micro}
    if window is not None:
        body["window"] = window
    summary = f"draft {format_micro_usdc(micro)} USDC on {model}"
    data, unknown = await send_journaled_write(
        call,
        tool_key="staking:set_stake_allocation",
        endpoint=ep.EVENT_STAKING_ALLOC_SET,
        path_args={"model": model},
        summary=summary,
        usdc=usd,
        print_=fingerprint("event_alloc", model, micro, window),
        body=body,
    )
    return unknown or WriteResult.from_raw(
        data, status="ok", message=f"Event draft saved: {summary}."
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


async def _describe_withdraw(
    _call: Call,
    to_address: str,
    amount_usdc: str | int,
    **_unused: Any,
) -> str:
    micro = parse_usdc_to_micro(amount_usdc)
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
        Field(description="Optional Idempotency-Key header; fresh UUID4 generated if omitted."),
    ] = None,
) -> WriteResult:
    """Withdraw USDC to verified destination wallet (requires ALLOW_WITHDRAWALS + confirmation)."""
    micro = parse_usdc_to_micro(amount_usdc)
    usd = micro_to_decimal(micro)
    check_usdc_limit(
        call.settings.max_withdraw_usdc,
        usd,
        "Withdrawal",
        "EVERESTEER_MAX_WITHDRAW_USDC",
    )
    check_daily_usdc(call, usd, "withdrawals")

    idem = idempotency_key or str(uuid.uuid4())
    summary = f"withdraw {format_micro_usdc(micro)} USDC to {to_address}"
    data, unknown = await send_journaled_write(
        call,
        tool_key="withdrawals:withdraw_usdc",
        endpoint=ep.STAKING_WITHDRAW,
        summary=summary,
        usdc=usd,
        print_=fingerprint("withdraw", to_address.lower(), micro, idem),
        body={"to_address": to_address, "amount_usdc": float(usd)},
        headers={"Idempotency-Key": idem},
    )
    return unknown or WriteResult.from_raw(
        data, status="ok", message=f"Withdrawal submitted: {summary}."
    )
