"""Unit tests for endpoint path rendering."""

import pytest

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.errors import InvalidRequest


@pytest.mark.parametrize("value", ["", ".", ".."])
def test_render_refuses_segments_that_would_change_the_route(value: str) -> None:
    with pytest.raises(InvalidRequest, match="`key_id`"):
        ep.API_KEYS_REVOKE.render({"key_id": value})


def test_render_keeps_other_values_as_one_opaque_segment() -> None:
    assert ep.API_KEYS_REVOKE.render({"key_id": "k.1"}) == "/api/v1/agents/keys/k.1"
    assert ep.API_KEYS_REVOKE.render({"key_id": "..."}) == "/api/v1/agents/keys/..."
    assert ep.API_KEYS_REVOKE.render({"key_id": "../x"}) == "/api/v1/agents/keys/..%2Fx"
