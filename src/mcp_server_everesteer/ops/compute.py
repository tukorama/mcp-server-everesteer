"""Hosted compute training, cost previews, job polling, logs, and artifact downloads."""

from typing import Annotated, Any, Literal

from pydantic import Field

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.errors import ServerError
from mcp_server_everesteer.models import ApiPayload, DownloadResult, WriteResult, as_dict
from mcp_server_everesteer.ops.registry import Call, JobId, Tournament, operation
from mcp_server_everesteer.redaction import clean_label


def _build_train_body(
    *,
    model: str,
    features: str | list[str],
    universe: str,
    gpu: str,
    max_hours: float,
    target: str | list[str] | None,
    custom_model_fn: str | None,
    custom_feature_fn: str | None,
    params: dict[str, Any] | None,
    cv: dict[str, Any] | None,
    train_filter: dict[str, Any] | None,
    val_filter: dict[str, Any] | None,
    transforms: dict[str, Any] | None,
    dry_run: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "features": features,
        "universe": universe,
        "gpu": gpu,
        "max_hours": max_hours,
    }
    optional_fields: dict[str, Any] = {
        "target": target,
        "custom_model_fn": custom_model_fn,
        "custom_feature_fn": custom_feature_fn,
        "params": params,
        "cv": cv,
        "train_filter": train_filter,
        "val_filter": val_filter,
        "transforms": transforms,
    }
    body |= {key: val for key, val in optional_fields.items() if val is not None}
    if dry_run:
        body["dry_run"] = True
    return body


@operation("List hosted model templates", toolset="compute", requires=("api_key",), read_only=True)
async def list_model_templates(call: Call) -> ApiPayload:
    """List built-in hosted compute model templates and parameter schemas."""
    return ApiPayload.from_raw(await call.json(ep.COMPUTE_MODELS))


@operation("Get compute credit balance", toolset="compute", requires=("api_key",), read_only=True)
async def get_compute_credits(call: Call) -> ApiPayload:
    """Check compute credit balance, usage history, and per-tier GPU rate card."""
    return ApiPayload.from_raw(await call.json(ep.COMPUTE_CREDITS))


@operation("List compute training jobs", toolset="compute", requires=("api_key",), read_only=True)
async def list_compute_jobs(
    call: Call,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    status: Literal["pending", "running", "completed", "failed", "timeout", "cancelled"] | None = (
        None
    ),
) -> ApiPayload:
    """List your hosted compute training jobs, newest first."""
    params: dict[str, Any] = {"limit": limit}
    if status is not None:
        params["status"] = status
    return ApiPayload.from_raw(await call.json(ep.COMPUTE_JOBS, params=params))


@operation("Get compute job status", toolset="compute", requires=("api_key",), read_only=True)
async def get_job_status(call: Call, job_id: JobId) -> ApiPayload:
    """Read full status, estimated progress percentage, stage, and metrics of a compute job."""
    return ApiPayload.from_raw(await call.json(ep.COMPUTE_JOB, path_args={"job_id": job_id}))


@operation("Get compute job logs", toolset="compute", requires=("api_key",), read_only=True)
async def get_job_log(call: Call, job_id: JobId) -> ApiPayload:
    """Read lifecycle and per-fold event trail for debugging or monitoring a compute job."""
    return ApiPayload.from_raw(await call.json(ep.COMPUTE_JOB_LOGS, path_args={"job_id": job_id}))


@operation("Get compute artifact URLs", toolset="compute", requires=("api_key",), read_only=True)
async def get_job_artifact_urls(
    call: Call,
    job_id: JobId,
    kind: Literal["both", "model", "predictions"] = "both",
) -> ApiPayload:
    """Fetch presigned (~1 h) download URLs for a job's `.pkl` model and/or predictions."""
    out: dict[str, Any] = {"job_id": job_id}
    if kind in ("both", "model"):
        out["model"] = await call.json(ep.COMPUTE_JOB_MODEL_URL, path_args={"job_id": job_id})
    if kind in ("both", "predictions"):
        out["predictions"] = await call.json(
            ep.COMPUTE_JOB_PREDICTIONS_URL, path_args={"job_id": job_id}
        )
    return ApiPayload.from_raw(out)


@operation(
    "Download compute job artifact", toolset="compute", requires=("api_key",), read_only=True
)
async def download_job_artifact(
    call: Call,
    job_id: JobId,
    kind: Literal["model", "output_file"] = "model",
    filename: Annotated[
        str | None, Field(description="Required when kind='output_file' (planned endpoint).")
    ] = None,
    output_path: Annotated[
        str | None, Field(description="Local path to save the artifact.")
    ] = None,
) -> DownloadResult:
    """Download a completed job's trained `.pkl` (via credential-free S3) or named output file."""
    if kind == "model":
        info = await call.json(ep.COMPUTE_JOB_MODEL_URL, path_args={"job_id": job_id})
        url = str(info["download_url"])
        dest = output_path or f"{job_id}.pkl"
        saved_path, size = await call.client.download_url_to_file(
            url, dest, server_supplied=False, deadline=call.deadline
        )
        return DownloadResult(path=saved_path, bytes_written=size, artifact="model_pkl")

    if not filename:
        raise ValueError("`filename` is required when `kind='output_file'`.")
    dest_file = call.client.resolve_download_path(output_path, filename)
    saved_path, size = await call.client.download_presigned(
        ep.COMPUTE_JOB_OUTPUT,
        str(dest_file),
        path_args={"job_id": job_id, "filename": filename},
        deadline=call.deadline,
    )
    return DownloadResult(path=saved_path, bytes_written=size, artifact=filename)


@operation(
    "Price hosted training job (dry-run)",
    toolset="compute",
    requires=("api_key",),
    read_only=True,
)
async def price_training_job(
    call: Call,
    model: Literal["lightgbm", "xgboost", "ridge", "mlp", "random_forest", "custom"] = "lightgbm",
    features: str | list[str] = "small",
    target: str | list[str] | None = None,
    universe: Tournament = "futures",
    gpu: Literal["CPU", "T4", "A10G", "A100"] = "CPU",
    max_hours: Annotated[float, Field(ge=0.1, le=4.0)] = 1.0,
    custom_model_fn: str | None = None,
    custom_feature_fn: str | None = None,
    params: dict[str, Any] | None = None,
    cv: dict[str, Any] | None = None,
    train_filter: dict[str, Any] | None = None,
    val_filter: dict[str, Any] | None = None,
    transforms: dict[str, Any] | None = None,
) -> WriteResult:
    """Validate a training config and return its credit cost preview without reserving credits."""
    body = _build_train_body(
        model=model,
        features=features,
        universe=universe,
        gpu=gpu,
        max_hours=max_hours,
        target=target,
        custom_model_fn=custom_model_fn,
        custom_feature_fn=custom_feature_fn,
        params=params,
        cv=cv,
        train_filter=train_filter,
        val_filter=val_filter,
        transforms=transforms,
        dry_run=True,
    )
    res = await call.json(ep.COMPUTE_TRAIN, body=body)
    return WriteResult.from_raw(res, status="preview", dry_run=True, message="Cost preview only.")


async def _describe_train(
    _call: Call,
    model: str = "lightgbm",
    gpu: str = "CPU",
    max_hours: float = 1.0,
    universe: str = "futures",
    **_unused: Any,
) -> str:
    return (
        f"Launch hosted training job on Everesteer ({clean_label(model)}, gpu={clean_label(gpu)}, "
        f"universe={universe}, max_hours={max_hours})? This reserves compute credits."
    )


@operation(
    "Launch hosted training job",
    toolset="compute",
    requires=("api_key",),
    gate="writes",
    describe=_describe_train,
)
async def train_model(
    call: Call,
    model: Literal["lightgbm", "xgboost", "ridge", "mlp", "random_forest", "custom"] = "lightgbm",
    features: str | list[str] = "small",
    target: str | list[str] | None = None,
    universe: Tournament = "futures",
    gpu: Literal["CPU", "T4", "A10G", "A100"] = "CPU",
    max_hours: Annotated[float, Field(ge=0.1, le=4.0)] = 1.0,
    custom_model_fn: str | None = None,
    custom_feature_fn: str | None = None,
    params: dict[str, Any] | None = None,
    cv: dict[str, Any] | None = None,
    train_filter: dict[str, Any] | None = None,
    val_filter: dict[str, Any] | None = None,
    transforms: dict[str, Any] | None = None,
    dry_run: Annotated[
        bool, Field(description="If true, return cost preview without spending credits.")
    ] = False,
    wait: Annotated[
        bool, Field(description="Poll until completion (capped to client wait limit).")
    ] = False,
    wait_timeout: Annotated[float, Field(gt=1, le=3600)] = 1800.0,
) -> WriteResult:
    """Submit a hosted training job on Everesteer compute (returns job_id or polls if wait=True)."""
    body = _build_train_body(
        model=model,
        features=features,
        universe=universe,
        gpu=gpu,
        max_hours=max_hours,
        target=target,
        custom_model_fn=custom_model_fn,
        custom_feature_fn=custom_feature_fn,
        params=params,
        cv=cv,
        train_filter=train_filter,
        val_filter=val_filter,
        transforms=transforms,
        dry_run=dry_run,
    )
    accepted = await call.json(ep.COMPUTE_TRAIN, body=body)
    if dry_run:
        return WriteResult.from_raw(accepted, status="preview", dry_run=True)

    accepted_d = as_dict(accepted)
    job_id = str(accepted_d.get("job_id") or "")
    if not wait or not job_id:
        return WriteResult.from_raw(
            accepted,
            status="accepted",
            message=f"Compute job {job_id} queued.",
            next_step=(
                f"Poll `get_job_status(job_id='{job_id}')` and `get_job_log(job_id='{job_id}')`."
            ),
        )

    def _check_job_error(status_d: dict[str, Any]) -> Exception | None:
        state = status_d.get("status")
        if state in ("failed", "timeout", "cancelled"):
            msg = f"Compute job {job_id} ended with status '{state}'."
            return ServerError(msg, detail=status_d)
        return None

    completed = await call.poll_until(
        lambda: call.json(ep.COMPUTE_JOB, path_args={"job_id": job_id}),
        wait_timeout=wait_timeout,
        progress_fraction=0.5,
        progress_message=lambda status_d: (
            f"Job {job_id} status: {status_d.get('status') or 'running'}"
        ),
        is_done=lambda status_d: status_d.get("status") == "completed",
        on_error=_check_job_error,
    )
    if completed is not None:
        return WriteResult.from_raw(completed, status="ok", message=f"Job {job_id} completed.")

    return WriteResult.from_raw(
        accepted,
        status="pending",
        message=f"Compute job {job_id} is still running past the tool wait cap.",
        next_step=f"Resume polling with `get_job_status(job_id='{job_id}')`.",
    )


@operation(
    "Cancel compute training job",
    toolset="compute",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def cancel_compute_job(call: Call, job_id: JobId) -> WriteResult:
    """Stop a running compute training job to avoid spending credits up to `max_hours`."""
    res = await call.json(ep.COMPUTE_JOB_CANCEL, path_args={"job_id": job_id})
    return WriteResult.from_raw(res, status="ok", message=f"Compute job {job_id} cancelled.")
