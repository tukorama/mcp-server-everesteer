"""Unit tests for HTTP retries, Cloudflare Access challenges, and error mapping."""

import httpx2
import pytest

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.client.base import EveresteerClient
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.errors import (
    AuthError,
    CloudflareAccessError,
    EmailVerificationRequired,
    EveresteerError,
    NotFound,
    NotSent,
    PermissionDenied,
    RateLimited,
    ServerError,
)
from tests.conftest import FakeApi, FakeClock

pytestmark = pytest.mark.anyio


async def test_get_retries_on_rate_limit_and_503_with_retry_after(
    authed_settings: Settings, api: FakeApi, clock: FakeClock
) -> None:
    api.add(
        ep.HEALTH,
        (429, {"detail": "slow down"}, {"Retry-After": "4"}),
        (503, {"detail": "restarting"}),
        {"status": "ok"},
    )
    client = EveresteerClient(authed_settings, transport=api.transport)
    try:
        res = await client.json(ep.HEALTH)
    finally:
        await client.aclose()

    assert res == {"status": "ok"}
    assert len(api.calls(ep.HEALTH)) == 3
    assert clock.sleeps[0] == 4.0


async def test_post_never_retries_on_server_error_or_read_timeout(
    authed_settings: Settings, api: FakeApi
) -> None:
    api.add(ep.MODELS_CREATE, (500, {"detail": "boom"}))
    client = EveresteerClient(authed_settings, transport=api.transport)
    try:
        with pytest.raises(ServerError):
            await client.json(ep.MODELS_CREATE, body={"name": "m1"})
    finally:
        await client.aclose()
    assert len(api.calls(ep.MODELS_CREATE)) == 1

    def timeout_request(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    api.add(ep.MODELS_CREATE, timeout_request)
    client2 = EveresteerClient(authed_settings, transport=api.transport)
    try:
        with pytest.raises(EveresteerError):
            await client2.json(ep.MODELS_CREATE, body={"name": "m2"})
    finally:
        await client2.aclose()
    assert len(api.calls(ep.MODELS_CREATE)) == 2


async def test_connect_error_raises_notsent(authed_settings: Settings, api: FakeApi) -> None:
    def refused(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    api.add(ep.MODELS_CREATE, refused)
    client = EveresteerClient(authed_settings, transport=api.transport)
    try:
        with pytest.raises(NotSent):
            await client.json(ep.MODELS_CREATE, body={})
    finally:
        await client.aclose()


async def test_cloudflare_html_and_email_verification_errors(
    authed_settings: Settings, api: FakeApi
) -> None:
    api.add(ep.GET_STARTED, (200, "<html><title>Cloudflare Access</title></html>"))
    api.add(ep.STATUS, (403, "<html><title>Cloudflare Access</title></html>"))
    api.add(ep.ROUNDS, (404, "<html><body>404 Not Found</body></html>"))
    api.add(ep.STAKING_STAKE, (403, {"code": "email_verification_required", "detail": "verify"}))
    api.add(ep.AGENT_PROFILE, (401, {"detail": "invalid key"}))
    api.add(ep.BENCHMARKS, (403, {"detail": "hackathon key"}))

    client = EveresteerClient(authed_settings, transport=api.transport)
    try:
        with pytest.raises(CloudflareAccessError):
            await client.json(ep.GET_STARTED)
        with pytest.raises(CloudflareAccessError):
            await client.json(ep.STATUS)
        with pytest.raises(NotFound):
            await client.json(ep.ROUNDS)
        with pytest.raises(EmailVerificationRequired):
            await client.json(ep.STAKING_STAKE, body={})
        with pytest.raises(AuthError):
            await client.json(ep.AGENT_PROFILE)
        with pytest.raises(PermissionDenied):
            await client.json(ep.BENCHMARKS)
    finally:
        await client.aclose()


async def test_rate_limit_exhaustion_raises_rate_limited(
    authed_settings: Settings, api: FakeApi
) -> None:
    api.add(ep.HEALTH, (429, {"detail": "busy"}))
    client = EveresteerClient(authed_settings, transport=api.transport)
    try:
        with pytest.raises(RateLimited):
            await client.json(ep.HEALTH)
    finally:
        await client.aclose()
