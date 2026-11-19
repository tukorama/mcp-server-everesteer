"""Confirmation wrapper tests (elicitation, dry-run, mandatory withdrawal confirm)."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import ElicitResult
from pydantic import Field

from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.models import WriteResult
from mcp_server_everesteer.ops.registry import Call, Gate, Operation, Runtime
from mcp_server_everesteer.tools import tool_function

pytestmark = pytest.mark.anyio
SENT: list[dict[str, Any]] = []


async def describe(_call: Call, amount: str, note: str = "", dry_run: bool = False) -> str:
    return f"Commit {amount} USDC ({note or 'no note'})?"


async def send(
    _call: Call,
    amount: Annotated[str, Field(description="Amount.")],
    note: str = "",
    dry_run: bool = False,
) -> WriteResult:
    """Test operation."""
    if dry_run:
        return WriteResult(status="preview", dry_run=True)
    SENT.append({"amount": amount, "note": note})
    return WriteResult(status="ok")


def build_op(gate: Gate = "staking", *, mandatory: bool = False) -> Operation:
    return Operation(
        fn=send,
        name="send",
        title="Send",
        toolset="staking",
        requires=(),
        gate=gate,
        read_only=False,
        idempotent=False,
        destructive=gate == "withdrawals",
        describe=describe,
        mandatory_confirm=mandatory,
    )


def make_server(
    settings: Settings, gate: Gate = "staking", *, mandatory: bool = False
) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncGenerator[Runtime, None]:
        runtime = Runtime.create(settings)
        try:
            yield runtime
        finally:
            await runtime.aclose()

    srv = MCPServer("test", lifespan=lifespan)
    op = build_op(gate, mandatory=mandatory)
    srv.add_tool(tool_function(op), name=op.name, description=op.description)
    return srv


@pytest.fixture(autouse=True)
def clear_sent() -> None:
    SENT.clear()


async def test_confirmation_is_hidden_from_llm_schema(settings: Settings) -> None:
    async with Client(make_server(settings)) as session:
        tool = (await session.list_tools()).tools[0]
    assert set(tool.input_schema["properties"]) == {"amount", "note", "dry_run"}


async def test_client_without_elicitation_is_refused(settings: Settings) -> None:
    async with Client(make_server(settings)) as session:
        result = await session.call_tool("send", {"amount": "5"})
    assert result.is_error
    assert SENT == []


async def test_dry_run_needs_no_confirmation(settings: Settings) -> None:
    async with Client(make_server(settings)) as session:
        result = await session.call_tool("send", {"amount": "5", "dry_run": True})
    assert result.structured_content is not None
    assert result.structured_content["status"] == "preview"
    assert SENT == []


async def test_confirm_none_skips_staking_but_never_withdrawals(settings: Settings) -> None:
    settings.confirm = "none"
    async with Client(make_server(settings, gate="staking")) as session:
        res = await session.call_tool("send", {"amount": "5"})
    assert res.structured_content is not None
    assert res.structured_content["status"] == "ok"

    async with Client(make_server(settings, gate="withdrawals", mandatory=True)) as session:
        res2 = await session.call_tool("send", {"amount": "5"})
    assert res2.is_error
    assert len(SENT) == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_accept_passes_server_text(settings: Settings, mode: Any) -> None:
    asked: list[str] = []

    async def accept(context: Any, params: Any) -> ElicitResult:
        _ = context
        asked.append(str(params.message))
        return ElicitResult(action="accept", content={"confirm": True})

    async with Client(make_server(settings), elicitation_callback=accept, mode=mode) as session:
        res = await session.call_tool("send", {"amount": "5", "note": "round 1"})
    assert res.structured_content is not None
    assert res.structured_content["status"] == "ok"
    assert asked == ["Commit 5 USDC (round 1)?"]
    assert SENT == [{"amount": "5", "note": "round 1"}]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(
    "reply_val",
    [
        ElicitResult(action="decline"),
        ElicitResult(action="cancel"),
        ElicitResult(action="accept", content={"confirm": False}),
    ],
)
async def test_decline_or_cancel_sends_nothing(
    settings: Settings, mode: Any, reply_val: ElicitResult
) -> None:
    async def reply(context: Any, params: Any) -> ElicitResult:
        _ = context, params
        return reply_val

    async with Client(make_server(settings), elicitation_callback=reply, mode=mode) as session:
        res = await session.call_tool("send", {"amount": "5"})
    assert res.structured_content is not None
    assert res.structured_content["status"] == "declined"
    assert SENT == []
