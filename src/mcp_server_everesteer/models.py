"""Lean, forward-compatible Pydantic v2 domain models for mcp-server-everesteer."""

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, model_serializer

from mcp_server_everesteer.redaction import clean_text

_UNTRUSTED_TEXT_KEYS = frozenset(
    {
        "agent_name",
        "client_label",
        "description",
        "error",
        "error_message",
        "label",
        "lane_note",
        "message",
        "model_name",
        "name",
        "note",
        "title",
    }
)


def as_dict(value: Any) -> dict[str, Any]:
    """Safely narrow an arbitrary JSON value to a typed string-keyed dict."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """Safely narrow an arbitrary JSON value to a list."""
    return cast(list[Any], value) if isinstance(value, list) else []


def sanitize_payload(value: Any, *, _key: str | None = None) -> Any:
    """Clean untrusted remote strings (drop control/bidi chars) and normalize score aliases."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [sanitize_payload(item) for item in as_list(value)]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in as_dict(value).items():
            ks = str(k)
            out[ks] = sanitize_payload(v, _key=ks)
        if "bmc" in out and "unq" not in out:
            out["unq"] = out["bmc"]
        if "fnc" in out and "inov" not in out:
            out["inov"] = out["fnc"]
        return out
    return value


class CompactModel(BaseModel):
    """Base output model that omits null fields to preserve agent context."""

    model_config = ConfigDict(extra="allow")

    @model_serializer(mode="wrap")
    def _omit_none(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if not isinstance(data, dict):
            return data
        return {k: v for k, v in as_dict(data).items() if v is not None}


class ApiPayload(CompactModel):
    """Forward-compatible response envelope wrapping any JSON object or list."""

    @classmethod
    def from_raw(cls, raw: Any, **extra_fields: Any) -> "ApiPayload":
        cleaned = sanitize_payload(raw)
        if isinstance(cleaned, dict):
            merged: dict[str, Any] = {str(k): v for k, v in as_dict(cleaned).items()}
            merged.update(extra_fields)
            return cls.model_validate(merged)
        return cls.model_validate({"items": cleaned, **extra_fields})


WriteStatus = Literal[
    "ok", "accepted", "declined", "unknown", "preview", "pending", "partial", "rejected"
]


class WriteResult(CompactModel):
    """Outcome of a mutating, gated, or confirmed operation."""

    status: WriteStatus = "ok"
    message: str | None = None
    request_id: str | None = None
    next_step: str | None = None
    dry_run: bool | None = None
    data: Any = None

    @classmethod
    def from_raw(
        cls,
        raw: Any,
        *,
        status: WriteStatus = "ok",
        message: str | None = None,
        request_id: str | None = None,
        next_step: str | None = None,
        dry_run: bool | None = None,
    ) -> "WriteResult":
        cleaned = sanitize_payload(raw)
        return cls(
            status=status,
            message=message,
            request_id=request_id,
            next_step=next_step,
            dry_run=dry_run,
            data=cleaned,
        )


class IdentityStatus(CompactModel):
    """Connection, API key fingerprint, active gates, and toolsets."""

    authenticated: bool
    identity: str
    scope: str = "unauthenticated"
    base_url: str
    gates: dict[str, bool] = {}
    toolsets: list[str] = []
    notes: list[str] = []
    status_detail: dict[str, Any] | None = None


class DownloadResult(CompactModel):
    """Metadata for a file downloaded locally via presigned CDN or API stream."""

    path: str
    bytes_written: int
    universe: str | None = None
    split: str | None = None
    version: str | None = None
    artifact: str | None = None


class BatchResult(WriteResult):
    """Per-item outcomes for batch submissions or batch diagnostics uploads."""

    items: list[dict[str, Any]] = []
    failed: int = 0
    all_succeeded: bool = True


class UnconfirmedWriteEntry(CompactModel):
    """One recorded journal entry whose network outcome was ambiguous."""

    id: str
    tool: str
    endpoint: str
    summary: str
    usdc: str | None = None
    status: str
    created: float


class UnconfirmedWritesResult(CompactModel):
    """List of unknown-outcome writes recorded in the local SQLite journal."""

    entries: list[UnconfirmedWriteEntry] = []
    count: int = 0
