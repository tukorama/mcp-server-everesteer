"""Core orientation, scoring, tournament schedule, and dataset operations."""

import hashlib
from typing import Annotated, Any

from pydantic import Field

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.client.cache import DAY, HOUR
from mcp_server_everesteer.errors import EveresteerError, NotFound
from mcp_server_everesteer.models import (
    ApiPayload,
    DownloadResult,
    IdentityStatus,
    as_dict,
    as_list,
)
from mcp_server_everesteer.ops.registry import Call, Split, Tournament, operation


async def _current_version(call: Call, universe: str) -> str:
    versions = call.runtime.cache.get("data:versions", HOUR)
    if versions is None:
        versions = await call.json(ep.DATA_VERSIONS)
        call.runtime.cache.set("data:versions", versions)
    if isinstance(versions, dict):
        items = as_list(as_dict(versions).get("versions"))
        for entry in items:
            entry_d = as_dict(entry)
            if universe in as_list(entry_d.get("universes")):
                ver = entry_d.get("version")
                if isinstance(ver, str) and ver:
                    return ver
        if items:
            ver = as_dict(items[0]).get("version")
            if isinstance(ver, str) and ver:
                return ver
    return "v0"


@operation("API health", toolset="core", read_only=True)
async def get_health(call: Call) -> ApiPayload:
    """Check whether the Everesteer API service is healthy."""
    return ApiPayload.from_raw(await call.json(ep.HEALTH))


@operation("Platform capability index", toolset="core", read_only=True)
async def get_capabilities(call: Call) -> ApiPayload:
    """Return the live Everesteer capability index (URLs, SDK metadata, and compute rate card)."""
    cached = call.runtime.cache.get("core:capabilities", HOUR)
    if cached is not None:
        return ApiPayload.from_raw(cached)
    data = await call.json(ep.CAPABILITIES)
    call.runtime.cache.set("core:capabilities", data)
    return ApiPayload.from_raw(data)


@operation("Scoring rules & payout formula", toolset="core", read_only=True)
async def explain_scoring(call: Call) -> ApiPayload:
    """Return the live payout formula, score_multiple_constant `b`, and FIT/UNQ/INOV rules.

    Read `weights.score_multiple_constant` here before self-scoring offline; never hardcode `b`.
    """
    cached = call.runtime.cache.get("core:scoring", HOUR)
    if cached is not None:
        return ApiPayload.from_raw(cached)
    data = await call.json(ep.SCORING)
    call.runtime.cache.set("core:scoring", data)
    return ApiPayload.from_raw(data)


@operation("Connection & key scope check", toolset="core", read_only=True)
async def whoami(call: Call) -> IdentityStatus:
    """Confirm API key authentication, SHA-256 fingerprint, scope, open gates, and toolsets."""
    creds = call.settings.credentials()
    caps = call.runtime.caps
    fingerprint = hashlib.sha256(creds.api_key.encode()).hexdigest()[:12] if creds else "none"
    scope = "unauthenticated"
    detail: dict[str, Any] | None = None
    notes = list(caps.notes)

    if creds is not None:
        try:
            started = await call.json(ep.GET_STARTED)
            if isinstance(started, dict):
                detail = {str(k): v for k, v in as_dict(started).items()}
                scope = "hackathon" if detail.get("mode") == "diagnostics_hackathon" else "full"
        except EveresteerError as exc:
            notes.append(exc.for_agent())

    return IdentityStatus(
        authenticated=creds is not None,
        identity=fingerprint,
        scope=scope,
        base_url=call.settings.resolved_base_url,
        gates={
            "writes": call.settings.allow_writes,
            "staking": call.settings.allow_staking,
            "withdrawals": call.settings.allow_withdrawals,
        },
        toolsets=sorted(caps.toolsets),
        notes=notes,
        status_detail=detail,
    )


@operation("Mode-aware orientation", toolset="core", requires=("api_key",), read_only=True)
async def get_started(call: Call) -> ApiPayload:
    """Return mode-aware orientation (hackathon vs full tournament) and recommended next actions."""
    return ApiPayload.from_raw(await call.json(ep.GET_STARTED))


@operation("Situational status", toolset="core", requires=("api_key",), read_only=True)
async def get_status(call: Call) -> ApiPayload:
    """Return your whole situational picture: models, submissions/scores, round clock, stake."""
    return ApiPayload.from_raw(await call.json(ep.STATUS))


@operation("Tournament rounds", toolset="core", requires=("api_key",), read_only=True)
async def get_rounds(
    call: Call,
    tournament: Tournament = "futures",
    current_only: Annotated[
        bool, Field(description="If true, fetch only the currently active round.")
    ] = False,
    limit: Annotated[int, Field(ge=1, le=200, description="Max rounds when listing.")] = 25,
) -> ApiPayload:
    """List tournament rounds, or inspect only the currently active round (`current_only=true`)."""
    if current_only:
        return ApiPayload.from_raw(
            await call.json(ep.CURRENT_ROUND, params={"tournament": tournament})
        )
    return ApiPayload.from_raw(
        await call.json(ep.ROUNDS, params={"tournament": tournament, "limit": limit})
    )


@operation("Round schedule", toolset="core", requires=("api_key",), read_only=True)
async def get_schedule(call: Call) -> ApiPayload:
    """Get upcoming round open/close schedule across tournaments."""
    return ApiPayload.from_raw(await call.json(ep.SCHEDULE))


@operation("Tournament seasons", toolset="core", requires=("api_key",), read_only=True)
async def get_seasons(
    call: Call,
    current_only: Annotated[
        bool, Field(description="If true, return only the active season with rankings.")
    ] = False,
) -> ApiPayload:
    """List tournament seasons, or inspect only the active season (`current_only=true`)."""
    endpoint = ep.CURRENT_SEASON if current_only else ep.SEASONS
    return ApiPayload.from_raw(await call.json(endpoint))


@operation("Official benchmarks", toolset="core", requires=("api_key",), read_only=True)
async def get_benchmarks(call: Call) -> ApiPayload:
    """List official benchmark models and their performance stats."""
    return ApiPayload.from_raw(await call.json(ep.BENCHMARKS))


@operation("List dataset versions", toolset="core", requires=("api_key",), read_only=True)
async def list_dataset_versions(call: Call) -> ApiPayload:
    """List available dataset versions and which universes they cover."""
    data = await call.json(ep.DATA_VERSIONS)
    call.runtime.cache.set("data:versions", data)
    return ApiPayload.from_raw(data)


@operation("Dataset schema", toolset="core", requires=("api_key",), read_only=True)
async def get_dataset_schema(
    call: Call,
    version: Annotated[
        str, Field(min_length=1, description="Dataset version or 'latest'.")
    ] = "latest",
    verbose: Annotated[
        bool,
        Field(description="Include full per-feature listing (large). Default is compact summary."),
    ] = False,
) -> ApiPayload:
    """Get dataset feature sets, feature encoding, primary_target, targets, and CV embargo."""
    resolved = (
        await _current_version(call, "futures") if version in ("latest", "current") else version
    )
    cache_key = f"data:schema:{resolved}:{verbose}"
    cached = call.runtime.cache.get(cache_key, DAY)
    if cached is not None:
        return ApiPayload.from_raw(cached)
    params = {"verbose": True} if verbose else None
    data = await call.json(ep.DATA_SCHEMA, path_args={"version": resolved}, params=params)
    call.runtime.cache.set(cache_key, data)
    return ApiPayload.from_raw(data)


@operation("Dataset universe metadata", toolset="core", requires=("api_key",), read_only=True)
async def get_dataset_info(call: Call, universe: Tournament = "futures") -> ApiPayload:
    """Get metadata and split summary for a dataset universe."""
    return ApiPayload.from_raw(await call.json(ep.DATA_INFO, path_args={"universe": universe}))


@operation("Futures universe", toolset="core", requires=("api_key",), read_only=True)
async def get_futures_universe(call: Call) -> ApiPayload:
    """Get the encoded futures (Himalayas) instrument universe."""
    return ApiPayload.from_raw(await call.json(ep.FUTURES_UNIVERSE))


@operation("Futures targets", toolset="core", requires=("api_key",), read_only=True)
async def get_futures_targets(call: Call) -> ApiPayload:
    """List available futures prediction target names."""
    return ApiPayload.from_raw(await call.json(ep.FUTURES_TARGETS))


@operation("Download dataset split", toolset="core", requires=("api_key",), read_only=True)
async def download_dataset(
    call: Call,
    universe: Tournament = "futures",
    split: Split = "train",
    output_path: Annotated[
        str | None,
        Field(description="Local file path to save. Defaults to '{universe}_{split}.parquet'."),
    ] = None,
    version: Annotated[str, Field(description="Dataset version or 'latest'.")] = "latest",
) -> DownloadResult:
    """Download a dataset split parquet (or `split='bulk'` zip bundle) via presigned CDN."""
    if split == "bulk":
        resolved = (
            await _current_version(call, universe) if version in ("latest", "current") else version
        )
        dest = output_path or f"{universe}_bulk.zip"
        try:
            saved_path, size = await call.client.download_presigned(
                ep.DATA_DOWNLOAD_BULK,
                dest,
                path_args={"version": resolved, "universe_id": universe},
                deadline=call.deadline,
            )
        except NotFound:
            current = await _current_version(call, universe)
            if current == resolved:
                raise
            resolved = current
            saved_path, size = await call.client.download_presigned(
                ep.DATA_DOWNLOAD_BULK,
                dest,
                path_args={"version": resolved, "universe_id": universe},
                deadline=call.deadline,
            )
        return DownloadResult(
            path=saved_path,
            bytes_written=size,
            universe=universe,
            split="bulk",
            version=resolved,
            artifact="bulk_zip",
        )

    dest = output_path or f"{universe}_{split}.parquet"
    if universe == "futures":
        saved_path, size = await call.client.download_presigned(
            ep.FUTURES_DATA, dest, path_args={"split": split}, deadline=call.deadline
        )
        return DownloadResult(path=saved_path, bytes_written=size, universe=universe, split=split)

    resolved = (
        await _current_version(call, universe) if version in ("latest", "current") else version
    )
    try:
        saved_path, size = await call.client.download_presigned(
            ep.DATA_DOWNLOAD,
            dest,
            path_args={"version": resolved, "universe": universe, "split": split},
            deadline=call.deadline,
        )
    except NotFound:
        current = await _current_version(call, universe)
        if current == resolved:
            raise
        resolved = current
        saved_path, size = await call.client.download_presigned(
            ep.DATA_DOWNLOAD,
            dest,
            path_args={"version": resolved, "universe": universe, "split": split},
            deadline=call.deadline,
        )
    return DownloadResult(
        path=saved_path, bytes_written=size, universe=universe, split=split, version=resolved
    )


@operation("Download benchmark predictions", toolset="core", requires=("api_key",), read_only=True)
async def download_benchmark(
    call: Call,
    universe: Tournament = "futures",
    split: Annotated[
        str, Field(description="Split: 'train', 'validation', or 'live'.")
    ] = "validation",
    output_path: Annotated[str | None, Field(description="Local file path to save.")] = None,
    version: Annotated[str, Field(description="Dataset version or 'latest'.")] = "latest",
) -> DownloadResult:
    """Download benchmark predictions parquet (hackathon keys can only download split='train')."""
    dest = output_path or f"benchmark_{universe}_{split}.parquet"
    if universe == "futures":
        bench_split = f"{split}_benchmark_models"
        saved_path, size = await call.client.download_presigned(
            ep.FUTURES_DATA, dest, path_args={"split": bench_split}, deadline=call.deadline
        )
        return DownloadResult(
            path=saved_path,
            bytes_written=size,
            universe=universe,
            split=split,
            artifact="benchmark",
        )
    resolved = (
        await _current_version(call, universe) if version in ("latest", "current") else version
    )
    saved_path, size = await call.client.download_presigned(
        ep.DATA_BENCHMARK,
        dest,
        path_args={"version": resolved, "universe": universe, "split": split},
        deadline=call.deadline,
    )
    return DownloadResult(
        path=saved_path,
        bytes_written=size,
        universe=universe,
        split=split,
        version=resolved,
        artifact="benchmark",
    )


@operation("Download AI consensus model", toolset="core", requires=("api_key",), read_only=True)
async def download_ai_model(
    call: Call,
    universe: Tournament = "futures",
    version: Annotated[
        str, Field(description="Dataset version (default 'v0' or 'latest').")
    ] = "v0",
    output_path: Annotated[str | None, Field(description="Local file path to save.")] = None,
) -> DownloadResult:
    """Download stake-weighted AI consensus model predictions parquet."""
    resolved = (
        await _current_version(call, universe) if version in ("latest", "current") else version
    )
    dest = output_path or f"ai_model_{universe}.parquet"
    saved_path, size = await call.client.download_presigned(
        ep.DATA_AI_MODEL,
        dest,
        path_args={"version": resolved, "universe": universe},
        deadline=call.deadline,
    )
    return DownloadResult(
        path=saved_path,
        bytes_written=size,
        universe=universe,
        version=resolved,
        artifact="ai_model",
    )
