"""Unit tests for safety checks, SQLite write journal, daily caps, and prediction ranges."""

from decimal import Decimal

import httpx2
import pytest

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.errors import InvalidRequest, Refused
from mcp_server_everesteer.models import WriteResult
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
