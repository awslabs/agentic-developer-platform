"""Load deletion candidates from completed bootstrap ownership, never requests.

The service supplies an authenticated teardown binding and the domain registration
store. This read is the input to retirement's provider operations; it performs no
deletion and grants no access. Provider adapters must recheck immutable identities
at the mutation boundary. Managed infrastructure remains with its Terraform state.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re

from superplane_contracts.provisioning import (
    OperationBinding,
    REQUIRED_PERMISSION,
    TEARDOWN,
)
from superplane_contracts.version import CONTRACT_VERSION
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.inventory import OwnedPrerequisite, inventory_from_mapping
from superplane_bootstrap.registry import registration_lock
from superplane_bootstrap.prerequisites import REQUIRED_PREREQUISITE_KINDS
from superplane_bootstrap.component_journal import (
    component_key,
    expected_component_keys,
    merge_component_records,
)


@dataclass(frozen=True)
class OwnedGrant:
    spec: dict
    identity: dict


@dataclass(frozen=True)
class ComponentOwnership:
    desired: dict
    identity: dict
    owned: bool


@dataclass(frozen=True)
class RetirementInventory:
    workspace_id: str
    org_id: str
    cluster_arn: str
    cluster_ownership: str
    namespace: str
    namespace_uid: str
    remove_namespace: bool
    grants: tuple[OwnedGrant, ...]
    prerequisites: tuple[OwnedPrerequisite, ...]
    components: tuple[ComponentOwnership, ...] = ()
    components_complete: bool = False

    @property
    def preserve_cluster(self):
        return self.cluster_ownership == "adopted"


def _check_binding(binding):
    if (
        not isinstance(binding, OperationBinding)
        or binding.action != TEARDOWN
        or binding.permission != REQUIRED_PERMISSION
        or binding.contract_version != CONTRACT_VERSION
        or not binding.operation_id.strip()
        or binding.is_expired(datetime.now(UTC))
    ):
        raise BootstrapRefused(
            "retirement requires current authenticated teardown authority"
        )


def _grant_key(spec):
    if spec.get("kind") == "eks-entry":
        return ("eks-entry", spec.get("principal_arn"))
    if spec.get("kind") != "kubernetes":
        raise BootstrapRefused("retirement encountered an unsupported retained grant")
    body = spec.get("body", {})
    if body.get("kind") not in {
        "Role",
        "RoleBinding",
        "ClusterRole",
        "ClusterRoleBinding",
    }:
        raise BootstrapRefused(
            "retirement encountered an unsupported retained Kubernetes grant"
        )
    metadata = body.get("metadata", {})
    return (body["kind"], metadata.get("namespace"), metadata.get("name"))


def load_bootstrap_retirement_inventory(*, registration_store, binding):
    """Read the canonical target and all its durable, completed ownership records.

    A legacy flag, missing journal, pending bootstrap or ambiguous ownership blocks
    retirement. Across retries, identical supervisor grants are deduplicated by
    immutable provider identity. An adopted namespace never becomes removable just
    because the final registration contains its UID.
    """
    _check_binding(binding)
    workspace_id, org_id = binding.principal.workspace_id, binding.principal.org_id
    return load_bootstrap_retirement_review(
        registration_store=registration_store, workspace_id=workspace_id, org_id=org_id
    )


def load_bootstrap_retirement_review(*, registration_store, workspace_id, org_id):
    """Read immutable ownership after the API verifies current workspace authority.

    This read grants no execution binding. The running retirement must use the
    binding-requiring loader above and compare this inventory's approved digest.
    """
    db = registration_store.store
    with registration_lock(db, workspace_id):
        target = registration_store.read(workspace_id)
        if target is None or (target.workspace_id, target.org_id) != (
            workspace_id,
            org_id,
        ):
            raise BootstrapRefused(
                "retirement workspace is unavailable in caller scope"
            )
        if target.cluster_ownership not in {"adopted", "adp-created"}:
            raise BootstrapRefused("retirement cluster ownership is unknown")
        # A dedicated bootstrap can become the first owner of a cluster later
        # opened for sharing. Its old Terraform/network/actor inventory does not
        # become permission to remove resources used by the new members. Keep
        # this fence in the canonical loader: execution calls it again under
        # current authority, rather than trusting an earlier preview.
        sharing = db.execute(
            "SELECT c.sharing_enabled, EXISTS(SELECT 1 FROM cluster_memberships m "
            "WHERE m.cluster_id=c.id AND m.state<>'removed' "
            "AND m.workspace_id<>w.id) AS has_peers "
            "FROM workspaces w JOIN clusters c ON c.id=w.cluster_id AND c.org_id=w.org_id "
            "WHERE w.id=CAST(:workspace_id AS uuid) FOR UPDATE OF c",
            {"workspace_id": workspace_id},
        )
        if (
            len(sharing) != 1
            or sharing[0]["sharing_enabled"]
            or sharing[0]["has_peers"]
        ):
            raise BootstrapRefused(
                "shared cluster dependencies require membership-scoped retirement; "
                "dedicated bootstrap ownership cannot authorize their removal"
            )
        canonical = db.execute(
            "SELECT w.namespace_name, c.eks_cluster_arn, c.endpoint, c.actual_state_json "
            "FROM workspaces w JOIN clusters c ON c.id=w.cluster_id AND c.workspace_id=w.id "
            "AND c.org_id=w.org_id JOIN organizations o ON o.id=w.org_id "
            "WHERE w.id=CAST(:workspace_id AS uuid) AND (CAST(o.id AS text)=:org_id OR o.adp_org_id=:org_id) "
            "FOR UPDATE OF w, c",
            {"workspace_id": workspace_id, "org_id": org_id},
        )
        if len(canonical) != 1 or (
            canonical[0]["namespace_name"],
            canonical[0]["eks_cluster_arn"],
            canonical[0]["endpoint"],
        ) != (target.namespace, target.cluster_arn, target.endpoint):
            raise BootstrapRefused(
                "canonical workspace no longer matches bootstrap registration"
            )
        metadata = canonical[0]["actual_state_json"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        from superplane_bootstrap.registry import _target_mapping

        expected = _target_mapping(target)
        if (
            not isinstance(metadata, dict)
            or metadata.get("workspace_bootstrap") != expected
        ):
            raise BootstrapRefused("canonical bootstrap identity is missing or changed")
        rows = db.execute(
            "SELECT generation, cluster_arn, org_id, plan_json, progress_json, revoked "
            "FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id",
            {"workspace_id": workspace_id},
        )
        if not rows or any(not row["revoked"] for row in rows):
            raise BootstrapRefused(
                "bootstrap ownership is missing or recovery is outstanding"
            )
        grants, prerequisites, component_records = {}, {}, {}
        components_complete = False
        namespace_owned = False
        complete_inventory = False
        for row in rows:
            if (row["org_id"], row["cluster_arn"]) != (org_id, target.cluster_arn):
                raise BootstrapRefused("bootstrap ownership describes another target")
            plan, progress = (
                json.loads(row["plan_json"]),
                json.loads(row["progress_json"]),
            )
            if progress.get("phase") != "revoked" or not progress.get("complete"):
                raise BootstrapRefused("bootstrap revocation is not complete")
            components = progress.get("components", {})
            if not isinstance(components, dict):
                raise BootstrapRefused("component ownership inventory is malformed")
            for key, component in components.items():
                if (
                    not isinstance(component, dict)
                    or component_key(component.get("desired", {})) != key
                ):
                    raise BootstrapRefused(
                        "component ownership key differs from its target"
                    )
                namespace = component["desired"]["metadata"].get("namespace", "")
                if namespace and namespace != target.namespace:
                    raise BootstrapRefused(
                        "component ownership describes another namespace"
                    )
                component_records.setdefault(key, []).append(component)
            if progress.get("component_inventory_complete") is True:
                deployments = [
                    c
                    for c in components.values()
                    if c["desired"]["kind"] == "Deployment"
                ]
                management = progress.get("component_inventory_mode") == "management"
                service_accounts = [
                    c
                    for c in components.values()
                    if c["desired"]["kind"] == "ServiceAccount"
                ]
                if (management and (deployments or len(service_accounts) != 1)) or (
                    not management and len(deployments) != 1
                ):
                    raise BootstrapRefused(
                        "component inventory lacks a unique observation identity"
                    )
                body = (service_accounts if management else deployments)[0]["desired"]
                try:
                    expected_keys = expected_component_keys(
                        target.namespace,
                        body["metadata"]["name"]
                        if management
                        else body["spec"]["template"]["spec"]["serviceAccountName"],
                        None if management else body["metadata"]["name"],
                    )
                except (KeyError, TypeError) as exc:
                    raise BootstrapRefused(
                        "component inventory controller is malformed"
                    ) from exc
                if set(components) != expected_keys:
                    raise BootstrapRefused("component inventory is incomplete")
                components_complete |= progress.get("retain_workspace") is True
            recorded = progress.get("prerequisite_inventory")
            if recorded is not None:
                inventory = inventory_from_mapping(recorded)
                if inventory.workspace_id != workspace_id:
                    raise BootstrapRefused(
                        "prerequisite inventory describes another workspace"
                    )
                if {item.kind for item in inventory.prerequisites} != set(
                    REQUIRED_PREREQUISITE_KINDS
                ):
                    raise BootstrapRefused(
                        "prerequisite ownership inventory is incomplete"
                    )
                for item in inventory.prerequisites:
                    key = item.kind, item.identifier
                    if key in prerequisites and prerequisites[key] != item:
                        raise BootstrapRefused("prerequisite ownership is ambiguous")
                    prerequisites[key] = item
                complete_inventory |= (
                    bool(inventory.prerequisites)
                    and progress.get("retain_workspace") is True
                )
            for spec in plan.get("grants", []):
                if spec.get("cluster_arn") != target.cluster_arn:
                    raise BootstrapRefused("owned grant describes another cluster")
                status = progress.get(spec.get("key"), {})
                identity = status.get("identity")
                if spec.get("key") == "workspace-namespace":
                    if status.get("phase") != "granted":
                        continue
                    if not isinstance(identity, dict) or (
                        spec.get("body", {}).get("metadata", {}).get("name"),
                        identity.get("uid"),
                    ) != (target.namespace, target.namespace_uid):
                        raise BootstrapRefused(
                            "owned namespace no longer matches registration"
                        )
                    namespace_owned = True
                elif spec.get("lifetime") == "workspace" and progress.get(
                    "retain_workspace"
                ):
                    if status.get("phase") not in {
                        "granted",
                        "adopted",
                    } or not isinstance(identity, dict):
                        raise BootstrapRefused(
                            "retained grant lacks immutable ownership"
                        )
                    if not identity.get(
                        "uid" if spec.get("kind") == "kubernetes" else "arn"
                    ):
                        raise BootstrapRefused(
                            "retained grant lacks immutable identity"
                        )
                    canonical = {
                        k: deepcopy(v)
                        for k, v in spec.items()
                        if k != "adopted_identity"
                    }
                    owned = OwnedGrant(canonical, deepcopy(identity))
                    key = _grant_key(spec)
                    if key in grants and grants[key] != owned:
                        raise BootstrapRefused("retained grant ownership is ambiguous")
                    grants[key] = owned
        if not complete_inventory or not grants:
            raise BootstrapRefused(
                "complete durable bootstrap retirement inventory is unavailable"
            )
        components = []
        for records in component_records.values():
            component = merge_component_records(records)
            identity = component.get("identity", {})
            if (
                component["phase"] not in {"owned", "adopted"}
                or not isinstance(identity.get("uid"), str)
                or not identity["uid"]
                or not isinstance(identity.get("digest"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", identity["digest"])
                or (
                    component["phase"] == "owned"
                    and (
                        not component.get("creation")
                        or identity.get("creation") != component["creation"]
                    )
                )
            ):
                raise BootstrapRefused("component creation ownership is unresolved")
            components.append(
                ComponentOwnership(
                    deepcopy(component["desired"]),
                    deepcopy(identity),
                    component["phase"] == "owned",
                )
            )
        return RetirementInventory(
            workspace_id,
            org_id,
            target.cluster_arn,
            target.cluster_ownership,
            target.namespace,
            target.namespace_uid,
            namespace_owned,
            tuple(grants.values()),
            tuple(prerequisites.values()),
            tuple(components),
            components_complete,
        )
