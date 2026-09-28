"""The cleanup control request never borrows a sealed workspace allocation."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
from harness_jobs.identity import (
    MAX_PARAMETER_VALUE_LENGTH,
    ContractViolation,
    OperationRequest,
    encode_payload,
    payload_digest,
)

from workspace_provisioning.retirement_access_authority import (
    access_request,
    request_revision,
    validate_access_request,
)
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_lifecycle_policy import policy
from .test_retirement_access_plan import compile_plan, inputs


@pytest.fixture
def access_case():
    owned, runtime = inputs()
    deployment = policy()
    deployment.update(
        runtime=runtime,
        permitted_target_accounts=["879318057152"],
        permitted_regions=["us-east-1"],
    )
    deployment["credential_references"]["879318057152"] = deployment[
        "credential_references"
    ].pop("000000000002")
    original = OperationRequest(
        action="provision",
        idempotency_key="bootstrap-request",
        parameters={
            "allocation_id": "original-allocation",
            "lifecycle_phase": "bootstrap-workspace",
            "lifecycle_request": json.dumps(
                {
                    "mode": "bring-existing-cluster",
                    "region": "us-east-1",
                    "target_account_id": "879318057152",
                    "workspace_id": owned.workspace_id,
                }
            ),
            "lifecycle_inputs": json.dumps({"isolation_mode": "namespace"}),
            "lifecycle_artifact_id": "a" * 64,
            "aws_account_id": "879318057152",
        },
    )
    source = SimpleNamespace(
        state="succeeded",
        operation_id="completed-bootstrap",
        org_id=owned.org_id,
        workspace_id=owned.workspace_id,
        job_id="source-job",
        attempt_id="source-attempt",
        plan_digest=payload_digest(original),
        request_payload=encode_payload(original),
        admitted_request=lambda: original,
    )
    operation = SimpleNamespace(
        request=access_request(compile_plan(owned, runtime), source, deployment),
        grant=SimpleNamespace(
            lease=SimpleNamespace(org_id=owned.org_id, workspace_id=owned.workspace_id)
        ),
    )
    # No authority resolver or provider credentials exist in this pure context.
    return operation, SimpleNamespace(policy=deployment, policy_fixture=True), source


def test_valid_control_request_is_zero_spend_distinct_and_keeps_original_lineage(
    access_case,
):
    operation, context, source = access_case
    assert validate_access_request(operation, context) == context.policy["runtime"]
    parameters = operation.request.parameters
    assert parameters["max_cost_micros"] == parameters["max_resource_units"] == "0"
    assert parameters["allocation_id"] != parameters["original_allocation_id"]
    assert parameters["lifecycle_artifact_id"] == "a" * 64
    assert parameters["retirement_source_operation_id"] == source.operation_id
    assert "allocation_source_operation_id" not in parameters
    assert "runtime_config" not in parameters


@pytest.mark.parametrize(
    "changed",
    [
        "allocation",
        "request",
        "phase",
        "spend",
        "recipe",
        "extra-field",
        "policy",
        "tenant",
    ],
)
def test_changed_access_scope_or_authority_is_refused(access_case, changed):
    operation, context, _ = access_case
    parameters = dict(operation.request.parameters)
    identity = operation.request.idempotency_key
    if changed == "allocation":
        parameters["allocation_id"] = parameters["original_allocation_id"]
        parameters["plan_revision"] = request_revision(parameters)
    elif changed == "request":
        identity = "different-request"
    elif changed == "phase":
        parameters["lifecycle_phase"] = "apply-infrastructure"
    elif changed == "spend":
        parameters["max_cost_micros"] = "1000"
        parameters["plan_revision"] = request_revision(parameters)
    elif changed == "recipe":
        parameters["retirement_access_recipe_sha256"] = "b" * 64
    elif changed == "extra-field":
        parameters["grant_policy"] = "unreviewed"
    elif changed == "policy":
        context.policy["runtime"]["actor_role_names"]["registrar"] = "another"
    else:
        operation.grant.lease.org_id = "another-org"
    operation.request = OperationRequest(
        action="provision", idempotency_key=identity, parameters=parameters
    )
    with pytest.raises(LifecycleRefused):
        validate_access_request(operation, context)


def test_shared_request_refuses_oversized_cleanup_input_before_domain_validation(
    access_case,
):
    operation, _, _ = access_case
    parameters = dict(operation.request.parameters)
    parameters["lifecycle_inputs"] = " " * (MAX_PARAMETER_VALUE_LENGTH + 1)
    with pytest.raises(ContractViolation, match="lifecycle_inputs.*exceeds"):
        OperationRequest(
            action="provision",
            idempotency_key=operation.request.idempotency_key,
            parameters=parameters,
        )


def test_source_bootstrap_must_have_completed_in_exact_scope(access_case):
    _, context, source = access_case
    owned, runtime = inputs()
    source.state = "running"
    with pytest.raises(LifecycleRefused, match="completed bootstrap"):
        access_request(compile_plan(owned, runtime), source, context.policy)
    source.state = "succeeded"
    with pytest.raises(LifecycleRefused, match="completed bootstrap"):
        access_request(
            compile_plan(replace(owned, org_id="another"), runtime),
            source,
            context.policy,
        )
