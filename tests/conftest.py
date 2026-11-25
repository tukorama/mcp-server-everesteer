"""Shared pytest fixtures and MockTransport harness for mcp-server-everesteer."""

import inspect
import json
import os
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx2
import pytest
from pydantic import SecretStr

from mcp_server_everesteer.client import base, cache
from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.config import Settings

SEEN: set[str] = set()


def parquet_bytes(payload: bytes) -> bytes:
    """Frame `payload` with the `PAR1` magic bytes every parquet file starts and ends with."""
    return b"PAR1" + payload + b"PAR1"


class FakeApi:
    """Scripted Everesteer API + presigned CDN/S3 MockTransport responder."""

    def __init__(self, settings: Settings) -> None:
        self.api_host = (urlsplit(settings.resolved_base_url).hostname or "").lower()
        self.cdn_hosts = {"cdn.everesteer.test", "s3.everesteer.test"}
        self.routes: dict[tuple[str, str | None], deque[tuple[int, Any, dict[str, str]]]] = {}
        self.cdn_routes: dict[str, tuple[int, Any, dict[str, str]]] = {}
        self.requests: list[tuple[ep.Endpoint | None, httpx2.Request]] = []
        self.cdn_requests: list[httpx2.Request] = []
        self.transport = httpx2.MockTransport(self._handle)

    def add(self, endpoint: ep.Endpoint, *responses: Any, path: str | None = None) -> None:
        normalized: list[tuple[int, Any, dict[str, str]]] = []
        for response in responses:
            if isinstance(response, tuple):
                tup = cast(tuple[Any, ...], response)
                status = int(tup[0])
                body: Any = tup[1]
                hdrs: dict[str, str] = dict(tup[2]) if len(tup) > 2 else {}
            else:
                status, body, hdrs = 200, response, {}
            normalized.append((status, body, hdrs))
        self.routes[(endpoint.key, path)] = deque(normalized)

    def add_cdn(
        self, url: str, body: Any = b"", *, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self.cdn_routes[url] = (status, body, headers or {})

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        host = (request.url.host or "").lower()
        if host in self.cdn_hosts:
            # Verify presigned storage requests NEVER receive Everesteer or CF Access credentials.
            assert "x-api-key" not in request.headers
            assert "cf-access-client-secret" not in request.headers
            self.cdn_requests.append(request)
            entry = self.cdn_routes.get(str(request.url))
            if entry is None:
                return httpx2.Response(200, content=b"ok-s3")
            status, body, hdrs = entry
            if isinstance(body, str | bytes):
                return httpx2.Response(status, content=body, headers=hdrs)
            return httpx2.Response(status, json=body, headers=hdrs)

        if host != self.api_host:
            return httpx2.Response(404, json={"error": f"unexpected host {request.url}"})

        endpoint = ep.find_endpoint(request.method, request.url.path)
        self.requests.append((endpoint, request))
        if endpoint is None:
            return httpx2.Response(404, json={"error": f"unscripted route {request.url}"})
        SEEN.add(endpoint.key)

        queue = self.routes.get((endpoint.key, request.url.path)) or self.routes.get(
            (endpoint.key, None)
        )
        if not queue:
            return httpx2.Response(404, json={"error": f"unscripted {endpoint.key}"})
        status, body, hdrs = queue.popleft() if len(queue) > 1 else queue[0]
        if callable(body):
            fn: Callable[[httpx2.Request], Any] = body
            body = fn(request)
            if inspect.isawaitable(body):
                body = await body
        if isinstance(body, httpx2.Response):
            return body
        if isinstance(body, str | bytes):
            return httpx2.Response(status, content=body, headers=hdrs)
        if body is None:
            return httpx2.Response(status, headers=hdrs)
        return httpx2.Response(status, json=body, headers=hdrs)

    def calls(self, endpoint: ep.Endpoint) -> list[httpx2.Request]:
        return [req for seen, req in self.requests if seen == endpoint]

    def json_body(self, endpoint: ep.Endpoint, index: int = -1) -> Any:
        return json.loads(self.calls(endpoint)[index].content)


class FakeClock:
    """Deterministic monotonic + wall clock for fast polling and retry tests."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.wall = 1_794_000_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall + (self.now - 1000.0)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(base, "_sleep", fake.sleep)
    monkeypatch.setattr(base, "_monotonic", fake.monotonic)
    monkeypatch.setattr(base, "_now", fake.time)
    monkeypatch.setattr(cache, "_now", fake.time)
    base.set_coverage_hook(lambda endpoint: SEEN.add(endpoint.key))
    return fake


@pytest.fixture(autouse=True)
def isolated_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if (
        request.node.get_closest_marker("live") is not None
        and os.environ.get("EVERESTEER_LIVE") == "1"
    ):
        return
    for name in list(os.environ):
        if name.startswith(("EVERESTEER_", "EIQ_", "EVEREST_", "CF_ACCESS_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "xdg-cache"))


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        journal_file=tmp_path / "config" / "journal.sqlite3",
        cache_dir=tmp_path / "cache",
        **overrides,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def authed_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, api_key=SecretStr("eiq_test_fixture_key_000001"))


@pytest.fixture
def api(settings: Settings) -> FakeApi:
    return FakeApi(settings)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if os.environ.get("EVERESTEER_TEST_COVERAGE") != "strict" or session.testsfailed:
        return
    unseen = sorted(endpoint.key for endpoint in ep.ALL_ENDPOINTS if endpoint.key not in SEEN)
    if unseen:
        print("\nEndpoints not exercised by any test:\n  " + "\n  ".join(unseen))
        session.exitstatus = 1
