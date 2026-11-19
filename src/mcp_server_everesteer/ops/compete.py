"""Submissions, validation diagnostics, event rounds, leaderboards, and scores."""

import csv
import importlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.errors import EveresteerError, InvalidRequest, NotFound, ServerError
from mcp_server_everesteer.models import ApiPayload, BatchResult, WriteResult, as_dict, as_list
from mcp_server_everesteer.ops.registry import Call, ModelId, Tournament, UploadId, operation
from mcp_server_everesteer.redaction import clean_label
from mcp_server_everesteer.safety import check_prediction_range


def _is_model_not_found(exc: EveresteerError) -> bool:
    low = str(exc).lower()
    return "no model" in low or "create_model" in low or "model not found" in low


def _rows_to_predictions(rows: list[dict[str, Any]], source: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        iid = row.get("instrument_id") if "instrument_id" in row else row.get("id")
        pred = row.get("prediction") if "prediction" in row else row.get("score")
        if iid is None or pred is None:
            raise InvalidRequest(
                f"Rows in {source} must contain `id`/`instrument_id` and `prediction` columns."
            )
        out[str(iid)] = float(pred)
    if not out:
        raise InvalidRequest(f"No predictions found in {source}.")
    return out


def _read_predictions_file(path_str: str) -> dict[str, float]:
    path = Path(path_str)
    if not path.is_file():
        raise InvalidRequest(f"Predictions file not found: {path_str}")
    suffix = path.suffix.lower()
    if suffix == ".json":
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            loaded_d = cast(dict[Any, Any], loaded)
            return {str(k): float(v) for k, v in loaded_d.items()}
        if isinstance(loaded, list):
            items = cast(list[Any], loaded)
            return _rows_to_predictions([as_dict(item) for item in items], path_str)
        raise InvalidRequest(f"JSON predictions in {path_str} must be an object or list of rows.")
    if suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
        return _rows_to_predictions(rows, path_str)
    if suffix in (".parquet", ".pq"):
        try:
            pq = importlib.import_module("pyarrow.parquet")
        except ImportError as exc:
            raise InvalidRequest(
                "Reading .parquet predictions requires `pyarrow` in this Python environment."
            ) from exc
        rows = cast(list[dict[str, Any]], cast(Any, pq).read_table(path).to_pylist())
        return _rows_to_predictions(rows, path_str)
    raise InvalidRequest(
        f"Unsupported predictions file extension '{path.suffix}'. Use .json, .csv, or .parquet."
    )


def _load_predictions_source(
    predictions: dict[str, float] | None, predictions_path: str | None
) -> dict[str, float]:
    if (predictions is None) == (predictions_path is None):
        raise InvalidRequest("Provide exactly one of `predictions` or `predictions_path`.")
    if predictions_path is not None:
        return _read_predictions_file(predictions_path)
    return cast(dict[str, float], predictions)


# -- Read Operations ---------------------------------------------------------


@operation(
    "Pre-flight futures submission", toolset="compete", requires=("api_key",), read_only=True
)
async def validate_futures_submission(
    call: Call,
    predictions: Annotated[
        dict[str, float] | None,
        Field(description="Mapping of instrument_id -> prediction in [0, 1]."),
    ] = None,
    predictions_path: Annotated[
        str | None,
        Field(description="Local .parquet, .csv, or .json predictions file path."),
    ] = None,
    exped: Annotated[
        str | None, Field(description="Target exped; omit to validate against the open round.")
    ] = None,
) -> ApiPayload:
    """Validate coverage, finiteness, and [0, 1] bounds server-side without writing anything."""
    resolved = _load_predictions_source(predictions, predictions_path)
    body: dict[str, Any] = {"predictions": resolved}
    if exped is not None:
        body["exped"] = exped
    return ApiPayload.from_raw(await call.json(ep.FUTURES_VALIDATE, body=body))


@operation("Check submission status", toolset="compete", requires=("api_key",), read_only=True)
async def get_submission_status(
    call: Call,
    tournament: Tournament = "futures",
    round: Annotated[str | None, Field(description="Optional exped/round identifier.")] = None,
    model_id: Annotated[
        str | None, Field(description="Optional model filter (owner-scoped).")
    ] = None,
) -> ApiPayload:
    """Check whether your submissions landed (coverage_pct, submitted_at, n_predictions)."""
    params: dict[str, Any] = {"tournament": tournament}
    if round is not None:
        params["round"] = round
    if model_id is not None:
        params["model_id"] = model_id
    return ApiPayload.from_raw(await call.json(ep.SUBMISSIONS, params=params))


@operation("Get model scores", toolset="compete", requires=("api_key",), read_only=True)
async def get_scores(
    call: Call,
    model_id: ModelId,
    days: Annotated[int, Field(ge=1, le=3650, description="Look-back window in days.")] = 30,
    response_format: Literal["concise", "detailed"] = "concise",
) -> ApiPayload:
    """Get FIT, UNQ, INOV, and payout scores for a model (`detailed` adds interpretation)."""
    return ApiPayload.from_raw(
        await call.json(
            ep.SCORES,
            params={"model_id": model_id, "days": days, "response_format": response_format},
        )
    )


@operation("Get tournament leaderboard", toolset="compete", requires=("api_key",), read_only=True)
async def get_leaderboard(
    call: Call,
    board: Literal["live", "historical", "futures", "legacy"] = "live",
    view: Literal["agents", "models"] = "agents",
    round: Annotated[int | None, Field(description="Live round number (live board only).")] = None,
    sort: Annotated[str | None, Field(description="Sort key (live board only).")] = None,
    dir: Literal["asc", "desc"] | None = None,
    metrics: Literal["scored", "diagnostics"] | None = None,
    q: Annotated[str | None, Field(description="Visible-name substring filter.")] = None,
    agent: Annotated[str | None, Field(description="Exact agent ID filter.")] = None,
    period: Annotated[
        str | None, Field(description="Period for legacy/futures boards (e.g. '30d').")
    ] = None,
) -> ApiPayload:
    """Read live, historical validation, or futures leaderboards without recomputing ranks."""
    if board in ("futures", "legacy"):
        endpoint = ep.FUTURES_LEADERBOARD if board == "futures" else ep.LEADERBOARD_LEGACY
        return ApiPayload.from_raw(await call.json(endpoint, params={"period": period or "30d"}))

    if board == "historical" and any(v is not None for v in (round, sort, dir)):
        raise InvalidRequest("`round`, `sort`, and `dir` apply only when `board='live'`.")

    started = as_dict(await call.json(ep.GET_STARTED))
    if started.get("mode") == "diagnostics_hackathon":
        return ApiPayload.from_raw(
            await call.json(ep.LEADERBOARD_LEGACY, params={"period": period or "30d"})
        )

    params: dict[str, Any] = {
        k: v for k, v in {"metrics": metrics, "q": q, "agent": agent}.items() if v is not None
    }
    if board == "historical":
        params["view"] = view
        return ApiPayload.from_raw(await call.json(ep.BOARD_HISTORICAL, params=params))

    params.update(
        {k: v for k, v in {"round": round, "sort": sort, "dir": dir}.items() if v is not None}
    )
    endpoint = ep.BOARD_LIVE_AGENTS if view == "agents" else ep.BOARD_LIVE_MODELS
    return ApiPayload.from_raw(await call.json(endpoint, params=params))


@operation(
    "Get validation diagnostics panel", toolset="compete", requires=("api_key",), read_only=True
)
async def get_validation_panel(
    call: Call,
    model_id: ModelId,
    days: Annotated[int, Field(ge=1, le=3650, description="Look-back days.")] = 365,
    source: Literal["auto", "scored"] = "auto",
    per_exped_only: Annotated[
        bool,
        Field(description="If true, force source='scored' and return the per_exped_fit series."),
    ] = False,
) -> ApiPayload:
    """Read full validation diagnostics panel or per-exped FIT series (`per_exped_only=true`)."""
    effective_source = "scored" if per_exped_only else source
    data = await call.json(
        ep.MODEL_VALIDATION_PANEL,
        path_args={"model_id": model_id},
        params={"days": days, "source": effective_source},
    )
    if per_exped_only and isinstance(data, dict):
        data_d = as_dict(data)
        raw_series = data_d.get("per_exped_fit")
        series = list(as_list(raw_series)) if raw_series is not None else None
        return ApiPayload.from_raw(
            {
                "model_id": model_id,
                "per_exped_fit": series,
                "withheld": series is None,
            }
        )
    return ApiPayload.from_raw(data)


@operation("Get scoring diagnostics", toolset="compete", requires=("api_key",), read_only=True)
async def get_diagnostics(
    call: Call,
    target_id: Annotated[
        str, Field(min_length=1, description="Model ID (or agent ID for futures scope).")
    ],
    scope: Literal["model", "rounds", "futures_agent"] = "model",
    date: Annotated[str | None, Field(description="Optional date (model scope).")] = None,
    round_id: Annotated[
        int | None, Field(description="Optional round id (futures_agent scope).")
    ] = None,
    response_format: Literal["concise", "detailed"] = "concise",
) -> ApiPayload:
    """Get model diagnostics (`model`), per-round breakdown (`rounds`), or futures agent view."""
    if scope == "rounds":
        return ApiPayload.from_raw(
            await call.json(
                ep.MODEL_DIAGNOSTICS_ROUNDS,
                path_args={"model_id": target_id},
                params={"response_format": response_format},
            )
        )
    if scope == "futures_agent":
        params = {"round_id": round_id} if round_id is not None else None
        return ApiPayload.from_raw(
            await call.json(
                ep.FUTURES_DIAGNOSTICS, path_args={"agent_id": target_id}, params=params
            )
        )
    params_m: dict[str, Any] = {"model_id": target_id}
    if date is not None:
        params_m["date"] = date
    return ApiPayload.from_raw(
        await call.json(ep.MODEL_DIAGNOSTICS, path_args={"model_id": target_id}, params=params_m)
    )


@operation(
    "Get T+1..T+20 daily progression", toolset="compete", requires=("api_key",), read_only=True
)
async def get_daily_progression(
    call: Call,
    round_id: Annotated[str, Field(min_length=1, description="Round number, round id, or exped.")],
    model_id: Annotated[
        str | None, Field(description="Omit for public benchmark walk; pass for your own model.")
    ] = None,
) -> ApiPayload:
    """Track T+1..T+20 partial scores for the round benchmark or one of your models."""
    if model_id is not None:
        return ApiPayload.from_raw(
            await call.json(
                ep.MODEL_DAILY_PROGRESSION,
                path_args={"round_id": round_id, "model_id": model_id},
            )
        )
    return ApiPayload.from_raw(
        await call.json(ep.ROUND_DAILY_PROGRESSION, path_args={"round_id": round_id})
    )


@operation("Get diagnostics run status", toolset="compete", requires=("api_key",), read_only=True)
async def get_diagnostics_run(call: Call, upload_id: UploadId) -> ApiPayload:
    """Read status, queue position, and scores of a validation or event diagnostics upload."""
    return ApiPayload.from_raw(
        await call.json(ep.DIAGNOSTICS_RUN, path_args={"upload_id": upload_id})
    )


@operation("Get upload transfer progress", toolset="compete", requires=("api_key",), read_only=True)
async def get_upload_progress(
    call: Call,
    upload_id: Annotated[str | None, Field(description="Inspect a single upload transfer.")] = None,
    batch_id: Annotated[str | None, Field(description="Filter by batch_id.")] = None,
    limit: Annotated[
        int, Field(ge=1, le=50, description="Recent uploads when upload_id omitted.")
    ] = 25,
) -> ApiPayload:
    """Check transfer pipeline state (`recipe_issued -> uploaded -> finalized -> nominated`)."""
    if upload_id is not None:
        return ApiPayload.from_raw(
            await call.json(ep.DIAGNOSTICS_UPLOAD_ITEM, path_args={"upload_id": upload_id})
        )
    params: dict[str, Any] = {"limit": limit}
    if batch_id is not None:
        params["batch_id"] = batch_id
    return ApiPayload.from_raw(await call.json(ep.DIAGNOSTICS_UPLOADS, params=params))


@operation(
    "Get event diagnostics leaderboard", toolset="compete", requires=("api_key",), read_only=True
)
async def get_diagnostics_leaderboard(
    call: Call,
    view: Literal["agents", "benchmarks"] = "agents",
    tournament: Tournament = "futures",
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
    window: str = "leaderboard",
    scoring_window: Annotated[
        str | None, Field(description="Multi-round events: 'leaderboard' or 'round_1'..'round_4'.")
    ] = None,
) -> ApiPayload:
    """Get one round's event diagnostics leaderboard (ranks models by event round score)."""
    params: dict[str, Any] = {
        "view": view,
        "window": window,
        "tournament": tournament,
        "limit": limit,
        "offset": offset,
    }
    if scoring_window is not None:
        params["scoring_window"] = scoring_window
    return ApiPayload.from_raw(await call.json(ep.DIAGNOSTICS_LEADERBOARD, params=params))


@operation(
    "Get cumulative event standings", toolset="compete", requires=("api_key",), read_only=True
)
async def get_diagnostics_standings(
    call: Call,
    scope: Literal["agent", "model"] = "agent",
    sort: Literal["event_score", "fit", "unq", "inov", "rounds", "name"] = "event_score",
    dir: Literal["asc", "desc"] = "desc",
    tournament: Literal["futures"] = "futures",
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> ApiPayload:
    """Get cumulative cross-round event standings (check `available` before reading entries)."""
    return ApiPayload.from_raw(
        await call.json(
            ep.DIAGNOSTICS_STANDINGS,
            params={
                "scope": scope,
                "sort": sort,
                "dir": dir,
                "tournament": tournament,
                "limit": limit,
                "offset": offset,
            },
        )
    )


@operation("Get event final selection", toolset="compete", requires=("api_key",), read_only=True)
async def get_final_selection(call: Call) -> ApiPayload:
    """Inspect up to 2 tagged models for events with a held-out final window."""
    return ApiPayload.from_raw(await call.json(ep.FINAL_SELECTION_GET))


# -- Write Operations --------------------------------------------------------


@operation(
    "Submit futures predictions",
    toolset="compete",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def submit_futures_predictions(
    call: Call,
    model_id: ModelId,
    predictions: Annotated[
        dict[str, float] | None,
        Field(description="Mapping of {instrument_id: prediction in [0, 1]} covering universe."),
    ] = None,
    predictions_path: Annotated[
        str | None,
        Field(description="Local .parquet, .csv, or .json predictions file path."),
    ] = None,
    exped: Annotated[
        str | None,
        Field(description="Literal open-round exped; omit to auto-resolve (never pass 'current')."),
    ] = None,
    data_datestamp: Annotated[
        int | None,
        Field(description="Optional YYYYMMDD data snapshot identifier for reproducibility."),
    ] = None,
) -> WriteResult:
    """Submit Himalayas futures predictions (safe to re-submit while round window is open)."""
    preds = _load_predictions_source(predictions, predictions_path)
    check_prediction_range(preds, lo=0.0, hi=1.0)
    resolved_exped = exped
    if resolved_exped is None:
        info = as_dict(await call.json(ep.FUTURES_CURRENT_INSTRUMENTS))
        resolved_exped = str(info["exped"])
    body: dict[str, Any] = {
        "model_id": model_id,
        "exped": resolved_exped,
        "predictions": [{"instrument_id": iid, "prediction": pred} for iid, pred in preds.items()],
    }
    if data_datestamp is not None:
        body["data_datestamp"] = data_datestamp
    res = await call.json(ep.FUTURES_SUBMIT_V2, body=body)
    return WriteResult.from_raw(res, status="ok", message="Futures predictions submitted.")


@operation(
    "Batch submit futures predictions",
    toolset="compete",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def submit_futures_predictions_batch(
    call: Call,
    items: Annotated[
        list[dict[str, Any]],
        Field(
            min_length=1,
            max_length=25,
            description=(
                "1-25 items: {model_id, predictions|predictions_path, exped?, data_datestamp?}."
            ),
        ),
    ],
    exped: Annotated[
        str | None, Field(description="Default exped for items that omit their own.")
    ] = None,
) -> BatchResult:
    """Submit futures predictions for 1-25 models with per-item outcome reporting."""
    default_exped = exped
    if default_exped is None and any(not item.get("exped") for item in items):
        info = as_dict(await call.json(ep.FUTURES_CURRENT_INSTRUMENTS))
        default_exped = str(info["exped"])

    results: list[dict[str, Any]] = []
    failed = 0
    for idx, item in enumerate(items):
        mid = str(item.get("model_id") or "")
        try:
            preds = _load_predictions_source(item.get("predictions"), item.get("predictions_path"))
            check_prediction_range(preds, lo=0.0, hi=1.0)
            target_exped = str(item.get("exped") or default_exped or "")
            body: dict[str, Any] = {
                "model_id": mid,
                "exped": target_exped,
                "predictions": [
                    {"instrument_id": iid, "prediction": pred} for iid, pred in preds.items()
                ],
            }
            if item.get("data_datestamp") is not None:
                body["data_datestamp"] = int(item["data_datestamp"])
            outcome = await call.json(ep.FUTURES_SUBMIT_V2, body=body)
            item_res = as_dict(outcome) if isinstance(outcome, dict) else {"result": outcome}
            results.append({"index": idx, "model_id": mid, **item_res})
        except Exception as exc:
            failed += 1
            results.append({"index": idx, "model_id": mid, "status": "error", "error": str(exc)})
    return BatchResult(
        status="ok" if failed == 0 else "accepted",
        items=results,
        failed=failed,
        all_succeeded=failed == 0,
        message=f"Submitted {len(items) - failed}/{len(items)} futures entries.",
    )


async def _submit_presigned_diagnostics(
    call: Call,
    presign_d: dict[str, Any],
    *,
    model_id: str,
    fname: str,
    data: bytes,
    pkl_fname: str,
    pkl_data: bytes | None,
    pkl_sha: str | None,
    model_pkl_python_version: str | None,
    tournament: str,
    target: str,
    client_label: str | None,
    complete_ep: ep.Endpoint,
) -> Any:
    await call.progress(0.35, "Uploading predictions to presigned storage")
    await call.client.s3_upload(
        as_dict(presign_d["predictions"]), fname, data, deadline=call.deadline
    )
    if pkl_data is not None and presign_d.get("model_pkl"):
        await call.progress(0.55, "Uploading model.pkl to presigned storage")
        await call.client.s3_upload(
            as_dict(presign_d["model_pkl"]), pkl_fname, pkl_data, deadline=call.deadline
        )
    complete_body: dict[str, Any] = {
        "upload_id": presign_d["upload_id"],
        "model_id": model_id,
        "filename": fname,
        "tournament": tournament,
        "target": target,
    }
    if client_label is not None:
        complete_body["client_label"] = client_label
    if pkl_data is not None and model_pkl_python_version is not None:
        complete_body["model_pkl_python_version"] = model_pkl_python_version
    if pkl_sha is not None:
        complete_body["model_pkl_sha256"] = pkl_sha
    return await call.json(complete_ep, body=complete_body)


async def _submit_multipart_diagnostics(
    call: Call,
    *,
    model_id: str,
    fname: str,
    data: bytes,
    pkl_fname: str,
    pkl_data: bytes | None,
    model_pkl_python_version: str | None,
    tournament: str,
    target: str,
    client_label: str | None,
    upload_ep: ep.Endpoint,
) -> Any:
    await call.progress(0.4, "Uploading via multipart diagnostics endpoint")
    files: dict[str, Any] = {"file": (fname, data, "application/octet-stream")}
    if pkl_data is not None:
        files["model_pkl"] = (pkl_fname, pkl_data, "application/octet-stream")
    form_data: dict[str, Any] = {
        "model_id": model_id,
        "tournament": tournament,
        "target": target,
    }
    if client_label is not None:
        form_data["client_label"] = client_label
    if pkl_data is not None and model_pkl_python_version is not None:
        form_data["model_pkl_python_version"] = model_pkl_python_version
    return await call.json(upload_ep, files=files, data=form_data)


async def _upload_diagnostics_lane(
    call: Call,
    *,
    model_id: str,
    file_path: str,
    model_pkl_path: str | None,
    model_pkl_python_version: str | None,
    tournament: str,
    target: str,
    client_label: str | None,
    wait: bool,
    wait_timeout: float,
    upload_ep: ep.Endpoint,
    presign_ep: ep.Endpoint,
    complete_ep: ep.Endpoint,
) -> WriteResult:
    await call.progress(0.1, f"Reading predictions file for {model_id}")
    data, fname, data_sha = await call.client.read_local_file(file_path)

    pkl_data: bytes | None = None
    pkl_fname = "model.pkl"
    pkl_sha: str | None = None
    if model_pkl_path is not None:
        pkl_data, pkl_fname, pkl_sha = await call.client.read_local_file(model_pkl_path)

    presign_body: dict[str, Any] = {
        "model_id": model_id,
        "filename": fname,
        "tournament": tournament,
        "target": target,
        "accepts": ["PUT", "POST"],
        "predictions_size_bytes": len(data),
        "predictions_sha256": data_sha,
    }
    if pkl_data is not None and pkl_sha is not None:
        presign_body["model_pkl_filename"] = pkl_fname
        presign_body["model_pkl_size_bytes"] = len(pkl_data)
        presign_body["model_pkl_sha256"] = pkl_sha
        if model_pkl_python_version is not None:
            presign_body["model_pkl_python_version"] = model_pkl_python_version

    try:
        presign = await call.json(presign_ep, body=presign_body)
    except NotFound as exc:
        if _is_model_not_found(exc):
            raise
        presign = None

    presign_d = as_dict(presign)
    if presign_d.get("available"):
        accepted = await _submit_presigned_diagnostics(
            call,
            presign_d,
            model_id=model_id,
            fname=fname,
            data=data,
            pkl_fname=pkl_fname,
            pkl_data=pkl_data,
            pkl_sha=pkl_sha,
            model_pkl_python_version=model_pkl_python_version,
            tournament=tournament,
            target=target,
            client_label=client_label,
            complete_ep=complete_ep,
        )
    else:
        accepted = await _submit_multipart_diagnostics(
            call,
            model_id=model_id,
            fname=fname,
            data=data,
            pkl_fname=pkl_fname,
            pkl_data=pkl_data,
            model_pkl_python_version=model_pkl_python_version,
            tournament=tournament,
            target=target,
            client_label=client_label,
            upload_ep=upload_ep,
        )

    accepted_d = as_dict(accepted)
    upload_id = str(accepted_d.get("upload_id") or "")
    if not wait or not upload_id:
        return WriteResult.from_raw(
            accepted,
            status="accepted",
            message="Diagnostics upload accepted for async scoring.",
            next_step=(
                f"Poll `get_diagnostics_run(upload_id='{upload_id}')` or "
                + f"`get_validation_panel(model_id='{model_id}')`."
                if upload_id
                else None
            ),
        )

    def _check_run_error(run_d: dict[str, Any]) -> Exception | None:
        if run_d.get("status") == "failed":
            err = run_d.get("error")
            return ServerError(f"Diagnostics run {upload_id} failed: {err or 'unknown error'}")
        return None

    completed = await call.poll_until(
        lambda: call.json(ep.DIAGNOSTICS_RUN, path_args={"upload_id": upload_id}),
        wait_timeout=wait_timeout,
        progress_fraction=0.75,
        progress_message=lambda run_d: (
            f"Waiting for diagnostics run {upload_id} ({run_d.get('status') or 'pending'})"
        ),
        is_done=lambda run_d: run_d.get("status") == "done",
        on_error=_check_run_error,
    )
    if completed is not None:
        return WriteResult.from_raw(completed, status="ok", message="Diagnostics run completed.")

    return WriteResult.from_raw(
        accepted,
        status="pending",
        message=f"Upload {upload_id} is still scoring past the tool wait cap.",
        next_step=f"Resume polling with `get_diagnostics_run(upload_id='{upload_id}')`.",
    )


@operation(
    "Upload validation diagnostics (practice board)",
    toolset="compete",
    requires=("api_key",),
    gate="writes",
)
async def submit_validation_diagnostics(
    call: Call,
    model_id: ModelId,
    file_path: Annotated[
        str, Field(min_length=1, description="Local .parquet or .csv predictions path.")
    ],
    model_pkl_path: Annotated[
        str | None,
        Field(description="Cloudpickled model .pkl path (required for hackathon event keys)."),
    ] = None,
    model_pkl_python_version: Annotated[
        str | None, Field(pattern=r"^\d+\.\d+$", description="Python 'major.minor' of the .pkl.")
    ] = None,
    tournament: Tournament = "futures",
    target: str = "target_everest",
    client_label: Annotated[str | None, Field(max_length=256)] = None,
    wait: Annotated[
        bool, Field(description="Poll until done (automatically capped to client call timeout).")
    ] = False,
    wait_timeout: Annotated[float, Field(gt=1, le=1800)] = 600.0,
) -> WriteResult:
    """Upload out-of-sample predictions to the free practice validation diagnostics board."""
    return await _upload_diagnostics_lane(
        call,
        model_id=model_id,
        file_path=file_path,
        model_pkl_path=model_pkl_path,
        model_pkl_python_version=model_pkl_python_version,
        tournament=tournament,
        target=target,
        client_label=client_label,
        wait=wait,
        wait_timeout=wait_timeout,
        upload_ep=ep.DIAGNOSTICS_UPLOAD,
        presign_ep=ep.DIAGNOSTICS_PRESIGN,
        complete_ep=ep.DIAGNOSTICS_COMPLETE,
    )


@operation(
    "Batch upload validation diagnostics",
    toolset="compete",
    requires=("api_key",),
    gate="writes",
)
async def submit_diagnostics_batch(
    call: Call,
    items: Annotated[
        list[dict[str, Any]],
        Field(
            min_length=1,
            max_length=25,
            description=(
                "1-25 items: {model_id, file_path, model_pkl_path?, "
                "model_pkl_python_version?, client_label?}."
            ),
        ),
    ],
    tournament: Tournament = "futures",
    target: str = "target_everest",
    wait: bool = False,
) -> BatchResult:
    """Upload validation diagnostics for 1-25 models with per-item outcome reporting."""
    results: list[dict[str, Any]] = []
    failed = 0
    for idx, item in enumerate(items):
        mid = str(item.get("model_id") or "")
        try:
            outcome = await _upload_diagnostics_lane(
                call,
                model_id=mid,
                file_path=str(item["file_path"]),
                model_pkl_path=item.get("model_pkl_path"),
                model_pkl_python_version=item.get("model_pkl_python_version"),
                tournament=tournament,
                target=target,
                client_label=item.get("client_label"),
                wait=wait,
                wait_timeout=600.0,
                upload_ep=ep.DIAGNOSTICS_UPLOAD,
                presign_ep=ep.DIAGNOSTICS_PRESIGN,
                complete_ep=ep.DIAGNOSTICS_COMPLETE,
            )
            payload = (
                as_dict(outcome.data)
                if isinstance(cast(Any, outcome).data, dict)
                else outcome.model_dump()
            )
            results.append({"index": idx, "model_id": mid, **payload})
        except Exception as exc:
            failed += 1
            results.append({"index": idx, "model_id": mid, "status": "error", "error": str(exc)})
    return BatchResult(
        status="ok" if failed == 0 else "accepted",
        items=results,
        failed=failed,
        all_succeeded=failed == 0,
        message=f"Processed {len(items) - failed}/{len(items)} diagnostics uploads.",
    )


async def _describe_event_upload(
    _call: Call,
    model_id: str,
    file_path: str,
    tournament: str = "futures",
    **_unused: Any,
) -> str:
    label = clean_label(model_id) or model_id
    return (
        f"Submit '{file_path}' for model '{label}' ({tournament}) to the PAID EVENT ROUND? "
        "This draws from your non-replenished per-event upload pool."
    )


@operation(
    "Submit paid event round predictions",
    toolset="compete",
    requires=("api_key",),
    gate="writes",
    describe=_describe_event_upload,
)
async def submit_event_predictions(
    call: Call,
    model_id: ModelId,
    file_path: Annotated[
        str, Field(min_length=1, description="Local .parquet or .csv predictions path.")
    ],
    model_pkl_path: Annotated[
        str | None,
        Field(description="Cloudpickled model .pkl path (required for event-scoped keys)."),
    ] = None,
    model_pkl_python_version: Annotated[
        str | None, Field(pattern=r"^\d+\.\d+$", description="Python 'major.minor' of the .pkl.")
    ] = None,
    tournament: Tournament = "futures",
    target: str = "target_everest",
    client_label: Annotated[str | None, Field(max_length=256)] = None,
    wait: bool = False,
    wait_timeout: Annotated[float, Field(gt=1, le=1800)] = 600.0,
) -> WriteResult:
    """Submit predictions + `.pkl` to the ranked/paid event round (uses event upload pool)."""
    return await _upload_diagnostics_lane(
        call,
        model_id=model_id,
        file_path=file_path,
        model_pkl_path=model_pkl_path,
        model_pkl_python_version=model_pkl_python_version,
        tournament=tournament,
        target=target,
        client_label=client_label,
        wait=wait,
        wait_timeout=wait_timeout,
        upload_ep=ep.EVENT_UPLOAD,
        presign_ep=ep.EVENT_PRESIGN,
        complete_ep=ep.EVENT_COMPLETE,
    )


@operation(
    "Cancel diagnostics run",
    toolset="compete",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def cancel_diagnostics_run(call: Call, upload_id: UploadId) -> WriteResult:
    """Cancel your own pending/running diagnostics upload and free the per-model slot."""
    res = await call.json(ep.DIAGNOSTICS_RUN_CANCEL, path_args={"upload_id": upload_id})
    return WriteResult.from_raw(res, status="ok", message=f"Run {upload_id} cancelled.")


async def _describe_delete_run(_call: Call, upload_id: str) -> str:
    return (
        f"Delete diagnostics run '{clean_label(upload_id) or upload_id}'? "
        "Note: completed paid-round runs cannot be deleted (HTTP 409)."
    )


@operation(
    "Delete diagnostics run",
    toolset="compete",
    requires=("api_key",),
    gate="withdrawals",
    destructive=True,
    describe=_describe_delete_run,
    mandatory_confirm=True,
)
async def delete_diagnostics_run(call: Call, upload_id: UploadId) -> WriteResult:
    """Delete a failed, cancelled, in-flight, or practice validation run."""
    res = await call.json(ep.DIAGNOSTICS_RUN_DELETE, path_args={"upload_id": upload_id})
    return WriteResult.from_raw(res, status="ok", message=f"Run {upload_id} deleted.")


@operation(
    "Set event final selection",
    toolset="compete",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def set_final_selection(
    call: Call,
    model_ids: Annotated[
        list[str],
        Field(
            max_length=2,
            description="Up to 2 model ids/names; empty list restores best-2 fallback.",
        ),
    ],
) -> WriteResult:
    """Tag up to 2 models for an event's held-out final window (active in selection window)."""
    res = await call.json(ep.FINAL_SELECTION_SET, body={"model_ids": list(model_ids)})
    return WriteResult.from_raw(res, status="ok", message="Final selection updated.")
