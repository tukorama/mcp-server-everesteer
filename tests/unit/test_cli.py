"""Unit tests for the CLI commands (`tools`, `status`, `auth`, `logout`, `diagram`)."""

import json
from pathlib import Path

import pytest

from mcp_server_everesteer import cli
from mcp_server_everesteer.config import Settings, write_private_json


def test_cli_tools_and_diagrams(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EVERESTEER_API_KEY", "eiq_test_fixture_key_000001")
    assert cli.main(["tools"]) == 0
    out = capsys.readouterr().out
    assert "45 tools exposed." in out
    assert "EVERESTEER_ALLOW_WRITES=true" in out

    for kind in ("architecture", "lifecycle", "safety", "tools"):
        assert cli.main(["diagram", "--kind", kind, "--fence"]) == 0
        diagram_out = capsys.readouterr().out
        assert diagram_out.startswith("```mermaid\n")
        assert diagram_out.strip().endswith("```")


def test_cli_auth_no_verify_and_logout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds = tmp_path / "credentials.json"
    monkeypatch.setenv("EVERESTEER_CREDENTIALS_FILE", str(creds))

    def fake_getpass(_prompt: str = "") -> str:
        return "eiq_test_fixture_cli_key_01"

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    assert cli.main(["auth", "--no-verify"]) == 0
    assert creds.exists()
    assert json.loads(creds.read_text(encoding="utf-8"))["api_key"] == "eiq_test_fixture_cli_key_01"

    assert cli.main(["logout"]) == 0
    assert not creds.exists()
    assert "Removed stored credentials" in capsys.readouterr().out


def test_cli_status_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["authenticated"] is False
    assert data["identity"] == "none"


def test_cli_invalid_env_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EVERESTEER_BASE_URL", "https://app.everesteer.ai")
    assert cli.main(["tools"]) == 2
    assert "Invalid EVERESTEER_BASE_URL" in capsys.readouterr().err


def test_write_private_json_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "credentials.json"
    write_private_json(target, {"api_key": "eiq_test_fixture_key_000001"})
    assert Settings(_env_file=None, credentials_file=target).credentials() is not None  # type: ignore[call-arg]
