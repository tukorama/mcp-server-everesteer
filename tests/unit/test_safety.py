"""Unit tests for safety checks, SQLite write journal, daily caps, and prediction ranges."""

import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest

from mcp_server_everesteer import journal
from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.errors import InvalidRequest, Refused
from mcp_server_everesteer.journal import Journal
from mcp_server_everesteer.models import WriteResult
from mcp_server_everesteer.ops import staking
from mcp_server_everesteer.ops.registry import Call, Runtime
from mcp_server_everesteer.safety import (
    check_daily_usdc,
    check_prediction_range,
    check_usdc_limit,
    fingerprint,
    send_journaled_write,
)
from tests.conftest import FakeApi

pytestmark = pytest.mark.anyio

DAY = 24 * 3600
WALLET = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def test_usdc_limit_and_prediction_range_guards() -> None:
    check_usdc_limit(Decimal("50"), Decimal("25"), "Stake", "EVERESTEER_MAX_STAKE_USDC")
    check_usdc_limit("unlimited", Decimal("999999"), "Stake", "EVERESTEER_MAX_STAKE_USDC")
    with pytest.raises(Refused):
        check_usdc_limit(Decimal("0"), Decimal("1"), "Stake", "EVERESTEER_MAX_STAKE_USDC")
    with pytest.raises(Refused):
        check_usdc_limit(Decimal("50"), Decimal("51"), "Stake", "EVERESTEER_MAX_STAKE_USDC")

    check_prediction_range({"a": 0.0, "b": 1.0, "c": 0.5})
    with pytest.raises(InvalidRequest):
        check_prediction_range({"a": 1.2})
    with pytest.raises(InvalidRequest):
        check_prediction_range({"a": float("nan")})
    with pytest.raises(InvalidRequest):
        check_prediction_range({"a": True})


async def test_journal_blocks_unknown_duplicates_and_enforces_daily_cap(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.max_daily_usdc = Decimal("60")
    runtime = Runtime.create(authed_settings, transport=api.transport)
    call = Call(runtime=runtime)
    fp = fingerprint("staking:stake", "m1", 40_000_000)

    try:

        def drop(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("dropped", request=request)

        api.add(ep.STAKING_STAKE, drop)
        data, unknown = await send_journaled_write(
            call,
            tool_key="staking:stake_on_model",
            endpoint=ep.STAKING_STAKE,
            summary="stake 40 USDC on m1",
            usdc=Decimal("40"),
            print_=fp,
            body={"model_id": "m1"},
        )
        assert data is None
        assert isinstance(unknown, WriteResult)
        assert unknown.status == "unknown"
        assert unknown.request_id is not None

        # Identical write while previous is unknown must be blocked locally.
        with pytest.raises(Refused):
            await send_journaled_write(
                call,
                tool_key="staking:stake_on_model",
                endpoint=ep.STAKING_STAKE,
                summary="stake 40 USDC on m1",
                usdc=Decimal("40"),
                print_=fp,
                body={"model_id": "m1"},
            )

        # Daily cap still counts the unknown 40 USDC write, so another 25 USDC exceeds 60.
        with pytest.raises(Refused):
            check_daily_usdc(call, Decimal("25"), "staking")
    finally:
        await runtime.aclose()


async def test_daily_cap_holds_when_two_server_processes_share_one_journal(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.max_daily_usdc = Decimal("60")
    api.add(ep.STAKING_STAKE, {"id": "stk-1", "status": "pending"})
    first = Runtime.create(authed_settings, transport=api.transport)
    second = Runtime.create(authed_settings, transport=api.transport)
    process_a, process_b = Call(runtime=first), Call(runtime=second)

    async def stake_40(call: Call, model_id: str) -> None:
        await send_journaled_write(
            call,
            tool_key="staking:stake_on_model",
            endpoint=ep.STAKING_STAKE,
            summary=f"stake 40 USDC on {model_id}",
            usdc=Decimal("40"),
            print_=fingerprint("staking:stake", model_id, 40_000_000),
            body={"model_id": model_id},
        )

    try:
        # Both processes pass the cap check before either has recorded its stake.
        check_daily_usdc(process_a, Decimal("40"), "staking")
        check_daily_usdc(process_b, Decimal("40"), "staking")
        await stake_40(process_b, "m2")
        with pytest.raises(Refused, match="EVERESTEER_MAX_DAILY_USDC"):
            await stake_40(process_a, "m1")
        assert len(api.calls(ep.STAKING_STAKE)) == 1
        assert first.journal.spent_today("staking") == Decimal("40")
    finally:
        await first.aclose()
        await second.aclose()


async def test_relay_stake_counts_toward_the_daily_cap(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.max_stake_usdc = Decimal("100")
    authed_settings.max_daily_usdc = Decimal("150")
    api.add(ep.STAKING_STAKE, {"id": "stk-1", "status": "pending"})
    api.add(ep.STAKING_RELAY_STAKE, {"relayed": True})
    runtime = Runtime.create(authed_settings, transport=api.transport)
    call = Call(runtime=runtime)
    try:
        await staking.stake_on_model(call, model_id="m1", amount_usdc="60", wallet_address=WALLET)
        with pytest.raises(Refused, match="EVERESTEER_MAX_DAILY_USDC"):
            await staking.relay_stake(call, model_id="m1")
        assert api.calls(ep.STAKING_RELAY_STAKE) == []
    finally:
        await runtime.aclose()


async def test_relay_stake_follows_the_staking_limits(
    authed_settings: Settings, api: FakeApi
) -> None:
    api.add(ep.STAKING_RELAY_STAKE, {"relayed": True})
    api.add(ep.STAKING_STAKE, {"id": "stk-1", "status": "pending"})
    runtime = Runtime.create(authed_settings, transport=api.transport)
    call = Call(runtime=runtime)
    try:
        authed_settings.max_stake_usdc = Decimal(0)
        with pytest.raises(Refused, match="EVERESTEER_MAX_STAKE_USDC=0"):
            await staking.relay_stake(call, model_id="m1")
        assert api.calls(ep.STAKING_RELAY_STAKE) == []

        authed_settings.max_stake_usdc = "unlimited"
        authed_settings.max_daily_usdc = Decimal("150")
        await staking.relay_stake(call, model_id="m1")
        assert runtime.journal.spent_today("staking") == Decimal("150")
        with pytest.raises(Refused, match="EVERESTEER_MAX_DAILY_USDC"):
            await staking.stake_on_model(
                call, model_id="m1", amount_usdc="1", wallet_address=WALLET
            )
    finally:
        await runtime.aclose()


async def test_per_round_plan_keeps_counting_on_later_days_until_replaced(
    authed_settings: Settings, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.add(ep.STAKING_PLAN_SET, {"model_id": "m1", "mode": "compound"})
    runtime = Runtime.create(authed_settings, transport=api.transport)
    call = Call(runtime=runtime)
    now = time.time()
    try:
        monkeypatch.setattr(journal, "_now", lambda: now - DAY)
        await staking.set_staking_plan(call, model_id="m1", per_round_stake_usdc="15")
        monkeypatch.setattr(journal, "_now", lambda: now)
        assert runtime.journal.spent_today("staking") == Decimal("15")

        # A disable confirmed today stops the charge from the next UTC day on.
        await staking.set_staking_plan(call, model_id="m1", enabled=False)
        assert runtime.journal.spent_today("staking") == Decimal("15")
        monkeypatch.setattr(journal, "_now", lambda: now + DAY)
        assert runtime.journal.spent_today("staking") == Decimal(0)
    finally:
        await runtime.aclose()


def test_journal_lock_keeps_a_second_process_out_of_check_and_record(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    holder, other = Journal(path), Journal(path)
    other.db.execute("PRAGMA busy_timeout = 0")
    try:
        with (
            holder.locked(),
            pytest.raises(sqlite3.OperationalError, match="locked"),
            other.locked(),
        ):
            pass
        with other.locked():
            other.record(
                tool="staking:stake_on_model",
                endpoint=ep.STAKING_STAKE.key,
                summary="stake 1 USDC",
                usdc=Decimal(1),
                fingerprint="fp",
            )
        assert holder.spent_today("staking") == Decimal(1)
    finally:
        holder.close()
        other.close()


def test_journal_upgrades_a_v0_1_1_database_in_place(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        """CREATE TABLE writes (
            id TEXT PRIMARY KEY, created REAL NOT NULL, tool TEXT NOT NULL,
            endpoint TEXT NOT NULL, summary TEXT NOT NULL, usdc TEXT, status TEXT NOT NULL,
            fingerprint TEXT, response TEXT
        )"""
    )
    legacy.execute(
        """INSERT INTO writes VALUES ('old', ?, 'staking:stake_on_model',
        'POST /api/v1/staking/stake', 'stake 25', '25', 'sent', 'fp-old', NULL)""",
        (time.time(),),
    )
    legacy.commit()
    legacy.close()

    upgraded = Journal(path)
    try:
        assert upgraded.spent_today("staking") == Decimal(25)
        entry_id = upgraded.record(
            tool="withdrawals:withdraw_usdc",
            endpoint=ep.STAKING_WITHDRAW.key,
            summary="withdraw 5 USDC",
            usdc=Decimal(5),
            fingerprint="fp-new",
            idempotency_key="key-1",
        )
        entry = upgraded.get(entry_id)
        assert entry is not None
        assert entry.idempotency_key == "key-1"
    finally:
        upgraded.close()
