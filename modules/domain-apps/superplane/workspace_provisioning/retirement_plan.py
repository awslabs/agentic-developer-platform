"""Compose retirement's ordered deletion plan before any deletion is attempted.

The plan is the approved set of provider mutations. It is produced from durable
ownership records, bound into the request that goes through approval, and then
enforced by the shared execution layer: `execution_rpc.execute_step` refuses a
descriptor that is not in the admitted plan and refuses a later step before its
predecessor is confirmed succeeded. So the deletion set is fixed before the first
mutation rather than chosen while deleting.

Two properties this module exists to make assertable offline:

* **Order.** Governed work is blocked and drained, then the workspace is
  unregistered, and only then is anything deleted. Deleting before unregistering
  would leave the canonical registry pointing at absent resources, which is the
  state that makes a later retry unable to tell "already deleted" from "never
  existed". Deletion itself runs dependency-last-first: the controller objects
  inside the namespace, then the namespace, then the grants that allowed access to
  it, then the out-of-Terraform network prerequisites that made it reachable.
* **Narrow ownership.** A step is emitted only for a resource recorded as created
  by this platform, carrying the immutable identity recorded at creation. Adopted
  resources, shared cluster-scoped definitions and a supplied cluster become
  stated preservations. Stating them is the evidence: an empty deletion list and a
  correct one are indistinguishable unless the preservation is explicit.

The descriptor type and the encoder come from `execution_contract.py`, a deliberate
structural copy of `harness_jobs.execution_descriptors` rather than an import of it —
see that module for why, and for the drift test that keeps the two byte-identical.

This module composes and refuses. It performs no deletion and grants no access,
and the descriptors it emits are not themselves mutation authority — the executing
adapter must recheck each live immutable identity at the mutation boundary, because
a resource with the right name can be a different resource created after ours was
removed. Account closure is not a step here in either mode; it is separately gated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from superplane_bootstrap.errors import BootstrapRefused

from .execution_contract import (
    MAX_DESCRIPTOR_VALUE_LENGTH,
    MAX_EXECUTION_PLAN_BYTES,
    MAX_EXECUTION_STEPS,
    ExecutionStep,
    encode_execution_steps,
)
from .retirement_inventory import RetirementInventory

# The provider each step is dispatched to. Named per boundary rather than one
# "workspace" provider so the trusted service can route a Kubernetes delete and an
# AWS revoke to different hooks with different credentials, and so an audit row
# names which boundary was crossed.
GOVERNANCE = "superplane-governance"
KUBERNETES = "superplane-kubernetes"
REGISTRY = "superplane-registry"
AWS = "superplane-aws"

# The operation kinds, in the order a retirement performs them. The first three are
# not deletions: they are the gate, the drain and the withdrawal that must precede
# one. `VERIFY_RESOURCES` is last because teardown completeness is a claim about the
# provider's own inventory, not about this plan having been executed.
BLOCK_ADMISSION = "block-governed-admission"
DRAIN_WORKLOADS = "drain-governed-workloads"
UNREGISTER = "unregister-workspace"
DELETE_COMPONENT = "delete-controller-component"
DELETE_NAMESPACE = "delete-namespace"
REVOKE_GRANT = "revoke-grant"
REVOKE_PREREQUISITE = "revoke-network-prerequisite"
VERIFY_RESOURCES = "verify-resource-inventory"


@dataclass(frozen=True)
class RetirementPlan:
    """The immutable, ordered mutation set plus the preservations it asserts.

    `steps` is what approval covers and what execution may perform. `preserved` is
    what retirement deliberately leaves intact, each entry carrying its reason so an
    operator can confirm a supplied cluster or an adopted namespace was considered
    and spared rather than merely absent from the list.
    """

    workspace_id: str
    org_id: str
    cluster_arn: str
    cluster_ownership: str
    steps: tuple[ExecutionStep, ...]
    preserved: tuple[str, ...]
    components_authorized: bool = True
    owned_namespace_remaining: bool = False
    cluster_rbac_remaining: bool = False

    @property
    def preserves_cluster(self) -> bool:
        """True when the cluster was supplied by its owner and is spared."""
        return self.cluster_ownership == "adopted"

    @property
    def completes_teardown(self) -> bool:
        """Whether executing this plan can be claimed to retire everything owned.

        A complete component journal is necessary but does not retire an owned
        namespace or managed Terraform infrastructure. This bootstrap-only plan
        deliberately retains those obligations. Provider inventory and ledger
        finalization remain required even when every planned step has completed.
        """
        return (
            self.components_authorized
            and self.preserves_cluster
            and not self.owned_namespace_remaining
            and not self.cluster_rbac_remaining
        )

    def deletion_steps(self) -> tuple[ExecutionStep, ...]:
        """The subset that removes something, for tests asserting on deletions."""
        return tuple(
            step
            for step in self.steps
            if step.operation_kind
            in {
                DELETE_COMPONENT,
                DELETE_NAMESPACE,
                REVOKE_GRANT,
                REVOKE_PREREQUISITE,
            }
        )

    def encode(self) -> str:
        """The approved plan as the `execution_steps` request parameter.

        Bound into the request digest by admission, so approval covers this exact
        ordered list and a changed plan is a different operation rather than a
        mutation of an approved one.
        """
        return encode_execution_steps(self.steps)


def _target(payload: dict) -> str:
    """A descriptor target that names the immutable identity, not just a name.

    Sorted keys and no whitespace so the same resource yields the same target on a
    retry — `execution_plan.step_key` hashes the descriptor's fields, and a target
    that reordered would acquire a fresh provider idempotency key on restart and
    reissue a delete that may already have happened.
    """
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if len(encoded) > MAX_DESCRIPTOR_VALUE_LENGTH:
        raise BootstrapRefused(
            "a retirement step's target exceeds the approved descriptor limit; "
            "refusing to truncate an identity a deletion is matched against"
        )
    return encoded


def _component_steps(
    inventory: RetirementInventory,
) -> tuple[list[ExecutionStep], list[str]]:
    """Deletion steps for owned controller objects; preservation for adopted ones.

    Without `components_complete` no component deletion is planned at all. A partial
    record cannot be completed by guessing, and deleting the recorded subset would
    report a finished teardown while leaving unrecorded objects behind. Declining the
    whole group is the documented behaviour for a bootstrap that predates the
    component journal; the caller learns it from `completes_teardown`.
    """
    steps: list[ExecutionStep] = []
    preserved: list[str] = []
    if not inventory.components_complete:
        preserved.append(
            f"Controller components in {inventory.namespace} ({len(inventory.components)} "
            "partial record(s); component ownership is incomplete, so no automatic "
            "component deletion is planned and this teardown cannot be reported "
            "complete)"
        )
        return steps, preserved
    priority = {"Deployment": 0, "RoleBinding": 1, "Role": 2, "ServiceAccount": 3}
    for index, component in sorted(
        enumerate(inventory.components),
        key=lambda item: (priority.get(item[1].desired.get("kind"), 4), item[0]),
    ):
        body = component.desired
        kind = body.get("kind", "")
        name = body.get("metadata", {}).get("name", "")
        if kind in {"ClusterRole", "ClusterRoleBinding"}:
            preserved.append(
                f"{kind}/{name} (owned cluster-scoped controller RBAC; independent "
                "exact-name cleanup authority is required; teardown remains incomplete)"
                if component.owned
                else f"{kind}/{name} (adopted cluster-scoped controller RBAC; not created by this bootstrap)"
            )
            continue
        if not component.owned:
            preserved.append(
                f"{kind}/{name} (adopted; matched an object this platform did not "
                "create, so its lifecycle is not ours to end)"
            )
            continue
        steps.append(
            ExecutionStep(
                step_id=f"delete-component-{index:02d}",
                provider=KUBERNETES,
                operation_kind=DELETE_COMPONENT,
                target=_target(
                    {
                        "kind": kind,
                        "name": name,
                        "namespace": body.get("metadata", {}).get("namespace", ""),
                        "uid": component.identity.get("uid", ""),
                        "digest": component.identity.get("digest", ""),
                    }
                ),
            )
        )
    return steps, preserved


def _grant_steps(
    inventory: RetirementInventory,
) -> tuple[list[ExecutionStep], list[str]]:
    """Revocation steps for retained grants, keyed by their immutable identity."""
    steps: list[ExecutionStep] = []
    preserved: list[str] = []
    for index, grant in enumerate(inventory.grants):
        spec, identity = grant.spec, grant.identity
        if spec.get("kind") == "eks-entry":
            identifier = identity.get("arn", "")
            descriptor = {
                "kind": "eks-access-entry",
                "principal_arn": spec.get("principal_arn", ""),
                "arn": identifier,
                "cluster_arn": spec.get("cluster_arn", ""),
            }
        else:
            body = spec.get("body", {})
            metadata = body.get("metadata", {})
            if body.get("kind") in {"ClusterRole", "ClusterRoleBinding"}:
                # Cluster scope does not imply shared ownership. In particular,
                # supervisor-cluster grants are unique to a bootstrap generation.
                # Namespaced cleanup authority cannot remove them, and preserving
                # them must not make an otherwise incomplete teardown look done.
                preserved.append(
                    f"{body.get('kind')}/{metadata.get('name', '')} (cluster-scoped "
                    "retained grant; exact ownership and independently provisioned "
                    "revocation authority are required; teardown remains incomplete)"
                )
                continue
            identifier = identity.get("uid", "")
            descriptor = {
                "kind": body.get("kind", ""),
                "name": metadata.get("name", ""),
                "namespace": metadata.get("namespace", ""),
                "uid": identifier,
            }
        if not identifier:
            raise BootstrapRefused(
                "a retained grant has no immutable identity to revoke against; "
                "refusing to plan a revocation matched only by name"
            )
        steps.append(
            ExecutionStep(
                step_id=f"revoke-grant-{index:02d}",
                provider=KUBERNETES if spec.get("kind") == "kubernetes" else AWS,
                operation_kind=REVOKE_GRANT,
                target=_target(descriptor),
            )
        )
    return steps, preserved


def _prerequisite_steps(
    inventory: RetirementInventory,
) -> tuple[list[ExecutionStep], list[str]]:
    """Revocation steps for the out-of-Terraform network prerequisites.

    Only rules recorded as created here are revoked. An adopted rule is one a
    shared platform or another workspace owns; revoking it by identity would still
    remove somebody else's reachability.
    """
    steps: list[ExecutionStep] = []
    preserved: list[str] = []
    for index, prerequisite in enumerate(inventory.prerequisites):
        if not prerequisite.removable:
            preserved.append(
                f"{prerequisite.kind}/{prerequisite.identifier} (adopted; owned by a "
                "shared platform or another workspace, so revoking it would remove "
                "reachability this retirement is not entitled to remove)"
            )
            continue
        steps.append(
            ExecutionStep(
                step_id=f"revoke-prerequisite-{index:02d}",
                provider=AWS,
                operation_kind=REVOKE_PREREQUISITE,
                target=_target(
                    {
                        "kind": prerequisite.kind,
                        "identifier": prerequisite.identifier,
                        "workspace_id": prerequisite.workspace_id,
                    }
                ),
            )
        )
    return steps, preserved


def compose_retirement_plan(
    inventory: RetirementInventory, *, managed_destroy=None
) -> RetirementPlan:
    """Order the durable ownership record into an approvable deletion plan.

    The sequence is fixed here rather than chosen at execution time: block new
    governed work, drain what is running, unregister, then delete owned resources
    dependency-last-first, then verify the provider's authoritative inventory.

    Refuses rather than trims. An ownership record that cannot be expressed inside
    one approved plan's bounds is refused, because a truncated deletion plan is
    exactly the teardown that silently leaves resources and cost behind.
    """
    if not isinstance(inventory, RetirementInventory):
        raise BootstrapRefused(
            "composing a retirement plan requires a durable ownership inventory"
        )
    workspace, org = inventory.workspace_id, inventory.org_id
    scope = _target({"workspace_id": workspace, "org_id": org})

    # Blocking admission and draining precede every deletion. A drain that ran
    # after a delete would be draining workloads whose namespace is already gone,
    # and the governed work admitted in between would be admitted against resources
    # that are being removed.
    steps: list[ExecutionStep] = [
        ExecutionStep(
            step_id="block-admission",
            provider=GOVERNANCE,
            operation_kind=BLOCK_ADMISSION,
            target=scope,
        ),
        ExecutionStep(
            step_id="drain-workloads",
            provider=GOVERNANCE,
            operation_kind=DRAIN_WORKLOADS,
            target=scope,
        ),
        ExecutionStep(
            step_id="unregister-workspace",
            provider=REGISTRY,
            operation_kind=UNREGISTER,
            target=scope,
        ),
    ]
    preserved: list[str] = []

    component_steps, component_preserved = _component_steps(inventory)
    steps.extend(component_steps)
    preserved.extend(component_preserved)

    # Ownership of a namespace does not authorize a cascading delete of everything
    # currently inside it. The component journal never enumerates all tenant objects
    # and cannot fence concurrent creation. Delete only separately owned objects;
    # retain the namespace until a dedicated, fenced namespace-retirement authority
    # can establish the complete approved deletion set at the mutation boundary.
    if inventory.remove_namespace:
        if not inventory.namespace_uid.strip():
            raise BootstrapRefused(
                "the namespace is recorded as owned but carries no uid; refusing to "
                "plan a delete identified only by name, because a namespace with "
                "this name may be a different object than the one created here"
            )
        preserved.append(
            f"Namespace/{inventory.namespace} (ADP-created, uid {inventory.namespace_uid}; "
            "retained because component ownership cannot authorize a cascading deletion "
            "or fence unrelated namespace contents; teardown remains incomplete)"
        )
    else:
        preserved.append(
            f"Namespace/{inventory.namespace} (adopted, not created by this "
            "bootstrap; may hold unrelated workloads)"
        )

    grant_steps, grant_preserved = _grant_steps(inventory)
    steps.extend(grant_steps)
    preserved.extend(grant_preserved)

    prerequisite_steps, prerequisite_preserved = _prerequisite_steps(inventory)
    steps.extend(prerequisite_steps)
    preserved.extend(prerequisite_preserved)

    if managed_destroy is not None:
        from .retirement_terraform import ReviewedDestroy

        if (
            not isinstance(managed_destroy, ReviewedDestroy)
            or inventory.preserve_cluster
        ):
            raise BootstrapRefused(
                "only managed infrastructure accepts a reviewed destroy artifact"
            )
        if (
            managed_destroy.target.get("workspace_id"),
            managed_destroy.target.get("org_id"),
        ) != (workspace, org):
            raise BootstrapRefused(
                "reviewed destroy artifact describes another workspace"
            )
        steps.append(managed_destroy.step())

    # The cluster and its network. Never deleted by this plan in either mode: an
    # ADP-created cluster's lifecycle belongs to its Terraform state, and a supplied
    # one belongs to its owner. The reason differs, and for a supplied cluster the
    # preservation is the claim retirement must produce evidence for.
    if inventory.preserve_cluster:
        preserved.append(
            f"Cluster/{inventory.cluster_arn} (supplied by its owner; this "
            "retirement removes the workspace from it and never its cluster or "
            "network)"
        )
    elif managed_destroy is None:
        preserved.append(
            f"Cluster/{inventory.cluster_arn} (ADP-created; its lifecycle belongs "
            "to the workspace Terraform state, not to this deletion plan)"
        )
    preserved.append(
        "Account (closure is separately gated and is never a step in workspace "
        "retirement)"
    )

    # Completeness is verified against the provider's own inventory, last. Having
    # executed every planned step is not the same claim as no owned resource or
    # cost remaining, and only the second one may end a retirement.
    steps.append(
        ExecutionStep(
            step_id="verify-inventory",
            provider=AWS,
            operation_kind=VERIFY_RESOURCES,
            target=scope,
        )
    )

    if len(steps) > MAX_EXECUTION_STEPS:
        raise BootstrapRefused(
            f"retirement requires {len(steps)} approved steps, above the "
            f"{MAX_EXECUTION_STEPS}-step plan limit; refusing to truncate a "
            "deletion plan, which would leave owned resources behind"
        )
    plan = RetirementPlan(
        workspace_id=workspace,
        org_id=org,
        cluster_arn=inventory.cluster_arn,
        cluster_ownership=inventory.cluster_ownership,
        steps=tuple(steps),
        preserved=tuple(preserved),
        components_authorized=inventory.components_complete,
        owned_namespace_remaining=inventory.remove_namespace,
        cluster_rbac_remaining=any(
            grant.spec.get("body", {}).get("kind")
            in {"ClusterRole", "ClusterRoleBinding"}
            for grant in inventory.grants
        )
        or any(
            component.owned
            and component.desired.get("kind") in {"ClusterRole", "ClusterRoleBinding"}
            for component in inventory.components
        ),
    )
    encoded = plan.encode()
    if len(encoded.encode()) > MAX_EXECUTION_PLAN_BYTES:
        raise BootstrapRefused(
            "the composed retirement plan exceeds the approved plan byte limit; "
            "refusing to truncate a deletion plan"
        )
    return plan
