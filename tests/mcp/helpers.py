"""Helpers for calling MCP tools inside tests using mcp.Client."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

from mcp import Client
from mcp.types import TextContent

from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.server import create_server
from tests.conftest import FakeApi


@asynccontextmanager
async def session(
    settings: Settings, api: FakeApi, **client_kwargs: Any
) -> AsyncGenerator[Client, None]:
    async with Client(create_server(settings, transport=api.transport), **client_kwargs) as client:
        yield client


async def call(client: Client, tool: str, /, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if result.is_error:
        msg = (
            result.content[0].text
            if result.content and isinstance(result.content[0], TextContent)
            else str(result)
        )
        raise AssertionError(f"Tool {tool} failed unexpectedly: {msg}")
    assert result.structured_content is not None
    return cast(dict[str, Any], result.structured_content)


async def error(client: Client, tool: str, /, **arguments: Any) -> str:
    result = await client.call_tool(tool, arguments)
    assert result.is_error, f"Expected error for {tool}, got {result.structured_content}"
    assert result.content
    assert isinstance(result.content[0], TextContent)
    return result.content[0].text
