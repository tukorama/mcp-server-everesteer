"""Model catalog, .pkl uploads, auto-submit configuration, and multipliers."""

from typing import Annotated, Any

from pydantic import Field

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.errors import ConflictError, EveresteerError, InvalidRequest, NotFound
from mcp_server_everesteer.models import ApiPayload, WriteResult, as_dict, as_list
from mcp_server_everesteer.ops.registry import Call, ModelId, operation
from mcp_server_everesteer.redaction import clean_label


@operation("List my models", toolset="models", requires=("api_key",), read_only=True)
async def list_models(call: Call) -> ApiPayload:
    """List your agent's registered models, `lane_active` status, and latest scores."""
    return ApiPayload.from_raw(await call.json(ep.MODELS_LIST))


@operation("Get model .pkl upload status", toolset="models", requires=("api_key",), read_only=True)
async def get_model_upload_status(call: Call, model_id: ModelId) -> ApiPayload:
    """Check validation status of an uploaded model `.pkl` (pending, validating, validated)."""
    return ApiPayload.from_raw(
        await call.json(ep.MODEL_UPLOAD_STATUS, path_args={"model_id": model_id})
    )


@operation("Get model multipliers", toolset="models", requires=("api_key",), read_only=True)
async def get_model_multipliers(call: Call, model_id: ModelId) -> ApiPayload:
    """Read current FIT and UNQ payout multipliers for a model."""
    return ApiPayload.from_raw(
        await call.json(ep.MODEL_MULTIPLIERS_GET, path_args={"model_id": model_id})
    )


@operation(
    "Create model slot",
    toolset="models",
    requires=("api_key",),
    gate="writes",
    idempotent=False,
)
async def create_model(
    call: Call,
    name: Annotated[
        str | None,
        Field(
            description=(
                "Optional display name (omit to let Everesteer assign an opaque name so your "
                "public leaderboard entry does not leak your strategy)."
            )
        ),
    ] = None,
    description: Annotated[
        str | None, Field(max_length=512, description="Model description.")
    ] = None,
    retrain_schedule: Annotated[
        str | None, Field(description="Optional retrain schedule tag.")
    ] = None,
) -> WriteResult:
    """Register a model slot under your agent (idempotent when `name` is provided)."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if retrain_schedule is not None:
        body["retrain_schedule"] = retrain_schedule
    try:
        res = await call.json(ep.MODELS_CREATE, body=body)
        return WriteResult.from_raw(res, status="ok", message="Model created.")
    except ConflictError:
        if name is None:
            raise
        listing = await call.json(ep.MODELS_LIST)
        models = as_list(as_dict(listing).get("models"))
        match: dict[str, Any] | None = None
        for item in models:
            candidate = as_dict(item)
            if candidate.get("name") == name:
                match = candidate
                break
        if match is not None:
            return WriteResult.from_raw(
                {**match, "status": "already_exists"},
                status="ok",
                message=f"Model '{name}' already exists; returning existing record.",
            )
        raise


@operation(
    "Update model metadata",
    toolset="models",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def update_model(
    call: Call,
    model_id: ModelId,
    description: Annotated[
        str | None, Field(max_length=512, description="Updated description (empty string clears).")
    ] = None,
    client_label: Annotated[
        str | None, Field(max_length=256, description="Private bookkeeping label (empty clears).")
    ] = None,
) -> WriteResult:
    """Update owner-scoped `description` and/or `client_label` on a model."""
    body: dict[str, Any] = {}
    if description is not None:
        body["description"] = description
    if client_label is not None:
        body["client_label"] = client_label
    res = await call.json(ep.MODEL_SETTINGS, path_args={"model_id": model_id}, body=body)
    return WriteResult.from_raw(res, status="ok", message="Model metadata updated.")


@operation(
    "Rename model",
    toolset="models",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def rename_model(
    call: Call,
    model_id: ModelId,
    name: Annotated[str, Field(min_length=1, description="New display name for the model.")],
) -> WriteResult:
    """Rename one of your models while keeping its UUID unchanged."""
    res = await call.json(ep.MODEL_RENAME, path_args={"model_id": model_id}, body={"name": name})
    return WriteResult.from_raw(res, status="ok", message=f"Model renamed to {name}.")


def _is_model_not_found(exc: EveresteerError) -> bool:
    low = str(exc).lower()
    return "no model" in low or "create_model" in low or "model not found" in low


@operation(
    "Upload model .pkl",
    toolset="models",
    requires=("api_key",),
    gate="writes",
)
async def upload_model(
    call: Call,
    model_id: ModelId,
    file_path: Annotated[
        str | None,
        Field(
            min_length=1,
            description=(
                "Local path to cloudpickled .pkl file (mutually exclusive with from_job_id)."
            ),
        ),
    ] = None,
    from_job_id: Annotated[
        str | None,
        Field(
            min_length=1,
            description=(
                "Completed hosted compute job_id to deploy server-side into this model slot."
            ),
        ),
    ] = None,
    python_version: Annotated[
        str | None,
        Field(
            pattern=r"^\d+\.\d+$",
            description="Python 'major.minor' that built the .pkl, e.g. '3.12'.",
        ),
    ] = None,
) -> WriteResult:
    """Upload a cloudpickled `.pkl` file or deploy directly from a hosted compute `from_job_id`."""
    if from_job_id is not None:
        if file_path is not None:
            raise InvalidRequest("Provide exactly one of `file_path` or `from_job_id`.")
        body: dict[str, Any] = {"job_id": from_job_id}
        if python_version is not None:
            body["python_version"] = python_version
        res = await call.json(
            ep.MODEL_UPLOAD_FROM_JOB,
            path_args={"model_id": model_id},
            body=body,
        )
        return WriteResult.from_raw(
            res,
            status="accepted",
            message=f"Deployed job {from_job_id} to model {model_id} for sandbox validation.",
            next_step=f"Poll `get_model_upload_status(model_id='{model_id}')` until validated.",
        )

    if file_path is None:
        raise InvalidRequest("Provide either `file_path` or `from_job_id`.")
    await call.progress(0.1, "Reading local .pkl artifact")
    data, fname, _ = await call.client.read_local_file(file_path)

    presign: Any = None
    try:
        presign = await call.json(
            ep.MODEL_UPLOAD_PRESIGN,
            path_args={"model_id": model_id},
            body={"filename": fname},
        )
    except (NotFound, EveresteerError) as exc:
        if _is_model_not_found(exc):
            raise
        if isinstance(exc, NotFound) or exc.status == 404:
            presign = None
        else:
            raise

    presign_d = as_dict(presign)
    if presign_d.get("available"):
        await call.progress(0.5, "Uploading .pkl directly to presigned storage")
        recipe = as_dict(presign_d["upload"])
        await call.client.s3_upload(recipe, fname, data, deadline=call.deadline)
        await call.progress(0.85, "Finalizing upload")
        complete_body: dict[str, Any] = {"upload_id": presign_d["upload_id"]}
        if python_version is not None:
            complete_body["python_version"] = python_version
        res = await call.json(
            ep.MODEL_UPLOAD_COMPLETE,
            path_args={"model_id": model_id},
            body=complete_body,
        )
    else:
        await call.progress(0.5, "Uploading .pkl via multipart endpoint")
        form_data = {"python_version": python_version} if python_version is not None else None
        res = await call.json(
            ep.MODEL_UPLOAD,
            path_args={"model_id": model_id},
            files={"file": (fname, data, "application/octet-stream")},
            data=form_data,
        )

    return WriteResult.from_raw(
        res,
        status="accepted",
        message="Model uploaded for sandbox validation.",
        next_step=f"Poll `get_model_upload_status(model_id='{model_id}')` until validated.",
    )


@operation(
    "Toggle model auto-submit",
    toolset="models",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def set_auto_submit(
    call: Call,
    model_id: ModelId,
    enabled: Annotated[
        bool, Field(description="Enable or disable automated per-round submission.")
    ],
) -> WriteResult:
    """Enable or disable automated per-round execution of a model's uploaded `.pkl`."""
    res = await call.json(
        ep.MODEL_SETTINGS,
        path_args={"model_id": model_id},
        body={"auto_submit": 1 if enabled else 0},
    )
    return WriteResult.from_raw(
        res,
        status="ok",
        message=f"Auto-submit {'enabled' if enabled else 'disabled'}.",
        next_step="Call `list_models` and verify `lane_active` is true.",
    )


@operation(
    "Set model multipliers",
    toolset="models",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def set_model_multipliers(
    call: Call,
    model_id: ModelId,
    fit_multiplier: Annotated[float, Field(description="FIT payout multiplier.")],
    unq_multiplier: Annotated[float, Field(description="UNQ payout multiplier.")],
) -> WriteResult:
    """Update FIT and UNQ payout multipliers for a model."""
    res = await call.json(
        ep.MODEL_MULTIPLIERS_SET,
        path_args={"model_id": model_id},
        body={"fit_multiplier": fit_multiplier, "unq_multiplier": unq_multiplier},
    )
    return WriteResult.from_raw(res, status="ok", message="Multipliers updated.")


async def _describe_archive(_call: Call, model_id: str) -> str:
    label = clean_label(model_id) or model_id
    return (
        f"Archive model '{label}'? It will become inactive and hidden from `list_models` "
        "(refused if a stake is active or inside the 20-day unstake lock)."
    )


@operation(
    "Archive model",
    toolset="models",
    requires=("api_key",),
    gate="withdrawals",
    destructive=True,
    describe=_describe_archive,
    mandatory_confirm=True,
)
async def archive_model(call: Call, model_id: ModelId) -> WriteResult:
    """Archive one of your models (keeps historical scores, frees active slot)."""
    res = await call.json(ep.MODEL_ARCHIVE, path_args={"model_id": model_id})
    return WriteResult.from_raw(res, status="ok", message=f"Model {model_id} archived.")
