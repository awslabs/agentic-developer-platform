"""Cleanup boundaries, and why account closure is never implicit — Issue #5530 (w6-07).

## The legacy teardown's two defects

**Account closure was a side effect of deleting one Kubernetes object.** `06-teardown-account.sh`
ran a single `kubectl delete fullaccountinfrastructure <name>`, and because the vendored
graph owns the `Account` resource, that one command cascaded into deleting the AWS account.
The script then printed, *after the fact*:

    NOTE: AWS account $ACCOUNT_ID will enter a 90-day suspended state.

A 90-day suspension is not reversible on demand, and it was reached by deleting a custom
resource whose name is the workspace's. The issue states the rule this module implements:
account closure is never implicit in custom-resource or workspace deletion.

**Cleanup had no boundary, and ignored its own errors.** `07-teardown-capabilities.sh`
deleted cluster-scoped CRDs (`fullaccountinfrastructures.kro.run`, and the `NetworkStack`
and `EKSClusterStack` definitions), uninstalled the shared kro and ACK controllers, deleted
the shared IAM roles, and removed an EKS access entry for `github-runner-org` — the ARC
runner role, which is core ADP's, not this domain's. Nothing scoped any of that to one
workspace, so "tear down my workspace" and "tear down the shared control plane every
workspace depends on" were adjacent commands with no boundary between them. And nearly
every destructive step ended in `2>/dev/null || true`, so a failed delete was reported as
completion.

## What this module does instead

`plan` produces a delete plan as DATA, scoped to resources the request owns, and refuses to
include anything it does not own. Nothing here executes a delete: the plan is reviewed, and
execution is a separate authorized operation. The three refusal rules:

1.  **Never outside the workspace.** A resource in another workspace's namespace, or in a
    core ADP namespace, is refused — not skipped with a warning.
2.  **Never a cluster ADP did not create.** In `bring-existing-cluster`, ADP adopted the
    cluster; a delete plan for that workspace removes the workspace's own namespaced
    resources and leaves the cluster.
3.  **Never the shared control plane, and never account closure.** Uninstalling kro/ACK or
    deleting the shared CRDs is a different act with a different scope (every workspace on
    the cluster depends on them), and closing an account is `closure_request`, below —
    which must be asked for by name, with the account id stated explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .modes import (
    AccountFactoryRequest,
    ClusterOwnership,
    OwnershipMode,
    ValidationAuthorization,
    ensure_valid,
)
from .render import MANAGED_BY

__all__ = [
    "CleanupError",
    "CleanupPlan",
    "CleanupScope",
    "ClosureRequest",
    "DeleteAction",
    "OwnershipEvidence",
    "ProvisionedAccountRecord",
    "closure_request",
    "plan",
]

_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")

_CORE_NAMESPACES = frozenset(
    {
        "adp",
        "adp-gateway",
        "adp-agent-factory",
        "adp-context",
        "adp-system",
        "bedrockgw",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "default",
        "arc-systems",
        "arc-runners",
        # The shared controller namespaces. Deleting these tears down the control plane
        # every workspace on the cluster depends on, which is not a workspace's cleanup.
        "kro-system",
        "ack-system",
    }
)

# Cluster-scoped kinds a workspace cleanup may never delete. The legacy
# `07-teardown-capabilities.sh` deleted exactly these — the kro-generated CRDs — as part of
# a teardown flow reachable right after a per-account teardown.
_SHARED_CLUSTER_KINDS = frozenset(
    {
        "CustomResourceDefinition",
        "ClusterRole",
        "ClusterRoleBinding",
        "ResourceGraphDefinition",
        "StorageClass",
        "PersistentVolume",
        "MutatingWebhookConfiguration",
        "ValidatingWebhookConfiguration",
    }
)


class CleanupError(Exception):
    """A delete plan was refused. No partial plan is returned."""


class CleanupScope(str, Enum):
    """How much a cleanup is being asked to remove.

    Two named scopes rather than one, because "remove the workspace's resources" and "close
    the AWS account" were the same command in the legacy flow. They are now different
    requests, and the second cannot be reached from the first.
    """

    WORKSPACE_RESOURCES = "workspace-resources"
    ACCOUNT_CLOSURE = "account-closure"


# The only kinds an Account Factory cleanup may delete. An allowlist, not a denylist: the
# previous guard rejected shared kinds and core namespaces and accepted EVERYTHING else in
# the workspace namespace, so an application's Secret, Deployment or PVC that happened to
# share the namespace was fair game for an infrastructure teardown. A denylist can only
# exclude what someone thought of; this can only include what this module creates.
#
# `Namespace` is absent deliberately. Deleting the namespace would delete every object in it,
# including application resources this module did not create — the single most destructive
# action available, and it would bypass every per-resource ownership check below by never
# examining those resources at all. Removing the workspace's namespace, if wanted, belongs to
# whatever owns the workspace's lifecycle.
_OWNED_KINDS = frozenset(
    {
        "WorkspaceInfrastructure",
        "NetworkStack",
        "EKSClusterStack",
        "IAMRoleSelector",
        "ConfigMap",
    }
)

# The account root, refused by a workspace cleanup in EVERY mode — including the mode that
# created the account.
#
# `AccountOwnership` declares the Organizations `Account`, so deleting it closes the AWS
# account into an irreversible 90-day suspension. That is exactly the legacy defect Design
# item 4 forbids: closure as a consequence of deleting a custom resource. The generated plan
# never deletes it, but a DISCOVERED resource of this kind would previously have been accepted
# on ownership evidence alone — and a genuinely owned `AccountOwnership` has perfectly valid
# evidence, so the ownership check could not refuse it. Ownership is the wrong question here:
# this object is ours, and that is precisely why deleting it is the destructive act that must
# go through `closure_request` instead.
_ACCOUNT_ROOT_KINDS = frozenset({"AccountOwnership", "Account"})

# What each mode may delete, because ADP may only delete what ADP created.
#
# `bring-existing-cluster` created no AWS infrastructure at all — no VPC, no cluster, no
# account — so the infrastructure kinds are not deletable in it, whatever evidence a discovered
# resource carries. Without this, a discovered `EKSClusterStack` or `WorkspaceInfrastructure`
# was accepted into an adopted-cluster teardown, which is ADP deleting a tenant's own cluster:
# the mode's central promise ("ADP adopted it and must not delete it") applied to the generated
# plan but not to the discovered one.
_INFRASTRUCTURE_KINDS = frozenset(
    {"WorkspaceInfrastructure", "NetworkStack", "EKSClusterStack"}
)
_DELETABLE_BY_MODE: dict[OwnershipMode, frozenset[str]] = {
    OwnershipMode.NEW_ACCOUNT_MANAGED: _OWNED_KINDS,
    OwnershipMode.EXISTING_ACCOUNT_MANAGED: _OWNED_KINDS,
    # ADP created only the access selection and the adoption record.
    OwnershipMode.BRING_EXISTING_CLUSTER: frozenset({"IAMRoleSelector", "ConfigMap"}),
}


@dataclass(frozen=True)
class OwnershipEvidence:
    """Proof, read from a live resource, that Account Factory created it.

    Every field is read from the resource as it exists on the cluster — its labels and its
    server-assigned `metadata.uid`. Nothing here is asserted by the caller about the
    resource; a caller who could assert ownership could assert it about anything.

    The uid is required because labels are mutable: a resource whose labels were edited to
    match could otherwise be presented as owned. A uid is assigned by the API server at
    creation and never changes, so recording it at provisioning time and requiring the match
    at deletion time is what makes the evidence immutable rather than merely present.
    """

    managed_by: str
    workspace_label: str
    uid: str

    @classmethod
    def from_live_object(cls, obj: dict) -> OwnershipEvidence:
        """Read evidence from a fetched Kubernetes object.

        A missing label or uid becomes an empty string rather than an error: absent evidence
        is the ordinary case for a resource this module did not create, and it is refused by
        `_require_ownership` rather than crashing discovery.
        """
        metadata = obj.get("metadata") or {}
        labels = metadata.get("labels") or {}
        return cls(
            managed_by=str(labels.get("app.kubernetes.io/managed-by") or ""),
            workspace_label=str(labels.get("adp.aws.dev/workspace") or ""),
            uid=str(metadata.get("uid") or ""),
        )


@dataclass(frozen=True)
class DeleteAction:
    """One resource to delete, with the namespace it is scoped to.

    `namespace` is always the workspace's own. There is no cluster-scoped variant of this
    type, which is the structural reason a workspace cleanup cannot express a cluster-wide
    delete.

    `evidence` is required for a DISCOVERED resource and unused for one this module rendered
    (which is owned by construction, having been named from the request). `recorded_uid` is
    the uid provisioning recorded for this resource; the discovered resource's uid must equal
    it.
    """

    kind: str
    name: str
    namespace: str
    reason: str
    evidence: OwnershipEvidence | None = None
    recorded_uid: str | None = None


def _require_ownership(action: DeleteAction, request: AccountFactoryRequest) -> None:
    """Refuse a discovered resource that is not provably this operation's.

    The rule is that being in the workspace's namespace establishes nothing. Four things must
    hold, and each closes a distinct way an unrelated resource could be swept up:

    1.  the kind is one this module creates — so an application's Deployment or Secret is
        refused on sight, whatever labels it carries;
    2.  it is labelled as managed by this module — so a resource created by something else
        is refused even when its kind collides (a `ConfigMap` is the obvious case);
    3.  its workspace label is THIS workspace — so one workspace's cleanup cannot reach
        another's resource that happened to be discovered;
    4.  its live uid matches the uid recorded at provisioning — so a resource whose labels
        were edited to look owned is still refused, and a resource deleted and recreated
        under the same name (a different object with the same identity on paper) is not
        deleted on the strength of its predecessor's record.

    Two refusals come BEFORE ownership, because for them ownership is not the question being
    asked. The account root is ours and must still never be deleted here (deleting it closes
    the account), and infrastructure in an adopted-cluster workspace was never ADP's to create
    — so neither can be excused by valid evidence.
    """
    if action.kind in _ACCOUNT_ROOT_KINDS:
        raise CleanupError(
            f"refusing to delete discovered {action.kind}/{action.name}: it declares the AWS "
            f"account, so deleting it would close the account into an irreversible 90-day "
            f"suspension. Account closure is never a consequence of a workspace cleanup — it "
            f"is a separate explicit request (`closure_request`), which additionally requires "
            f"a recorded provisioned account and an acknowledgement. Ownership evidence "
            f"cannot authorize this: a genuinely owned account root is exactly the object "
            f"that must not be deleted here"
        )

    deletable = _DELETABLE_BY_MODE.get(request.mode, frozenset())
    if action.kind not in deletable and action.kind in _INFRASTRUCTURE_KINDS:
        raise CleanupError(
            f"refusing to delete discovered {action.kind}/{action.name}: mode "
            f"{request.mode.value!r} created no AWS infrastructure, so this was not created "
            f"by ADP and is not ADP's to delete. ADP adopted this cluster and its network; "
            f"deleting them would destroy a tenant's own infrastructure. Ownership labels do "
            f"not change who created it"
        )

    if action.kind not in _OWNED_KINDS:
        raise CleanupError(
            f"refusing to delete discovered {action.kind}/{action.name}: Account Factory "
            f"does not create {action.kind} resources, so this is not its to delete. "
            f"Cleanup deletes only {', '.join(sorted(_OWNED_KINDS))} — sharing namespace "
            f"{action.namespace!r} is not ownership"
        )

    evidence = action.evidence
    if evidence is None:
        raise CleanupError(
            f"refusing to delete discovered {action.kind}/{action.name}: no ownership "
            f"evidence was supplied. A resource found in namespace {action.namespace!r} must "
            f"prove Account Factory created it; the legacy teardown deleted whatever it "
            f"found and reported success either way"
        )
    if evidence.managed_by != MANAGED_BY:
        raise CleanupError(
            f"refusing to delete discovered {action.kind}/{action.name}: its "
            f"app.kubernetes.io/managed-by is {evidence.managed_by or '<absent>'!r}, not "
            f"{MANAGED_BY!r}. It was created by something else and may be in active use"
        )
    if evidence.workspace_label != request.workspace_id:
        raise CleanupError(
            f"refusing to delete discovered {action.kind}/{action.name}: it is labelled for "
            f"workspace {evidence.workspace_label or '<absent>'!r}, not "
            f"{request.workspace_id!r}. Deleting another workspace's resource is refused"
        )
    if not action.recorded_uid:
        raise CleanupError(
            f"refusing to delete discovered {action.kind}/{action.name}: provisioning "
            f"recorded no uid for it. Labels are mutable, so a label match alone can be "
            f"manufactured; the recorded uid is what makes the evidence immutable"
        )
    if not evidence.uid:
        raise CleanupError(
            f"refusing to delete discovered {action.kind}/{action.name}: the live resource "
            f"reports no metadata.uid, so it cannot be matched against the recorded one"
        )
    if evidence.uid != action.recorded_uid:
        # Same name, different object. Deleting it would destroy a resource that merely
        # inherited a name from one this module created.
        raise CleanupError(
            f"refusing to delete discovered {action.kind}/{action.name}: its uid does not "
            f"match the uid recorded at provisioning. Same name, different object — the "
            f"recorded resource no longer exists and this one was created by something else"
        )


@dataclass(frozen=True)
class CleanupPlan:
    """A reviewed delete plan. Executing it is a separate authorized operation.

    `retained` exists so the plan says what it is deliberately NOT deleting. A plan that
    silently omits the cluster is indistinguishable from a plan that forgot it; naming the
    retention makes the boundary reviewable.
    """

    request: AccountFactoryRequest
    scope: CleanupScope
    actions: tuple[DeleteAction, ...]
    retained: tuple[str, ...]
    closes_account: bool = False
    # The retained root kinds as data, alongside the prose in `retained`. Exposed so a
    # reviewer (and a test) can check the convergence property directly instead of reading it
    # out of an English sentence.
    retained_roots: tuple[str, ...] = ()

    def describes_account_closure(self) -> bool:
        """Whether this plan closes an AWS account. Always False for a workspace cleanup."""
        return self.closes_account


def _guard_action(
    action: DeleteAction,
    request: AccountFactoryRequest,
    *,
    require_evidence: bool = False,
) -> None:
    """Refuse an action that reaches outside the workspace. Refusal, never a skip.

    `require_evidence` is set for DISCOVERED resources. A resource this module rendered is
    owned by construction — the plan named it from the request. A resource found on the
    cluster is not: it is in the workspace's namespace, which is a statement about location,
    not about ownership. So a discovered resource must additionally be of a kind this module
    creates AND carry evidence that this module created it.

    ## Why location is checked before ownership

    The two checks refuse overlapping sets, and a resource outside the workspace also has no
    valid ownership evidence — so either order refuses it. Location runs first for two
    reasons. The refusal a reviewer reads should name the most specific thing wrong: "that is
    a core ADP namespace" is actionable, while "no ownership evidence" invites someone to go
    looking for evidence that must never be sufficient there. And the boundaries stay
    INDEPENDENTLY exercised — if the ownership check ever regressed, the location tests would
    still fail rather than passing for the wrong reason.
    """
    if action.kind in _SHARED_CLUSTER_KINDS:
        raise CleanupError(
            f"refusing to delete {action.kind}/{action.name}: it is cluster-scoped and "
            f"shared by every workspace on this management cluster. Removing the shared "
            f"control plane is a separate operation with a separate authorization, not part "
            f"of cleaning up workspace {request.workspace_id!r}"
        )
    if action.namespace in _CORE_NAMESPACES:
        raise CleanupError(
            f"refusing to delete {action.kind}/{action.name} in namespace "
            f"{action.namespace!r}: that is a core ADP, Kubernetes or shared-controller "
            f"namespace, not workspace {request.workspace_id!r}"
        )
    if action.namespace != request.workspace_id:
        raise CleanupError(
            f"refusing to delete {action.kind}/{action.name} in namespace "
            f"{action.namespace!r}: workspace {request.workspace_id!r} owns only its own "
            f"namespace. Deleting another workspace's resources is refused"
        )
    # Last, because being in the right namespace is a necessary condition for ownership and
    # never a sufficient one.
    if require_evidence:
        _require_ownership(action, request)


# Which kinds each root object DECLARES. A controller makes reality match a declaration, so
# deleting a resource whose declaring root survives is not a deletion — it is a request that
# the controller rebuild it. This is the reconciliation fact the previous plan violated.
_DECLARED_BY = {
    "WorkspaceInfrastructure": frozenset({"NetworkStack", "EKSClusterStack"}),
    "AccountOwnership": frozenset({"Account", "IAMRoleSelector"}),
    # Retained as vendored evidence only, never instantiated — recorded here so that
    # instantiating it in future cannot quietly reintroduce the non-convergent plan.
    "FullAccountInfrastructure": frozenset(
        {"Namespace", "Account", "IAMRoleSelector", "NetworkStack", "EKSClusterStack"}
    ),
}


def _check_convergence(
    actions: list[DeleteAction], retained_roots: frozenset[str]
) -> None:
    """Refuse a plan that deletes something a retained root object still declares.

    A delete plan is only meaningful if it converges: after execution, the deleted resources
    must stay deleted. They do not if any retained root still declares them, because a
    controller's job is to recreate whatever its declaration says should exist. The previous
    plan deleted `NetworkStack` and `EKSClusterStack` while retaining the
    `FullAccountInfrastructure` that declares both, so executing it would have deleted real
    infrastructure and then had it rebuilt — spend recreated after a teardown reported
    success.

    `retained_roots` is passed as data rather than derived from the human-readable `retained`
    strings on purpose. Reading it out of that prose is what an earlier draft of this function
    did, and it misfired immediately: the existing-account-managed explanation contains the
    phrase "no AccountOwnership object was ever created for it", and a substring match read
    that as a retained `AccountOwnership`. Prose explains a decision to a reader; it must not
    also BE the decision a check reads.

    This asserts the property rather than a list of kinds, so a future edit that retains a
    declaring root fails here even if no test named that combination.

    It must be given the FINAL action set, including resources discovered from live state.
    Convergence is a property of the plan that gets executed, not of the part of it this module
    generated: a discovered `IAMRoleSelector` deleted while `AccountOwnership` is retained is
    reconciled straight back, exactly as a generated one would be.
    """
    deleted_kinds = {action.kind for action in actions}
    for root in sorted(retained_roots):
        rebuilt = sorted(_DECLARED_BY.get(root, frozenset()) & deleted_kinds)
        if rebuilt:
            raise CleanupError(
                f"this plan cannot converge: it deletes {', '.join(rebuilt)} while retaining "
                f"{root}, which declares them. A controller reconciling the retained {root} "
                f"would recreate them, so the teardown would report success and then rebuild "
                f"the resources — and the spend — it had just removed"
            )


def plan(
    request: AccountFactoryRequest,
    authorization: ValidationAuthorization | None = None,
    *,
    extra_actions: tuple[DeleteAction, ...] = (),
) -> CleanupPlan:
    """A delete plan for one workspace's resources. Never closes an account.

    `extra_actions` lets a caller add resources discovered on the cluster (a plan built from
    live state rather than only from the request). Each one goes through the same guard, so
    a resource discovered outside the workspace is refused rather than trusted because it
    came from the cluster — the legacy flow's `|| true` treated whatever it found as fair
    game.

    Validates the request first: a delete plan built from an unvalidated request could name
    a workspace the caller does not own.
    """
    ensure_valid(request, authorization)

    namespace = request.workspace_id
    actions: list[DeleteAction] = []
    retained: list[str] = []
    # The retained ROOT KINDS, as data. `retained` holds the human explanation; this holds
    # what `_check_convergence` reads. See that function for why the two must not be one.
    retained_roots: set[str] = set()

    if request.mode is OwnershipMode.NEW_ACCOUNT_MANAGED:
        # Delete the infrastructure ROOT, not the stacks inside it. Those stacks are declared
        # by `WorkspaceInfrastructure`, so deleting them while it survives would have kro
        # reconcile them straight back — a plan that cannot converge and re-creates spend
        # after reporting a teardown. Deleting the root removes the declaration too, so what
        # is deleted stays deleted.
        #
        # This is only safe because `AccountOwnership` is a SEPARATE root object: nothing
        # retained here declares the VPC or the cluster, and nothing deleted here declares
        # the account.
        actions.append(
            DeleteAction(
                kind="WorkspaceInfrastructure",
                name=request.workspace_id,
                namespace=namespace,
                reason=(
                    "the VPC and cluster ADP created for this workspace. Deleting this root "
                    "removes their declaration as well, so they are not reconciled back"
                ),
            )
        )
        retained_roots.add("AccountOwnership")
        retained.append(
            "the AccountOwnership custom resource and the AWS account it owns — deleting it "
            "deletes the Organizations `Account`, which closes the account into an "
            "irreversible 90-day suspension. Closure is a separate explicit request "
            "(`closure_request`), never a consequence of removing a workspace. Retaining it "
            "does NOT keep the infrastructure alive: the infrastructure is a separate root "
            "object, which is why this plan converges"
        )
    elif request.mode is OwnershipMode.EXISTING_ACCOUNT_MANAGED:
        actions.append(
            DeleteAction(
                kind="WorkspaceInfrastructure",
                name=request.workspace_id,
                namespace=namespace,
                reason=(
                    "the VPC and cluster ADP created inside the adopted account. Deleting "
                    "this root removes their declaration as well"
                ),
            )
        )
        retained.append(
            "the AWS account — it existed before this workspace and was adopted, not "
            "created. ADP does not close an account it did not open, and no AccountOwnership "
            "object was ever created for it"
        )
    elif request.mode is OwnershipMode.BRING_EXISTING_CLUSTER:
        # No stacks: ADP created no AWS infrastructure in this mode, so there is nothing
        # here whose deletion would be ADP's to perform.
        retained.append(
            f"the EKS cluster {request.cluster_name!r} — ADP adopted it and did not create "
            f"it ({ClusterOwnership.ADOPTED.value}), so ADP must not delete it"
        )
        retained.append("the AWS account — adopted, not created by ADP")
        retained.append("the VPC, subnets and node groups — none were created by ADP")
    else:  # pragma: no cover - defensive; OwnershipMode is exhaustive
        raise CleanupError(f"no cleanup plan for mode {request.mode!r}")

    if request.mode is not OwnershipMode.NEW_ACCOUNT_MANAGED:
        # Only in the modes where the renderer emits a STANDALONE IAMRoleSelector. In
        # new-account-managed the role selector is declared inside `AccountOwnership` — whose
        # ARN is built from the account's own status, which is why it lives there — and
        # `AccountOwnership` is retained. Deleting a resource its retained declaring root
        # still declares would not converge; `_check_convergence` below refuses exactly that,
        # and caught this case when the delete was unconditional.
        #
        # Nothing is orphaned by the omission: retaining the account retains its access path,
        # which is the coherent state for "the account stays, its infrastructure goes".
        actions.append(
            DeleteAction(
                kind="IAMRoleSelector",
                name=request.workspace_id,
                namespace=namespace,
                reason="the cross-account role selection this workspace added",
            )
        )
    if request.mode is OwnershipMode.BRING_EXISTING_CLUSTER:
        actions.append(
            DeleteAction(
                kind="ConfigMap",
                name=f"{request.workspace_id}-adopted-cluster",
                namespace=namespace,
                reason="the adoption record this workspace added",
            )
        )

    # Rendered actions are owned by construction — the plan named them from the request.
    # Discovered ones must prove it.
    for action in actions:
        _guard_action(action, request)
    for action in extra_actions:
        _guard_action(action, request, require_evidence=True)

    # Over the FINAL set, not just the generated one. Checking only `actions` left the
    # convergence property unenforced on exactly the half of the plan that comes from live
    # cluster state — where a leftover resource is most likely to be found and deleted.
    _check_convergence([*actions, *extra_actions], frozenset(retained_roots))

    retained.append(
        "the shared kro and ACK controllers, their namespaces, and the "
        "ResourceGraphDefinitions — every workspace on this management cluster depends on "
        "them. The legacy teardown removed these (and an EKS access entry belonging to "
        "core ADP's ARC runner role) with no workspace boundary"
    )

    return CleanupPlan(
        request=request,
        scope=CleanupScope.WORKSPACE_RESOURCES,
        actions=(*actions, *extra_actions),
        retained=tuple(retained),
        closes_account=False,
        retained_roots=tuple(sorted(retained_roots)),
    )


@dataclass(frozen=True)
class ClosureRequest:
    """An explicit request to close an AWS account. Separate by construction.

    Cannot be produced by `plan`, and carries no default: the account id must be stated,
    and it must be an account ADP opened. `acknowledged_irreversible` must be set by the
    caller — a closure that was not deliberately acknowledged is refused, because the
    legacy flow's equivalent was a note printed after the deletion had already happened.
    """

    account_id: str
    workspace_id: str
    reason: str
    acknowledged_irreversible: bool
    scope: CleanupScope = CleanupScope.ACCOUNT_CLOSURE

    @property
    def consequence(self) -> str:
        return (
            f"account {self.account_id} enters AWS's 90-day suspended state. This is not "
            f"reversible on demand and the account id cannot be reused during that period."
        )


@dataclass(frozen=True)
class ProvisionedAccountRecord:
    """A durable record that Account Factory opened one specific AWS account.

    Written when new-account provisioning succeeded, by whatever holds provisioning state —
    read from the `AccountOwnership` object's `status.accountId` and the workspace it was
    created for. It is the ONLY thing that makes an account closable.

    Requiring a record inverts the previous default. Checking that a supplied id is non-empty
    accepts every well-formed account number in existence, including accounts belonging to
    other teams and other organizations; requiring a match against a record accepts exactly
    one. For the single most destructive and least reversible action in this module — a 90-day
    suspension that cannot be lifted on demand — the default must be refusal.
    """

    account_id: str
    workspace_id: str
    organization_id: str

    def __post_init__(self) -> None:
        if not _ACCOUNT_ID_RE.match(self.account_id or ""):
            raise CleanupError(
                f"a provisioned-account record must carry a 12-digit AWS account id, not "
                f"{self.account_id!r}. A record with a malformed id could authorize closing "
                f"nothing identifiable"
            )
        if not (self.workspace_id or "").strip():
            raise CleanupError(
                "a provisioned-account record must name the workspace it was created for"
            )
        if not (self.organization_id or "").strip():
            raise CleanupError(
                "a provisioned-account record must name the organization it was created in"
            )


def closure_request(
    request: AccountFactoryRequest,
    *,
    account_id: str,
    reason: str,
    acknowledged_irreversible: bool,
    provisioned: ProvisionedAccountRecord,
    authorization: ValidationAuthorization | None = None,
) -> ClosureRequest:
    """Build an account-closure request, refusing every implicit route to one.

    Closure is irreversible: the account enters a 90-day suspension that cannot be lifted on
    demand. So this refuses unless every one of the following holds, and each closes a
    distinct way the wrong account could be closed:

    * the request itself validates against its authorization — a closure built from an
      unvalidated request could name a workspace the caller does not own;
    * the mode created the account — ADP does not close an account it adopted;
    * the caller acknowledged the irreversible consequence explicitly;
    * a reason was given;
    * `account_id` has the shape of a real AWS account id;
    * **it matches a durable record of an account this module opened for THIS workspace and
      organization.** This is the check whose absence meant any well-formed account id was
      accepted — an executor trusting the resulting object could have closed an unrelated
      account while every other safeguard here reported success.
    """
    # Validate first: without this, a closure could be built for a workspace or organization
    # the run was never authorized to act on, and the record comparison below would then be
    # against the wrong expectation.
    ensure_valid(request, authorization)

    if not request.mode.creates_account:
        raise CleanupError(
            f"refusing to build an account-closure request for mode "
            f"{request.mode.value!r}: ADP adopted this account rather than creating it, so "
            f"closing it is not ADP's to do"
        )
    if not acknowledged_irreversible:
        raise CleanupError(
            "refusing to build an account-closure request without an explicit "
            "acknowledgement: closure puts the account into an irreversible 90-day "
            "suspended state. The legacy flow printed this consequence AFTER the delete "
            "had already been issued"
        )
    if not (reason or "").strip():
        raise CleanupError("an account-closure request must state a reason")
    if not (account_id or "").strip():
        raise CleanupError(
            "an account-closure request must state the account id. It is neither defaulted "
            "nor derived from the workspace: the account to close is named deliberately or "
            "not at all"
        )
    if not _ACCOUNT_ID_RE.match(account_id.strip()):
        raise CleanupError(
            f"refusing to close {account_id!r}: an AWS account id is a 12-digit number. A "
            f"value that is not an account id identifies nothing, so closing it cannot be "
            f"verified"
        )

    account_id = account_id.strip()
    if provisioned is None:
        # Refused, not defaulted to trusting `account_id`. That fallback is precisely the
        # defect: it accepted any well-formed id, including a real unrelated account, while
        # every other check here reported success.
        raise CleanupError(
            f"refusing to close {account_id}: no provisioned-account record was supplied, so "
            f"there is nothing to verify the id against. Closure is irreversible, so an "
            f"unverifiable target is refused rather than trusted"
        )
    if provisioned.workspace_id != request.workspace_id:
        raise CleanupError(
            f"refusing to close {account_id}: the provisioned-account record belongs to "
            f"workspace {provisioned.workspace_id!r}, not {request.workspace_id!r}. One "
            f"workspace's record cannot authorize closing another's account"
        )
    if provisioned.organization_id != request.organization_id:
        raise CleanupError(
            f"refusing to close {account_id}: the provisioned-account record is in "
            f"organization {provisioned.organization_id!r}, not "
            f"{request.organization_id!r}"
        )
    if provisioned.account_id != account_id:
        raise CleanupError(
            f"refusing to close {account_id}: Account Factory's record for workspace "
            f"{request.workspace_id!r} is account {provisioned.account_id}. Only the account "
            f"this module actually opened for this workspace may be closed — a different "
            f"well-formed id is exactly the case that must be refused, because it names "
            f"someone else's account"
        )

    return ClosureRequest(
        account_id=account_id,
        workspace_id=request.workspace_id,
        reason=reason,
        acknowledged_irreversible=True,
    )
