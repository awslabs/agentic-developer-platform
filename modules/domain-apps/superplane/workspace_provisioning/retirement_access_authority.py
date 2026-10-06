"""Pure admission checks for a separately approved cleanup-access operation.

This validates the immutable request and fresh deployment policy only. Runtime
composition must additionally reread original successful bootstrap provenance,
the historical artifact, canonical ownership and the exact recompiled recipe
before any provider effect. It must never reuse the bootstrap execution grant.
"""

import json
import re

from harness_jobs.allocation import allocation_id_for
from harness_jobs.identity import OperationRequest, decode_payload, payload_digest

from .artifacts import digest
from .authority import load_policy
from .execution_contract import ExecutionStep, encode_execution_steps
from .lifecycle_policy import policy_digest, policy_document
from .retirement_access_plan import PHASE, access_identity
from .retirement_managed_access import ManagedRetirementAccessPlan
from .runtime_config import LifecycleRefused, validate_runtime_config

FIELDS = frozenset(
    {
        "lifecycle_phase",
        "lifecycle_request",
        "lifecycle_inputs",
        "lifecycle_artifact_id",
        "retirement_request_id",
        "retirement_source_operation_id",
        "retirement_source_job_id",
        "retirement_source_attempt_id",
        "retirement_source_payload_digest",
        "retirement_inventory_sha256",
        "retirement_access_recipe_sha256",
        "retirement_access_plan_sha256",
        "original_allocation_id",
        "allocation_id",
        "aws_account_id",
        "provider_account_id",
        "provider",
        "region",
        "runtime_config_sha256",
        "lifecycle_policy_sha256",
        "max_resource_units",
        "max_cost_micros",
        "max_runtime_seconds",
        "credential_id",
        "credential_service",
        "credential_label",
        "plan_revision",
        "execution_steps",
    }
)


def request_revision(parameters):
    return digest(
        {
            key: value
            for key, value in parameters.items()
            if key not in {"plan_revision", "execution_steps"}
        }
    )


def execution_steps(recipe_sha256):
    return encode_execution_steps(
        [ExecutionStep(PHASE, "superplane-lifecycle", PHASE, recipe_sha256)]
    )


def access_request(plan, source, deployment_policy, *, allocation_source=None):
    """Build from server-read bootstrap metadata and the service-compiled recipe."""
    policy = policy_document(deployment_policy)
    original = decode_payload(source.request_payload)
    managed = isinstance(plan, ManagedRetirementAccessPlan)
    paid_source = allocation_source if managed else source
    if (
        source.state != "succeeded"
        or original.action != "provision"
        or original.parameters.get("lifecycle_phase") != "bootstrap-workspace"
        or payload_digest(original) != source.plan_digest
        or paid_source is None
        or allocation_id_for(paid_source) != plan.original_allocation_id
        or (
            managed
            and (
                paid_source.state != "succeeded"
                or paid_source.operation_id
                != original.parameters.get("lifecycle_source_operation_id")
                or paid_source.org_id != source.org_id
                or paid_source.workspace_id != source.workspace_id
                or paid_source.admitted_request().parameters.get("lifecycle_phase")
                != "apply-infrastructure"
            )
        )
        or source.org_id != plan.org_id
        or source.workspace_id != plan.workspace_id
    ):
        raise LifecycleRefused(
            "cleanup access requires its original completed bootstrap"
        )
    request = json.loads(original.parameters["lifecycle_request"])
    account, region = plan.cluster_arn.split(":")[4], plan.cluster_arn.split(":")[3]
    expected_mode = "existing-account-managed" if managed else "bring-existing-cluster"
    if (
        request.get("mode") != expected_mode
        or request.get("region") != region
        or request.get("target_account_id") != account
        or request.get("workspace_id") != plan.workspace_id
        or original.parameters.get("aws_account_id") != account
        or (
            managed
            and (
                original.parameters.get("lifecycle_artifact_id")
                != plan.bootstrap_artifact_id
                or json.loads(original.parameters["lifecycle_inputs"]).get(
                    "isolation_mode"
                )
                != "dedicated"
            )
        )
    ):
        raise LifecycleRefused("cleanup access cannot change original ownership")
    reference = policy["credential_references"].get(account)
    if reference is None:
        raise LifecycleRefused("cleanup access credential reference is unavailable")
    parameters = {
        "lifecycle_phase": PHASE,
        "lifecycle_request": original.parameters["lifecycle_request"],
        "lifecycle_inputs": original.parameters["lifecycle_inputs"],
        "lifecycle_artifact_id": original.parameters["lifecycle_artifact_id"],
        "retirement_request_id": plan.retirement_request_id,
        "retirement_source_operation_id": source.operation_id,
        "retirement_source_job_id": source.job_id,
        "retirement_source_attempt_id": source.attempt_id,
        "retirement_source_payload_digest": source.plan_digest,
        "retirement_inventory_sha256": plan.inventory_sha256,
        "retirement_access_recipe_sha256": digest(plan.recipe()),
        "retirement_access_plan_sha256": plan.revision,
        "allocation_id": plan.allocation_id,
        "original_allocation_id": plan.original_allocation_id,
        "aws_account_id": account,
        "provider_account_id": account,
        "provider": "aws",
        "region": region,
        "runtime_config_sha256": plan.runtime_config_sha256,
        "lifecycle_policy_sha256": policy_digest(policy),
        "max_resource_units": "0",
        "max_cost_micros": "0",
        "max_runtime_seconds": str(policy["operation_max_runtime_seconds"]),
        **reference,
    }
    parameters["plan_revision"] = request_revision(parameters)
    parameters["execution_steps"] = execution_steps(
        parameters["retirement_access_recipe_sha256"]
    )
    result = OperationRequest(
        action="provision", idempotency_key=plan.request_id, parameters=parameters
    )
    validate_request(
        result, org_id=plan.org_id, workspace_id=plan.workspace_id, policy=policy
    )
    return result


def validate_request(request, *, org_id, workspace_id, policy):
    """No live provider/credential/execution calls; safe for no-call recovery."""
    parameters = request.parameters
    if (
        request.action != "provision"
        or set(parameters) != FIELDS
        or parameters.get("lifecycle_phase") != PHASE
        or any(
            not isinstance(value, str) or len(value) > 2048
            for value in parameters.values()
        )
    ):
        raise LifecycleRefused(
            "cleanup access request has an unsupported action or field"
        )
    policy = policy_document(policy)
    config = validate_runtime_config(policy["runtime"])
    if parameters["lifecycle_policy_sha256"] != policy_digest(policy) or parameters[
        "runtime_config_sha256"
    ] != digest(config):
        raise LifecycleRefused("approved cleanup access policy or runtime changed")
    account = parameters["aws_account_id"]
    original = json.loads(parameters["lifecycle_request"])
    mode = original.get("mode")
    permission = {
        "existing-account-managed": "managed",
        "bring-existing-cluster": "adopt",
    }.get(mode)
    if (
        parameters["provider"] != "aws"
        or parameters["provider_account_id"] != account
        or account not in policy["permitted_target_accounts"]
        or parameters["region"] not in policy["permitted_regions"]
        or permission is None
        or permission not in policy["permitted_modes"]
    ):
        raise LifecycleRefused("cleanup access target is outside current policy")
    reference = policy["credential_references"].get(account)
    if reference is None or any(
        parameters.get(key) != value for key, value in reference.items()
    ):
        raise LifecycleRefused("cleanup access credential reference changed")
    if (
        parameters["max_resource_units"] != "0"
        or parameters["max_cost_micros"] != "0"
        or parameters["max_runtime_seconds"]
        != str(policy["operation_max_runtime_seconds"])
    ):
        raise LifecycleRefused("cleanup access requires a zero-spend bounded operation")
    identity, allocation = access_identity(
        org_id,
        workspace_id,
        parameters["original_allocation_id"],
        parameters["retirement_request_id"],
    )
    if request.idempotency_key != identity or parameters["allocation_id"] != allocation:
        raise LifecycleRefused(
            "cleanup access must use its distinct derived allocation and request"
        )
    if any(
        not re.fullmatch(r"[a-f0-9]{64}", parameters[key])
        for key in (
            "lifecycle_artifact_id",
            "retirement_source_payload_digest",
            "retirement_inventory_sha256",
            "retirement_access_recipe_sha256",
            "retirement_access_plan_sha256",
        )
    ) or any(
        not parameters[key].strip()
        for key in (
            "retirement_source_operation_id",
            "retirement_source_job_id",
            "retirement_source_attempt_id",
        )
    ):
        raise LifecycleRefused("cleanup access source evidence is incomplete")
    if parameters["plan_revision"] != request_revision(parameters) or parameters[
        "execution_steps"
    ] != execution_steps(parameters["retirement_access_recipe_sha256"]):
        raise LifecycleRefused(
            "cleanup access recipe differs from its reviewed request"
        )
    public = json.loads(parameters["lifecycle_inputs"])
    if (
        original.get("region") != parameters["region"]
        or original.get("target_account_id") != account
        or original.get("workspace_id") != workspace_id
        or public.get("isolation_mode") not in policy["isolation_modes"]
        or (
            mode == "existing-account-managed"
            and public.get("isolation_mode") != "dedicated"
        )
    ):
        raise LifecycleRefused("cleanup access does not retain supported ownership")
    return config


def validate_access_request(operation, context):
    return validate_request(
        operation.request,
        org_id=operation.grant.lease.org_id,
        workspace_id=operation.grant.lease.workspace_id,
        policy=load_policy(context, operation.grant.lease.org_id),
    )
