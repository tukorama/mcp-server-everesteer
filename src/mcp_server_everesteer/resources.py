"""MCP resources exposing live Everesteer state."""

from collections.abc import Callable

from mcp.server import MCPServer

from mcp_server_everesteer.ops.core import explain_scoring, get_capabilities, get_rounds
from mcp_server_everesteer.ops.models_api import list_models
from mcp_server_everesteer.ops.registry import Call, Runtime


def register_resources(server: MCPServer, runtime: Callable[[], Runtime]) -> None:
    """Register live read-only JSON resources."""

    @server.resource(
        "everesteer://capabilities",
        description="Live Everesteer platform capability index and compute rate card.",
        mime_type="application/json",
    )
    async def capabilities_resource() -> str:
        result = await get_capabilities(Call(runtime=runtime()))
        return result.model_dump_json()

    @server.resource(
        "everesteer://scoring",
        description="Live Everesteer scoring formula and score_multiple_constant `b`.",
        mime_type="application/json",
    )
    async def scoring_resource() -> str:
        result = await explain_scoring(Call(runtime=runtime()))
        return result.model_dump_json()

    @server.resource(
        "everesteer://rounds/current",
        description="Currently active futures tournament round.",
        mime_type="application/json",
    )
    async def current_round_resource() -> str:
        result = await get_rounds(Call(runtime=runtime()), tournament="futures", current_only=True)
        return result.model_dump_json()

    @server.resource(
        "everesteer://models",
        description="Your registered Everesteer models, auto-submit status, and latest scores.",
        mime_type="application/json",
    )
    async def models_resource() -> str:
        result = await list_models(Call(runtime=runtime()))
        return result.model_dump_json()
