"""Read-only smoke tests against the live Everesteer API (`EVERESTEER_LIVE=1`)."""

import os

import pytest
from mcp import Client

from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.server import create_server

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("EVERESTEER_LIVE") != "1",
        reason="Live smoke tests require EVERESTEER_LIVE=1 and real credentials.",
    ),
]


async def test_live_readonly_smoke() -> None:
    settings = Settings()
    if settings.credentials() is None:
        pytest.skip("No Everesteer API key configured in environment or credentials file.")

    server = create_server(settings)
    async with Client(server) as client:
        for tool_name in ("get_health", "get_capabilities", "explain_scoring", "whoami"):
            result = await client.call_tool(tool_name, {})
            assert not result.is_error, f"{tool_name} failed on live API: {result.content}"
            assert result.structured_content is not None
