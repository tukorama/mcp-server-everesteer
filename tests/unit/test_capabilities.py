"""Unit tests for toolset parsing, 3 safety gates, and capability visibility."""

import pytest
from pydantic import SecretStr

from mcp_server_everesteer.capabilities import Capabilities, parse_toolsets, visible
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.errors import ConfigError


def test_parse_toolsets_modes_and_deltas() -> None:
    assert parse_toolsets("auto") == {"core", "models", "compete", "compute", "account"}
    assert parse_toolsets("auto,+staking,+equities") == {
        "core",
        "models",
        "compete",
        "compute",
        "account",
        "staking",
        "equities",
    }
    assert parse_toolsets("+staking,-account") == {
        "core",
        "models",
        "compete",
        "compute",
        "staking",
    }
    assert parse_toolsets("auto", include_staking=True) == {
        "core",
        "models",
        "compete",
        "compute",
        "account",
        "staking",
    }
    assert parse_toolsets("auto,-staking", include_staking=True) == {
        "core",
        "models",
        "compete",
        "compute",
        "account",
    }
    with pytest.raises(ConfigError):
        parse_toolsets("unknown_set")


def test_capability_detection_and_visibility(settings: Settings) -> None:
    caps_no_key = Capabilities.detect(settings)
    assert caps_no_key.have == frozenset()
    assert caps_no_key.gates == frozenset()
    shown, reason = visible("models", ("api_key",), None, caps_no_key)
    assert not shown
    assert "EVERESTEER_API_KEY" in reason

    settings.api_key = SecretStr("eiq_test_fixture_key_000001")
    settings.allow_writes = True
    settings.allow_staking = True
    caps = Capabilities.detect(settings)
    assert "api_key" in caps.have
    assert "staking" in caps.toolsets
    assert visible("compete", ("api_key",), "writes", caps) == (True, "")
    assert visible("staking", ("api_key",), "withdrawals", caps)[0] is False
