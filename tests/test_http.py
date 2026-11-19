"""Streamable HTTP transport, `/health` probe, and Bearer auth tests."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.errors import ConfigError
from mcp_server_everesteer.http_app import build_http_app
from mcp_server_everesteer.server import create_server

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


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


def test_http_refuses_non_loopback_without_token(settings: Settings) -> None:
    server = create_server(settings)
    with pytest.raises(ConfigError, match="without authentication"):
        build_http_app(server, settings, host="0.0.0.0")  # noqa: S104


async def test_http_health_open_and_mcp_requires_bearer_token(tmp_path: Path) -> None:
    port = _free_port()
    token = "http-bearer-test-token-value"
    base_url = f"http://127.0.0.1:{port}"
    async with await anyio.open_process(
        [
            sys.executable,
            "-m",
            "mcp_server_everesteer",
            "serve",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=_clean_env(tmp_path, EVERESTEER_HTTP_BEARER_TOKEN=token),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        try:
            async with httpx2.AsyncClient(timeout=2.0) as client:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if proc.returncode is not None:
                        err = await proc.stderr.receive() if proc.stderr else b""
                        msg = err.decode("utf-8", "replace")
                        raise AssertionError(f"HTTP server exited early ({proc.returncode}): {msg}")
                    try:
                        resp = await client.get(f"{base_url}/health")
                        if resp.status_code == 200:
                            assert resp.json() == {"status": "ok"}
                            break
                    except httpx2.HTTPError:
                        pass
                    await anyio.sleep(0.05)
                else:
                    raise AssertionError("Timed out waiting for streamable-http /health endpoint")

                unauthed = await client.post(f"{base_url}/mcp", json={})
                assert unauthed.status_code == 401
                assert unauthed.json() == {"error": "unauthorized"}

                wrong = await client.post(
                    f"{base_url}/mcp",
                    json={},
                    headers={"Authorization": "Bearer wrong-token"},
                )
                assert wrong.status_code == 401

            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as authed_client,
                streamable_http_client(
                    f"{base_url}/mcp",
                    http_client=authed_client,
                ) as (read, write),
                ClientSession(read, write) as session,
            ):
                init = await session.initialize()
                assert init.server_info.name == "mcp-server-everesteer"
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == {
                    "get_health",
                    "get_capabilities",
                    "explain_scoring",
                    "whoami",
                }
        finally:
            if proc.returncode is None:
                proc.terminate()
                with anyio.move_on_after(5, shield=True):
                    await proc.wait()
                if proc.returncode is None:
                    proc.kill()
                    with anyio.move_on_after(5, shield=True):
                        await proc.wait()
