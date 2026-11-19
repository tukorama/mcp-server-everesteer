"""Unit tests for LangGraph & LangChain integration, human interrupts, and draw_mermaid."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command
from mcp import Client

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.integrations import langgraph as integration
from mcp_server_everesteer.ops import Runtime
from mcp_server_everesteer.server import create_server
from tests.conftest import FakeApi

pytestmark = pytest.mark.anyio

README = Path(__file__).parents[2] / "README.md"
WALLET = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def inject(monkeypatch: pytest.MonkeyPatch, api: FakeApi) -> None:
    original = Runtime.create

    def fake_create(cls: Any, settings: Settings, *, caps: Any = None, **kw: Any) -> Runtime:
        _ = cls, kw
        return original(settings, transport=api.transport, caps=caps)

    monkeypatch.setattr(integration.Runtime, "create", classmethod(fake_create))


def tool_call_model(name: str, args: dict[str, Any]) -> GenericFakeChatModel:
    class BindableFake(GenericFakeChatModel):
        def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "BindableFake":
            return self

    messages: list[AIMessage | str] = [
        AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1"}]),
        AIMessage(content="done"),
    ]
    return BindableFake(messages=iter(messages))


async def test_langchain_schemas_match_mcp_tools(
    authed_settings: Settings, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    authed_settings.allow_writes = True
    authed_settings.allow_staking = True
    authed_settings.allow_withdrawals = True
    inject(monkeypatch, api)

    async with Client(create_server(authed_settings, transport=api.transport)) as session:
        mcp_tools = {tool.name: tool for tool in (await session.list_tools()).tools}

    tools = integration.get_everesteer_tools(authed_settings, include_gated=True)
    assert {tool.name for tool in tools} == set(mcp_tools)
    for tool in tools:
        schema = cast(dict[str, Any], tool.args_schema.model_json_schema())  # type: ignore[union-attr]
        mcp_schema = mcp_tools[tool.name].input_schema
        mcp_props = cast(dict[str, Any], mcp_schema.get("properties", {}))
        assert set(schema.get("properties", {})) == set(mcp_props), tool.name
        assert tool.description == mcp_tools[tool.name].description


def test_gated_tools_excluded_unless_requested(authed_settings: Settings) -> None:
    authed_settings.allow_writes = True
    authed_settings.allow_staking = True
    names = {t.name for t in integration.get_everesteer_tools(authed_settings)}
    assert "stake_on_model" not in names
    assert "list_models" in names


def test_toolsets_and_prefix(authed_settings: Settings) -> None:
    tools = integration.get_everesteer_tools(authed_settings, toolsets="models", prefix="ev_")
    assert tools
    assert all(t.name.startswith("ev_") for t in tools)


async def test_async_and_sync_tools(
    authed_settings: Settings, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    inject(monkeypatch, api)
    api.add(ep.HEALTH, {"status": "ok"})
    tool = next(
        t for t in integration.get_everesteer_tools(authed_settings) if t.name == "get_health"
    )
    assert (await tool.ainvoke({}))["status"] == "ok"


def test_sync_tool_invocation(
    authed_settings: Settings, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    inject(monkeypatch, api)
    api.add(ep.HEALTH, {"status": "ok"})
    tool = next(
        t for t in integration.get_everesteer_tools(authed_settings) if t.name == "get_health"
    )
    assert tool.invoke({})["status"] == "ok"


async def test_errors_reach_model_as_text(
    authed_settings: Settings, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    inject(monkeypatch, api)
    api.add(ep.DIAGNOSTICS_RUN, (404, {"detail": "upload not found"}))
    tool = next(
        t
        for t in integration.get_everesteer_tools(authed_settings)
        if t.name == "get_diagnostics_run"
    )
    res = await tool.ainvoke({"upload_id": "missing"})
    assert isinstance(res, str)
    assert "Not found" in res


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (True, True),
        ("yes", True),
        ({"type": "approve"}, True),
        ({"confirm": True}, True),
        (False, False),
        ("no", False),
        ({"type": "reject"}, False),
        (None, False),
        (1, False),
    ],
)
def test_approved(answer: Any, expected: bool) -> None:
    assert integration.approved(answer) is expected


async def test_agent_pauses_for_confirmation_and_resumes(
    authed_settings: Settings, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    authed_settings.allow_staking = True
    inject(monkeypatch, api)
    api.add(ep.STAKING_STAKE, {"id": "stk_1", "status": "pending"})

    stake_args: dict[str, Any] = {
        "model_id": "m1",
        "amount_usdc": "10",
        "wallet_address": WALLET,
    }
    agent = integration.create_everesteer_agent(
        tool_call_model("stake_on_model", stake_args),
        settings=authed_settings,
    )
    config: Any = {"configurable": {"thread_id": "t-approve"}}
    state: dict[str, Any] = await agent.ainvoke({"messages": [("user", "stake")]}, config=config)
    pause = state["__interrupt__"][0].value
    assert pause["tool"] == "stake_on_model"
    assert "10.00 USDC" in pause["message"]
    assert api.calls(ep.STAKING_STAKE) == []

    resumed: dict[str, Any] = await agent.ainvoke(Command(resume=True), config=config)
    assert len(api.calls(ep.STAKING_STAKE)) == 1
    assert resumed["messages"][-1].content == "done"


async def test_agent_declines_without_sending(
    authed_settings: Settings, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    authed_settings.allow_staking = True
    inject(monkeypatch, api)

    agent = integration.create_everesteer_agent(
        tool_call_model(
            "stake_on_model",
            {"model_id": "m1", "amount_usdc": "10", "wallet_address": WALLET},
        ),
        settings=authed_settings,
    )
    config: Any = {"configurable": {"thread_id": "t-decline"}}
    await agent.ainvoke({"messages": [("user", "stake")]}, config=config)
    resumed: dict[str, Any] = await agent.ainvoke(Command(resume="no"), config=config)
    assert api.calls(ep.STAKING_STAKE) == []
    assert "did not confirm" in str(resumed["messages"][-2].content)


async def test_dry_run_skips_interrupt(
    authed_settings: Settings, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    authed_settings.allow_writes = True
    inject(monkeypatch, api)
    api.add(ep.COMPUTE_TRAIN, {"estimated_credits": 1.25, "dry_run": True})

    agent = integration.create_everesteer_agent(
        tool_call_model("train_model", {"model": "lightgbm", "dry_run": True}),
        settings=authed_settings,
    )
    state: dict[str, Any] = await agent.ainvoke(
        {"messages": [("user", "price")]}, config={"configurable": {"thread_id": "t-dry"}}
    )
    assert "__interrupt__" not in state
    assert len(api.calls(ep.COMPUTE_TRAIN)) == 1


def test_readme_graph_is_current(authed_settings: Settings) -> None:
    agent = integration.create_everesteer_agent(
        tool_call_model("whoami", {}), settings=authed_settings
    )
    drawn = integration.draw_mermaid(agent)
    assert drawn in README.read_text(encoding="utf-8")


def test_draw_mermaid_custom_graph_and_access_classes(authed_settings: Settings) -> None:
    authed_settings.allow_writes = True
    authed_settings.allow_staking = True
    authed_settings.allow_withdrawals = True

    agent = integration.create_everesteer_agent(
        tool_call_model("whoami", {}), settings=authed_settings
    )
    drawn = integration.draw_mermaid(agent)
    assert ":::funds" in drawn
    assert ":::destructive" in drawn

    def local_helper(name: str) -> str:
        return name

    def decide(state: MessagesState) -> MessagesState:
        return state

    def route(_state: MessagesState) -> str:
        return "stop"

    foreign = StructuredTool.from_function(
        local_helper, name="local_helper", description="Local helper tool."
    )
    builder: Any = StateGraph(MessagesState)
    builder.add_node("decide", decide)
    builder.add_node(
        "call", ToolNode([*integration.get_everesteer_tools(authed_settings), foreign])
    )
    builder.add_edge(START, "decide")
    builder.add_conditional_edges("decide", route, {"more": "call", "stop": END})
    builder.add_edge("call", "decide")
    custom_drawn = integration.draw_mermaid(builder.compile())
    assert 'tools_other_all["local_helper"]' in custom_drawn
