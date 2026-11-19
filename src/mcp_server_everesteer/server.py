"""MCPServer factory and module-level `server` hook for `mcp dev` / `mcp run`."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp.server import MCPServer

from mcp_server_everesteer import __version__, knowledge
from mcp_server_everesteer.capabilities import Capabilities
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.logging_setup import configure_logging
from mcp_server_everesteer.ops import Runtime
from mcp_server_everesteer.prompts import register_prompts
from mcp_server_everesteer.resources import register_resources
from mcp_server_everesteer.tools import register_tools


def detect_capabilities(settings: Settings) -> Capabilities:
    settings.register_secrets()
    return Capabilities.detect(settings)


def create_server(
    settings: Settings | None = None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> MCPServer:
    """Create a configured Everesteer MCPServer instance."""
    resolved = settings or Settings()
    caps = detect_capabilities(resolved)
    running: list[Runtime] = []

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncGenerator[Runtime, None]:
        runtime = Runtime.create(resolved, transport=transport, caps=caps)
        running.append(runtime)
        try:
            yield runtime
        finally:
            running.remove(runtime)
            await runtime.aclose()

    server = MCPServer(
        "mcp-server-everesteer",
        title="Everesteer (unofficial)",
        version=__version__,
        instructions=knowledge.agent_guide(caps.toolsets, gates=caps.gates),
        lifespan=lifespan,
        log_level=resolved.log_level,
    )
    configure_logging(resolved.log_level)
    register_tools(server, caps)
    register_resources(server, lambda: running[-1])
    register_prompts(server, caps)
    return server


def __getattr__(name: str) -> Any:
    if name == "server":
        return create_server()
    raise AttributeError(name)
