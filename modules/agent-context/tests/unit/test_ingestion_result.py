"""Real subprocess results survive queue routing, including partial ingestion."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

INGESTION = Path(__file__).resolve().parents[2] / "images" / "ingestion"
sys.path.insert(0, str(INGESTION))

from ingestion_result import RESULT_ENV, publish, read, summarize  # noqa: E402


@pytest.fixture
def worker(monkeypatch):
    import boto3

    monkeypatch.setattr(boto3, "client", MagicMock())
    monkeypatch.setattr(boto3, "resource", MagicMock())
    spec = importlib.util.spec_from_file_location("result_worker", INGESTION / "sqs-worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("status,steps,exit_code", [
    ("complete", {"clone": "verified", "deepwiki": "verified", "graphrag": "skipped"}, 0),
    ("partial", {"clone": "verified", "deepwiki": "failed", "graphrag": "skipped"}, 0),
    ("partial", {"s3_upload": "verified", "entities": "failed"}, 1),
    ("failed", {"fetch": "failed"}, 1),
])
def test_real_subprocess_keeps_actual_stage_results(worker, status, steps, exit_code):
    outcome = summarize(steps, status=status)
    program = (
        "import json, os, sys; from pathlib import Path; "
        f"Path(os.environ[{RESULT_ENV!r}]).write_text({json.dumps(outcome)!r}); "
        "print('arbitrary JSON-looking logs are not result authority'); "
        f"sys.exit({exit_code})"
    )
    assert worker._run_subprocess([sys.executable, "-c", program], 10) == outcome


@pytest.mark.parametrize("program", [
    "print('all done')",
    f"import os; open(os.environ[{RESULT_ENV!r}], 'w').write('{{}}')",
])
def test_zero_exit_without_valid_result_is_not_complete(worker, program):
    with pytest.raises(RuntimeError, match="no valid result"):
        worker._run_subprocess([sys.executable, "-c", program], 10)


def test_contradictory_success_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "result.json"
    monkeypatch.setenv(RESULT_ENV, str(path))
    publish({"version": 1, "status": "complete", "steps": {"deepwiki": "failed"}})
    with pytest.raises(ValueError, match="contradictory"):
        read(str(path))


@pytest.mark.parametrize("content_type,producer", [("repo", "ingest_repo"), ("url", "ingest_url"), ("doc", "ingest_doc"), ("infra", "discover_infra")])
@pytest.mark.parametrize("status", ["complete", "partial", "failed"])
def test_worker_callback_keeps_outcomes_and_acknowledges_only_usable_results(worker, monkeypatch, content_type, producer, status):
    steps = {"s3_upload": "verified", "deepwiki": "verified" if status == "complete" else "failed", "graphrag": "skipped"}
    outcome = summarize(steps, status=status, run_id="run-test")
    message = {
        "content_type": content_type, "source": "fixture", "scope": {"visibility": "shared"},
        "registry_asset_id": "asset-test", "callback_grant": "opaque-grant", "steps": ["invented"],
    }
    monkeypatch.setattr(worker, "SQS_QUEUE_URL", "https://sqs.example.test/queue")
    monkeypatch.setattr(worker, "receive_sqs_message", lambda: (message, "receipt"))
    monkeypatch.setattr(worker, "validate_source", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_mint_github_token", lambda: True)
    monkeypatch.setattr(worker, producer, lambda *a, **k: outcome)
    for name in ("update_dynamo_status", "write_dynamo_run_record", "emit_status_callback", "delete_sqs_message"):
        monkeypatch.setattr(worker, name, MagicMock())
    monkeypatch.setattr(worker, "safe_emit", lambda fn, *a, **k: fn(*a, **k))
    if status == "failed":
        with pytest.raises(SystemExit) as exit_info:
            worker.main()
        assert exit_info.value.code == 1
        worker.delete_sqs_message.assert_not_called()
    else:
        worker.main()
        worker.delete_sqs_message.assert_called_once_with("receipt")
    args, kwargs = worker.emit_status_callback.call_args
    assert args == ("asset-test", status)
    assert kwargs["status_detail"]["steps"] == steps
    assert kwargs["callback_grant"] == "opaque-grant"
    assert worker.write_dynamo_run_record.call_args.kwargs["steps"] == steps


def test_dynamo_does_not_turn_skipped_or_failed_stages_green(worker):
    worker.update_dynamo_status("org/repo", "repo", "partial", steps={"deepwiki": "failed", "graphrag": "skipped", "cgc_structural": "verified"})
    values = worker.dynamodb.Table.return_value.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":deepwiki_status"] == "failed"
    assert values[":graphrag_status"] == "skipped"
    assert values[":code_index_status"] == "complete"
    assert ":sbom_source_status" not in values
