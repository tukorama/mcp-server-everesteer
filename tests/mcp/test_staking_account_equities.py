"""MCP tests for `staking`, `account`, and `equities` toolsets."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.types import ElicitResult

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.journal import Journal
from tests.conftest import FakeApi
from tests.mcp.helpers import call, error, session

pytestmark = pytest.mark.anyio

WALLET = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
TX_HASH = "0x" + "ab" * 32
WITHDRAWN = {"tx_id": "wd-1", "status": "submitted"}


def _drop(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("dropped after sending", request=request)


class Prompts:
    """Elicitation callback that approves every prompt and remembers its text."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def __call__(self, _ctx: Any, params: Any) -> ElicitResult:
        self.seen.append(str(params.message))
        return ElicitResult(action="accept", content={"confirm": True})


def _idempotency_keys(api: FakeApi) -> list[str]:
    return [request.headers["idempotency-key"] for request in api.calls(ep.STAKING_WITHDRAW)]


async def _withdraw_while_another_server_has_it_in_flight(
    settings: Settings, api: FakeApi, prompts: Prompts
) -> tuple[str, dict[str, Any]]:
    """Hold one server's withdrawal POST open and repeat it from a second server meanwhile."""
    in_flight, release = anyio.Event(), anyio.Event()
    posts = 0

    async def hold_the_first_post(_request: httpx2.Request) -> dict[str, str]:
        nonlocal posts
        posts += 1
        if posts == 1:
            in_flight.set()
            await release.wait()
        return WITHDRAWN

    api.add(ep.STAKING_WITHDRAW, hold_the_first_post)
    args = {"to_address": WALLET, "amount_usdc": "10", "idempotency_key": "wd-key-1"}
    first: dict[str, Any] = {}
    message = ""

    async def withdraw_first(client: Client) -> None:
        first.update(await call(client, "withdraw_usdc", **args))

    with anyio.fail_after(30):
        async with (
            session(settings, api, elicitation_callback=prompts) as busy,
            session(settings, api, elicitation_callback=prompts) as other,
            anyio.create_task_group() as group,
        ):
            group.start_soon(withdraw_first, busy)
            await in_flight.wait()
            message = await error(other, "withdraw_usdc", **args)
            release.set()
    return message, first


async def test_staking_and_withdrawals_end_to_end(authed_settings: Settings, api: FakeApi) -> None:
    authed_settings.allow_staking = True
    authed_settings.allow_withdrawals = True

    api.add(ep.STAKING_BALANCE, {"model_id": "m1", "total_staked": 50.0})
    api.add(ep.STAKING_HISTORY, {"history": []})
    api.add(ep.STAKING_STRATEGY, {"enabled": True, "deployed": True, "address": WALLET})
    api.add(ep.STAKING_PLANS, [{"model_id": "m1", "mode": "compound", "enabled": True}])
    api.add(ep.STAKING_POSITIONS, [{"model_id": "m1", "stake_micro": 15_000_000}])
    api.add(ep.STAKING_WALLETS, {"withdraw_eligible": True, "frozen": False})
    api.add(ep.EVENT_STAKING_SUMMARY, {"staking_armed": True, "principal_micro": 10_000_000})
    api.add(ep.STAKING_STAKE, {"id": "stk-1", "status": "pending"})
    api.add(ep.STAKING_CONFIRM, {"id": "stk-1", "status": "confirmed"})
    api.add(ep.STAKING_UNSTAKE, {"id": "stk-1", "status": "unstaking"})
    api.add(ep.STAKING_CLAIM, {"claimed": True})
    api.add(ep.STAKING_RELAY_STAKE, {"relayed": True})
    api.add(ep.STAKING_RELAY_CLAIM, {"relayed": True})
    api.add(ep.STAKING_PLAN_SET, {"model_id": "m1", "mode": "compound", "enabled": True})
    api.add(ep.EVENT_STAKING_ALLOC_SET, {"model_id": "m1", "amount_micro": 5_000_000})
    api.add(ep.EVENT_STAKING_ALLOC_DEL, {"deleted": True})
    api.add(ep.STAKING_WITHDRAW, {"tx_id": "wd-1", "status": "submitted"})

    prompts: list[str] = []

    async def accept(_ctx: Any, params: Any) -> ElicitResult:
        prompts.append(str(params.message))
        return ElicitResult(action="accept", content={"confirm": True})

    async with session(authed_settings, api, elicitation_callback=accept) as client:
        assert (await call(client, "get_stake_balance", model_id="m1"))["total_staked"] == 50.0
        assert (await call(client, "get_staking_history", model_id="m1"))["history"] == []
        assert (await call(client, "get_staking_strategy"))["address"] == WALLET
        assert (await call(client, "get_staking_plans"))["items"][0]["model_id"] == "m1"
        positions = await call(client, "get_staking_positions")
        assert positions["items"][0]["stake_micro"] == 15_000_000
        assert (await call(client, "get_wallet_status"))["withdraw_eligible"] is True
        assert (await call(client, "get_event_staking"))["staking_armed"] is True
        assert (await call(client, "list_unconfirmed_writes"))["count"] == 0

        await call(
            client, "stake_on_model", model_id="m1", amount_usdc="10.5", wallet_address=WALLET
        )
        await call(client, "confirm_stake", stake_id="stk-1", txn_hash=TX_HASH)
        await call(client, "unstake_from_model", stake_id="stk-1")
        await call(client, "claim_payout", model_id="m1", round_id="42")
        await call(client, "relay_stake", model_id="m1")
        await call(client, "relay_claim", round_id=42)
        await call(
            client,
            "set_staking_plan",
            model_id="m1",
            per_round_stake_usdc="15",
            mode="compound",
        )
        await call(
            client, "set_stake_allocation", model="m1", amount_micro=5_000_000, window="round_1"
        )
        await call(client, "withdraw_stake_allocation", model="m1", window="round_1")
        wd = await call(client, "withdraw_usdc", to_address=WALLET, amount_usdc="5.25")
        assert wd["data"]["tx_id"] == "wd-1"
        assert any("WITHDRAW 5.25 USDC" in msg for msg in prompts)


async def test_identical_withdrawal_retry_after_a_timeout_reuses_the_idempotency_key(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_withdrawals = True
    api.add(ep.STAKING_WITHDRAW, _drop, WITHDRAWN)

    async with session(authed_settings, api, elicitation_callback=Prompts()) as client:
        first = await call(client, "withdraw_usdc", to_address=WALLET, amount_usdc="10")
        second = await call(client, "withdraw_usdc", to_address=WALLET, amount_usdc="10")

    assert first["status"] == "unknown"
    assert second["status"] == "ok"
    keys = _idempotency_keys(api)
    assert len(keys) == 2
    assert keys[0] == keys[1]


async def test_withdrawal_retry_with_the_same_key_is_sent_for_server_dedupe(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_withdrawals = True
    api.add(ep.STAKING_WITHDRAW, _drop, WITHDRAWN)

    async with session(authed_settings, api, elicitation_callback=Prompts()) as client:
        args = {"to_address": WALLET, "amount_usdc": "10", "idempotency_key": "wd-key-1"}
        assert (await call(client, "withdraw_usdc", **args))["status"] == "unknown"
        assert (await call(client, "withdraw_usdc", **args))["status"] == "ok"

    assert _idempotency_keys(api) == ["wd-key-1", "wd-key-1"]


async def test_identical_withdrawal_in_flight_elsewhere_is_refused_and_not_sent(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_withdrawals = True

    message, first = await _withdraw_while_another_server_has_it_in_flight(
        authed_settings, api, Prompts()
    )

    assert "is still in flight" in message
    assert first["status"] == "ok"
    assert _idempotency_keys(api) == ["wd-key-1"]


async def test_identical_withdrawal_in_flight_elsewhere_is_refused_before_confirmation(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_withdrawals = True
    prompts = Prompts()

    message, _ = await _withdraw_while_another_server_has_it_in_flight(
        authed_settings, api, prompts
    )

    assert "is still in flight" in message
    assert len(prompts.seen) == 1


async def test_withdrawal_retry_under_a_different_key_is_refused(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_withdrawals = True
    api.add(ep.STAKING_WITHDRAW, _drop, WITHDRAWN)

    async with session(authed_settings, api, elicitation_callback=Prompts()) as client:
        args = {"to_address": WALLET, "amount_usdc": "10"}
        await call(client, "withdraw_usdc", **args, idempotency_key="wd-key-1")
        message = await error(client, "withdraw_usdc", **args, idempotency_key="wd-key-2")

    assert "idempotency_key='wd-key-1'" in message
    assert _idempotency_keys(api) == ["wd-key-1"]


async def test_idempotent_withdrawal_retry_counts_once_toward_the_daily_cap(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_withdrawals = True
    authed_settings.max_daily_usdc = Decimal("15")
    api.add(ep.STAKING_WITHDRAW, _drop, WITHDRAWN)

    async with session(authed_settings, api, elicitation_callback=Prompts()) as client:
        assert (await call(client, "withdraw_usdc", to_address=WALLET, amount_usdc="10"))[
            "status"
        ] == "unknown"
        retry = await call(client, "withdraw_usdc", to_address=WALLET, amount_usdc="10")

    assert retry["status"] == "ok"
    journal = Journal(authed_settings.resolved_journal_file)
    try:
        assert journal.spent_today("withdrawals") == Decimal("10")
    finally:
        journal.close()


async def test_repeat_of_a_successful_withdrawal_is_refused_before_confirmation(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_withdrawals = True
    api.add(ep.STAKING_WITHDRAW, WITHDRAWN)
    prompts = Prompts()

    async with session(authed_settings, api, elicitation_callback=prompts) as client:
        await call(client, "withdraw_usdc", to_address=WALLET, amount_usdc="10")
        message = await error(client, "withdraw_usdc", to_address=WALLET, amount_usdc="10")

    assert "already succeeded" in message
    assert len(prompts.seen) == 1
    assert len(api.calls(ep.STAKING_WITHDRAW)) == 1


async def test_disabling_a_stake_plan_is_sent_and_moves_no_usdc(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_staking = True
    api.add(ep.STAKING_PLAN_SET, {"model_id": "m1", "enabled": False})
    prompts = Prompts()

    async with session(authed_settings, api, elicitation_callback=prompts) as client:
        result = await call(client, "set_staking_plan", model_id="m1", enabled=False)

    assert result["status"] == "ok"
    assert api.json_body(ep.STAKING_PLAN_SET) == {"mode": "compound", "enabled": False}
    assert prompts.seen == [
        "Disable staking plan for model 'm1' (0 USDC (disable), mode=compound)?"
    ]
    journal = Journal(authed_settings.resolved_journal_file)
    try:
        assert journal.spent_today("staking") == Decimal(0)
    finally:
        journal.close()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("stake_on_model", {"model_id": "m1", "amount_usdc": "150", "wallet_address": WALLET}),
        ("set_staking_plan", {"model_id": "m1", "per_round_stake_usdc": "150"}),
        ("set_stake_allocation", {"model": "m1", "amount_usdc": "150"}),
        ("withdraw_usdc", {"to_address": WALLET, "amount_usdc": "150"}),
    ],
)
async def test_money_limits_are_enforced_before_the_user_is_asked(
    authed_settings: Settings, api: FakeApi, tool: str, arguments: dict[str, Any]
) -> None:
    authed_settings.allow_staking = True
    authed_settings.allow_withdrawals = True
    prompts = Prompts()

    async with session(authed_settings, api, elicitation_callback=prompts) as client:
        message = await error(client, tool, **arguments)

    assert "exceeds EVERESTEER_MAX_" in message
    assert prompts.seen == []
    assert api.requests == []


async def test_daily_cap_is_enforced_before_the_user_is_asked(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_staking = True
    authed_settings.max_daily_usdc = Decimal("100")
    api.add(ep.STAKING_STAKE, {"id": "stk-1", "status": "pending"})
    prompts = Prompts()

    async with session(authed_settings, api, elicitation_callback=prompts) as client:
        args = {"model_id": "m1", "amount_usdc": "60", "wallet_address": WALLET}
        await call(client, "stake_on_model", **args)
        message = await error(client, "stake_on_model", **args)

    assert "EVERESTEER_MAX_DAILY_USDC" in message
    assert len(prompts.seen) == 1
    assert len(api.calls(ep.STAKING_STAKE)) == 1


async def test_dot_segment_ids_are_refused_before_any_request(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_staking = True
    authed_settings.allow_withdrawals = True
    api.add(ep.API_KEYS_REVOKE, {"revoked": True})
    api.add(ep.STAKING_UNSTAKE, {"id": "stk-1", "status": "unstaking"})

    async with session(authed_settings, api, elicitation_callback=Prompts()) as client:
        revoke = await error(client, "revoke_api_key", key_id=".")
        unstake = await error(client, "unstake_from_model", stake_id="..")

    assert "`key_id`" in revoke
    assert "`stake_id`" in unstake
    assert api.requests == []


async def test_account_and_equities_tools(
    authed_settings: Settings, api: FakeApi, tmp_path: Path
) -> None:
    authed_settings.allow_writes = True
    authed_settings.allow_withdrawals = True
    authed_settings.toolsets = "auto,+equities"

    eq_file = tmp_path / "eq_preds.parquet"
    eq_file.write_bytes(b"EQ_PARQUET")

    api.add(ep.AGENT_REGISTER, {"agent_id": "ag-1", "api_key": "eiq_test_fixture_new_0001"})
    api.add(ep.AGENT_PROFILE, {"id": "ag-1", "email_verified_at": "2026-11-01T00:00:00Z"})
    api.add(ep.NOTIFICATIONS_LIST, {"notifications": [{"id": "n1"}]})
    api.add(ep.BADGES_LIST, {"badges": ["first_sub"]})
    api.add(ep.MODEL_BADGES, {"badges": ["top10"]})
    api.add(ep.API_KEYS_LIST, {"keys": [{"id": "k1"}]})
    api.add(ep.AGENT_RENAME, {"name": "quant-bot"})
    api.add(ep.AGENT_EMAIL_VERIFY, {"sent": True})
    api.add(ep.NOTIFICATION_READ, {"read": True})
    api.add(ep.NOTIFICATIONS_READ_ALL, {"read_all": True})
    api.add(ep.API_KEYS_CREATE, {"id": "k2"})
    api.add(ep.API_KEYS_REVOKE, {"revoked": True})
    api.add(ep.SECURITY_FREEZE, {"frozen": True})

    api.add(ep.EQUITIES_UNIVERSE, {"tickers": ["AAPL", "MSFT"]})
    api.add(ep.EQUITIES_FEATURES, {"feature_names": ["f1"], "features": {}})
    api.add(ep.EQUITIES_SUBMIT, {"submission_id": "eq-1"})
    api.add(ep.EQUITIES_SUBMIT_UPLOAD, {"submission_id": "eq-2"})

    async def accept(_ctx: Any, _params: Any) -> ElicitResult:
        return ElicitResult(action="accept", content={"confirm": True})

    async with session(authed_settings, api, elicitation_callback=accept) as client:
        assert (await call(client, "get_profile"))["id"] == "ag-1"
        assert (await call(client, "get_notifications"))["notifications"][0]["id"] == "n1"
        assert (await call(client, "get_badges"))["badges"] == ["first_sub"]
        assert (await call(client, "get_badges", model_id="m1"))["badges"] == ["top10"]
        assert (await call(client, "list_api_keys"))["keys"][0]["id"] == "k1"

        await call(client, "register_agent", name="new-agent", email="a@example.com")
        await call(client, "rename_agent", name="quant-bot")
        await call(client, "resend_email_verification")
        await call(client, "mark_notifications_read", notification_id="n1")
        await call(client, "mark_notifications_read")
        await call(client, "create_api_key", name="ci-key")
        await call(client, "revoke_api_key", key_id="k2")
        await call(client, "freeze_withdrawals")

        assert (await call(client, "get_equities_universe"))["tickers"] == ["AAPL", "MSFT"]
        assert (await call(client, "get_equities_features", date="2026-11-18"))[
            "feature_names"
        ] == ["f1"]
        await call(
            client,
            "submit_equities_predictions",
            model_id="m1",
            predictions=[{"ticker": "AAPL", "score": 0.5}],
        )
        await call(
            client,
            "submit_equities_predictions_file",
            model_id="m1",
            file_path=str(eq_file),
        )
