"""Operation registry, Runtime lifecycle, and Call execution context."""

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypeVar

import httpx2
from pydantic import Field

from mcp_server_everesteer.capabilities import Capabilities, visible
from mcp_server_everesteer.client import base
from mcp_server_everesteer.client.base import EveresteerClient
from mcp_server_everesteer.client.cache import JsonCache
from mcp_server_everesteer.client.endpoints import Endpoint
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.journal import Journal
from mcp_server_everesteer.models import as_dict

Report = Callable[[float, str], Awaitable[None]]
Describe = Callable[..., Awaitable[str]]
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])
Gate = Literal["writes", "staking", "withdrawals"]

ModelId = Annotated[
    str,
    Field(min_length=1, description="Model UUID (preferred) or model name."),
]
UploadId = Annotated[
    str,
    Field(min_length=1, description="Diagnostics upload/run ID returned by a diagnostics upload."),
]
JobId = Annotated[
    str,
    Field(min_length=1, description="Compute training job ID returned by `train_model`."),
]
Tournament = Annotated[
    Literal["futures", "equities"],
    Field(description="Tournament: 'futures' (Himalayas, live) or 'equities' (Alps)."),
]
Split = Annotated[
    str,
    Field(
        min_length=1,
        description=(
            "Dataset split: 'train', 'validation', 'live', 'bulk', or 'train_benchmark_models'."
        ),
    ),
]


def _now_monotonic() -> float:
    return base.monotonic()


async def _no_report(_fraction: float, _message: str) -> None:
    return None


def _account_cache_dir(settings: Settings) -> str:
    creds = settings.credentials()
    if creds is None:
        return "anon"
    return hashlib.sha256(creds.api_key.encode()).hexdigest()[:12]


@dataclass
class Runtime:
    """Shared per-server or per-LangGraph-bridge runtime holding client, cache, and journal."""

    settings: Settings
    client: EveresteerClient
    cache: JsonCache
    caps: Capabilities
    _journal: Journal | None = None

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        caps: Capabilities | None = None,
    ) -> "Runtime":
        settings.register_secrets()
        resolved_caps = caps or Capabilities.detect(settings)
        cache_root = settings.resolved_cache_dir / _account_cache_dir(settings)
        return cls(
            settings=settings,
            client=EveresteerClient(settings, transport=transport),
            cache=JsonCache(cache_root, default_ttl=settings.cache_ttl),
            caps=resolved_caps,
        )

    @property
    def journal(self) -> Journal:
        if self._journal is None:
            self._journal = Journal(self.settings.resolved_journal_file)
        return self._journal

    async def aclose(self) -> None:
        await self.client.aclose()
        if self._journal is not None:
            self._journal.close()
            self._journal = None


@dataclass
class Call:
    """Invocation context passed to every operation."""

    runtime: Runtime
    report: Report = _no_report
    wait_cap: float | None = None
    started: float = field(default_factory=_now_monotonic)
    _last_fraction: float = 0.0

    @property
    def settings(self) -> Settings:
        return self.runtime.settings

    @property
    def client(self) -> EveresteerClient:
        return self.runtime.client

    @property
    def deadline(self) -> float | None:
        return None if self.wait_cap is None else self.started + self.wait_cap

    def limit_wait(self, seconds: float) -> float:
        """Clamp a blocking wait to the client's remaining call budget."""
        if self.wait_cap is None:
            return seconds
        return min(seconds, max(self.wait_cap - (base.monotonic() - self.started), 0.0))

    async def poll_until(
        self,
        fetch: Callable[[], Awaitable[Any]],
        *,
        wait_timeout: float,
        progress_fraction: float,
        progress_message: Callable[[dict[str, Any]], str],
        is_done: Callable[[dict[str, Any]], bool],
        on_error: Callable[[dict[str, Any]], Exception | None] | None = None,
        initial_interval: float = 2.0,
    ) -> Any:
        """Poll `fetch` with bounded exponential backoff until `is_done`, error, or wait cap."""
        deadline = base.monotonic() + self.limit_wait(wait_timeout)
        interval = initial_interval
        while base.monotonic() < deadline:
            raw = await fetch()
            payload = as_dict(raw)
            if is_done(payload):
                return raw
            if on_error is not None:
                err = on_error(payload)
                if err is not None:
                    raise err
            remaining = deadline - base.monotonic()
            if remaining <= 0:
                break
            await self.progress(progress_fraction, progress_message(payload))
            await base.sleep(min(interval, remaining))
            interval = min(interval * 1.5, self.settings.poll_max_interval)
        return None

    async def json(
        self,
        endpoint: Endpoint,
        *,
        path_args: dict[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        raw_body: bytes | None = None,
        files: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] = (),
    ) -> Any:
        return await self.client.json(
            endpoint,
            path_args=path_args,
            params=params,
            body=body,
            raw_body=raw_body,
            files=files,
            data=data,
            headers=headers,
            deadline=self.deadline,
            expect=expect,
        )

    async def request(
        self,
        endpoint: Endpoint,
        *,
        path_args: dict[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] = (),
    ) -> httpx2.Response:
        return await self.client.request(
            endpoint,
            path_args=path_args,
            params=params,
            body=body,
            headers=headers,
            deadline=self.deadline,
            expect=expect,
        )

    async def progress(self, fraction: float, message: str) -> None:
        """Report strictly monotonically increasing progress to MCP clients."""
        last = self._last_fraction
        value = fraction if last < fraction < 1.0 else last + (1.0 - last) * 0.01
        self._last_fraction = value
        await self.report(value, message)


@dataclass(frozen=True)
class Operation:
    """Metadata and callable for a single platform operation."""

    fn: Callable[..., Awaitable[Any]]
    name: str
    title: str
    toolset: str
    requires: tuple[str, ...]
    gate: Gate | None
    read_only: bool
    idempotent: bool
    destructive: bool
    describe: Describe | None
    mandatory_confirm: bool

    @property
    def description(self) -> str:
        return (self.fn.__doc__ or "").strip()

    @property
    def needs_confirmation(self) -> bool:
        return self.describe is not None


OPERATIONS: list[Operation] = []


def operation(
    title: str,
    *,
    toolset: str,
    requires: tuple[str, ...] = (),
    gate: Gate | None = None,
    read_only: bool = False,
    idempotent: bool = False,
    destructive: bool = False,
    describe: Describe | None = None,
    mandatory_confirm: bool = False,
) -> Callable[[F], F]:
    """Register an async function as both an MCP tool and a LangGraph tool."""

    def register(fn: F) -> F:
        if any(op.name == fn.__name__ for op in OPERATIONS):
            raise RuntimeError(f"Duplicate operation registered: {fn.__name__}")
        OPERATIONS.append(
            Operation(
                fn=fn,
                name=fn.__name__,
                title=title,
                toolset=toolset,
                requires=requires,
                gate=gate,
                read_only=read_only,
                idempotent=idempotent or read_only,
                destructive=destructive,
                describe=describe,
                mandatory_confirm=mandatory_confirm,
            )
        )
        return fn

    return register


def enabled_operations(caps: Capabilities) -> list[Operation]:
    return [op for op in OPERATIONS if visible(op.toolset, op.requires, op.gate, caps)[0]]


def hidden_operations(caps: Capabilities) -> list[tuple[Operation, str]]:
    hidden: list[tuple[Operation, str]] = []
    for op in OPERATIONS:
        shown, reason = visible(op.toolset, op.requires, op.gate, caps)
        if not shown:
            hidden.append((op, reason))
    return hidden


def confirmation_required(op: Operation, settings: Settings, kwargs: dict[str, Any]) -> bool:
    """Return whether `op` must ask the user before executing."""
    if not op.needs_confirmation:
        return False
    if kwargs.get("dry_run"):
        return False
    if op.mandatory_confirm or op.gate == "withdrawals":
        return True
    return settings.confirm != "none"
