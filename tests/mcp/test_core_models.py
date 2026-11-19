"""MCP tests for `core` and `models` toolsets."""

from pathlib import Path
from typing import Any

import pytest
from mcp.types import ElicitResult

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.config import Settings
from tests.conftest import FakeApi
from tests.mcp.helpers import call, error, session

pytestmark = pytest.mark.anyio


async def test_core_orientation_and_downloads(
    authed_settings: Settings, api: FakeApi, tmp_path: Path
) -> None:
    api.add(ep.HEALTH, {"status": "ok"})
    api.add(ep.CAPABILITIES, {"api_base": "https://api.everesteer.ai", "mcp": "/mcp"})
    api.add(ep.SCORING, {"weights": {"score_multiple_constant": 0.1}})
    api.add(ep.GET_STARTED, {"mode": "tournament"})
    api.add(ep.STATUS, {"models": [], "open_round": {"exped": "exped_20261119"}})
    api.add(ep.ROUNDS, {"rounds": [{"id": 1, "status": "open"}]})
    api.add(ep.CURRENT_ROUND, {"round_number": 42, "status": "open"})
    api.add(ep.SCHEDULE, {"futures": {"cadence": "weekday"}})
    api.add(ep.SEASONS, {"seasons": [{"id": 1}]})
    api.add(ep.CURRENT_SEASON, {"id": 2, "rankings": []})
    api.add(ep.BENCHMARKS, {"benchmarks": [{"name": "eiq_minera_model"}]})
    api.add(
        ep.DATA_VERSIONS, {"versions": [{"version": "v2", "universes": ["equities", "futures"]}]}
    )
    api.add(ep.DATA_SCHEMA, {"primary_target": "target_everest", "feature_sets": {"small": 42}})
    api.add(ep.DATA_INFO, {"universe": "futures", "rows": 1000})
    api.add(ep.FUTURES_UNIVERSE, {"instruments": ["ES", "NQ"]})
    api.add(ep.FUTURES_TARGETS, {"targets": ["target_everest"]})

    # Presigned CDN redirect on futures train download, plus 404 self-heal on equities v1 -> v2.
    cdn_futures = "https://cdn.everesteer.test/futures_train.parquet?sig=1"
    api.add(ep.FUTURES_DATA, (302, b"", {"Location": cdn_futures}))
    api.add_cdn(cdn_futures, b"FUTURES_PARQUET")
    api.add(
        ep.DATA_DOWNLOAD,
        (404, {"detail": "stale version"}),
        b"EQUITIES_PARQUET",
    )
    api.add(ep.DATA_DOWNLOAD_BULK, b"BULK_ZIP")
    api.add(ep.DATA_BENCHMARK, b"BENCH_PARQUET")
    api.add(ep.DATA_AI_MODEL, b"AI_MODEL_PARQUET")

    async with session(authed_settings, api) as client:
        assert (await call(client, "get_health"))["status"] == "ok"
        assert (await call(client, "get_capabilities"))["mcp"] == "/mcp"
        assert (await call(client, "explain_scoring"))["weights"]["score_multiple_constant"] == 0.1
        id_res = await call(client, "whoami")
        assert id_res["authenticated"] is True
        assert id_res["scope"] == "full"
        assert (await call(client, "get_started"))["mode"] == "tournament"
        assert (await call(client, "get_status"))["open_round"]["exped"] == "exped_20261119"
        assert (await call(client, "get_rounds"))["rounds"][0]["id"] == 1
        assert (await call(client, "get_rounds", current_only=True))["round_number"] == 42
        assert (await call(client, "get_schedule"))["futures"]["cadence"] == "weekday"
        assert (await call(client, "get_seasons"))["seasons"][0]["id"] == 1
        assert (await call(client, "get_seasons", current_only=True))["id"] == 2
        assert (await call(client, "get_benchmarks"))["benchmarks"][0]["name"] == "eiq_minera_model"
        assert (await call(client, "list_dataset_versions"))["versions"][0]["version"] == "v2"
        assert (await call(client, "get_dataset_schema", verbose=True))[
            "primary_target"
        ] == "target_everest"
        assert (await call(client, "get_dataset_info"))["rows"] == 1000
        assert (await call(client, "get_futures_universe"))["instruments"] == ["ES", "NQ"]
        assert (await call(client, "get_futures_targets"))["targets"] == ["target_everest"]

        d1 = await call(
            client,
            "download_dataset",
            universe="futures",
            split="train",
            output_path=str(tmp_path / "fut.parquet"),
        )
        assert d1["bytes_written"] == len(b"FUTURES_PARQUET")

        # Trigger 404 version self-heal on equities
        d2 = await call(
            client,
            "download_dataset",
            universe="equities",
            split="train",
            version="v1",
            output_path=str(tmp_path / "eq.parquet"),
        )
        assert d2["version"] == "v2"
        assert d2["bytes_written"] == len(b"EQUITIES_PARQUET")

        dbulk = await call(
            client,
            "download_dataset",
            universe="equities",
            split="bulk",
            output_path=str(tmp_path / "bulk.zip"),
        )
        assert dbulk["artifact"] == "bulk_zip"
        assert dbulk["bytes_written"] == len(b"BULK_ZIP")

        b1 = await call(
            client,
            "download_benchmark",
            universe="futures",
            split="train",
            output_path=str(tmp_path / "b_fut.parquet"),
        )
        assert b1["bytes_written"] == len(b"FUTURES_PARQUET")
        b2 = await call(
            client,
            "download_benchmark",
            universe="equities",
            split="validation",
            output_path=str(tmp_path / "b_eq.parquet"),
        )
        assert b2["bytes_written"] == len(b"BENCH_PARQUET")
        ai = await call(
            client,
            "download_ai_model",
            universe="futures",
            version="v0",
            output_path=str(tmp_path / "ai.parquet"),
        )
        assert ai["bytes_written"] == len(b"AI_MODEL_PARQUET")


async def test_models_crud_presigned_and_multipart_upload_and_archive(
    authed_settings: Settings, api: FakeApi, tmp_path: Path
) -> None:
    authed_settings.allow_writes = True
    authed_settings.allow_withdrawals = True

    pkl = tmp_path / "model.pkl"
    pkl.write_bytes(b"FAKE_CLOUDPICKLE_BYTES")

    s3_put = "https://s3.everesteer.test/models/m1/model.pkl?sig=put"
    api.add_cdn(s3_put, b"", status=204)

    api.add(
        ep.MODELS_LIST,
        {"models": [{"id": "m-uuid-1", "name": "alpha-one", "lane_active": True}]},
    )
    api.add(
        ep.MODELS_CREATE,
        {"id": "m-uuid-1", "name": "opaque-1"},
        (409, {"detail": "already exists"}),
    )
    api.add(ep.MODEL_SETTINGS, {"id": "m-uuid-1", "auto_submit": 1})
    api.add(ep.MODEL_RENAME, {"id": "m-uuid-1", "name": "alpha-two"})
    api.add(
        ep.MODEL_UPLOAD_PRESIGN,
        {
            "available": True,
            "upload_id": "up-1",
            "upload": {"method": "PUT", "url": s3_put, "headers": {"x-amz-acl": "private"}},
        },
        (404, {"detail": "not offered"}),
        (404, {"detail": "No model 'missing'. Create it first with create_model."}),
    )
    api.add(ep.MODEL_UPLOAD_COMPLETE, {"upload_id": "up-1", "status": "validating"})
    api.add(ep.MODEL_UPLOAD_FROM_JOB, {"upload_id": "up-job-1", "status": "validating"})
    api.add(ep.MODEL_UPLOAD, {"upload_id": "up-multipart", "status": "validating"})
    api.add(ep.MODEL_UPLOAD_STATUS, {"status": "validated"})
    api.add(ep.MODEL_MULTIPLIERS_GET, {"fit_multiplier": 1.0, "unq_multiplier": 1.0})
    api.add(ep.MODEL_MULTIPLIERS_SET, {"fit_multiplier": 2.0, "unq_multiplier": 1.5})
    api.add(ep.MODEL_ARCHIVE, {"id": "m-uuid-1", "archived": True})

    async def accept(_ctx: Any, _params: Any) -> ElicitResult:
        return ElicitResult(action="accept", content={"confirm": True})

    async with session(authed_settings, api, elicitation_callback=accept) as client:
        assert (await call(client, "list_models"))["models"][0]["id"] == "m-uuid-1"
        assert (await call(client, "create_model"))["data"]["id"] == "m-uuid-1"
        # 409 idempotent lookup by name
        dup = await call(client, "create_model", name="alpha-one")
        assert dup["data"]["status"] == "already_exists"

        await call(
            client, "update_model", model_id="m-uuid-1", description="desc", client_label="v1"
        )
        await call(client, "rename_model", model_id="m-uuid-1", name="alpha-two")

        # First upload uses presigned PUT
        up1 = await call(
            client, "upload_model", model_id="m-uuid-1", file_path=str(pkl), python_version="3.12"
        )
        assert up1["data"]["upload_id"] == "up-1"
        # Second upload falls back to multipart on 404
        up2 = await call(client, "upload_model", model_id="m-uuid-1", file_path=str(pkl))
        assert up2["data"]["upload_id"] == "up-multipart"
        # Model-not-found 404 must surface instead of falling back to multipart
        await error(client, "upload_model", model_id="missing", file_path=str(pkl))
        # Direct deploy from hosted training job
        up_job = await call(
            client,
            "upload_model",
            model_id="m-uuid-1",
            from_job_id="job-1",
            python_version="3.12",
        )
        assert up_job["data"]["upload_id"] == "up-job-1"

        assert (await call(client, "get_model_upload_status", model_id="m-uuid-1"))[
            "status"
        ] == "validated"
        assert (await call(client, "set_auto_submit", model_id="m-uuid-1", enabled=True))[
            "status"
        ] == "ok"
        assert (await call(client, "get_model_multipliers", model_id="m-uuid-1"))[
            "fit_multiplier"
        ] == 1.0
        assert (
            await call(
                client,
                "set_model_multipliers",
                model_id="m-uuid-1",
                fit_multiplier=2.0,
                unq_multiplier=1.5,
            )
        )["status"] == "ok"
        archived = await call(client, "archive_model", model_id="m-uuid-1")
        assert archived["data"]["archived"] is True
