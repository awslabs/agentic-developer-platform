"""Access readback cannot hide an extra grant or become retirement completion."""

import json

import pytest
from superplane_bootstrap.kube_grants import _digest as grant_digest

from workspace_provisioning.artifacts import canonical, continuation_parameters
from workspace_provisioning.retirement_access_artifact import (
    access_metadata,
    access_result,
    access_target,
    validate_access_artifact,
)
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_retirement_access_authority import access_case as access_case
from .test_retirement_access_plan import compile_plan, inputs


def grant_identities(plan):
    result = {}
    for spec in plan.grants:
        identity = {"generation": spec["generation"]}
        if spec["kind"] == "kubernetes":
            identity.update(uid="uid-" + spec["key"], digest=grant_digest(spec["body"]))
        else:
            identity.update(
                arn=plan.cluster_arn.replace(":cluster/", ":access-entry/")
                + "/role/role-name/identifier/created",
                groups=sorted(spec["groups"]),
                username=spec["username"],
            )
            if spec["kind"] == "eks-policy":
                identity.update(
                    policy_arn=spec["policy_arn"],
                    scope=spec["scope"],
                    associated_at="2026-09-24T08:00:00Z",
                )
        result[spec["key"]] = identity
    return result


def artifact(operation, plan):
    return {
        "artifact_id": "f" * 64,
        "source_operation_id": "access-operation",
        "org_id": plan.org_id,
        "workspace_id": plan.workspace_id,
        "account_id": plan.cluster_arn.split(":")[4],
        "target_json": canonical(
            {
                "account_id": plan.cluster_arn.split(":")[4],
                "cluster_arn": plan.cluster_arn,
                "org_id": plan.org_id,
                "workspace_id": plan.workspace_id,
                "aws_region": plan.cluster_arn.split(":")[3],
            }
        ),
        "parameters_json": canonical(dict(operation.request.parameters)),
        "artifact_metadata_json": canonical(
            access_metadata(plan, grant_identities(plan))
        ),
    }


def test_access_result_retains_both_allocations_and_never_claims_retirement(
    access_case,  # noqa: F811
):
    operation, _, _ = access_case
    plan = compile_plan(*inputs())
    row = artifact(operation, plan)
    assert validate_access_artifact(row, plan) == grant_identities(plan)
    result = access_result(row, plan)
    assert result["retirement_complete"] is False
    assert result["allocation_id"] != result["original_allocation_id"]
    assert result["retirement_access_artifact_id"] == row["artifact_id"]
    assert "grants" not in result
    with pytest.raises(LifecycleRefused, match="phase"):
        continuation_parameters(row)


def test_control_artifact_uses_only_approved_target_not_apply_metadata(access_case):  # noqa: F811
    operation, _, _ = access_case
    plan = compile_plan(*inputs())
    row = artifact(operation, plan)
    expected = json.loads(row["target_json"])
    assert access_target(plan) == expected
    extended = {
        **expected,
        "workspace_name": "display-only",
        "environment": "dev",
    }
    row["target_json"] = canonical(extended)
    with pytest.raises(LifecycleRefused, match="original allocation"):
        validate_access_artifact(row, plan)
    row["target_json"] = canonical(access_target(plan))
    assert validate_access_artifact(row, plan) == grant_identities(plan)


@pytest.mark.parametrize(
    "changed",
    ["missing", "extra", "generation", "scope", "fields", "kube-digest", "entry-arn"],
)
def test_changed_or_secret_shaped_provider_evidence_is_refused(changed):
    plan = compile_plan(*inputs())
    identities = grant_identities(plan)
    if changed == "missing":
        identities.pop("registrar-entry")
    elif changed == "extra":
        identities["unapproved-grant"] = {}
    elif changed == "generation":
        identities["registrar-entry"]["generation"] = "another"
    elif changed == "scope":
        identities["registrar-policy"]["scope"] = {"type": "cluster"}
    elif changed == "fields":
        identities["registrar-entry"]["secret_access_key"] = "must-not-be-persisted"
    elif changed == "kube-digest":
        key = next(spec["key"] for spec in plan.grants if spec["kind"] == "kubernetes")
        identities[key]["digest"] = "0" * 64
    else:
        identities["registrar-entry"]["arn"] = (
            "arn:aws:eks:us-east-1:000000000000:access-entry/foreign/role/x/y/z"
        )
    with pytest.raises(LifecycleRefused):
        access_metadata(plan, identities)


@pytest.mark.parametrize(
    "changed", ["allocation", "workspace", "target", "recipe", "grants", "extra-field"]
)
def test_artifact_cannot_be_rebound_or_expand_its_recorded_plan(access_case, changed):  # noqa: F811
    operation, _, _ = access_case
    plan = compile_plan(*inputs())
    row = artifact(operation, plan)
    parameters = json.loads(row["parameters_json"])
    metadata = json.loads(row["artifact_metadata_json"])
    if changed == "allocation":
        parameters["allocation_id"] = parameters["original_allocation_id"]
    elif changed == "workspace":
        row["workspace_id"] = "another"
    elif changed == "target":
        row["target_json"] = "{}"
    elif changed == "recipe":
        metadata["retirement_access_recipe_sha256"] = "0" * 64
    elif changed == "grants":
        metadata["grants"].append(metadata["grants"][0])
    else:
        metadata["unreviewed"] = True
    row["parameters_json"] = canonical(parameters)
    row["artifact_metadata_json"] = canonical(metadata)
    with pytest.raises(LifecycleRefused):
        validate_access_artifact(row, plan)
