"""Read the maintained managed cleanup preparation review without admitting it."""

import json

from harness_jobs.identity import OperationRequest, payload_digest

from workspace_provisioning.artifacts import digest as document_digest
from workspace_provisioning.retirement_access_authority import (
    execution_steps,
    request_fields,
    request_revision,
)
from workspace_provisioning.retirement_access_plan import PHASE, access_identity
from workspace_provisioning.retirement_managed_access import ManagedRetirementAccessPlan
from workspace_provisioning.runtime_config import LifecycleRefused

from .demo1_browser import PREFIX, _response
from .demo1_evidence import EvidenceError, digest, identifier, text
from .demo1_report import reference


def require(condition):
    if not condition:
        raise EvidenceError(
            "retirement review: original managed target or plan differs"
        )


def validate_access_review(review, selected, envelope, original, source, now):
    try:
        require(isinstance(review, dict))
        approval = review["approval_request"]
        require(isinstance(approval, dict))
        parameters = approval["parameters"]
        require(
            isinstance(parameters, dict)
            and set(parameters) == request_fields(parameters)
            and all(
                isinstance(value, str) and len(value) <= 2048
                for value in parameters.values()
            )
        )
        plan = ManagedRetirementAccessPlan(**review["access_plan"])
        request_id, allocation = access_identity(
            selected.org_id,
            original.workspace_id,
            plan.original_allocation_id,
            original.retirement_request_id,
        )
        require(
            review["admission_available"] is True
            and review["phase"] == PHASE
            and review["retirement_request_id"] == original.retirement_request_id
            and review["request_id"] == request_id == plan.request_id
            and review["workspace_id"] == original.workspace_id == plan.workspace_id
            and plan.org_id == selected.org_id
            and plan.retirement_request_id == original.retirement_request_id
            and review["source_operation_id"] == source
            and review["allocation_id"] == allocation == plan.allocation_id
            and review["original_allocation_id"] == plan.original_allocation_id
            and review["inventory_sha256"] == plan.inventory_sha256
            and type(review["max_resource_units"]) is int
            and review["max_resource_units"] == 0
            and type(review["max_cost_micros"]) is int
            and review["max_cost_micros"] == 0
            and approval["workspace_id"] == original.workspace_id
            and approval["action"] == "provision"
            and approval["idempotency_key"] == request_id
        )
        require(
            plan.cluster_arn.startswith(
                f"arn:aws:eks:{selected.region}:{selected.account}:cluster/"
            )
            and plan.cluster_arn.rsplit("/", 1)[-1]
            and isinstance(plan.fence_recipe, dict)
            and set(plan.fence_recipe) == {"activate-retirement-fence"}
            and isinstance(plan.retained_grants, (list, tuple))
            and bool(plan.retained_grants)
        )
        fence = plan.fence_recipe["activate-retirement-fence"]
        require(
            fence["service"] == "kubernetes"
            and fence["method"] == "patch_validating_admission_policy"
            and fence["account_id"] == selected.account
            and fence["arguments"]["cluster_arn"] == plan.cluster_arn
            and all(
                isinstance(fence["arguments"][key], str) and fence["arguments"][key]
                for key in ("name", "policy_uid", "binding_uid", "generation")
            )
        )
        digest(fence["arguments"]["active_spec_sha256"], "retirement fence digest")
        recipe = plan.recipe()
        require(
            set(recipe) == {"cleaner-entry", "activate-retirement-fence"}
            and not plan.registrar_namespaces
            and list(plan.revocation_order) == ["cleaner-entry"]
            and recipe["cleaner-entry"]["arguments"]["kubernetesGroups"]
            == [plan.cleanup_group]
            and recipe["cleaner-entry"]["arguments"]["principalArn"].startswith(
                f"arn:aws:iam::{selected.account}:role/"
            )
        )
        expected = {
            "lifecycle_phase": PHASE,
            "lifecycle_artifact_id": plan.bootstrap_artifact_id,
            "retirement_request_id": original.retirement_request_id,
            "retirement_source_operation_id": source,
            "retirement_inventory_sha256": plan.inventory_sha256,
            "retirement_access_recipe_sha256": document_digest(recipe),
            "retirement_access_plan_sha256": plan.revision,
            "allocation_id": allocation,
            "original_allocation_id": plan.original_allocation_id,
            "aws_account_id": selected.account,
            "provider_account_id": selected.account,
            "provider": "aws",
            "region": selected.region,
            "runtime_config_sha256": plan.runtime_config_sha256,
            "max_resource_units": "0",
            "max_cost_micros": "0",
            "retirement_prepare_destroy": "v1",
        }
        require(all(parameters.get(key) == value for key, value in expected.items()))
        for key in (
            "lifecycle_artifact_id",
            "retirement_source_payload_digest",
            "retirement_inventory_sha256",
            "runtime_config_sha256",
            "lifecycle_policy_sha256",
        ):
            digest(parameters[key], "retirement source digest")
        for key in (
            "retirement_source_operation_id",
            "retirement_source_job_id",
            "retirement_source_attempt_id",
        ):
            text(parameters[key], "retirement source identity")
        lifecycle = json.loads(parameters["lifecycle_request"])
        inputs = json.loads(parameters["lifecycle_inputs"])
        require(
            isinstance(lifecycle, dict)
            and isinstance(inputs, dict)
            and all(
                lifecycle.get(key) == value
                for key, value in {
                    "mode": "existing-account-managed",
                    "target_account_id": selected.account,
                    "region": selected.region,
                    "workspace_id": original.workspace_id,
                }.items()
            )
            and inputs.get("isolation_mode") == "dedicated"
            and inputs.get("cluster_placement", "dedicated") == "dedicated"
            and 0
            < int(parameters["max_runtime_seconds"])
            <= min(
                envelope.max_runtime_seconds, (selected.deadline - now).total_seconds()
            )
            and parameters["plan_revision"] == request_revision(parameters)
            and parameters["execution_steps"]
            == execution_steps(parameters["retirement_access_recipe_sha256"])
            and review["revision"]
            == payload_digest(OperationRequest("provision", request_id, parameters))
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        IndexError,
        LifecycleRefused,
    ):
        raise EvidenceError(
            "retirement review: incomplete or incompatible producer response"
        ) from None
    return {
        "status": "BLOCKED",
        "review_status": "OBSERVED",
        "phase": PHASE,
        "request_ref": reference(request_id),
        "retirement_request_ref": reference(original.retirement_request_id),
        "source_operation_ref": reference(source),
        "revision_ref": reference(review["revision"]),
        "inventory_ref": reference(plan.inventory_sha256),
        "recipe_ref": reference(parameters["retirement_access_recipe_sha256"]),
        "observed_at": now.isoformat(),
        "admission_submitted": False,
        "reason": "preparation review only; independent approval and immutable grant not verified; fence, resource coverage, deletion and cost unproved",
    }


def workspace_source(selected, envelope, transport, original, browser, *, now):
    require(
        original is not None
        and original.submitted
        and original.request_id == selected.request_id
        and original.plan_revision == selected.plan_revision
        and selected.authorized_at <= now < selected.deadline
        and transport.origin == envelope.origin
        and browser.get("creation_observed") is True
    )
    progress = browser.get("lifecycle", {})
    bootstrap = progress.get("phases", {}).get("bootstrap-workspace", {})
    require(
        progress.get("workspace_ref") == reference(original.workspace_id)
        and progress.get("original_request_ref") == reference(selected.request_id)
        and bootstrap.get("state") == "succeeded"
        and bootstrap.get("status") == "OBSERVED"
    )
    prefix = PREFIX + f"/workspaces/{original.workspace_id}"
    workspace = _response(transport, "GET", prefix)
    source = identifier(
        workspace.get("provisioning_operation_id"), "retirement bootstrap"
    )
    require(
        workspace.get("id") == original.workspace_id
        and workspace.get("org_id") == selected.org_id
        and workspace.get("name") == selected.workspace_name
        and workspace.get("status") in ("Active", "active")
        and reference(source) == bootstrap.get("operation_ref")
    )
    return workspace, source


def read_access_review(selected, envelope, transport, original, browser, *, now):
    workspace, source = workspace_source(
        selected, envelope, transport, original, browser, now=now
    )
    prefix = PREFIX + f"/workspaces/{original.workspace_id}"
    review = _response(
        transport,
        "POST",
        prefix + "/retirement/access/preview",
        {"operation_id": original.retirement_request_id},
    )
    result = validate_access_review(review, selected, envelope, original, source, now)
    refreshed = _response(transport, "GET", prefix)
    require(
        all(
            refreshed.get(key) == workspace.get(key)
            for key in ("id", "org_id", "name", "status", "provisioning_operation_id")
        )
    )
    return review, result


def review_access(selected, envelope, transport, original, browser, *, now):
    return read_access_review(
        selected, envelope, transport, original, browser, now=now
    )[1]
