"""Review a dormant managed cleanup capability without activating it."""

import json
import re
import uuid
from dataclasses import asdict, dataclass

from superplane_bootstrap.eks_grants import EksGrants
from superplane_bootstrap.kube_grants import KubeGrants

from .artifacts import digest
from .retirement_access_plan import RetirementAccessPlan, access_identity
from .retirement_inventory import (
    require_dormant_cleanup_group,
    retained_cleanup_capability,
)
from .runtime_config import LifecycleRefused, validate_runtime_config


@dataclass(frozen=True)
class ManagedRetirementAccessPlan(RetirementAccessPlan):
    retained_grants: tuple[dict, ...]
    cleanup_group: str
    bootstrap_artifact_id: str


def managed_recipe_inputs(inventory, runtime):
    from superplane_bootstrap.components import WORKSPACE_CRDS
    from superplane_bootstrap.grant_plan import BootstrapRelease

    config = validate_runtime_config(runtime)
    account = inventory.cluster_arn.split(":")[4]
    return {
        "release": BootstrapRelease(
            namespace=config["namespace"],
            service_account="superplane-controller",
            controller="superplane-controller",
            enforce_version=config["enforce_version"],
            crds=tuple(WORKSPACE_CRDS),
        ),
        "principals": {
            actor: f"arn:aws:iam::{account}:role/{name}"
            for actor, name in config["actor_role_names"].items()
        },
        "controller_mode": "management",
    }


def verify_managed_access_artifact(row, request, plan):
    """Read only the signed historical apply outputs; target discovery is live later."""
    if (
        request.mode.value != "existing-account-managed"
        or request.cluster_ownership.value != "adp-created"
        or row["account_id"] != request.target_account_id
        or plan.cluster_arn.split(":")[4] != request.target_account_id
    ):
        raise LifecycleRefused("cleanup does not match its managed AWS target")
    target = json.loads(row["target_json"])
    metadata = json.loads(row["artifact_metadata_json"])
    if (
        metadata.get("next_phase") != "bootstrap-workspace"
        or metadata.get("allocation_source_operation_id") != row["source_operation_id"]
        or any(
            target.get(key) != expected
            for key, expected in {
                "account_id": request.target_account_id,
                "aws_region": request.region,
                "org_id": plan.org_id,
                "workspace_id": plan.workspace_id,
            }.items()
        )
    ):
        raise LifecycleRefused("cleanup apply target differs from original ownership")
    raw = metadata.get("outputs")
    if not isinstance(raw, dict) or any(
        not isinstance(value, dict)
        or set(value) != {"value", "type", "sensitive"}
        or not isinstance(value["sensitive"], bool)
        for value in raw.values()
    ):
        raise LifecycleRefused("cleanup apply output inventory is malformed")
    outputs = {key: value["value"] for key, value in raw.items()}
    if (
        any(
            outputs.get(key) != target[key]
            for key in ("account_id", "aws_region", "org_id", "workspace_id")
        )
        or outputs.get("cluster_arn") != plan.cluster_arn
    ):
        raise LifecycleRefused("cleanup apply output names another cluster")
    return outputs


def compile_managed_access_plan(
    inventory,
    runtime,
    *,
    original_allocation_id,
    bootstrap_artifact_id,
    retirement_request_id,
    release,
    principals,
    controller_mode,
    kubernetes,
    eks,
    review_only=False,
):
    """Bind one temporary EKS mapping to six original-UID grants."""
    config = validate_runtime_config(runtime)
    if (
        inventory.cluster_ownership != "adp-created"
        or not inventory.remove_namespace
        or not inventory.components_complete
        or not inventory.components
        or any(not item.owned for item in inventory.components)
        or config["namespace"] != inventory.namespace
        or not isinstance(bootstrap_artifact_id, str)
        or not re.fullmatch(r"[a-f0-9]{64}", bootstrap_artifact_id)
        or (review_only and (eks is not None or kubernetes is not None))
        or (
            not review_only
            and (
                not isinstance(eks, EksGrants) or not isinstance(kubernetes, KubeGrants)
            )
        )
    ):
        raise LifecycleRefused(
            "managed cleanup requires complete owned dedicated workspace inventory"
        )
    capability = retained_cleanup_capability(
        inventory,
        original_allocation_id=original_allocation_id,
        release=release,
        principals=principals,
        controller_mode=controller_mode,
        kubernetes=kubernetes,
        review_only=review_only,
    )
    if not review_only:
        require_dormant_cleanup_group(capability, eks)
    request_id, allocation_id = access_identity(
        inventory.org_id,
        inventory.workspace_id,
        capability.original_allocation_id,
        retirement_request_id,
    )
    account_id = inventory.cluster_arn.split(":")[4]
    inventory_sha256 = digest(asdict(inventory))
    generation = digest(
        {
            "allocation_id": allocation_id,
            "inventory_sha256": inventory_sha256,
            "bootstrap_artifact_id": bootstrap_artifact_id,
            "retained_grants": [asdict(item) for item in capability.grants],
            "runtime_config_sha256": digest(config),
        }
    )
    role_name = config["actor_role_names"]["installer"]
    principal_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    if principals.get("installer") != principal_arn:
        raise LifecycleRefused("cleanup actor differs from its original approved role")
    grant = {
        "key": "cleaner-entry",
        "kind": "eks-entry",
        "actor": "cleaner",
        "cluster_arn": capability.cluster_arn,
        "generation": generation,
        "principal_arn": principal_arn,
        "groups": [capability.group],
        "username": "sp-retire-" + generation[:24] + ":cleaner:{{SessionName}}",
        "client_token": digest({"generation": generation, "actor": "cleaner"}),
        "lifetime": "retirement",
    }
    return ManagedRetirementAccessPlan(
        request_id=request_id,
        allocation_id=allocation_id,
        original_allocation_id=capability.original_allocation_id,
        retirement_request_id=str(uuid.UUID(str(retirement_request_id))),
        org_id=capability.org_id,
        workspace_id=capability.workspace_id,
        cluster_arn=capability.cluster_arn,
        namespace_uid=inventory.namespace_uid,
        inventory_sha256=inventory_sha256,
        runtime_config_sha256=digest(config),
        generation=generation,
        registrar_namespaces=(),
        owned_objects=(),
        grants=(grant,),
        revocation_order=(grant["key"],),
        retained_grants=tuple(asdict(item) for item in capability.grants),
        cleanup_group=capability.group,
        bootstrap_artifact_id=bootstrap_artifact_id,
    )


def compile_managed_access_review(inventory, runtime, **parameters):
    return compile_managed_access_plan(
        inventory,
        runtime,
        kubernetes=None,
        eks=None,
        review_only=True,
        **parameters,
    )


async def require_managed_sealed_plan(connection, source, plan):
    from harness_jobs.allocation import allocation_id_for

    from .retirement_access_context import require_original_seal

    if (
        not isinstance(plan, ManagedRetirementAccessPlan)
        or source.state != "succeeded"
        or source.admitted_request().parameters.get("lifecycle_phase")
        != "apply-infrastructure"
        or (source.org_id, source.workspace_id) != (plan.org_id, plan.workspace_id)
        or plan.original_allocation_id != allocation_id_for(source)
        or plan.allocation_id == plan.original_allocation_id
    ):
        raise LifecycleRefused(
            "managed cleanup cannot exchange the original allocation"
        )
    await require_original_seal(
        connection, source, {"original_allocation_id": plan.original_allocation_id}
    )


async def require_managed_paid_plan(connection, source, plan):
    await require_managed_sealed_plan(connection, source, plan)
    approved = await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM harness_approval_consumption "
        "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 "
        "AND plan_digest=$4 AND reservation_state IN ('confirmed','retained','released'))",
        source.operation_id,
        plan.org_id,
        plan.workspace_id,
        source.plan_digest,
    )
    if not approved:
        raise LifecycleRefused("managed cleanup requires its original paid approval")


async def require_managed_control_source(
    connection, *, plan, access_artifact, paid_operation_id
):
    """Check stored paid/control admissions before consuming an immutable access row.

    The caller must obtain the row through read_artifact and hold current retirement
    execution authority. This proof is read-only and cannot authorize a delete.
    """
    from types import SimpleNamespace

    from harness_jobs.identity import decode_payload, payload_digest

    from .artifacts import canonical
    from .retirement_access_artifact import validate_access_artifact

    validate_access_artifact(access_artifact, plan)

    async def admitted(operation_id):
        record = await connection.fetchrow(
            "SELECT operation_id,org_id,workspace_id,job_id,attempt_id,state,"
            "action,idempotency_key,plan_digest,request_payload "
            "FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
            operation_id,
            plan.org_id,
            plan.workspace_id,
        )
        if record is None:
            raise LifecycleRefused(
                "managed control source is not in the original scope"
            )
        request = decode_payload(record["request_payload"])
        if (
            payload_digest(request) != record["plan_digest"]
            or record["state"] != "succeeded"
            or record["action"] != request.action
            or record["idempotency_key"] != request.idempotency_key
        ):
            raise LifecycleRefused("managed control source admission changed")
        return record, request

    paid, paid_request = await admitted(paid_operation_id)
    paid_source = SimpleNamespace(
        state=paid["state"],
        org_id=paid["org_id"],
        workspace_id=paid["workspace_id"],
        operation_id=paid["operation_id"],
        plan_digest=paid["plan_digest"],
        admitted_request=lambda: paid_request,
    )
    await require_managed_paid_plan(connection, paid_source, plan)
    control, control_request = await admitted(access_artifact["source_operation_id"])
    if (
        control_request.action != "provision"
        or control_request.idempotency_key != plan.request_id
        or canonical(dict(control_request.parameters))
        != access_artifact["parameters_json"]
        or control["plan_digest"] != access_artifact["source_payload_digest"]
        or control["request_payload"] != access_artifact["source_request_payload"]
        or control["job_id"] != access_artifact["source_job_id"]
        or control["attempt_id"] != access_artifact["source_attempt_id"]
        or control_request.parameters.get("original_allocation_id")
        != plan.original_allocation_id
        or control_request.parameters.get("allocation_id") != plan.allocation_id
        or control_request.parameters.get("retirement_access_plan_sha256")
        != plan.revision
    ):
        raise LifecycleRefused("managed control artifact lost its admitted producer")
    parameters = control_request.parameters
    bootstrap, bootstrap_request = await admitted(
        parameters.get("retirement_source_operation_id")
    )
    if (
        bootstrap["plan_digest"] != parameters.get("retirement_source_payload_digest")
        or bootstrap["job_id"] != parameters.get("retirement_source_job_id")
        or bootstrap["attempt_id"] != parameters.get("retirement_source_attempt_id")
        or bootstrap_request.parameters.get("lifecycle_source_operation_id")
        != paid_operation_id
        or bootstrap_request.parameters.get("lifecycle_artifact_id")
        != plan.bootstrap_artifact_id
    ):
        raise LifecycleRefused("managed control bootstrap and paid lineage changed")
    approved = await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM harness_approval_consumption "
        "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 "
        "AND plan_digest=$4 AND reservation_state IN ('confirmed','retained'))",
        control["operation_id"],
        plan.org_id,
        plan.workspace_id,
        control["plan_digest"],
    )
    if not approved:
        raise LifecycleRefused("managed control approval is no longer retained")
    return control["operation_id"]
