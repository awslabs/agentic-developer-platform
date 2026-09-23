"""The cleanup boundary: what ADP may remove, and what it must never touch.

Issue #5533 (w6-10), EPIC #4910. Design item 3: "Validate existing-cluster mode
without taking ownership of its VPC/EKS lifecycle; make bootstrap retry/rollback
idempotent and confine cleanup to owned objects." AC-02: "BYOC cleanup preserves the
cluster and unrelated workloads".

## Why this module produces a PLAN and does not delete

`plan_cleanup` returns a description of what would be removed. Nothing here calls a
delete. Three reasons, and the third is the one that decided it:

1. The plan is assertable offline. AC-02's requirement — that a supplied cluster and
   its unrelated workloads survive — becomes a test that reads a data structure,
   rather than a claim about code that only a live teardown could check.
2. The plan is reviewable before it runs. An operator can see the exact object list.
3. **A function that both decides and deletes cannot be tested for what it does not
   delete.** The dangerous case is not "deleted the wrong thing" but "deleted
   something adjacent" — and a test can only observe absence of an action if the
   action is a separate step. The `ClusterAccess` seam has no `delete` method at all
   (see `access.py`), so this package could not perform the deletion it plans.

## The rule, stated once

ADP removes exactly what ADP created, identified by recorded uid, and nothing else.

Everything below follows from that:

- **The cluster itself is never in a cleanup plan when adopted.** `VerifiedTarget.is_adopted`
  comes from the request's ownership mode (#5530's `ClusterOwnership`), recorded at
  verification rather than inferred — a bootstrap that guessed would guess wrong for
  a supplied cluster inside an ADP-managed account. For an ADP-created cluster the
  cluster's lifecycle belongs to Terraform (`../infra/workspaces/`), not to this
  package, so it is not in the plan either; what differs is that a BYOC plan asserts
  the cluster's preservation explicitly, because that is the claim AC-02 requires
  evidence for.
- **An adopted namespace is never deleted.** `components.py` records whether the
  namespace was created here. A pre-existing workspace namespace on a supplied
  cluster may hold workloads ADP knows nothing about.
- **CRDs are never deleted, in either mode.** They are `scope: Cluster` and shared
  between every workspace on the cluster; deleting one deletes every custom resource
  of that type cluster-wide, including other workspaces'. Idempotent to leave,
  unbounded to remove.
- **A uid mismatch aborts rather than proceeding by name.** A namespace with the
  right name and a different uid is a different object — deleted and recreated by
  somebody else in between. Deleting it by name would destroy exactly the unrelated
  workload AC-02 protects. `installation/cluster_probe.py::__exit__` sets the
  precedent, passing `preconditions.uid` on its delete.

## Partial bootstrap is the normal input

Cleanup most often runs after a bootstrap that failed halfway, so the plan is built
from what was recorded as created, not from what a successful run would have created.
Anything never recorded is not in the plan — which is the correct behaviour and the
reason `components.py` records the namespace uid at creation time rather than
re-reading it later.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .components import ComponentInstallation, InstalledObject
from .errors import BootstrapRefused
from .inventory import PrerequisiteInventory
from .prerequisites import require_inventory
from .target import VerifiedTarget


@dataclass(frozen=True)
class CleanupPlan:
    """What cleanup would remove, what it preserves, and why.

    `preserved` is populated deliberately rather than left implicit. An empty
    "preserved" list and a correct "remove" list would satisfy the letter of AC-02
    while giving an operator no way to confirm the supplied cluster was considered
    and spared. Stating the preservation is the evidence.
    """

    workspace_id: str
    cluster_arn: str
    cluster_ownership: str
    remove_namespace: str = ""
    remove_namespace_uid: str = ""
    remove_prerequisites: tuple[str, ...] = field(default_factory=tuple)
    preserved: tuple[str, ...] = field(default_factory=tuple)

    @property
    def deletes_nothing(self) -> bool:
        """True when a partial bootstrap created nothing that may be removed."""
        return not self.remove_namespace and not self.remove_prerequisites

    @property
    def preserves_cluster(self) -> bool:
        """The AC-02 assertion, in one property a test can read."""
        return any(self.cluster_arn in entry for entry in self.preserved)


def _namespace_removal(
    installation: ComponentInstallation,
) -> tuple[str, str, list[str]]:
    """The namespace to remove and its required uid precondition, or nothing.

    Returns preservation reasons for a namespace that must be left alone.
    """
    preserved: list[str] = []
    if not installation.namespace_owned:
        preserved.append(
            f"Namespace/{installation.namespace} (adopted, not created by this "
            "bootstrap; may hold unrelated workloads)"
        )
        return "", "", preserved
    if not installation.namespace_uid.strip():
        raise BootstrapRefused(
            f"namespace {installation.namespace!r} is recorded as owned but has no "
            "recorded uid; refusing to plan a delete identified only by name, "
            "because a namespace with this name may be a different object than the "
            "one created here"
        )
    return installation.namespace, installation.namespace_uid, preserved


def _shared_preservation(objects: tuple[InstalledObject, ...]) -> list[str]:
    """Explicit preservation entries for shared cluster-scoped objects."""
    return [
        f"{obj.kind}/{obj.name} (cluster-scoped and shared between every workspace "
        "on this cluster; deleting it would remove other workspaces' resources)"
        for obj in objects
        if obj.shared
    ]


def plan_cleanup(
    *,
    target: VerifiedTarget,
    installation: ComponentInstallation,
    inventory: PrerequisiteInventory,
) -> CleanupPlan:
    """Describe the cleanup ADP is authorized to perform. Deletes nothing.

    Safe to call after a partial bootstrap — that is the expected case. Refuses only
    when the inputs describe different workspaces, because a plan built from a
    mismatched pair could name one workspace's namespace under another's authority.

    `inventory` is REQUIRED (F4). It used to default to None, which meant a cleanup
    plan could be produced for a workspace whose out-of-Terraform access prerequisites
    were recorded nowhere — and a plan that omits them is a plan that leaves an access
    path behind permanently, since nothing else in the system knows those objects
    exist. Requiring it here is safe by construction: `prerequisites.verify_prerequisites`
    is the FIRST gate in the sequence, so any bootstrap far enough along to have an
    installation to clean up necessarily has an inventory too.
    """
    if installation.workspace_id != target.workspace_id:
        raise BootstrapRefused(
            "the component installation and the verified target describe different "
            "workspaces; refusing to plan cleanup from a mismatched pair"
        )
    if installation.cluster_arn != target.cluster_arn:
        raise BootstrapRefused(
            "the component installation was performed on a different cluster than "
            "the verified target; refusing to plan cleanup"
        )
    inventory = require_inventory(
        inventory, workspace_id=target.workspace_id, what="cleanup planning"
    )

    namespace, namespace_uid, preserved = _namespace_removal(installation)
    preserved.extend(_shared_preservation(installation.objects))

    # The cluster. Never removed by this package in either mode — Terraform owns an
    # ADP-created cluster's lifecycle — but the reason differs, and for a supplied
    # cluster the preservation is the claim AC-02 requires evidence for.
    if target.is_adopted:
        preserved.append(
            f"Cluster/{target.cluster_arn} (supplied by its owner; ADP never took "
            "ownership of its VPC or EKS lifecycle and must not delete it)"
        )
        preserved.append(
            f"All workloads on Cluster/{target.cluster_arn} outside "
            f"Namespace/{installation.namespace} (never enumerated, never touched)"
        )
    else:
        preserved.append(
            f"Cluster/{target.cluster_arn} (ADP-created; its lifecycle belongs to "
            "the workspace Terraform module, not to bootstrap cleanup)"
        )

    remove_prerequisites = tuple(
        f"{item.kind}/{item.identifier}" for item in inventory.removable
    )
    preserved.extend(
        f"{item.kind}/{item.identifier} (adopted: {item.reason})"
        for item in inventory.preserved
    )

    return CleanupPlan(
        workspace_id=target.workspace_id,
        cluster_arn=target.cluster_arn,
        cluster_ownership=target.cluster_ownership,
        remove_namespace=namespace,
        remove_namespace_uid=namespace_uid,
        remove_prerequisites=remove_prerequisites,
        preserved=tuple(preserved),
    )
