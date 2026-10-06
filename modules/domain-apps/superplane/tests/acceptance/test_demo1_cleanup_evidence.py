"""Pinned-runtime cleanup evidence integration using synthetic transport responses."""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_demo1_cleanup as cleanup_fixtures
from harness_jobs.identity import OperationRequest, payload_digest
from test_demo1_browser import identity

from superplane_acceptance.demo1_report import reference
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
    state = SimpleNamespace(
        change=lambda document: document, probes=[], succeeded=True, provider_calls=[]
    )
    state.review = teardown_document(cleanup)
    state.review_change = lambda document: document
    serve = cleanup.driver.page.service.request

    def request(method, path, body=None):
        if path.endswith("/retirement/preview"):
            cleanup.calls.append((method, path, body))
            assert method == "POST" and body == {
                "operation_id": cleanup.original.retirement_request_id
            }
            saved = json.loads((cleanup.driver.path / "cleanup.json").read_text())[
                "checkpoint"
            ]
            projection = {
                "version": 1,
                "status": "OBSERVED",
                "org_id": cleanup.driver.selected.org_id,
                "workspace_id": cleanup.original.workspace_id,
                "source_operation_id": saved["source_operation_id"],
                "retirement_request_id": saved["retirement_request_id"],
                "preparation_request_id": saved["request_id"],
                "preparation_revision": saved["revision"],
                "preparation_plan_revision": saved["plan_revision"],
                "preparation_approval_id": saved["approval_id"],
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
                        "managed_workload_inventory_sha256",
                        "destroy_sha256",
                        "plan_file_sha256",
                        "plan_json_sha256",
                        "backend_sha256",
                    )
                },
                "inventory_sha256": cleanup.review["inventory_sha256"],
                "retirement_revision_sha256": state.review["revision"],
                "retirement_plan_sha256": state.review["approval_request"][
                    "parameters"
                ]["plan_revision"],
            }
            result = json.loads(json.dumps(state.review))
            result["cleanup_preparation"] = state.change(projection)
            return 200, state.review_change(result)
        return serve(method, path, body)

    monkeypatch.setattr(cleanup.driver.page.service, "request", request)
    cleanup.receipt_change = lambda receipt: {
        **receipt,
        "state": "succeeded" if state.succeeded else "pending",
    }
    cleanup.run()
    cleanup.approved = True
    state.cleanup = cleanup
    return state


def test_authenticated_preparation_projection_preserves_distinct_approval_and_compiled_plan(
    observed,
):
    report = observed.cleanup.run()
    artifact = report["browser"]["cleanup_preparation"]["artifact"]
    assert artifact["status"] == "OBSERVED" and artifact["grant_count"] == 1
    assert artifact["artifact_ref"] == reference("a" * 64)
    assert artifact["retirement_revision_ref"] == reference(observed.review["revision"])
    assert artifact["current_eks_grants"]["status"] == "UNVERIFIED"
    assert artifact["current_kubernetes"]["status"] == "UNVERIFIED"
    assert observed.provider_calls == [] and observed.probes == []
    assert observed.cleanup.admissions == 1 and report["live_acceptance"] is False
    assert all(
        "_demo1_" not in str(call) for call in observed.cleanup.driver.producer.calls
    )
    assert not any(
        route.endswith(("/decision", "/retirement"))
        for _, route, _ in observed.cleanup.calls
    )


def test_pending_preparation_has_no_historical_proof(observed):
    observed.succeeded = False
    report = observed.cleanup.run()
    assert "artifact" not in report["browser"]["cleanup_preparation"]
    assert not any(
        path.endswith("/retirement/preview") for _, path, _ in observed.cleanup.calls
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("org_id", identity(99)),
        ("workspace_id", identity(99)),
        ("source_operation_id", identity(99)),
        ("preparation_request_id", identity(99)),
        ("preparation_approval_id", identity(99)),
        ("retirement_request_id", identity(99)),
        ("preparation_revision", "e" * 64),
        ("preparation_plan_revision", "e" * 64),
        ("retirement_revision_sha256", "e" * 64),
        ("retirement_plan_sha256", "e" * 64),
        ("operation_id", identity(99)),
        ("producer_fence_token", 0),
        ("grant_count", 0),
        ("grant_count", 129),
        ("artifact_id", "not-a-digest"),
        ("status", "READY"),
        ("version", 2),
        ("recorded_at", "2099-01-01T00:00:00Z"),
    ],
)
def test_changed_projection_refuses_without_deletion_or_checkpoint_drift(
    observed, field, value
):
    observed.cleanup.run()
    path = observed.cleanup.driver.path / "cleanup.json"
    before = path.read_bytes()
    observed.change = lambda proof: {**proof, field: value}
    report = observed.cleanup.run()
    assert report["status"] == "BLOCKED" and "cleanup" in report["reason"]
    assert path.read_bytes() == before and observed.cleanup.admissions == 1
    assert not any(
        route.endswith("/retirement") for _, route, _ in observed.cleanup.calls
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda _: None,
        lambda p: {k: v for k, v in p.items() if k != "preparation_approval_id"},
        lambda p: {**p, "grants": []},
    ],
)
def test_absent_or_unmodeled_projection_is_not_success(observed, change):
    observed.change = change
    report = observed.cleanup.run()
    assert report["status"] == "BLOCKED" and "retirement_review" not in report.get(
        "browser", {}
    )
