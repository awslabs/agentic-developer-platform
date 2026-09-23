"""Gate: the workspace runtime actually exists and is available, before readiness.

Issue #5533 (w6-10), EPIC #4910. Added by the F2 repair.

## What the first revision declared ready, and why that was wrong

`install_components` created or adopted a namespace, established the CRDs, and
returned success — and the orchestration then went straight to the isolation probes,
removed the scheduling taint and registered the workspace. Review finding F2: nothing
had installed or verified a controller, nothing had completed the single-controller
handover, nothing had established the scoped RBAC the controller runs under, and
nothing had placed or checked the cluster's own system services.

That last one is not hypothetical on the selected deployment. The live handoff records
that **CoreDNS remains unscheduled behind the bootstrap taint**: the taint that keeps
tenant work off the nodes is also keeping the cluster's DNS off them. So the first
revision could clear the taint and publish a Ready workspace whose pods would schedule
onto nodes with no DNS and no controller — a workspace that accepts work and cannot
run it, which is exactly the "cluster ready but bootstrap failed" defect AC-01 names.

A registration is the domain's statement that work may be scheduled here. This module
is what makes that statement true rather than merely permitted.

## The tenant scheduling interlock is preserved, not worked around

The obvious way to get CoreDNS running is to remove the taint first. That is
forbidden: the handoff says so directly ("Do not assign a tenant toleration to bypass
bootstrap"), and it would put tenant pods on nodes whose isolation is still unproved.

The distinction this module relies on is between a **system** workload and a
**tenant** workload. A system workload in a system namespace may carry a toleration
for the bootstrap taint, because it is ADP's own software and its scheduling is not
what the interlock exists to prevent. A tenant workload may not, ever. So
`prepare_system_workloads` asks for placement of the named system components and then
verifies — via `tenant_scheduling_denied` — that a tenant pod is STILL unschedulable
afterwards. Preparing the system side without that check would be indistinguishable
from removing the interlock.

## Why every check requires an explicit observation

Same rule as `admission.py`: an unanswered question is not a negative answer. A probe
that cannot determine whether CoreDNS has available replicas must refuse, because the
natural implementation returns something falsy and falsy reads as "fine" in the
direction that matters here. Each check below requires the specific fact it claims,
and a missing fact is a refusal naming what was not observed.

## What this module does NOT do

It does not build the controller image, choose its version, or own the workspace
component manifests — those belong to the release owner and to #5534's composition.
It establishes what the workspace needs to function, verifies availability, and
refuses otherwise. Like every other module here it holds no credential and performs no
delete.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .access import ClusterAccess, ObservedWorkload
from .errors import BootstrapRefused

# The system workloads a workspace cluster needs before ANY pod can usefully run.
# CoreDNS is named explicitly because the live handoff records it as unscheduled behind
# the bootstrap taint on the selected deployment: without it, a scheduled tenant pod
# cannot resolve the API server, the database or anything else.
REQUIRED_SYSTEM_WORKLOADS: tuple[str, ...] = ("coredns",)

# The namespace system workloads live in. Separate from the tenant namespace on
# purpose: a toleration for the bootstrap taint is acceptable here and never in the
# tenant namespace, and keeping them in different namespaces is what lets admission
# and RBAC express that difference.
SYSTEM_NAMESPACE = "kube-system"

# The scoped permissions the workspace controller must hold, and must hold ONLY.
# Mirrors the verbs `installation/runner.py::Installer.workspace()` already checks with
# `kubectl auth can-i` in its preflight, so the credential this gate establishes is by
# construction the credential that preflight later accepts. Restating a different set
# here is how the two disagree and a workspace passes install and fails preflight.
REQUIRED_CONTROLLER_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("list", "nodes"),
    ("watch", "pods"),
    ("watch", "nodepools.superplane.ai"),
    ("watch", "superplanenodes.superplane.ai"),
    ("create", "leases.coordination.k8s.io"),
    ("update", "leases.coordination.k8s.io"),
)

# Permissions the controller credential must NOT hold. Checked explicitly because
# "has everything it needs" and "has only what it needs" are different claims, and a
# cluster-admin credential satisfies the first while failing the second. A controller
# that can create ClusterRoleBindings can escalate past every boundary this package
# establishes.
FORBIDDEN_CONTROLLER_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("create", "clusterrolebindings.rbac.authorization.k8s.io"),
    ("delete", "namespaces"),
    ("get", "secrets"),
)


@dataclass(frozen=True)
class ReadinessCheck:
    """One named readiness fact, with the observation that established it.

    Same shape and same rule as `admission.AdmissionProof`: an unverified check must
    carry a detail, because an unexplained negative cannot be told apart from a check
    that never ran.
    """

    name: str
    verified: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise BootstrapRefused("ReadinessCheck.name is required")
        if not self.verified and not self.detail.strip():
            raise BootstrapRefused(
                f"ReadinessCheck {self.name!r} is unverified with no detail; an "
                "unexplained negative cannot be told apart from a check that did "
                "not run"
            )


@dataclass(frozen=True)
class RuntimeReadiness:
    """Every readiness fact for one workspace, and whether the runtime is usable.

    `usable` is computed, never stored — the same reason
    `IsolationEvidence.may_clear_taint` is a property. No code path may construct a
    readiness object asserting a runtime it did not observe.
    """

    namespace: str
    checks: tuple[ReadinessCheck, ...] = field(default_factory=tuple)

    @property
    def unverified(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.verified)

    @property
    def usable(self) -> bool:
        """True only when every readiness check has an actual verified result."""
        return bool(self.checks) and not self.unverified

    @property
    def failures(self) -> tuple[str, ...]:
        """Name and detail for each unverified check, for a refusal message."""
        return tuple(
            f"{check.name}: {check.detail}"
            for check in self.checks
            if not check.verified
        )


def _workload_check(
    name: str, observed: ObservedWorkload | None, *, what: str
) -> ReadinessCheck:
    """A workload must exist AND report available replicas.

    "Exists" is not "available": a Deployment with `availableReplicas: 0` is present in
    every listing and serves nothing. On the selected deployment that is the expected
    state for CoreDNS while the bootstrap taint is still in place, which is precisely
    why this distinction is the one that matters here.
    """
    if observed is None:
        return ReadinessCheck(
            name=name,
            verified=False,
            detail=(
                f"{what} is absent from the cluster; a workspace registered without "
                "it would accept work that cannot run"
            ),
        )
    if observed.desired_replicas <= 0:
        return ReadinessCheck(
            name=name,
            verified=False,
            detail=(
                f"{what} is present but desires no replicas, so it will never become "
                "available"
            ),
        )
    if observed.available_replicas <= 0:
        return ReadinessCheck(
            name=name,
            verified=False,
            detail=(
                f"{what} has no available replicas "
                f"({observed.available_replicas}/{observed.desired_replicas}); it is "
                "scheduled or pending, not serving"
            ),
        )
    return ReadinessCheck(name=name, verified=True)


def _system_workload_checks(
    access: ClusterAccess, required: Sequence[str]
) -> list[ReadinessCheck]:
    """Each required system workload must be available in the system namespace."""
    checks: list[ReadinessCheck] = []
    for workload in required:
        observed = access.workload(SYSTEM_NAMESPACE, workload)
        checks.append(
            _workload_check(
                f"system_workload_available:{workload}",
                observed,
                what=f"the required system workload {workload!r} in {SYSTEM_NAMESPACE}",
            )
        )
    return checks


def _controller_checks(
    access: ClusterAccess, namespace: str, controller_name: str
) -> list[ReadinessCheck]:
    """The controller must be present, available, and the SOLE reconciler.

    Three separate facts, deliberately not collapsed:

    - It exists and is available (a workspace with no controller reconciles nothing).
    - Exactly one controller reconciles this cluster. `components.py` refuses at
      install time if any controller is already present; this re-checks AFTER
      installation, which is the only place a second one could appear — and two
      controllers on one cluster-scoped NodePool set contend continuously rather than
      failing cleanly.
    - The handover is complete. A previous controller that is still holding the
      coordination lease has not handed over, even when its Deployment is gone.
    """
    checks = [
        _workload_check(
            "workspace_controller_available",
            access.workload(namespace, controller_name),
            what=f"the workspace controller {controller_name!r} in {namespace}",
        )
    ]

    reconcilers = [image for image in access.controller_deployments() if image.strip()]
    if len(reconcilers) != 1:
        checks.append(
            ReadinessCheck(
                name="single_controller_reconciles_the_cluster",
                verified=False,
                detail=(
                    f"{len(reconcilers)} controller deployment(s) reconcile this "
                    "cluster, not exactly one; two controllers contend over the same "
                    "cluster-scoped NodePools continuously"
                ),
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                name="single_controller_reconciles_the_cluster", verified=True
            )
        )

    handover = access.controller_handover(namespace)
    if "complete" not in handover:
        checks.append(
            ReadinessCheck(
                name="controller_handover_complete",
                verified=False,
                detail=(
                    "no observation of whether a previous controller completed its "
                    "handover; an unanswered question is not a completed handover"
                ),
            )
        )
    elif not handover["complete"]:
        checks.append(
            ReadinessCheck(
                name="controller_handover_complete",
                verified=False,
                detail=(
                    "a previous workspace controller has not completed its explicit "
                    f"handover (holder: {handover.get('holder', 'unknown')!r}); it may "
                    "still be reconciling this workspace"
                ),
            )
        )
    else:
        checks.append(
            ReadinessCheck(name="controller_handover_complete", verified=True)
        )

    return checks


def _rbac_checks(access: ClusterAccess, namespace: str) -> list[ReadinessCheck]:
    """The controller credential must hold every required verb and no forbidden one."""
    observed = access.controller_permissions(namespace)

    missing = [
        f"{verb} {resource}"
        for verb, resource in REQUIRED_CONTROLLER_PERMISSIONS
        if (verb, resource) not in observed
    ]
    if missing:
        return [
            ReadinessCheck(
                name="controller_rbac_scoped",
                verified=False,
                detail=(
                    "no observation for " + ", ".join(missing) + "; an unanswered "
                    "permission question is not a granted permission or a denied one"
                ),
            )
        ]

    denied = [
        f"{verb} {resource}"
        for verb, resource in REQUIRED_CONTROLLER_PERMISSIONS
        if not observed[(verb, resource)]
    ]
    if denied:
        return [
            ReadinessCheck(
                name="controller_rbac_scoped",
                verified=False,
                detail=(
                    "the workspace controller credential lacks required scoped "
                    "permissions (" + ", ".join(denied) + "), so the controller "
                    "cannot reconcile the workspace it is registered for"
                ),
            )
        ]

    # The other half: holding only what it needs. A credential that can bind cluster
    # roles or read secrets can escalate past every boundary established here.
    granted_forbidden = [
        f"{verb} {resource}"
        for verb, resource in FORBIDDEN_CONTROLLER_PERMISSIONS
        if observed.get((verb, resource)) is True
    ]
    if granted_forbidden:
        return [
            ReadinessCheck(
                name="controller_rbac_scoped",
                verified=False,
                detail=(
                    "the workspace controller credential holds permissions it must "
                    "not (" + ", ".join(granted_forbidden) + "); a controller that "
                    "can escalate makes the tenant boundary advisory"
                ),
            )
        ]

    return [ReadinessCheck(name="controller_rbac_scoped", verified=True)]


def _interlock_check(access: ClusterAccess, namespace: str) -> ReadinessCheck:
    """Tenant scheduling must STILL be denied after the system side was prepared.

    This is the check that makes `prepare_system_workloads` safe. Placing system
    workloads means granting something a toleration for the bootstrap taint, and the
    failure mode is granting it too broadly — a toleration on a shared default, or the
    taint removed outright. Either would let tenant pods schedule before their
    isolation is proved, so the interlock is re-verified rather than assumed.
    """
    denied = access.tenant_scheduling_denied(namespace)
    if denied is None:
        return ReadinessCheck(
            name="tenant_scheduling_still_denied",
            verified=False,
            detail=(
                "no observation of whether a tenant pod is still unschedulable; "
                "system-workload placement must not have relaxed the interlock, and "
                "an unanswered question does not establish that"
            ),
        )
    if not denied:
        return ReadinessCheck(
            name="tenant_scheduling_still_denied",
            verified=False,
            detail=(
                "a tenant pod is already schedulable while bootstrap is incomplete; "
                "preparing system workloads must not grant tenant workloads a "
                "toleration for the bootstrap taint"
            ),
        )
    return ReadinessCheck(name="tenant_scheduling_still_denied", verified=True)


def prepare_system_workloads(
    *,
    access: ClusterAccess,
    required: Sequence[str] = REQUIRED_SYSTEM_WORKLOADS,
) -> Mapping[str, bool]:
    """Place the required system workloads while tenant scheduling stays denied.

    Returns which workloads the cluster reports as placed. Placement is requested
    through a seam that names the SYSTEM namespace and the specific workloads, so this
    package cannot express "tolerate the bootstrap taint everywhere" — the seam has no
    way to say it. `establish_runtime_readiness` then verifies both that the workloads
    became available and that a tenant pod is still unschedulable.
    """
    if not required:
        raise BootstrapRefused(
            "no system workloads were declared as required; an empty set would let "
            "this gate pass without preparing the runtime a tenant pod needs"
        )
    placed = access.place_system_workloads(SYSTEM_NAMESPACE, tuple(required))
    unplaced = [name for name in required if not placed.get(name)]
    if unplaced:
        raise BootstrapRefused(
            "the cluster did not accept placement for required system workload(s): "
            + ", ".join(sorted(unplaced))
            + ". Without them a scheduled tenant pod cannot resolve or reach anything"
        )
    return dict(placed)


def establish_runtime_readiness(
    *,
    access: ClusterAccess,
    namespace: str,
    controller_name: str,
    required_system_workloads: Sequence[str] = REQUIRED_SYSTEM_WORKLOADS,
) -> RuntimeReadiness:
    """Verify the workspace runtime is actually available. Returns facts, not a verdict.

    Returns readiness whether or not it verifies, for the same reason
    `prove_tenant_isolation` does: a refusal that returned nothing would give the
    operator no way to see WHICH part of the runtime is missing. `workspace.py` decides,
    and clears the interlock only when `usable` is true.

    Called AFTER `prepare_system_workloads` and BEFORE the taint is cleared. That is
    the whole ordering F2 asked for: establish the runtime, verify it, and only then
    permit tenant scheduling and registration.
    """
    if not namespace.strip():
        raise BootstrapRefused("a workspace namespace is required to verify readiness")
    if not controller_name.strip():
        raise BootstrapRefused(
            "a workspace controller name is required; without one this gate would "
            "report a ready runtime without having looked for the controller"
        )

    checks: list[ReadinessCheck] = []
    checks.extend(_rbac_checks(access, namespace))
    checks.extend(_system_workload_checks(access, required_system_workloads))
    checks.extend(_controller_checks(access, namespace, controller_name))
    checks.append(_interlock_check(access, namespace))

    return RuntimeReadiness(namespace=namespace, checks=tuple(checks))
