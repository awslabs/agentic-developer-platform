"""Immutable cleanup-access evidence, never a generic provisioning continuation."""

from dataclasses import asdict
import json
import re

from superplane_bootstrap.kube_grants import _digest as grant_digest

from .artifacts import canonical, digest
from .retirement_access_plan import PHASE
from .runtime_config import LifecycleRefused

READY = "retirement-access-ready"


def access_metadata(plan, identities):
    """Whitelist exact adapter readback; no provider response extras are persisted."""
    if not isinstance(identities, dict) or set(identities) != {
        spec["key"] for spec in plan.grants
    }:
        raise LifecycleRefused("cleanup artifact requires every approved grant")
    grants = []
    for spec in plan.grants:
        identity = identities[spec["key"]]
        fields = (
            {"uid", "generation", "digest"}
            if spec["kind"] == "kubernetes"
            else {"arn", "generation", "groups", "username"}
            | (
                {"policy_arn", "scope", "associated_at"}
                if spec["kind"] == "eks-policy"
                else set()
            )
        )
        if (
            not isinstance(identity, dict)
            or set(identity) != fields
            or identity.get("generation") != plan.generation
        ):
            raise LifecycleRefused("cleanup grant evidence contains unsupported fields")
        if spec["kind"] == "kubernetes":
            if (
                not isinstance(identity["uid"], str)
                or not identity["uid"]
                or len(identity["uid"]) > 255
                or identity["digest"] != grant_digest(spec["body"])
            ):
                raise LifecycleRefused("cleanup Kubernetes grant evidence changed")
        else:
            prefix = plan.cluster_arn.replace(":cluster/", ":access-entry/") + "/"
            arn = identity["arn"]
            if (
                not isinstance(arn, str)
                or len(arn) > 2048
                or not arn.startswith(prefix)
                or len(arn[len(prefix) :].split("/")) < 4
                or identity["groups"] != sorted(spec["groups"])
                or identity["username"] != spec["username"]
            ):
                raise LifecycleRefused("cleanup EKS grant evidence changed")
            if spec["kind"] == "eks-policy" and (
                identity["policy_arn"] != spec["policy_arn"]
                or identity["scope"] != spec["scope"]
                or not isinstance(identity["associated_at"], str)
                or not identity["associated_at"]
                or len(identity["associated_at"]) > 255
            ):
                raise LifecycleRefused("cleanup EKS policy authority changed")
        grants.append({"spec": spec, "identity": identity})
    return {
        "next_phase": READY,
        "retirement_access_plan": asdict(plan),
        "retirement_access_plan_sha256": plan.revision,
        "retirement_access_recipe_sha256": digest(plan.recipe()),
        "grants": grants,
    }


def validate_access_artifact(row, plan):
    """Call after read_artifact verifies the stored row's immutable digest.

    The caller must separately verify the successful access producer and the
    original bootstrap's current ownership. This projection grants no authority.
    """
    metadata = json.loads(row["artifact_metadata_json"])
    parameters = json.loads(row["parameters_json"])
    if (
        not isinstance(metadata, dict)
        or json.loads(row["target_json"])
        != {
            "account_id": plan.cluster_arn.split(":")[4],
            "cluster_arn": plan.cluster_arn,
            "org_id": plan.org_id,
            "workspace_id": plan.workspace_id,
            "aws_region": plan.cluster_arn.split(":")[3],
        }
        or (row["org_id"], row["workspace_id"], row["account_id"])
        != (plan.org_id, plan.workspace_id, plan.cluster_arn.split(":")[4])
        or parameters.get("lifecycle_phase") != PHASE
        or parameters.get("allocation_id") != plan.allocation_id
        or parameters.get("original_allocation_id") != plan.original_allocation_id
        or parameters.get("retirement_request_id") != plan.retirement_request_id
        or parameters.get("retirement_inventory_sha256") != plan.inventory_sha256
        or parameters.get("retirement_access_plan_sha256") != plan.revision
        or parameters.get("retirement_access_recipe_sha256") != digest(plan.recipe())
        or not re.fullmatch(r"[a-f0-9]{64}", row["artifact_id"])
    ):
        raise LifecycleRefused("cleanup artifact differs from its original allocation")
    grants = metadata.get("grants")
    if (
        not isinstance(grants, list)
        or len(grants) != len(plan.grants)
        or any(
            not isinstance(item, dict) or set(item) != {"spec", "identity"}
            for item in grants
        )
        or canonical([item["spec"] for item in grants]) != canonical(plan.grants)
    ):
        raise LifecycleRefused("cleanup artifact grant set differs from its plan")
    identities = {item["spec"]["key"]: item["identity"] for item in grants}
    if canonical(metadata) != canonical(access_metadata(plan, identities)):
        raise LifecycleRefused("cleanup artifact contains changed or extra evidence")
    return identities


def access_result(row, plan):
    validate_access_artifact(row, plan)
    return {
        "status": "retirement_access_ready",
        "retirement_access_artifact_id": row["artifact_id"],
        "source_operation_id": row["source_operation_id"],
        "workspace_id": plan.workspace_id,
        "retirement_request_id": plan.retirement_request_id,
        "allocation_id": plan.allocation_id,
        "original_allocation_id": plan.original_allocation_id,
        "retirement_complete": False,
    }


async def record_access_artifact(facts, effects, identities):
    """Persist readback under the access operation's lease and outer intent lock."""
    from harness_jobs.store import OperationStore

    operation, plan = facts.operation, facts.plan
    lease = operation.grant.lease
    metadata = access_metadata(plan, identities)
    if canonical(await effects.complete()) != canonical(identities):
        raise LifecycleRefused("cleanup artifact differs from confirmed grant journal")
    values = {
        "org_id": lease.org_id,
        "workspace_id": lease.workspace_id,
        "source_operation_id": lease.operation_id,
        "source_job_id": operation.job_id,
        "producer_holder": lease.holder,
        "producer_attempt_id": lease.attempt_id,
        "producer_fence_token": lease.fence_token,
        "request_revision": operation.request.parameters["plan_revision"],
        "account_id": plan.cluster_arn.split(":")[4],
        "target_json": facts.artifact["target_json"],
        "parameters_json": canonical(dict(operation.request.parameters)),
        "artifact_metadata_json": canonical(metadata),
    }
    async with effects.fenced() as domain:
        async with effects.context.connect() as execution:
            source = await OperationStore().get(
                execution, operation.grant.principal, lease.operation_id
            )
        if (
            source is None
            or source.job_id != operation.job_id
            or source.plan_digest != operation.plan_digest
            or source.request_payload != operation.request_payload
        ):
            raise LifecycleRefused("cleanup artifact producer admission changed")
        values.update(
            source_attempt_id=source.attempt_id,
            source_payload_digest=source.plan_digest,
            source_request_payload=source.request_payload,
        )
        artifact_id = digest(values)
        columns = tuple(values)
        await domain.execute(
            "INSERT INTO workspace_lifecycle_artifacts (artifact_id,"
            + ",".join(columns)
            + ") VALUES ($1,"
            + ",".join("$" + str(i) for i in range(2, len(columns) + 2))
            + ") ON CONFLICT (artifact_id) DO NOTHING",
            artifact_id,
            *values.values(),
        )
        row = await domain.fetchrow(
            "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1",
            artifact_id,
        )
        if row is None or any(row[key] != value for key, value in values.items()):
            raise LifecycleRefused("immutable cleanup access evidence differs")
    await effects.authority()
    return access_result(dict(row), plan)
