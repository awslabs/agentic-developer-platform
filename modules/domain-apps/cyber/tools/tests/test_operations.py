"""Task cyber authorization and irreversible-send claims against moto DynamoDB."""

# ruff: noqa: F811
import dataclasses
import hashlib
import io
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from cyber_tools import operations as module
from adp_tools.storage import serialize as _serialize
from datetime import datetime, UTC, timedelta
from dataclasses import dataclass
import boto3
from moto import mock_aws
from adp_tools.storage import OperationRepository

NOW = datetime(2026, 9, 25, tzinfo=UTC)


@dataclass(frozen=True)
class Identity:
    task_id: str
    invocation_id: str
    generation: int
    runtime_attempt_id: str
    tenant: str = "tenant-a"
    canonical_principal: str = "principal-a"


class TestRepository(OperationRepository):
    __test__ = False

    def read_task(self, task_id):
        return self._get("TASK#" + task_id, "META")


@pytest.fixture
def runtime():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="cyber-operations",
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        identity = Identity(
            "tsk_" + str(uuid.uuid4()), str(uuid.uuid4()), 1, str(uuid.uuid4())
        )
        repo = TestRepository(client, "cyber-operations", None)
        task = {
            "event_id": "TASK#" + identity.task_id,
            "arrived_at": "META",
            "task_id": identity.task_id,
            "invocation_id": identity.invocation_id,
            "runtime_attempt_id": identity.runtime_attempt_id,
            "generation": 1,
            "version": 1,
            "scope": {
                "tenant": identity.tenant,
                "canonical_principal": identity.canonical_principal,
            },
            "state": "running",
            "deadline_at": (NOW + timedelta(minutes=30)).isoformat(),
        }
        client.put_item(TableName=repo.table_name, Item=_serialize(task))
        yield (SimpleNamespace(repository=repo), identity)


def _attempt_identity(runtime):
    return runtime[1]


def update(repo, identity, **fields):
    row = repo.read_task(identity.task_id)
    row.update(fields)
    repo._client.put_item(TableName=repo.table_name, Item=_serialize(row))


@pytest.fixture
def cyber(runtime, monkeypatch):
    identity = _attempt_identity(runtime)
    repo = runtime[0].repository
    uri = f"s3://samples/o/{identity.tenant}/t/task-service/u/sp-{identity.canonical_principal}/s/session/task/in/sample.bin"
    sample = b"non-executable fixture bytes"
    digest = hashlib.sha256(sample).hexdigest()
    update(
        repo,
        identity,
        persona="agent-task-cyber",
        input_payload={
            "inputs": {
                "sample_s3_uri": uri,
                "sha256": digest,
                "url": "https://example.com",
            }
        },
    )
    monkeypatch.setattr(module.time, "time", lambda: NOW.timestamp())
    s3 = SimpleNamespace(
        head_object=Mock(
            return_value={
                "VersionId": "immutable-version",
                "ContentLength": len(sample),
            }
        ),
        get_object=Mock(side_effect=lambda **kw: {"Body": io.BytesIO(sample)}),
    )
    backend = SimpleNamespace(
        _client=Mock(return_value=s3),
        submit=Mock(
            side_effect=lambda kind, job_id, sample, options, deadline: {
                "status": "queued",
                "job_id": job_id,
                "_job": {
                    "job_id": job_id,
                    "kind": kind,
                    "sample": sample,
                    "deadline_epoch": deadline,
                },
            }
        ),
        result=Mock(
            side_effect=lambda job: {
                "status": "completed",
                "job_id": job["job_id"],
                "findings": {"supported": True},
            }
        ),
        cancel=Mock(return_value={"status": "unknown"}),
        enrich=Mock(return_value={"status": "completed", "findings": {}}),
        url_analysis=Mock(return_value={"status": "completed", "findings": {}}),
    )
    artifacts = []

    def put(**kwargs):
        artifacts.append(kwargs)
        return SimpleNamespace(
            artifact_id="art_" + str(uuid.uuid4()),
            content_type=kwargs["content_type"],
            content_sha256=kwargs["digest"],
        )

    evidence = SimpleNamespace(put_run_artifact=Mock(side_effect=put))
    service = module.CyberOperations(
        repo, evidence, backend, {"CYBER_SAMPLE_BUCKET": "samples"}
    )
    return SimpleNamespace(
        service=service,
        identity=identity,
        repo=repo,
        uri=uri,
        digest=digest,
        s3=s3,
        backend=backend,
        artifacts=artifacts,
        evidence=evidence,
    )


def execute(cyber, operation="triage", payload=None, operation_id=None):
    return cyber.service.execute(
        cyber.identity,
        operation_id or str(uuid.uuid4()),
        operation,
        {"sample_s3_uri": cyber.uri} if payload is None else payload,
    )


def test_bound_sample_version_artifact_and_replay(cyber):
    request_id = str(uuid.uuid4())
    first = execute(cyber, operation_id=request_id)
    second = execute(cyber, operation_id=str(uuid.uuid4()))
    assert first["operation_status"] == second["operation_status"] == "confirmed"
    assert first["artifact"] == second["artifact"]
    cyber.backend.submit.assert_called_once()
    cyber.s3.get_object.assert_called_once_with(
        Bucket="samples",
        Key=cyber.uri.removeprefix("s3://samples/"),
        VersionId="immutable-version",
    )
    artifact = cyber.artifacts[0]
    assert (
        hashlib.sha256(artifact["content"]).hexdigest()
        == first["artifact"]["content_sha256"]
    )
    assert artifact["attempt"] == cyber.identity
    rows = cyber.service.rows(cyber.identity, "CYBER_")
    assert rows and all(row["record_type"] == "TASK_OPS" for row in rows)
    for row in rows:
        assert (
            not {
                "tenant_id",
                "user_id",
                "correlation_id",
                "root_human_id",
                "engine_command_status",
            }
            & row.keys()
        )
        assert row["scope"] == {
            "tenant": cyber.identity.tenant,
            "canonical_principal": cyber.identity.canonical_principal,
        }


@pytest.mark.parametrize(
    "change",
    [
        {"runtime_attempt_id": str(uuid.uuid4())},
        {"generation": 99},
        {"persona": "agent-task-investigator"},
        {"state": "cancel_requested"},
        {"deadline_at": "2020-01-01T00:00:00Z"},
    ],
)
def test_stale_or_cancelled_task_never_calls_backend(cyber, change):
    update(cyber.repo, cyber.identity, **change)
    with pytest.raises(HTTPException):
        execute(cyber)
    cyber.backend.submit.assert_not_called()


def test_foreign_identity_cannot_read_task_or_jobs(cyber):
    first = execute(cyber)
    other = dataclasses.replace(cyber.identity, canonical_principal="foreign")
    with pytest.raises(HTTPException):
        cyber.service.execute(
            other, str(uuid.uuid4()), "result", {"job_id": first["result"]["job_id"]}
        )


@pytest.mark.parametrize("fault", ["hash", "unversioned", "uri", "namespace"])
def test_sample_refusal_before_any_submission(cyber, fault):
    payload = {"sample_s3_uri": cyber.uri}
    if fault == "hash":
        update(
            cyber.repo,
            cyber.identity,
            input_payload={"inputs": {"sample_s3_uri": cyber.uri, "sha256": "0" * 64}},
        )
    elif fault == "unversioned":
        cyber.s3.head_object.return_value["VersionId"] = "null"
    elif fault == "uri":
        payload["sample_s3_uri"] += ".other"
    else:
        payload["sample_s3_uri"] = cyber.uri.replace("/u/sp-", "/u/foreign-")
        update(cyber.repo, cyber.identity, input_payload={"inputs": payload})
    with pytest.raises(HTTPException):
        execute(cyber, payload=payload)
    cyber.backend.submit.assert_not_called()


def test_lost_submit_receipt_does_not_repeat_external_send(cyber):
    cyber.backend.submit.side_effect = TimeoutError("receipt lost after external send")
    with pytest.raises(TimeoutError):
        execute(cyber)
    retry = execute(cyber)
    assert retry["operation_status"] == "unknown"
    cyber.backend.submit.assert_called_once()


def test_lost_artifact_receipt_does_not_repeat_external_send(cyber):
    cyber.evidence.put_run_artifact.side_effect = TimeoutError("artifact receipt lost")
    with pytest.raises(TimeoutError):
        execute(cyber)
    assert execute(cyber)["operation_status"] == "unknown"
    cyber.backend.submit.assert_called_once()


def test_same_request_id_cannot_change_operation_payload(cyber):
    request_id = str(uuid.uuid4())
    execute(cyber, operation_id=request_id)
    with pytest.raises(HTTPException) as error:
        execute(cyber, operation="static", operation_id=request_id)
    assert error.value.status_code == 409
    cyber.backend.submit.assert_called_once()


def test_cleanup_cannot_confirm_between_claim_and_job_registration(cyber):
    receipts = []

    def admission_barrier():
        receipts.append(cyber.service.cleanup(cyber.identity, str(uuid.uuid4())))
        return cyber.identity

    cyber.service.revalidate = admission_barrier
    with pytest.raises(HTTPException):
        execute(cyber)
    cyber.backend.submit.assert_not_called()
    assert receipts and receipts[0]["operation_status"] in {"pending", "unknown"}


def test_policy_revalidation_refusal_never_submits(cyber):
    def revoked():
        raise HTTPException(403, "policy revoked")

    cyber.service.revalidate = revoked
    with pytest.raises(HTTPException):
        execute(cyber)
    cyber.backend.submit.assert_not_called()


def test_cancel_during_atomic_claim_never_submits(cyber):
    original = cyber.repo._client.transact_write_items

    def commit_then_cancel(**kwargs):
        receipt = original(**kwargs)
        update(cyber.repo, cyber.identity, state="cancel_requested")
        return receipt

    cyber.repo._client.transact_write_items = commit_then_cancel
    with pytest.raises(HTTPException):
        execute(cyber)
    cyber.backend.submit.assert_not_called()


@pytest.mark.parametrize(
    "status, expected",
    [
        ("completed", "confirmed"),
        ("pending", "pending"),
        ("partial", "pending"),
        ("unknown", "pending"),
    ],
)
def test_cleanup_never_equates_unknown_jobs_with_stopped(cyber, status, expected):
    execute(cyber)
    cyber.backend.result.side_effect = lambda job: {"status": status}
    update(cyber.repo, cyber.identity, state="cancel_requested")
    receipt = execute(cyber, operation="cancel_jobs", payload={})
    assert receipt["operation_status"] == expected
    assert bool(receipt["result"]["pending_jobs"]) == (expected == "pending")


def test_stages_share_first_pinned_sample_even_if_bucket_head_changes(cyber):
    execute(cyber)
    cyber.s3.head_object.return_value["VersionId"] = "new-untrusted-version"
    cyber.s3.get_object.side_effect = AssertionError("must reuse first pinned sample")
    execute(cyber, operation="static")
    assert cyber.backend.submit.call_count == 2
    samples = [call.args[2] for call in cyber.backend.submit.call_args_list]
    assert samples[0] == samples[1]
    assert samples[1]["version"] == "immutable-version"
    assert type(samples[1]["size"]) is int
    cyber.s3.head_object.assert_called_once()


def test_simultaneous_duplicate_cannot_send_twice(cyber):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    original = cyber.backend.submit.side_effect

    def submit(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    cyber.backend.submit.side_effect = submit
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, cyber)
        assert entered.wait(5)
        try:
            second = execute(cyber)
            assert second["operation_status"] == "unknown"
        finally:
            release.set()
        assert first.result()["operation_status"] == "confirmed"
    cyber.backend.submit.assert_called_once()


def test_cap_denial_rolls_back_new_operation_and_identity(cyber):
    update(cyber.repo, cyber.identity, cyber_operation_count=128)
    with pytest.raises(HTTPException) as error:
        execute(cyber)
    assert error.value.status_code == 409
    assert not cyber.service.rows(cyber.identity, "CYBER_OP#")
    assert not cyber.service.rows(cyber.identity, "CYBER_ID#")
    cyber.backend.submit.assert_not_called()


def test_replay_aliases_consume_bounded_capacity_atomically(cyber):
    original_id = str(uuid.uuid4())
    execute(cyber, operation_id=original_id)
    update(cyber.repo, cyber.identity, cyber_operation_count=127)
    accepted_id = str(uuid.uuid4())
    execute(cyber, operation_id=accepted_id)
    assert cyber.repo.read_task(cyber.identity.task_id)["cyber_operation_count"] == 128
    before = cyber.service.rows(cyber.identity, "CYBER_ID#")
    with pytest.raises(HTTPException) as error:
        execute(cyber)
    assert error.value.status_code == 409
    assert cyber.service.rows(cyber.identity, "CYBER_ID#") == before
    assert execute(cyber, operation_id=original_id)["operation_status"] == "confirmed"
    assert execute(cyber, operation_id=accepted_id)["operation_status"] == "confirmed"
    assert cyber.repo.read_task(cyber.identity.task_id)["cyber_operation_count"] == 128
    cyber.backend.submit.assert_called_once()


def test_cleanup_closes_attempt_even_when_jobs_are_pending(cyber):
    first = execute(cyber)
    cyber.backend.result.side_effect = lambda job: {"status": "pending"}
    assert (
        execute(cyber, operation="cancel_jobs", payload={})["operation_status"]
        == "pending"
    )
    assert (
        cyber.repo.read_task(cyber.identity.task_id)["cyber_closed_attempt"]
        == cyber.identity.runtime_attempt_id
    )
    for operation, payload in [
        ("static", {"sample_s3_uri": cyber.uri}),
        ("result", {"job_id": first["result"]["job_id"]}),
    ]:
        with pytest.raises(HTTPException) as error:
            execute(cyber, operation=operation, payload=payload)
        assert error.value.status_code == 409
    cyber.backend.submit.assert_called_once()
    cyber.backend.result.side_effect = lambda job: {"status": "completed"}
    assert (
        execute(cyber, operation="cancel_jobs", payload={})["operation_status"]
        == "confirmed"
    )


def test_cleanup_fence_wins_before_new_claim_transaction(cyber):
    original = cyber.repo._client.transact_write_items

    def cleanup_then_commit(**kwargs):
        receipt = cyber.service.cleanup(cyber.identity, str(uuid.uuid4()))
        assert receipt["operation_status"] == "confirmed"
        return original(**kwargs)

    cyber.repo._client.transact_write_items = cleanup_then_commit
    with pytest.raises(HTTPException) as error:
        execute(cyber)
    assert error.value.status_code == 409
    assert not cyber.service.rows(cyber.identity, "CYBER_OP#")
    assert not cyber.service.rows(cyber.identity, "CYBER_ID#")
    cyber.backend.submit.assert_not_called()


def test_lost_submit_receipt_can_settle_only_with_positive_job_stop_evidence(cyber):
    cyber.backend.submit.side_effect = TimeoutError("provider receipt lost")
    with pytest.raises(TimeoutError):
        execute(cyber)
    cyber.backend.result.side_effect = lambda job: {"status": "partial"}
    assert (
        execute(cyber, operation="cancel_jobs", payload={})["operation_status"]
        == "pending"
    )
    cyber.backend.result.side_effect = lambda job: {
        "status": "partial",
        "execution_status": "completed",
    }
    assert (
        execute(cyber, operation="cancel_jobs", payload={})["operation_status"]
        == "confirmed"
    )


def test_cached_sample_pin_passes_real_backend_manifest_serialization(cyber):
    import json

    from cyber_tools.backends import CyberBackends

    execute(cyber)
    cyber.s3.head_object.side_effect = AssertionError(
        "cached stage must not read mutable head"
    )
    cyber.s3.get_object.side_effect = AssertionError(
        "cached stage must reuse verified pin"
    )
    cyber.s3.generate_presigned_url = Mock(
        return_value="https://sample.invalid/version-pinned"
    )
    sqs = SimpleNamespace(
        send_message=Mock(return_value={"MessageId": "fixture-message"})
    )
    cyber.service.backend = CyberBackends(
        env={"CYBER_STATIC_QUEUE": "https://queue.invalid/static.fifo"},
        clients={"s3": cyber.s3, "sqs": sqs},
        clock=lambda: NOW.timestamp(),
    )
    receipt = execute(cyber, operation="static")
    assert receipt["operation_status"] == "confirmed"
    assert receipt["result"]["status"] == "pending"
    sqs.send_message.assert_called_once()
    arguments = sqs.send_message.call_args.kwargs
    manifest = json.loads(arguments["MessageBody"])
    sample = manifest["sample_download"]
    assert type(sample["size"]) is int and sample["size"] == len(
        b"non-executable fixture bytes"
    )
    assert sample["sha256"] == cyber.digest
    assert sample["version"] == "immutable-version"
    assert (
        arguments["MessageGroupId"]
        == arguments["MessageDeduplicationId"]
        == receipt["result"]["job_id"]
    )
    assert (
        cyber.s3.generate_presigned_url.call_args.kwargs["Params"]["VersionId"]
        == "immutable-version"
    )


def test_cleanup_retains_terminal_progress_and_bounds_work_per_invocation(cyber):
    first = execute(cyber)
    second = execute(cyber, operation="static")
    remaining = iter([20000, 1000])
    cyber.service.remaining_ms = lambda: next(remaining)
    receipt = execute(cyber, operation="cancel_jobs", payload={})
    assert receipt["operation_status"] == "pending"
    assert cyber.backend.result.call_count == 1
    cyber.service.remaining_ms = lambda: 20000
    receipt = execute(cyber, operation="cancel_jobs", payload={})
    assert receipt["operation_status"] == "confirmed"
    assert cyber.backend.result.call_count == 2
    polled = {call.args[0]["job_id"] for call in cyber.backend.result.call_args_list}
    assert polled == {first["result"]["job_id"], second["result"]["job_id"]}
