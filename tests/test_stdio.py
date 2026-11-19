"""Stdio JSON-RPC integration tests."""

import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.anyio

_INHERIT_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
)


def _clean_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in _INHERIT_ENV if key in os.environ}
    env.update(
        {
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "APPDATA": str(tmp_path / "config"),
            "LOCALAPPDATA": str(tmp_path / "cache"),
            "EVERESTEER_JOURNAL_FILE": str(tmp_path / "journal.sqlite3"),
            "EVERESTEER_CACHE_DIR": str(tmp_path / "http_cache"),
            "EVERESTEER_LOG_LEVEL": "ERROR",
        }
    )
    env.update(extra)
    return env


async def test_stdio_unauthenticated_exposes_only_safe_tools(tmp_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server_everesteer"],
        env=_clean_env(tmp_path),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "mcp-server-everesteer"
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "get_health",
            "get_capabilities",
            "explain_scoring",
            "whoami",
        }
        who = await session.call_tool("whoami", {})
        assert not who.is_error
        assert who.structured_content is not None
        assert who.structured_content["authenticated"] is False
        assert who.structured_content["scope"] == "unauthenticated"


async def test_stdio_authenticated_exposes_default_readonly_toolsets(tmp_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server_everesteer"],
        env=_clean_env(tmp_path, EVERESTEER_API_KEY="eiq_test_fixture_key_000001"),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "mcp-server-everesteer"
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        assert len(names) == 45
        assert {"get_started", "list_models", "price_training_job", "get_profile"} <= names
        assert "create_model" not in names
        assert "stake_on_model" not in names
        assert "withdraw_usdc" not in names
        assert "download_equities_dataset" not in names
