"""Public teardown review binding; synthetic requests cannot prove full cleanup."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import test_demo1_cleanup_evidence as fixtures
from harness_jobs.identity import OperationRequest, payload_digest
from test_demo1_browser import identity

from superplane_acceptance.demo1_evidence import EvidenceError
from superplane_acceptance.demo1_report import reference
from superplane_acceptance.demo1_teardown import validate_teardown_review
from workspace_provisioning.retirement_access_authority import request_revision

driver, prepared, cleanup, observed = (
    fixtures.driver,
    fixtures.prepared,
    fixtures.cleanup,
    fixtures.observed,
)


def test_review_is_matched_without_requesting_approval_or_submitting_deletion(observed):
    report = observed.cleanup.run()
    result = report["browser"]["retirement_review"]
    assert result["review_status"] == "OBSERVED"
    assert result["status"] == "BLOCKED" and result["admission_submitted"] is False
    assert result["revision_ref"] == reference(observed.review["revision"])
    assert "canonical recorded deletion plan matched" in result["reason"]
    assert "provider coverage" in result["reason"]
    assert report["live_acceptance"] is False
    path = observed.cleanup.driver.path / "cleanup.json"
    checkpoint = path.read_bytes()
    observed.cleanup.calls.clear()
    recovered = observed.cleanup.run()
    assert recovered["browser"]["retirement_review"]["review_status"] == "OBSERVED"
    assert path.read_bytes() == checkpoint and observed.cleanup.admissions == 1
    assert all(
        not route.endswith(("/decision", "/operation-approvals", "/retirement"))
        for _, route, _ in observed.cleanup.calls
    )
    assert all(
        value not in json.dumps(result)
        for value in (
            observed.cleanup.original.workspace_id,
            observed.cleanup.driver.selected.account,
            observed.cleanup.driver.selected.org_id,
            observed.cleanup.original.retirement_request_id,
        )
    )


@pytest.mark.parametrize(
    "changed",
    [
        "retirement_access_artifact_id",
        "terraform_plan_file_sha256",
        "terraform_backend_sha256",
        "managed_workload_inventory_sha256",
        "retirement_inventory_sha256",
        "control_allocation_id",
        "cleanup_allocation_ids",
        "original_allocation_id",
        "allocation_id",
        "retirement_source_operation_id",
        "retirement_source_job_id",
        "retirement_source_attempt_id",
        "retirement_source_payload_digest",
        "lifecycle_request",
        "lifecycle_inputs",
        "lifecycle_policy_sha256",
        "runtime_config_sha256",
        "credential_label",
        "extra",
        "max_resource_units",
        "max_cost_micros",
        "max_runtime_seconds",
    ],
)
def test_rehashed_foreign_request_cannot_replace_prepared_inputs(observed, changed):
    observed.cleanup.run()
    path = observed.cleanup.driver.path / "cleanup.json"
    before = path.read_bytes()

    def change(document):
        parameters = document["approval_request"]["parameters"]
        parameters[changed] = "foreign-value"
        parameters["plan_revision"] = request_revision(parameters)
        document["revision"] = payload_digest(
            OperationRequest("teardown", document["request_id"], parameters)
        )
        return document

    observed.review_change = change
    report = observed.cleanup.run()
    assert report["status"] == "BLOCKED" and any(
        label in report["reason"] for label in ("retirement review", "cleanup evidence")
    )
    assert "retirement_review" not in report.get("browser", {})
    assert path.read_bytes() == before and observed.cleanup.admissions == 1
    assert not any(
        route.endswith("/retirement") for _, route, _ in observed.cleanup.calls
    )


@pytest.mark.parametrize("change", ["extra", "reordered", "replaced"])
def test_self_consistent_step_list_must_match_canonical_compiled_request(
    observed, change
):
    def rehash(document):
        parameters = document["approval_request"]["parameters"]
        parameters["execution_steps"] = json.dumps(document["steps"])
        document["revision"] = payload_digest(
            OperationRequest("teardown", document["request_id"], parameters)
        )

    observed.review["steps"].insert(
        0,
        {
            "step_id": "block-admission",
            "provider": "superplane-governance",
            "operation_kind": "block-governed-admission",
            "target": "fixture-workspace",
        },
    )
    rehash(observed.review)
    observed.cleanup.run()
    path = observed.cleanup.driver.path / "cleanup.json"
    before = path.read_bytes()

    def changed(document):
        step = {
            "step_id": "foreign-delete",
            "provider": "superplane-aws",
            "operation_kind": "revoke-network-prerequisite",
            "target": "unrelated-resource",
        }
        if change == "extra":
            document["steps"].append(step)
        elif change == "reordered":
            document["steps"].reverse()
        else:
            document["steps"][0]["provider"] = "foreign-provider"
        rehash(document)
        return document

    observed.review_change = changed
    report = observed.cleanup.run()
    assert report["status"] == "BLOCKED" and any(
        label in report["reason"] for label in ("retirement review", "cleanup evidence")
    )
    assert "retirement_review" not in report.get("browser", {})
    assert path.read_bytes() == before and observed.cleanup.admissions == 1


@pytest.mark.parametrize(
    "changed",
    [
        "denied",
        "unavailable",
        "action",
        "workspace",
        "request",
        "account",
        "region",
        "source",
        "revision",
        "display",
        "destroy",
        "duplicate",
        "missing",
        "malformed",
    ],
)
def test_incompatible_or_misleading_public_review_is_refused(observed, changed):
    def change(document):
        if changed == "denied":
            raise ConnectionError("private denied response")
        if changed == "unavailable":
            document["admission_available"] = False
        elif changed == "action":
            document["approval_request"]["action"] = "provision"
        elif changed in (
            "workspace",
            "request",
            "account",
            "region",
            "source",
            "revision",
        ):
            key = {
                "workspace": "workspace_id",
                "request": "request_id",
                "account": "account_id",
                "source": "source_operation_id",
            }.get(changed, changed)
            document[key] = identity(99)
        elif changed == "display":
            document["steps"][0]["target"] = "different displayed target"
        elif changed in ("destroy", "duplicate", "missing", "malformed"):
            parameters = document["approval_request"]["parameters"]
            if changed == "destroy":
                document["steps"][0]["target"] = "foreign-target"
            elif changed == "duplicate":
                document["steps"].append(document["steps"][0])
            elif changed == "missing":
                document["steps"] = []
            parameters["execution_steps"] = (
                "null" if changed == "malformed" else json.dumps(document["steps"])
            )
            document["revision"] = payload_digest(
                OperationRequest("teardown", document["request_id"], parameters)
            )
        return document

    observed.review_change = change
    result = observed.cleanup.run()
    assert result["status"] == "BLOCKED" and result["live_acceptance"] is False
    assert "retirement_review" not in result.get("browser", {})
    assert "private denied response" not in json.dumps(result)


def test_changed_workspace_after_preview_is_not_reported_as_matched(
    observed, monkeypatch
):
    serve = observed.cleanup.driver.page.service.request
    read_preview = False

    def request(method, path, body=None):
        nonlocal read_preview
        status, result = serve(method, path, body)
        if path.endswith("/retirement/preview"):
            read_preview = True
        elif read_preview and path.endswith(observed.cleanup.original.workspace_id):
            result = {**result, "provisioning_operation_id": identity(99)}
        return status, result

    monkeypatch.setattr(observed.cleanup.driver.page.service, "request", request)
    report = observed.cleanup.run()
    assert any(
        label in report["reason"] for label in ("retirement review", "cleanup evidence")
    )
    assert "retirement_review" not in report.get("browser", {})


@pytest.mark.parametrize("change", ["expired", "runtime", "release", "future", "stale"])
def test_review_requires_current_envelope_and_runtime_bound_evidence(observed, change):
    report = observed.cleanup.run()
    artifact = report["browser"]["cleanup_preparation"]["artifact"]
    selected, envelope = (
        observed.cleanup.driver.selected,
        observed.cleanup.driver.envelope,
    )
    now = datetime.now(UTC)
    if change == "expired":
        now = selected.deadline
    elif change == "runtime":
        envelope = replace(envelope, max_runtime_seconds=1)
    elif change == "release":
        artifact["release_ref"] = reference("foreign-release")
    elif change == "future":
        artifact["observed_at"] = (now + timedelta(hours=1)).isoformat()
    elif change == "stale":
        artifact["recorded_at"] = (
            selected.authorized_at - timedelta(seconds=1)
        ).isoformat()
    with pytest.raises(EvidenceError):
        validate_teardown_review(
            observed.review,
            selected,
            envelope,
            observed.cleanup.original,
            artifact,
            now,
        )
