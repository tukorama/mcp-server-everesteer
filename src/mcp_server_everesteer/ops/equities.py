"""Alps (equities) tournament tools — opt-in via EVERESTEER_TOOLSETS=auto,+equities."""

from typing import Annotated, Any

from pydantic import Field

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.models import ApiPayload, WriteResult
from mcp_server_everesteer.ops.registry import Call, ModelId, operation
from mcp_server_everesteer.safety import check_prediction_range


@operation(
    "Get equities universe (Alps)", toolset="equities", requires=("api_key",), read_only=True
)
async def get_equities_universe(call: Call) -> ApiPayload:
    """Get the current Alps equities tournament universe."""
    return ApiPayload.from_raw(await call.json(ep.EQUITIES_UNIVERSE))


@operation(
    "Get equities features (Alps)", toolset="equities", requires=("api_key",), read_only=True
)
async def get_equities_features(
    call: Call,
    date: Annotated[
        str | None, Field(description="Date in YYYY-MM-DD format; omit for latest.")
    ] = None,
) -> ApiPayload:
    """Get encoded Alps equities features for a date (`-1` means missing)."""
    params = {"date": date} if date is not None else None
    return ApiPayload.from_raw(await call.json(ep.EQUITIES_FEATURES, params=params))


@operation(
    "Submit equities predictions (JSON)",
    toolset="equities",
    requires=("api_key",),
    gate="writes",
    idempotent=True,
)
async def submit_equities_predictions(
    call: Call,
    model_id: ModelId,
    predictions: Annotated[
        list[dict[str, Any]],
        Field(description="List of {ticker: str, score: float in [-1, 1]}."),
    ],
) -> WriteResult:
    """Submit daily Alps equities predictions (scores must be finite and within [-1, 1])."""
    mapped = {
        str(item.get("ticker", idx)): float(item["score"]) for idx, item in enumerate(predictions)
    }
    check_prediction_range(mapped, lo=-1.0, hi=1.0)
    res = await call.json(
        ep.EQUITIES_SUBMIT, body={"model_id": model_id, "predictions": predictions}
    )
    return WriteResult.from_raw(res, status="ok", message="Equities predictions submitted.")


@operation(
    "Submit equities predictions (file)",
    toolset="equities",
    requires=("api_key",),
    gate="writes",
)
async def submit_equities_predictions_file(
    call: Call,
    model_id: ModelId,
    file_path: Annotated[str, Field(min_length=1, description="Local .csv or .parquet file.")],
    tournament: str = "equities",
) -> WriteResult:
    """Upload Alps equities predictions from a local `.csv` or `.parquet` file."""
    data, fname, _ = await call.client.read_local_file(file_path)
    res = await call.json(
        ep.EQUITIES_SUBMIT_UPLOAD,
        params={"model_id": model_id, "tournament": tournament},
        files={"file": (fname, data, "application/octet-stream")},
    )
    return WriteResult.from_raw(res, status="ok", message="Equities prediction file submitted.")
