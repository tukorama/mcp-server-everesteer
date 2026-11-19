"""Configuration and restricted .env handling for mcp-server-everesteer."""

import json
import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from mcp_server_everesteer.errors import ConfigError
from mcp_server_everesteer.redaction import register_secret

APP_DIR = "mcp-server-everesteer"
DEFAULT_BASE_URL = "https://api.everesteer.ai"
_WRONG_HOSTS = {"everesteer.ai", "www.everesteer.ai", "app.everesteer.ai"}

# A workspace .env file must not be able to exfiltrate credentials, raise USDC limits,
# or enable mutating / money-moving gates behind the user's back.
_NOT_FROM_DOTENV = {
    "base_url",
    "api_key",
    "cf_access_client_id",
    "cf_access_client_secret",
    "basic_auth_user",
    "basic_auth_pass",
    "credentials_file",
    "journal_file",
    "cache_dir",
    "allow_writes",
    "allow_staking",
    "allow_withdrawals",
    "confirm",
    "max_stake_usdc",
    "max_withdraw_usdc",
    "max_daily_usdc",
    "http_bearer_token",
}

Limit = Decimal | Literal["unlimited"]


def config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR


def cache_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / APP_DIR


def write_private_json(path: Path, payload: Any) -> None:
    """Atomically write JSON readable only by the current user (0600 on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class EveresteerCredentials:
    """Stored or environment-supplied API credentials."""

    api_key: str
    cf_access_client_id: str | None = None
    cf_access_client_secret: str | None = None

    def register(self) -> None:
        register_secret(self.api_key)
        if self.cf_access_client_secret:
            register_secret(self.cf_access_client_secret)


class _RestrictedDotEnv(PydanticBaseSettingsSource):
    def __init__(
        self, settings_cls: type[BaseSettings], source: PydanticBaseSettingsSource
    ) -> None:
        super().__init__(settings_cls)
        self.source = source

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self.source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        return {key: val for key, val in self.source().items() if key not in _NOT_FROM_DOTENV}


def _parse_limit(value: Any) -> Limit:
    if isinstance(value, str) and value.strip().lower() == "unlimited":
        return "unlimited"
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError("expected a non-negative number, 0 to block, or 'unlimited'") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("expected a non-negative number, 0 to block, or 'unlimited'")
    return number


def _validate_api_url(value: str) -> str:
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    if host in _WRONG_HOSTS:
        raise ValueError(f"{host} does not serve the Everesteer REST API; use {DEFAULT_BASE_URL}")
    local = host in ("127.0.0.1", "localhost", "::1")
    if parts.scheme != "https" and not local:
        raise ValueError("base_url must use https, since API credentials are sent to it")
    return value.strip().rstrip("/")


class Settings(BaseSettings):
    """Server configuration loaded from EVERESTEER_* env vars (with EIQ_* fallbacks)."""

    model_config = SettingsConfigDict(
        env_prefix="EVERESTEER_",
        env_file=".env",
        extra="ignore",
        env_ignore_empty=True,
        validate_assignment=True,
    )

    api_key: SecretStr | None = None
    base_url: str = DEFAULT_BASE_URL

    cf_access_client_id: str | None = None
    cf_access_client_secret: SecretStr | None = None
    basic_auth_user: str | None = None
    basic_auth_pass: SecretStr | None = None

    credentials_file: Path | None = None
    journal_file: Path | None = None
    cache_dir: Path | None = None
    cache_ttl: int = Field(default=3600, ge=0)

    # Three default-closed safety gates.
    allow_writes: bool = False
    allow_staking: bool = False
    allow_withdrawals: bool = False
    confirm: Literal["elicit", "none"] = "elicit"

    max_stake_usdc: Limit = Decimal(100)
    max_withdraw_usdc: Limit = Decimal(100)
    max_daily_usdc: Limit = Decimal(1000)

    toolsets: str = "auto"

    http_timeout: float = Field(default=30.0, gt=0)
    upload_timeout: float = Field(default=300.0, gt=0)
    poll_max_interval: float = Field(default=15.0, ge=1)
    http_bearer_token: SecretStr | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            _RestrictedDotEnv(settings_cls, dotenv_settings),
            file_secret_settings,
        )

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("max_stake_usdc", "max_withdraw_usdc", "max_daily_usdc", mode="before")
    @classmethod
    def _parse_limit_field(cls, value: Any) -> Limit:
        return _parse_limit(value)

    @field_validator("base_url", mode="after")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _validate_api_url(value)

    @property
    def resolved_credentials_file(self) -> Path:
        return (self.credentials_file or config_dir() / "credentials.json").expanduser()

    @property
    def resolved_journal_file(self) -> Path:
        return (self.journal_file or config_dir() / "journal.sqlite3").expanduser()

    @property
    def resolved_cache_dir(self) -> Path:
        return (self.cache_dir or cache_dir()).expanduser()

    @property
    def resolved_base_url(self) -> str:
        if self.base_url != DEFAULT_BASE_URL:
            return self.base_url
        env_url = os.environ.get("EIQ_BASE_URL") or os.environ.get("EVEREST_API_URL")
        if env_url:
            return _validate_api_url(env_url)
        return self.base_url

    def credentials(self) -> EveresteerCredentials | None:
        """Return configured API + Cloudflare Access credentials if an API key is available."""
        key = (
            self.api_key.get_secret_value().strip()
            if self.api_key is not None
            else (os.environ.get("EIQ_API_KEY") or os.environ.get("EVEREST_API_KEY") or "").strip()
        )
        file_data = self._read_credentials_file()
        if not key and file_data is not None:
            key = str(file_data.get("api_key") or "").strip()

        cf_id = (
            self.cf_access_client_id
            or os.environ.get("CF_ACCESS_CLIENT_ID")
            or (
                str(file_data.get("cf_access_client_id"))
                if file_data and file_data.get("cf_access_client_id")
                else None
            )
        )
        cf_secret = (
            self.cf_access_client_secret.get_secret_value()
            if self.cf_access_client_secret is not None
            else os.environ.get("CF_ACCESS_CLIENT_SECRET")
            or (
                str(file_data.get("cf_access_client_secret"))
                if file_data and file_data.get("cf_access_client_secret")
                else None
            )
        )
        if not key:
            return None
        creds = EveresteerCredentials(
            api_key=key,
            cf_access_client_id=cf_id,
            cf_access_client_secret=cf_secret,
        )
        creds.register()
        return creds

    def require_credentials(self) -> EveresteerCredentials:
        creds = self.credentials()
        if creds is None:
            raise ConfigError(
                "No Everesteer API key is configured.",
                hint=(
                    "Run `uvx mcp-server-everesteer auth`, or set EVERESTEER_API_KEY "
                    f"(or EIQ_API_KEY), or store credentials at {self.resolved_credentials_file}."
                ),
            )
        return creds

    def basic_auth(self) -> tuple[str, str] | None:
        user = self.basic_auth_user or os.environ.get("EIQ_BASIC_AUTH_USER")
        secret = (
            self.basic_auth_pass.get_secret_value()
            if self.basic_auth_pass is not None
            else os.environ.get("EIQ_BASIC_AUTH_PASS")
        )
        if user and secret:
            register_secret(secret)
            return (user, secret)
        return None

    def register_secrets(self) -> None:
        if self.http_bearer_token is not None:
            register_secret(self.http_bearer_token.get_secret_value())
        if self.cf_access_client_secret is not None:
            register_secret(self.cf_access_client_secret.get_secret_value())
        if self.basic_auth_pass is not None:
            register_secret(self.basic_auth_pass.get_secret_value())
        _ = self.credentials()

    def _read_credentials_file(self) -> dict[str, Any] | None:
        path = self.resolved_credentials_file
        if not path.exists():
            if self.credentials_file is not None:
                raise ConfigError(f"EVERESTEER_CREDENTIALS_FILE points to a missing file: {path}")
            return None
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Could not read {path}: {type(exc).__name__}") from exc
        if isinstance(parsed, dict):
            mapping = cast(dict[Any, Any], parsed)
            if mapping.get("api_key"):
                return {str(k): v for k, v in mapping.items()}
        raise ConfigError(f'{path} must contain a JSON object with an "api_key" field.')
