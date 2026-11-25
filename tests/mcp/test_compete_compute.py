"""MCP tests for `compete` and `compute` toolsets."""

from pathlib import Path
from typing import Any

import pytest
from mcp.types import ElicitResult, Implementation

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.config import Settings
from tests.conftest import FakeApi, FakeClock
from tests.mcp.helpers import call, error, session

pytestmark = pytest.mark.anyio


async def test_compete_reads_submissions_and_diagnostics(
    authed_settings: Settings, api: FakeApi, tmp_path: Path
) -> None:
    authed_settings.allow_writes = True
    authed_settings.allow_withdrawals = True

    preds_file = tmp_path / "preds.parquet"
    preds_file.write_bytes(b"PARQUET_PREDS")
    pkl_file = tmp_path / "model.pkl"
    pkl_file.write_bytes(b"PKL_BYTES")

    s3_post = "https://s3.everesteer.test/diag/upload"
    api.add_cdn(s3_post, b"", status=201)

    api.add(ep.GET_STARTED, {"mode": "tournament"})
    api.add(ep.FUTURES_CURRENT_INSTRUMENTS, {"exped": "exped_20261119", "instruments": ["ES"]})
    api.add(ep.FUTURES_VALIDATE, {"valid": True, "issues": []})
    api.add(
        ep.FUTURES_SUBMIT_V2,
        {"submission_id": "sub-1", "exped": "exped_20261119", "replaced": False},
    )
    api.add(ep.SUBMISSIONS, {"submissions": [{"model_id": "m1", "accepted": True}]})
    api.add(ep.SCORES, {"model_id": "m1", "scores": [{"fit": 0.04, "bmc": 0.02, "fnc": 0.01}]})
    api.add(ep.BOARD_LIVE_AGENTS, {"available": True, "rows": [{"rank": 1, "agent_name": "a1"}]})
    api.add(ep.BOARD_LIVE_MODELS, {"available": True, "rows": [{"rank": 1, "model_name": "m1"}]})
    api.add(ep.BOARD_HISTORICAL, {"available": True, "rows": [{"rank": 1}]})
    api.add(ep.FUTURES_LEADERBOARD, {"entries": [{"rank": 1}]})
    api.add(ep.LEADERBOARD_LEGACY, {"entries": [{"rank": 2}]})
    api.add(ep.MODEL_VALIDATION_PANEL, {"fit20": 0.05, "per_exped_fit": [0.01, 0.02]})
    api.add(ep.MODEL_DIAGNOSTICS, {"fit20": 0.03})
    api.add(ep.MODEL_DIAGNOSTICS_ROUNDS, {"rounds": [{"round": 1}]})
    api.add(ep.FUTURES_DIAGNOSTICS, {"agent_id": "a1", "fit": 0.02})
    api.add(ep.ROUND_DAILY_PROGRESSION, {"days": [{"day": 1}]})
    api.add(ep.MODEL_DAILY_PROGRESSION, {"days": [{"day": 1, "fit": 0.02}]})

    api.add(
        ep.DIAGNOSTICS_PRESIGN,
        {
            "available": True,
            "upload_id": "diag-1",
            "predictions": {"method": "POST", "url": s3_post, "fields": {"key": "p"}},
            "model_pkl": {"method": "POST", "url": s3_post, "fields": {"key": "m"}},
        },
        (404, {"detail": "no presign"}),
        (404, {"detail": "No model 'missing'. Create it first with create_model."}),
    )
    api.add(ep.DIAGNOSTICS_COMPLETE, {"upload_id": "diag-1", "status": "pending"})
    api.add(ep.DIAGNOSTICS_UPLOAD, {"upload_id": "diag-2", "status": "pending"})
    api.add(
        ep.DIAGNOSTICS_RUN,
        {"upload_id": "diag-1", "status": "running"},
        {"upload_id": "diag-1", "status": "done", "fit20": 0.06},
    )
    api.add(ep.DIAGNOSTICS_UPLOADS, {"items": [{"upload_id": "diag-1", "stage": "finalized"}]})
    api.add(ep.DIAGNOSTICS_UPLOAD_ITEM, {"upload_id": "diag-1", "stage": "finalized"})
    api.add(ep.DIAGNOSTICS_LEADERBOARD, {"available": True, "entries": []})
    api.add(ep.DIAGNOSTICS_STANDINGS, {"available": True, "entries": []})
    api.add(ep.FINAL_SELECTION_GET, {"model_ids": ["m1"], "phase": "selection"})
    api.add(ep.FINAL_SELECTION_SET, {"model_ids": ["m1", "m2"]})
    api.add(ep.EVENT_PRESIGN, (404, {"detail": "multipart only"}))
    api.add(ep.EVENT_UPLOAD, {"upload_id": "evt-1", "status": "pending"})
    api.add(ep.EVENT_COMPLETE, {"upload_id": "evt-1", "status": "pending"})
    api.add(ep.DIAGNOSTICS_RUN_CANCEL, {"upload_id": "diag-1", "status": "cancelled"})
    api.add(ep.DIAGNOSTICS_RUN_DELETE, {"upload_id": "diag-1", "deleted": True})

    async def accept(_ctx: Any, _params: Any) -> ElicitResult:
        return ElicitResult(action="accept", content={"confirm": True})

    async with session(authed_settings, api, elicitation_callback=accept) as client:
        assert (await call(client, "validate_futures_submission", predictions={"ES": 0.5}))["valid"]
        preds_csv = tmp_path / "preds.csv"
        preds_csv.write_text("id,prediction\nES,0.5\n", encoding="utf-8")
        assert (await call(client, "validate_futures_submission", predictions_path=str(preds_csv)))[
            "valid"
        ]
        sub = await call(
            client,
            "submit_futures_predictions",
            model_id="m1",
            predictions={"ES": 0.6},
            data_datestamp=20261119,
        )
        assert sub["data"]["submission_id"] == "sub-1"

        # Batch submission: one valid + one out-of-range item
        batch = await call(
            client,
            "submit_futures_predictions_batch",
            items=[
                {"model_id": "m1", "predictions": {"ES": 0.6}},
                {"model_id": "m2", "predictions": {"ES": 2.5}},
            ],
        )
        assert batch["failed"] == 1
        assert batch["all_succeeded"] is False

        assert (await call(client, "get_submission_status"))["submissions"][0]["accepted"] is True
        scores = await call(client, "get_scores", model_id="m1")
        # bmc/fnc normalized to unq/inov alongside original keys
        assert scores["scores"][0]["unq"] == 0.02
        assert scores["scores"][0]["inov"] == 0.01

        assert (await call(client, "get_leaderboard", board="live", view="agents"))["rows"][0][
            "rank"
        ] == 1
        assert (await call(client, "get_leaderboard", board="live", view="models"))["rows"][0][
            "model_name"
        ] == "m1"
        assert (await call(client, "get_leaderboard", board="historical"))["rows"][0]["rank"] == 1
        assert (await call(client, "get_leaderboard", board="futures"))["entries"][0]["rank"] == 1
        assert (await call(client, "get_leaderboard", board="legacy"))["entries"][0]["rank"] == 2
        await error(client, "get_leaderboard", board="historical", round=1)

        panel = await call(client, "get_validation_panel", model_id="m1")
        assert panel["fit20"] == 0.05
        per_exped = await call(client, "get_validation_panel", model_id="m1", per_exped_only=True)
        assert per_exped["per_exped_fit"] == [0.01, 0.02]

        await call(client, "get_diagnostics", target_id="m1", scope="model")
        await call(client, "get_diagnostics", target_id="m1", scope="rounds")
        await call(client, "get_diagnostics", target_id="a1", scope="futures_agent")
        await call(client, "get_daily_progression", round_id="42")
        await call(client, "get_daily_progression", round_id="42", model_id="m1")

        diag_ok = await call(
            client,
            "submit_validation_diagnostics",
            model_id="m1",
            file_path=str(preds_file),
            model_pkl_path=str(pkl_file),
            model_pkl_python_version="3.12",
            wait=True,
        )
        assert diag_ok["status"] == "ok"

        diag_batch = await call(
            client,
            "submit_diagnostics_batch",
            items=[{"model_id": "m1", "file_path": str(preds_file)}],
        )
        assert diag_batch["all_succeeded"] is True

        evt = await call(
            client,
            "submit_event_predictions",
            model_id="m1",
            file_path=str(preds_file),
            model_pkl_path=str(pkl_file),
        )
        assert evt["data"]["upload_id"] == "evt-1"

        await call(client, "get_diagnostics_run", upload_id="diag-1")
        await call(client, "get_upload_progress")
        await call(client, "get_upload_progress", upload_id="diag-1")
        await call(client, "get_diagnostics_leaderboard")
        await call(client, "get_diagnostics_standings")
        await call(client, "get_final_selection")
        await call(client, "set_final_selection", model_ids=["m1", "m2"])
        await call(client, "cancel_diagnostics_run", upload_id="diag-1")
        await call(client, "delete_diagnostics_run", upload_id="diag-1")

        # Hit EVENT_COMPLETE via presigned path as well
        api.add(
            ep.EVENT_PRESIGN,
            {
                "available": True,
                "upload_id": "evt-2",
                "predictions": {"method": "POST", "url": s3_post, "fields": {"key": "e"}},
            },
        )
        api.add(ep.EVENT_COMPLETE, {"upload_id": "evt-2", "status": "pending"})
        evt2 = await call(
            client, "submit_event_predictions", model_id="m1", file_path=str(preds_file)
        )
        assert evt2["data"]["upload_id"] == "evt-2"
        # Model-not-found 404 on diagnostics presign surfaces directly without multipart fallback
        await error(
            client, "submit_validation_diagnostics", model_id="missing", file_path=str(preds_file)
        )


@pytest.mark.parametrize(
    ("name", "content", "named"),
    [
        ("preds.json", '{"ES": true, "NQ": 0.5}', "ES=True"),
        ("preds.json", '{"ES": null, "NQ": 0.5}', "ES=None"),
        ("preds.json", '{"ES": NaN, "NQ": 0.5}', "ES=nan"),
        ("preds.csv", "id,prediction\nES,\nNQ,0.5\n", "ES=''"),
        (
            "preds.json",
            '[{"id": "ES", "prediction": null}, {"id": "NQ", "prediction": 0.5}]',
            "ES=None",
        ),
    ],
)
async def test_prediction_files_with_non_numbers_are_refused_by_instrument(
    authed_settings: Settings, api: FakeApi, tmp_path: Path, name: str, content: str, named: str
) -> None:
    authed_settings.allow_writes = True
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    api.add(ep.FUTURES_VALIDATE, {"valid": True})
    api.add(ep.FUTURES_SUBMIT_V2, {"submission_id": "sub-1"})

    async with session(authed_settings, api) as client:
        validate = await error(client, "validate_futures_submission", predictions_path=str(path))
        submit = await error(
            client,
            "submit_futures_predictions",
            model_id="m1",
            predictions_path=str(path),
            exped="exped_20261119",
        )

    for message in (validate, submit):
        assert named in message
        assert "not finite numbers" in message
    assert api.calls(ep.FUTURES_VALIDATE) == []
    assert api.calls(ep.FUTURES_SUBMIT_V2) == []


async def test_empty_prediction_mappings_are_refused(
    authed_settings: Settings, api: FakeApi, tmp_path: Path
) -> None:
    authed_settings.allow_writes = True
    empty = tmp_path / "preds.json"
    empty.write_text("{}", encoding="utf-8")
    api.add(ep.FUTURES_SUBMIT_V2, {"submission_id": "sub-1"})

    async with session(authed_settings, api) as client:
        from_file = await error(
            client,
            "submit_futures_predictions",
            model_id="m1",
            predictions_path=str(empty),
            exped="exped_20261119",
        )
        inline = await error(
            client, "submit_futures_predictions", model_id="m1", predictions={}, exped="e1"
        )

    assert "No predictions found" in from_file
    assert "No predictions found" in inline
    assert api.calls(ep.FUTURES_SUBMIT_V2) == []


@pytest.mark.parametrize("current", [{"exped": None, "instruments": []}, {"instruments": ["ES"]}])
async def test_unresolvable_open_round_is_refused_instead_of_submitting(
    authed_settings: Settings, api: FakeApi, current: dict[str, Any]
) -> None:
    authed_settings.allow_writes = True
    api.add(ep.FUTURES_CURRENT_INSTRUMENTS, current)
    api.add(ep.FUTURES_SUBMIT_V2, {"submission_id": "sub-1"})

    async with session(authed_settings, api) as client:
        single = await error(
            client, "submit_futures_predictions", model_id="m1", predictions={"ES": 0.5}
        )
        batch = await error(
            client,
            "submit_futures_predictions_batch",
            items=[{"model_id": "m1", "predictions": {"ES": 0.5}}],
        )

    for message in (single, batch):
        assert "Could not resolve the open futures round's exped" in message
    assert api.calls(ep.FUTURES_SUBMIT_V2) == []


async def test_batch_status_reports_rejected_and_partial_outcomes(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_writes = True
    api.add(ep.FUTURES_SUBMIT_V2, {"submission_id": "sub-1"})

    async with session(authed_settings, api) as client:
        rejected = await call(
            client,
            "submit_futures_predictions_batch",
            exped="e1",
            items=[
                {"model_id": "m1", "predictions": {"ES": 2.5}},
                {"model_id": "m2", "predictions": {"ES": -1}},
            ],
        )
        partial = await call(
            client,
            "submit_futures_predictions_batch",
            exped="e1",
            items=[
                {"model_id": "m1", "predictions": {"ES": 0.5}},
                {"model_id": "m2", "predictions": {"ES": 2.5}},
            ],
        )

    assert (rejected["status"], rejected["failed"], rejected["all_succeeded"]) == (
        "rejected",
        2,
        False,
    )
    assert (partial["status"], partial["failed"], partial["all_succeeded"]) == ("partial", 1, False)


async def test_diagnostics_batch_with_only_failures_is_rejected(
    authed_settings: Settings, api: FakeApi, tmp_path: Path
) -> None:
    authed_settings.allow_writes = True

    async with session(authed_settings, api) as client:
        result = await call(
            client,
            "submit_diagnostics_batch",
            items=[{"model_id": "m1", "file_path": str(tmp_path / "missing.parquet")}],
        )

    assert (result["status"], result["failed"]) == ("rejected", 1)
    assert api.requests == []


async def test_batch_sends_inline_predictions_as_numbers(
    authed_settings: Settings, api: FakeApi
) -> None:
    authed_settings.allow_writes = True
    api.add(ep.FUTURES_SUBMIT_V2, {"submission_id": "sub-1"})

    async with session(authed_settings, api) as client:
        result = await call(
            client,
            "submit_futures_predictions_batch",
            exped="e1",
            items=[{"model_id": "m1", "predictions": {"ES": "0.25", "NQ": 1}}],
        )
        bad = await call(
            client,
            "submit_futures_predictions_batch",
            exped="e1",
            items=[{"model_id": "m1", "predictions": {"ES": True}}],
        )

    assert result["status"] == "ok"
    assert api.json_body(ep.FUTURES_SUBMIT_V2, 0)["predictions"] == [
        {"instrument_id": "ES", "prediction": 0.25},
        {"instrument_id": "NQ", "prediction": 1.0},
    ]
    assert bad["status"] == "rejected"
    assert "ES=True" in bad["items"][0]["error"]
    assert len(api.calls(ep.FUTURES_SUBMIT_V2)) == 1


async def test_compute_pricing_training_wait_cap_and_artifacts(
    authed_settings: Settings,
    api: FakeApi,
    clock: FakeClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    authed_settings.allow_writes = True

    api.add(ep.COMPUTE_MODELS, {"templates": ["lightgbm", "xgboost"]})
    api.add(ep.COMPUTE_CREDITS, {"balance": 25.0, "rates": {"CPU": 0.1}})
    api.add(ep.COMPUTE_JOBS, {"jobs": [{"job_id": "j1", "status": "completed"}]})
    api.add(ep.COMPUTE_JOB_LOGS, {"events": ["fold 1 complete"]})
    model_url = "https://cdn.everesteer.test/models/j1.pkl?sig=1"
    api.add(ep.COMPUTE_JOB_MODEL_URL, {"download_url": model_url})
    api.add_cdn(model_url, b"TRAINED_PKL")
    api.add(
        ep.COMPUTE_JOB_PREDICTIONS_URL, {"download_url": "https://cdn.everesteer.test/p.parquet"}
    )
    api.add(ep.COMPUTE_JOB_OUTPUT, b"OUTPUT_TXT")
    api.add(ep.COMPUTE_TRAIN, {"job_id": "j-new", "estimated_credits": 0.25, "dry_run": True})
    api.add(ep.COMPUTE_JOB, {"job_id": "j-new", "status": "running"})
    api.add(ep.COMPUTE_JOB_CANCEL, {"job_id": "j-new", "status": "cancelled"})

    async def accept(_ctx: Any, _params: Any) -> ElicitResult:
        return ElicitResult(action="accept", content={"confirm": True})

    # Simulate Claude Desktop client triggering the 45s wait cap cleanly.
    desktop = Implementation(name="claude-desktop", version="1.0")
    async with session(
        authed_settings, api, elicitation_callback=accept, client_info=desktop
    ) as client:
        assert (await call(client, "list_model_templates"))["templates"] == ["lightgbm", "xgboost"]
        assert (await call(client, "get_compute_credits"))["balance"] == 25.0
        assert (await call(client, "list_compute_jobs"))["jobs"][0]["job_id"] == "j1"
        assert (await call(client, "get_job_status", job_id="j1"))["status"] == "running"
        assert (await call(client, "get_job_log", job_id="j1"))["events"][0] == "fold 1 complete"

        urls = await call(client, "get_job_artifact_urls", job_id="j1")
        assert urls["model"]["download_url"] == model_url
        dl1 = await call(
            client,
            "download_job_artifact",
            job_id="j1",
            kind="model",
            output_path=str(tmp_path / "m.pkl"),
        )
        assert dl1["bytes_written"] == len(b"TRAINED_PKL")
        dl2 = await call(
            client,
            "download_job_artifact",
            job_id="j1",
            kind="output_file",
            filename="metrics.txt",
            output_path=str(tmp_path / "metrics.txt"),
        )
        assert dl2["bytes_written"] == len(b"OUTPUT_TXT")

        # Dry-run preview requires no confirmation (including T4 tier & deploy_to_model_id)
        preview = await call(client, "price_training_job", model="lightgbm", gpu="T4")
        assert preview["status"] == "preview"
        assert preview["dry_run"] is True
        dry_deploy = await call(
            client,
            "train_model",
            model="lightgbm",
            gpu="T4",
            deploy_to_model_id="m1",
            dry_run=True,
        )
        assert dry_deploy["dry_run"] is True

        # Wait capped at 45s returns clean `pending` status instead of hanging
        pending = await call(
            client, "train_model", model="lightgbm", gpu="CPU", wait=True, wait_timeout=1200.0
        )
        assert pending["status"] == "pending"
        assert sum(clock.sleeps) <= 45.0
        assert (await call(client, "cancel_compute_job", job_id="j-new"))["status"] == "ok"
