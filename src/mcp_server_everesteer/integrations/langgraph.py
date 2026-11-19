"""Use Everesteer directly from LangChain and LangGraph without an MCP subprocess.

Requires the optional extra: ``uv add "mcp-server-everesteer[langgraph]"``.

Confirmed operations (staking, withdrawals, paid event submissions, hosted training, destructive
calls) pause the graph via ``interrupt()`` with the exact server-authored confirmation message.
Resume with ``Command(resume=True)`` to approve, or any other value to decline.
"""

import asyncio
import concurrent.futures
import inspect
import threading
import typing
from collections.abc import Awaitable, Coroutine
from typing import Any, TypeVar, cast

import langchain.agents as _lc_agents
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt
from pydantic import BaseModel, create_model

from mcp_server_everesteer import diagrams, knowledge
from mcp_server_everesteer.capabilities import Capabilities
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.errors import EveresteerError
from mcp_server_everesteer.models import WriteResult
from mcp_server_everesteer.ops import (
    Call,
    Operation,
    Runtime,
    confirmation_required,
    enabled_operations,
)
from mcp_server_everesteer.redaction import scrub
from mcp_server_everesteer.server import detect_capabilities

T = TypeVar("T")


def agent_prompt(caps: Capabilities) -> str:
    """Return the dynamic Everesteer system prompt matching ``caps``."""
    return knowledge.agent_guide(caps.toolsets, gates=caps.gates)


class _Bridge:
    """Runs all HTTP and SQLite journal calls on one dedicated daemon event loop.

    Works cleanly whether LangGraph executes tools from async coroutines or sync worker threads.
    """

    def __init__(self, settings: Settings, caps: Capabilities) -> None:
        self.settings = settings
        self.caps = caps
        self._loop = asyncio.new_event_loop()
        self._runtime: Runtime | None = None
        threading.Thread(target=self._loop.run_forever, daemon=True).start()

    def _call_of(self) -> Call:
        if self._runtime is None:
            self._runtime = Runtime.create(self.settings, caps=self.caps)
        return Call(runtime=self._runtime)

    async def _guard(self, coroutine: Awaitable[T]) -> T:
        try:
            return await coroutine
        except EveresteerError as exc:
            raise ToolException(scrub(exc.for_agent())) from exc
        except Exception as exc:
            raise ToolException(scrub(f"Internal error: {type(exc).__name__}: {exc}")) from exc

    async def run(self, op: Operation, kwargs: dict[str, Any]) -> Any:
        result = await self._guard(op.fn(self._call_of(), **kwargs))
        return result.model_dump(mode="json") if isinstance(result, BaseModel) else result

    async def needs_asking(self, op: Operation, kwargs: dict[str, Any]) -> bool:
        return confirmation_required(op, self.settings, kwargs)

    async def describe(self, op: Operation, kwargs: dict[str, Any]) -> str:
        describe = op.describe
        if describe is None:
            raise RuntimeError(f"{op.name} has no describe function")
        return scrub(await self._guard(describe(self._call_of(), **kwargs)))

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)


def _args_schema(op: Operation) -> type[BaseModel]:
    signature = inspect.signature(op.fn, eval_str=True)
    hints = typing.get_type_hints(op.fn, include_extras=True)
    fields: dict[str, Any] = {
        param.name: (
            hints.get(param.name, Any),
            ... if param.default is inspect.Parameter.empty else param.default,
        )
        for param in list(signature.parameters.values())[1:]
    }
    return create_model(f"{op.name}_input", **fields)


def approved(answer: Any) -> bool:
    """Return True only when a LangGraph ``Command(resume=...)`` value explicitly approves."""
    if answer is True:
        return True
    if isinstance(answer, str):
        return answer.strip().lower() in ("y", "yes", "approve", "approved", "confirm")
    if isinstance(answer, dict):
        mapping = cast(dict[str, Any], answer)
        if mapping.get("type") in ("approve", "accept"):
            return True
        return mapping.get("approve") is True or mapping.get("confirm") is True
    return False


def _declined(op: Operation) -> dict[str, Any]:
    hints = typing.get_type_hints(op.fn)
    result_type = cast(type[WriteResult], hints.get("return", WriteResult))
    return result_type(
        status="declined",
        message="The user did not confirm, so nothing was sent.",
        next_step="Do not retry unless the user explicitly asks again.",
    ).model_dump(mode="json")


def _approval_payload(op: Operation, text: str) -> dict[str, Any]:
    return {"tool": op.name, "message": text, "gate": op.gate, "destructive": op.destructive}


def _build_tool(op: Operation, bridge: _Bridge, prefix: str) -> BaseTool:
    # `interrupt()` must run inside LangGraph's execution context rather than the bridge thread.
    def run(**kwargs: Any) -> Any:
        if bridge.submit(bridge.needs_asking(op, kwargs)).result():
            text = bridge.submit(bridge.describe(op, kwargs)).result()
            if not approved(interrupt(_approval_payload(op, text))):
                return _declined(op)
        return bridge.submit(bridge.run(op, kwargs)).result()

    async def arun(**kwargs: Any) -> Any:
        if await asyncio.wrap_future(bridge.submit(bridge.needs_asking(op, kwargs))):
            text = await asyncio.wrap_future(bridge.submit(bridge.describe(op, kwargs)))
            if not approved(interrupt(_approval_payload(op, text))):
                return _declined(op)
        return await asyncio.wrap_future(bridge.submit(bridge.run(op, kwargs)))

    return StructuredTool.from_function(
        func=run,
        coroutine=arun,
        name=f"{prefix}{op.name}",
        description=op.description,
        args_schema=_args_schema(op),
        handle_tool_error=True,
        metadata={"toolset": op.toolset, "gate": op.gate, "read_only": op.read_only},
    )


def _resolve(settings: Settings | None, toolsets: str | None) -> Settings:
    resolved = settings or Settings()
    if toolsets is not None:
        resolved = resolved.model_copy(update={"toolsets": toolsets})
    return resolved


def get_everesteer_tools(
    settings: Settings | None = None,
    *,
    toolsets: str | None = None,
    prefix: str = "",
    include_gated: bool = False,
) -> list[BaseTool]:
    """Return LangChain tools for enabled Everesteer operations.

    Tools that call ``interrupt()`` require a checkpointer inside your graph and are omitted
    unless ``include_gated=True`` (``create_everesteer_agent`` attaches them automatically).
    """
    resolved = _resolve(settings, toolsets)
    caps = detect_capabilities(resolved)
    bridge = _Bridge(resolved, caps)
    return [
        _build_tool(op, bridge, prefix)
        for op in enabled_operations(caps)
        if include_gated or not op.needs_confirmation
    ]


def create_everesteer_agent(
    model: Any,
    *,
    settings: Settings | None = None,
    toolsets: str | None = None,
    tools: list[Any] | None = None,
    system_prompt: str | None = None,
    middleware: list[Any] | None = None,
    checkpointer: Any = "memory",
) -> Any:
    """Create a compiled LangGraph agent wired to Everesteer tools and human-approval interrupts."""
    resolved = _resolve(settings, toolsets)
    caps = detect_capabilities(resolved)
    own = get_everesteer_tools(resolved, include_gated=True)
    has_confirm = any(op.needs_confirmation for op in enabled_operations(caps))
    if checkpointer == "memory":
        checkpointer = InMemorySaver() if has_confirm else None
    agent_factory = cast(Any, _lc_agents).create_agent
    return agent_factory(
        model,
        [*own, *(tools or [])],
        system_prompt=system_prompt or agent_prompt(caps),
        middleware=list(middleware or []),
        checkpointer=checkpointer,
    )


def _graph_lines(drawn: Any) -> list[str]:
    ids = {name: f"n{index}" for index, name in enumerate(drawn.nodes)}
    lines: list[str] = []
    for name in drawn.nodes:
        if name in ("__start__", "__end__"):
            lines.append(f"{ids[name]}([{name.strip('_')}])")
        else:
            lines.append(f'{ids[name]}["{name}"]')
    for edge in drawn.edges:
        arrow = "-.->" if edge.conditional else "-->"
        link = arrow if edge.data is None else f'{arrow}|"{edge.data}"|'
        lines.append(f"{ids[edge.source]} {link} {ids[edge.target]}")
    return lines


def draw_mermaid(graph: Any) -> str:
    """Render any compiled LangGraph (prebuilt or custom) and its Everesteer tools in Mermaid."""
    drawn = graph.get_graph()
    tool_names = [
        name
        for node in drawn.nodes.values()
        for name in getattr(node.data, "tools_by_name", None) or ()
    ]
    return diagrams.render_tool_flowchart(
        tool_names,
        title="Tools",
        lead_lines=diagrams.subgraph("agent", "Agent graph", "LR", _graph_lines(drawn)),
        lead_boxes=["agent"],
        connect_from="agent",
    )
