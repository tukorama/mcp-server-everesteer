"""MCP tests for `staking`, `account`, and `equities` toolsets."""

from pathlib import Path
from typing import Any

import pytest
from mcp.types import ElicitResult

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.config import Settings
from tests.conftest import FakeApi
from tests.mcp.helpers import call, session

pytestmark = pytest.mark.anyio

WALLET = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
TX_HASH = "0x" + "ab" * 32


async def test_staking_and_withdrawals_end_to_end(authed_settings: Settings, api: FakeApi) -> None:
    authed_settings.allow_staking = True
    authed_settings.allow_withdrawals = True

    api.add(ep.STAKING_BALANCE, {"model_id": "m1", "total_staked": 50.0})
    api.add(ep.STAKING_HISTORY, {"history": []})
    api.add(ep.STAKING_DEPOSIT_ADDRESS, {"address": WALLET})
    api.add(ep.STAKING_FORWARDER_BALANCE, {"balance_micro": 25_000_000})
    api.add(ep.STAKING_VAULT, {"vault_address": WALLET, "automation_enabled": True})
    api.add(ep.STAKING_WALLETS, {"withdraw_eligible": True, "frozen": False})
    api.add(ep.EVENT_STAKING_SUMMARY, {"staking_armed": True, "principal_micro": 10_000_000})
    api.add(ep.STAKING_STAKE, {"id": "stk-1", "status": "pending"})
    api.add(ep.STAKING_CONFIRM, {"id": "stk-1", "status": "confirmed"})
    api.add(ep.STAKING_UNSTAKE, {"id": "stk-1", "status": "unstaking"})
    api.add(ep.STAKING_CLAIM, {"claimed": True})
    api.add(ep.STAKING_RELAY_STAKE, {"relayed": True})
    api.add(ep.STAKING_RELAY_CLAIM, {"relayed": True})
    api.add(ep.STAKING_VAULT_CONFIG_TX, {"to": WALLET, "data": "0x1234"})
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
        assert (await call(client, "get_deposit_address"))["address"] == WALLET
        assert (await call(client, "get_forwarder_balance"))["balance_micro"] == 25_000_000
        assert (await call(client, "get_staking_vault", models=["m1"]))[
            "automation_enabled"
        ] is True
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
        await call(client, "build_vault_config_tx", model_id="m1", per_round_stake_usdc="15")
        await call(
            client, "set_stake_allocation", model="m1", amount_micro=5_000_000, window="round_1"
        )
        await call(client, "withdraw_stake_allocation", model="m1", window="round_1")
        wd = await call(client, "withdraw_usdc", to_address=WALLET, amount_usdc="5.25")
        assert wd["data"]["tx_id"] == "wd-1"
        assert any("WITHDRAW 5.25 USDC" in msg for msg in prompts)


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
