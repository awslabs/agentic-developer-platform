"""Review a dormant managed cleanup capability without activating it."""

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
):
    """Bind one temporary EKS mapping to six live, original-UID grants."""
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
        or not isinstance(eks, EksGrants)
        or not isinstance(kubernetes, KubeGrants)
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
    )
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


async def require_managed_sealed_plan(connection, source, plan):
    from harness_jobs.allocation import allocation_id_for

    from .retirement_access_context import require_original_seal

    if (
        not isinstance(plan, ManagedRetirementAccessPlan)
        or plan.original_allocation_id != allocation_id_for(source)
        or plan.allocation_id == plan.original_allocation_id
    ):
        raise LifecycleRefused(
            "managed cleanup cannot exchange the original allocation"
        )
    await require_original_seal(
        connection, source, {"original_allocation_id": plan.original_allocation_id}
    )
