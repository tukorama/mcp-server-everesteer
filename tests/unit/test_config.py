"""Unit tests for configuration, restricted .env loading, and credentials files."""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_server_everesteer.config import Settings, write_private_json
from mcp_server_everesteer.errors import ConfigError


def test_dotenv_cannot_override_sensitive_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "EVERESTEER_API_KEY=eiq_test_fixture_from_dotenv_999",
                "EVERESTEER_BASE_URL=https://evil.example.test",
                "EVERESTEER_ALLOW_WRITES=true",
                "EVERESTEER_ALLOW_STAKING=true",
                "EVERESTEER_ALLOW_WITHDRAWALS=true",
                "EVERESTEER_MAX_STAKE_USDC=unlimited",
                "EVERESTEER_MAX_WITHDRAW_USDC=999999",
                "EVERESTEER_LOG_LEVEL=debug",
            ]
        ),
        encoding="utf-8",
    )
    s = Settings(
        credentials_file=tmp_path / "missing-creds.json",
    )
    assert s.api_key is None
    assert s.base_url == "https://api.everesteer.ai"
    assert s.allow_writes is False
    assert s.allow_staking is False
    assert s.allow_withdrawals is False
    assert str(s.max_stake_usdc) == "100"
    assert str(s.max_withdraw_usdc) == "100"
    # Harmless logging setting still comes through from .env
    assert s.log_level == "DEBUG"


def test_dotenv_cannot_widen_the_download_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("EVERESTEER_DOWNLOAD_DIR=/\n", encoding="utf-8")
    assert Settings().download_roots == (tmp_path.resolve(),)


def test_download_roots_split_the_environment_variable_on_pathsep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("EVERESTEER_DOWNLOAD_DIR", os.pathsep.join([str(first), " ", str(second)]))
    roots = Settings(_env_file=None).download_roots  # type: ignore[call-arg]
    assert roots == (first.resolve(), second.resolve())


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://everesteer.ai",
        "https://www.everesteer.ai",
        "https://app.everesteer.ai",
        "http://api.everesteer.ai",
    ],
)
def test_rejects_wrong_or_insecure_base_urls(bad_url: str, tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            base_url=bad_url,
            credentials_file=tmp_path / "creds.json",
        )


def test_accepts_loopback_http_for_local_testing(tmp_path: Path) -> None:
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        base_url="http://127.0.0.1:8080/",
        credentials_file=tmp_path / "creds.json",
    )
    assert s.resolved_base_url == "http://127.0.0.1:8080"


def test_eiq_env_fallback_and_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds_path = tmp_path / "credentials.json"
    write_private_json(creds_path, {"api_key": "eiq_test_fixture_file_key_001"})
    if os.name != "nt":
        assert (creds_path.stat().st_mode & 0o777) == 0o600

    s_file = Settings(_env_file=None, credentials_file=creds_path)  # type: ignore[call-arg]
    creds = s_file.require_credentials()
    assert creds.api_key == "eiq_test_fixture_file_key_001"

    monkeypatch.setenv("EIQ_API_KEY", "eiq_test_fixture_env_override_02")
    s_env = Settings(_env_file=None, credentials_file=creds_path)  # type: ignore[call-arg]
    assert s_env.require_credentials().api_key == "eiq_test_fixture_env_override_02"


def test_missing_or_corrupt_explicit_credentials_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    s = Settings(_env_file=None, credentials_file=missing)  # type: ignore[call-arg]
    with pytest.raises(ConfigError):
        s.credentials()

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not_key": 1}), encoding="utf-8")
    s_bad = Settings(_env_file=None, credentials_file=bad)  # type: ignore[call-arg]
    with pytest.raises(ConfigError):
        s_bad.credentials()
