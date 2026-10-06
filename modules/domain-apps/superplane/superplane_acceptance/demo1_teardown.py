"""Match public removal review to recorded preparation without authorizing deletion."""

from dataclasses import asdict

from harness_jobs.identity import OperationRequest, payload_digest

from workspace_provisioning.execution_contract import parse_execution_steps
from workspace_provisioning.retirement_access_authority import request_revision
from workspace_provisioning.retirement_destroy_producer import DestroyPlanReference

from .demo1_browser import PREFIX, _response
from .demo1_evidence import EvidenceError, digest, instant
from .demo1_report import reference
from .demo1_retirement import workspace_source


def require(condition):
    if not condition:
        raise EvidenceError("retirement review: prepared destroy binding differs")


def validate_teardown_review(review, selected, envelope, original, artifact, now):
    try:
        require(
            selected.authorized_at <= now < selected.deadline
            and artifact["status"] == "OBSERVED"
            and artifact["release_ref"] == reference(envelope.runtime_target.release_id)
            and selected.authorized_at
            <= instant(artifact["recorded_at"], "cleanup artifact time")
            <= instant(artifact["observed_at"], "cleanup observation time")
            <= now
        )
        approval = review["approval_request"]
        parameters = approval["parameters"]
        require(
            isinstance(parameters, dict)
            and all(isinstance(value, str) for value in parameters.values())
            and approval["action"] == "teardown"
            and approval["workspace_id"] == original.workspace_id
            and approval["idempotency_key"] == original.retirement_request_id
            and review["request_id"] == original.retirement_request_id
            and review["workspace_id"] == original.workspace_id
            and review["account_id"] == selected.account
            and review["region"] == selected.region
            and review["admission_available"] is True
            and review["blocked_reason"] is None
            and parameters["lifecycle_phase"] == "retire-workspace"
            and parameters["retirement_request_id"] == original.retirement_request_id
            and parameters["aws_account_id"] == selected.account
            and parameters["provider_account_id"] == selected.account
            and parameters["region"] == selected.region
            and parameters["provider"] == "aws"
            and parameters["max_resource_units"] == "0"
            and parameters["max_cost_micros"] == "0"
            and 0
            < int(parameters["max_runtime_seconds"])
            <= min(
                envelope.max_runtime_seconds, (selected.deadline - now).total_seconds()
            )
            and parameters["plan_revision"] == request_revision(parameters)
            and reference(parameters["plan_revision"])
            == artifact["retirement_plan_ref"]
            and review["revision"]
            == payload_digest(
                OperationRequest("teardown", original.retirement_request_id, parameters)
            )
            and reference(review["revision"]) == artifact["retirement_revision_ref"]
        )
        for public, parameter in {
            "source_operation_id": "retirement_source_operation_id",
            "source_payload_digest": "retirement_source_payload_digest",
            "lifecycle_artifact_id": "lifecycle_artifact_id",
            "inventory_sha256": "retirement_inventory_sha256",
            "lifecycle_policy_sha256": "lifecycle_policy_sha256",
            "runtime_config_sha256": "runtime_config_sha256",
        }.items():
            require(review[public] == parameters[parameter])
        for parameter, recorded in {
            "retirement_access_artifact_id": "artifact_ref",
            "terraform_plan_file_sha256": "plan_file_ref",
            "terraform_backend_sha256": "backend_ref",
            "retirement_inventory_sha256": "inventory_ref",
            "managed_workload_inventory_sha256": "managed_workload_inventory_ref",
        }.items():
            digest(parameters[parameter], "retirement digest")
            require(reference(parameters[parameter]) == artifact[recorded])
        steps = parse_execution_steps(parameters["execution_steps"])
        destroy = DestroyPlanReference(
            parameters["original_allocation_id"],
            parameters["terraform_plan_file_sha256"],
            parameters["terraform_backend_sha256"],
            {},
        ).step()
        require(
            review["steps"] == [asdict(step) for step in steps]
            and [step for step in steps if step.step_id == destroy.step_id] == [destroy]
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise EvidenceError(
            "retirement review: incomplete or incompatible teardown"
        ) from None
    return {
        "status": "BLOCKED",
        "review_status": "OBSERVED",
        "request_ref": reference(original.retirement_request_id),
        "revision_ref": reference(review["revision"]),
        "artifact_ref": artifact["artifact_ref"],
        "plan_file_ref": artifact["plan_file_ref"],
        "observed_at": now.isoformat(),
        "admission_submitted": False,
        "reason": "canonical recorded deletion plan matched; current grants, fence, provider coverage, separate approval and cleanup unverified",
    }


def read_teardown_review(
    selected, envelope, transport, store, browser, artifact, *, clock, review=None
):
    saved, original = store.load(), store.original
    require(saved is not None and saved.submitted)
    workspace, source = workspace_source(
        selected, envelope, transport, original, browser, now=clock()
    )
    require(source == saved.source_operation_id)
    prefix = PREFIX + f"/workspaces/{original.workspace_id}"
    if review is None:
        review = _response(
            transport,
            "POST",
            prefix + "/retirement/preview",
            {
                "operation_id": original.retirement_request_id,
            },
        )
    refreshed = _response(transport, "GET", prefix)
    require(
        all(
            refreshed.get(key) == workspace.get(key)
            for key in ("id", "org_id", "name", "status", "provisioning_operation_id")
        )
        and store.load() == saved
        and review.get("source_operation_id") == source
    )
    return review, validate_teardown_review(
        review, selected, envelope, original, artifact, clock()
    )
