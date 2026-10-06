"""Pinned-runtime cleanup evidence integration using synthetic transport responses."""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_demo1_cleanup as cleanup_fixtures
from harness_jobs.identity import OperationRequest, payload_digest
from test_demo1_browser import identity

from superplane_acceptance import demo1_journey
from superplane_acceptance.demo1_evidence import EvidenceError
from superplane_acceptance.demo1_report import reference
from superplane_acceptance.demo1_runtime import RuntimeReader
from workspace_provisioning.artifacts import canonical
from workspace_provisioning.execution_contract import encode_execution_steps
from workspace_provisioning.retirement_access_authority import request_revision
from workspace_provisioning.retirement_destroy_producer import DestroyPlanReference

driver = cleanup_fixtures.driver
prepared = cleanup_fixtures.prepared
cleanup = cleanup_fixtures.cleanup


def teardown_document(cleanup):
    preparation = cleanup.review["approval_request"]["parameters"]
    parameters = {
        key: value
        for key, value in preparation.items()
        if key
        not in {
            "retirement_access_recipe_sha256",
            "retirement_access_plan_sha256",
            "retirement_prepare_destroy",
            "plan_revision",
            "execution_steps",
        }
    }
    parameters.update(
        lifecycle_phase="retire-workspace",
        allocation_id=preparation["original_allocation_id"],
        control_allocation_id=preparation["allocation_id"],
        cleanup_allocation_ids=canonical(
            [preparation["allocation_id"], "bootstrap-allocation"]
        ),
        retirement_access_artifact_id="a" * 64,
        terraform_plan_file_sha256="b" * 64,
        terraform_backend_sha256="b" * 64,
        managed_workload_inventory_sha256="b" * 64,
    )
    parameters["plan_revision"] = request_revision(parameters)
    destroy = DestroyPlanReference(
        parameters["original_allocation_id"], "b" * 64, "b" * 64, {}
    ).step()
    parameters["execution_steps"] = encode_execution_steps([destroy])
    request = OperationRequest(
        "teardown", cleanup.original.retirement_request_id, parameters
    )
    return {
        "request_id": request.idempotency_key,
        "workspace_id": cleanup.original.workspace_id,
        "source_operation_id": preparation["retirement_source_operation_id"],
        "source_payload_digest": preparation["retirement_source_payload_digest"],
        "lifecycle_artifact_id": preparation["lifecycle_artifact_id"],
        "inventory_sha256": preparation["retirement_inventory_sha256"],
        "lifecycle_policy_sha256": preparation["lifecycle_policy_sha256"],
        "runtime_config_sha256": preparation["runtime_config_sha256"],
        "account_id": cleanup.driver.selected.account,
        "region": cleanup.driver.selected.region,
        "admission_available": True,
        "blocked_reason": None,
        "revision": payload_digest(request),
        "steps": [asdict(destroy)],
        "preserved": ["Account (closure is separately gated)"],
        "approval_request": {
            "workspace_id": cleanup.original.workspace_id,
            "action": request.action,
            "idempotency_key": request.idempotency_key,
            "parameters": parameters,
        },
    }


@pytest.fixture
def observed(cleanup, monkeypatch):
    state = SimpleNamespace(change=lambda document: document, probes=[], succeeded=True)
    state.review = teardown_document(cleanup)
    state.review_change = lambda document: document
    serve = cleanup.driver.page.service.request

    def request(method, path, body=None):
        if path.endswith("/retirement/preview"):
            cleanup.calls.append((method, path, body))
            assert method == "POST" and body == {
                "operation_id": cleanup.original.retirement_request_id
            }
            return 200, state.review_change(json.loads(json.dumps(state.review)))
        return serve(method, path, body)

    monkeypatch.setattr(cleanup.driver.page.service, "request", request)
    producer = cleanup.driver.producer

    def run(command, **options):
        if (
            "python" in command
            and "immutable managed cleanup preparation" in command[-2]
        ):
            assert command[13:20] == [
                "exec",
                "pod/superplane-api-example",
                "-c",
                "superplane-api",
                "--",
                "python",
                "-c",
            ]
            assert "readonly=True" in command[-2]
            assert producer.calls[-1][8:10] == ["sts", "get-caller-identity"]
            scope = json.loads(command[-1])
            state.probes.append(scope)
            result = {
                "status": "OBSERVED",
                "scope": scope,
                "artifact_id": "a" * 64,
                "operation_id": identity(81),
                "recorded_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                "producer_attempt_id": identity(83),
                "producer_fence_token": 1,
                "grant_count": 1,
                **{
                    key: "b" * 64
                    for key in (
                        "grant_set_sha256",
                        "fence_sha256",
                        "inventory_sha256",
                        "managed_workload_inventory_sha256",
                        "destroy_sha256",
                        "plan_file_sha256",
                        "plan_json_sha256",
                        "backend_sha256",
                    )
                },
                "inventory_sha256": cleanup.review["inventory_sha256"],
                "retirement_plan_sha256": state.review["approval_request"][
                    "parameters"
                ]["plan_revision"],
            }
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(state.change(result)), stderr=""
            )
        return producer(command, **options)

    monkeypatch.setattr(
        demo1_journey,
        "RuntimeReader",
        lambda selected, target: RuntimeReader(selected, target, runner=run),
    )
    cleanup.receipt_change = lambda receipt: {
        **receipt,
        "state": "succeeded" if state.succeeded else "pending",
    }
    cleanup.run()
    cleanup.approved = True
    state.cleanup = cleanup
    return state


def test_successful_preparation_reads_recorded_grants_and_destroy_hashes_only(observed):
    report = observed.cleanup.run()
    preparation = report["browser"]["cleanup_preparation"]
    artifact = preparation["artifact"]
    assert artifact["status"] == "OBSERVED" and artifact["grant_count"] == 1
    assert artifact["artifact_ref"] == reference("a" * 64)
    assert artifact["plan_file_ref"] == reference("b" * 64)
    assert (
        "current grants, fence, inventory, plan bytes and cleanup unverified"
        in artifact["scope"]
    )
    assert report["status"] == "BLOCKED" and report["live_acceptance"] is False
    assert (
        preparation["retirement_complete"] is False and observed.cleanup.admissions == 1
    )
    saved = json.loads((observed.cleanup.driver.path / "cleanup.json").read_text())[
        "checkpoint"
    ]
    scope = observed.probes[0]
    assert scope["approval_id"] == saved["approval_id"]
    assert scope["preparation_revision"] == saved["revision"]
    assert scope["retirement_request_id"] == saved["retirement_request_id"]
    assert scope["request_id"] == observed.cleanup.driver.selected.request_id
    assert all(
        value not in json.dumps(artifact)
        for value in (
            scope["org_id"],
            scope["workspace_id"],
            scope["account"],
            scope["approval_id"],
        )
    )
    assert not any(
        path.endswith(("/decision", "/retirement"))
        for _, path, _ in observed.cleanup.calls
    )


def test_pending_preparation_does_not_read_an_artifact(observed):
    observed.succeeded = False
    result = observed.cleanup.run()["browser"]["cleanup_preparation"]
    assert "artifact" not in result and observed.probes == []


@pytest.mark.parametrize(
    "change",
    [
        "blocked",
        "scope",
        "operation",
        "future",
        "digest",
        "extra",
        "grant_count",
        "attempt",
        "fence_token",
    ],
)
def test_changed_or_incomplete_cleanup_evidence_never_becomes_acceptance(
    observed, change
):
    observed.cleanup.run()
    path = observed.cleanup.driver.path / "cleanup.json"
    before = path.read_bytes()

    def changed(document):
        if change == "blocked":
            return {"status": "BLOCKED"}
        if change == "scope":
            document["scope"]["preparation_revision"] = "f" * 64
        elif change == "operation":
            document["operation_id"] = identity(99)
        elif change == "future":
            document["recorded_at"] = (
                datetime.now(UTC) + timedelta(days=1)
            ).isoformat()
        elif change == "digest":
            document["plan_file_sha256"] = "not-a-digest"
        elif change == "extra":
            document["credential"] = "must-not-be-exported"
        elif change == "grant_count":
            document["grant_count"] = 0
        elif change == "attempt":
            document["producer_attempt_id"] = "not-an-identity"
        elif change == "fence_token":
            document["producer_fence_token"] = True
        return document

    observed.change = changed
    report = observed.cleanup.run()
    assert report["status"] == "BLOCKED" and not report["live_acceptance"]
    assert "cleanup" in report["reason"]
    assert "artifact" not in report.get("browser", {}).get("cleanup_preparation", {})
    assert path.read_bytes() == before and observed.cleanup.admissions == 1


def test_cleanup_observation_cannot_combine_other_runtime_scopes(observed):
    reader = RuntimeReader(
        observed.cleanup.driver.selected,
        observed.cleanup.driver.envelope.runtime_target,
        runner=lambda *args, **kwargs: pytest.fail("no remote calls permitted"),
    )
    with pytest.raises(EvidenceError):
        reader.observe(30, cleanup_scope={}, ownership_scope={})
    with pytest.raises(EvidenceError):
        reader.observe(30, cleanup_scope={}, lineage_scope={})
