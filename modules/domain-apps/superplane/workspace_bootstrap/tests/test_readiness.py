"""The runtime readiness gate — Issue #5533 (w6-10), review finding F2.

F2 verbatim: readiness was "declared after only namespace + CRD setup — no controller
install/verification, no single-controller handover, no CoreDNS placement (CoreDNS
unscheduled behind the pending taint), no readiness check."

Two kinds of test live here, and the split is deliberate.

The first kind reads `establish_runtime_readiness` directly and asserts WHICH check
failed and what its detail says. That granularity is the point: a gate that refused
with "not ready" would be correct and useless, because the operator's next action
differs completely between "CoreDNS has no replicas" and "the controller credential
can read secrets".

The second kind runs the real `bootstrap_workspace` and asserts the two consequences
that actually protect a tenant: **the bootstrap taint is still on the nodes** and **the
workspace is still unregistered**. Those are the assertions that would have caught F2.
A unit test proving `usable is False` proves nothing about safety on its own — the
first revision's readiness object was never consulted before the taint came off, so a
correct verdict computed and ignored is exactly the defect. Every negative below is
therefore asserted in both places.

The interlock cases deserve one note. `prepare_system_workloads` is the step that grants
something a toleration for the bootstrap taint, so it is the step that could relax the
interlock. Tests here pin both directions: the placement happens in `kube-system` and
names only the required workloads, and readiness re-verifies afterwards that a tenant pod
is STILL unschedulable — so a future change that got CoreDNS running by removing the
taint fails here rather than in production.
"""

from __future__ import annotations

import dataclasses

import pytest
from superplane_bootstrap.access import ObservedWorkload
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.prerequisites import ExpectedPrerequisites
from superplane_bootstrap.readiness import (
    FORBIDDEN_CONTROLLER_PERMISSIONS,
    REQUIRED_CONTROLLER_PERMISSIONS,
    REQUIRED_SYSTEM_WORKLOADS,
    SYSTEM_NAMESPACE,
    ReadinessCheck,
    RuntimeReadiness,
    establish_runtime_readiness,
    prepare_system_workloads,
)
from superplane_bootstrap.workspace import BOOTSTRAP_TAINT_KEY, bootstrap_workspace
from superplane_contracts.secrets import assert_no_secret_material

from .conftest import (
    ACCOUNT_ID,
    CLUSTER_SG_ID,
    CNI_ROLE_ARN,
    CONTROLLER_NAME,
    CREDENTIAL_ID,
    ENFORCE_VERSION,
    MANAGEMENT_SG_ID,
    NAMESPACE,
    VPC_ID,
    WORKSPACE_ID,
    FakeClusterAccess,
    FakePrerequisiteAccess,
    FakeRegistrationStore,
    FakeStateStore,
)


def _installed(access: FakeClusterAccess) -> FakeClusterAccess:
    """Put the access fake in the state readiness is asked about: post-install.

    `establish_runtime_readiness` runs AFTER `install_components`, so a fake handed
    straight to it has not had its controller installed and would fail the availability
    check for a reason no test here is about. Rather than seeding `workloads` by hand —
    which would let these tests pass against a package that installs nothing, the very
    thing F2 was about — this calls the real install seams. The controller the readiness
    gate then reads is one the production seam actually created.
    """
    access.establish_controller_rbac(NAMESPACE, CONTROLLER_NAME)
    access.install_controller(NAMESPACE, CONTROLLER_NAME, CONTROLLER_NAME)
    return access


def _readiness(access: FakeClusterAccess) -> RuntimeReadiness:
    return establish_runtime_readiness(
        access=access, namespace=NAMESPACE, controller_name=CONTROLLER_NAME
    )


def _failed(readiness: RuntimeReadiness, name: str) -> ReadinessCheck:
    """The named check, asserted to have failed.

    Looking the check up by name rather than by index is what keeps these tests honest
    when a check is added: an index-based assertion would silently start reading a
    different check's result.
    """
    matching = [check for check in readiness.checks if check.name == name]
    assert matching, (
        f"no readiness check named {name!r} ran; checks were "
        f"{[check.name for check in readiness.checks]}"
    )
    check = matching[0]
    assert check.verified is False, f"expected {name!r} to fail, but it verified"
    return check


def _run(
    access,
    store,
    binding,
    provider_identity,
    observed_cluster,
    expected_target,
    **overrides,
):
    """The real entry point, with every mandatory seam supplied.

    Duplicated from `test_workspace.py` rather than shared, on purpose: this module
    asserts the taint/registration consequences of READINESS failures specifically, and
    a helper shared between the two would couple the F2 evidence to unrelated edits in
    the other file's fixtures.
    """
    return bootstrap_workspace(
        **{
            "binding": binding,
            "provider": provider_identity,
            "access": access,
            "prerequisite_access": FakePrerequisiteAccess(),
            "store": store,
            "state_store": FakeStateStore(),
            "observed_cluster": observed_cluster,
            "expected_account_id": expected_target["expected_account_id"],
            "expected_region": expected_target["expected_region"],
            "expected_cluster_name": expected_target["expected_cluster_name"],
            "expected_cluster_arn": expected_target["expected_cluster_arn"],
            "expected_certificate_authority_data": expected_target[
                "expected_certificate_authority_data"
            ],
            "expected_cni_role_arn": CNI_ROLE_ARN,
            "expected_prerequisites": ExpectedPrerequisites(
                account_id=ACCOUNT_ID,
                vpc_id=VPC_ID,
                cluster_security_group_id=CLUSTER_SG_ID,
                management_security_group_id=MANAGEMENT_SG_ID,
                node_security_group_id="sg-synthetic-nodes",
                sts_endpoint_security_group_id="sg-synthetic-sts",
                sts_endpoint_vpc_id=VPC_ID,
            ),
            "cluster_ownership": "adp-created",
            "namespace": NAMESPACE,
            "enforce_version": ENFORCE_VERSION,
            "credential_reference_id": CREDENTIAL_ID,
            "contract_version": "v1",
            "screen": assert_no_secret_material,
            **overrides,
        }
    )


def _taint_present(access) -> bool:
    return any(t.get("key") == BOOTSTRAP_TAINT_KEY for t in access.node_taints())


def _assert_interlock_held(outcome, access, store) -> None:
    """The two consequences that protect a tenant, asserted together.

    Separating them would allow a half-pass — a refusal that left the workspace
    unregistered but the nodes schedulable is the `nodes_left_schedulable` alarm state,
    not a safe refusal, and it is the state F5 found in the first revision.
    """
    assert isinstance(outcome.refusal, BootstrapRefused)
    assert outcome.ready is False
    assert outcome.registered is False
    assert store.finalized == []
    assert _taint_present(access), (
        "the bootstrap taint was removed for a workspace whose runtime was not ready; "
        "tenant pods can now schedule onto nodes with no usable runtime"
    )
    assert outcome.nodes_left_schedulable is False


# --- Control ---------------------------------------------------------------------


def test_a_prepared_cluster_is_usable():
    """The positive control. Without it, every negative below could pass because the
    gate refuses unconditionally."""
    readiness = _readiness(_installed(FakeClusterAccess()))

    assert readiness.usable is True
    assert readiness.unverified == ()
    assert readiness.failures == ()


def test_every_gate_f2_named_is_actually_checked():
    """F2 named four missing things. This asserts each produced a named check.

    A refusal-only test suite cannot show that a check EXISTS — only that some
    refusal happened. This pins the four names, so deleting one is a test failure
    rather than a silent reduction in coverage.
    """
    readiness = _readiness(_installed(FakeClusterAccess()))
    names = {check.name for check in readiness.checks}

    assert "controller_rbac_scoped" in names
    assert f"system_workload_available:{REQUIRED_SYSTEM_WORKLOADS[0]}" in names
    assert "workspace_controller_available" in names
    assert "single_controller_reconciles_the_cluster" in names
    assert "controller_handover_complete" in names
    assert "tenant_scheduling_still_denied" in names


# --- CoreDNS: the live-handoff case (F2's named example) -------------------------


def test_coredns_present_but_unavailable_is_not_ready():
    """The exact state the live handoff recorded: CoreDNS exists, 0 replicas available.

    This is the one F2 called out, and the reason `ObservedWorkload` carries both
    counts. A seam reporting only presence would make this cluster look ready.
    """
    access = _installed(FakeClusterAccess())
    access.workloads[(SYSTEM_NAMESPACE, "coredns")] = ObservedWorkload(
        name="coredns",
        namespace=SYSTEM_NAMESPACE,
        desired_replicas=2,
        available_replicas=0,
    )

    readiness = _readiness(access)

    assert readiness.usable is False
    check = _failed(readiness, "system_workload_available:coredns")
    assert "0/2" in check.detail
    assert "not serving" in check.detail


def test_coredns_absent_is_not_ready():
    access = _installed(FakeClusterAccess())
    del access.workloads[(SYSTEM_NAMESPACE, "coredns")]

    readiness = _readiness(access)

    assert readiness.usable is False
    assert "absent" in _failed(readiness, "system_workload_available:coredns").detail


def test_coredns_scaled_to_zero_is_reported_as_its_own_case():
    """Distinguished from pending on purpose: "desires no replicas" is somebody's
    deliberate action and will never resolve on its own, so telling an operator to
    wait would be wrong."""
    access = _installed(FakeClusterAccess())
    access.workloads[(SYSTEM_NAMESPACE, "coredns")] = ObservedWorkload(
        name="coredns",
        namespace=SYSTEM_NAMESPACE,
        desired_replicas=0,
        available_replicas=0,
    )

    check = _failed(_readiness(access), "system_workload_available:coredns")

    assert "desires no replicas" in check.detail


def test_unavailable_coredns_leaves_the_taint_on_and_the_workspace_unregistered(
    binding, provider_identity, observed_cluster, expected_target
):
    """The consequence assertion for F2's named case, through the real entry point.

    The first revision would have cleared the taint here — putting tenant pods on nodes
    with no DNS, which is the "cluster ready but bootstrap failed" defect AC-01 names.

    `stays_unavailable` rather than a hand-set 0/2 workload: the cluster ACCEPTS the
    placement and CoreDNS still never comes up (an image it cannot pull, no capacity, a
    failing probe). That is the case only the post-placement readiness check can catch,
    and setting the replica counts directly would not reach it — placement legitimately
    heals a workload it successfully placed, so the direct edit would be undone before
    the check ran and the test would pass for the wrong reason.
    """
    access = FakeClusterAccess(crds=[], stays_unavailable=("coredns",))
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    _assert_interlock_held(outcome, access, store)
    assert "not usable" in str(outcome.refusal)
    assert "coredns" in str(outcome.refusal)


def test_coredns_that_cannot_be_placed_refuses_before_readiness_is_even_asked(
    binding, provider_identity, observed_cluster, expected_target
):
    """Placement failing is a refusal, not an unready readiness object.

    `prepare_system_workloads` raises, so the run never reaches the readiness gate —
    which is why `outcome.readiness` is None here. Worth asserting: it shows the
    cluster's refusal to place CoreDNS behind the pending taint is caught at the
    placement step and does not need the later check to save it.
    """
    access = FakeClusterAccess(crds=[], unplaceable=("coredns",))
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    _assert_interlock_held(outcome, access, store)
    assert outcome.readiness is None
    assert "did not accept placement" in str(outcome.refusal)


def test_placement_names_only_the_required_workloads_in_the_system_namespace():
    """The seam cannot express "tolerate the bootstrap taint everywhere".

    This is what makes granting a toleration acceptable at all: the request is scoped to
    `kube-system` and to named workloads, so the widest thing this package can ask for
    is bounded by the call rather than by reviewer vigilance.
    """
    access = FakeClusterAccess()

    prepare_system_workloads(access=access)

    assert access.placed_workloads == [(SYSTEM_NAMESPACE, REQUIRED_SYSTEM_WORKLOADS)]
    assert NAMESPACE not in [namespace for namespace, _ in access.placed_workloads]


def test_declaring_no_required_system_workloads_is_refused():
    """An empty set would let this gate pass without preparing anything — a vacuous
    success, which is the shape of the original defect."""
    with pytest.raises(BootstrapRefused, match="no system workloads"):
        prepare_system_workloads(access=FakeClusterAccess(), required=())


# --- The controller ---------------------------------------------------------------


def test_an_absent_controller_is_not_ready():
    """The gap F2 found between install and verification: nothing installed a
    controller, so nothing reconciled the workspace."""
    access = FakeClusterAccess()  # deliberately NOT installed

    readiness = _readiness(access)

    assert readiness.usable is False
    check = _failed(readiness, "workspace_controller_available")
    assert "absent" in check.detail
    assert "cannot run" in check.detail


def test_a_present_but_unready_controller_is_not_ready():
    """Installed and not serving. `install_controller` deliberately does not judge
    availability — that answer belongs here, in one place."""
    access = _installed(FakeClusterAccess(controller_install="unavailable"))

    readiness = _readiness(access)

    assert readiness.usable is False
    assert "0/2" in _failed(readiness, "workspace_controller_available").detail


def test_an_absent_controller_leaves_the_taint_on_and_the_workspace_unregistered(
    binding, provider_identity, observed_cluster, expected_target
):
    """Through the real entry point, with the install seam producing an unavailable
    controller — the closest thing to "the Deployment landed and never came up"."""
    access = FakeClusterAccess(crds=[], controller_install="unavailable")
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    _assert_interlock_held(outcome, access, store)
    assert "workspace_controller_available" in str(outcome.refusal)


def test_two_reconcilers_are_not_ready():
    """Re-checked AFTER install, because after install is the only place a second one
    can appear. Two controllers on one cluster-scoped NodePool set contend
    continuously rather than failing cleanly, so this never resolves on its own."""
    access = _installed(FakeClusterAccess())
    access.controller_images.append("registry.example/superplane-controller:v2")

    readiness = _readiness(access)

    assert readiness.usable is False
    check = _failed(readiness, "single_controller_reconciles_the_cluster")
    assert "2 controller deployment(s)" in check.detail
    assert "contend" in check.detail


def test_an_incomplete_handover_is_not_ready():
    """A previous controller still holding the coordination lease has not handed over,
    even when its Deployment is already gone — which is why the lease is a separate
    observation from the Deployment listing."""
    access = _installed(FakeClusterAccess())
    access.handover = {"complete": False, "holder": "controller-abc", "reconcilers": 1}

    readiness = _readiness(access)

    assert readiness.usable is False
    check = _failed(readiness, "controller_handover_complete")
    assert "has not completed" in check.detail
    assert "controller-abc" in check.detail, (
        "the refusal does not name the holder, so an operator cannot tell which "
        "controller to retire"
    )


def test_an_unanswered_handover_is_not_read_as_a_completed_one():
    """ "An unanswered question is not a negative answer" — and here it is not a
    POSITIVE one either. An observation with no `complete` key means the lease could not
    be read; defaulting that to "handed over" is the failure direction that matters."""
    access = _installed(FakeClusterAccess())
    access.handover = {"reconcilers": 1}

    readiness = _readiness(access)

    assert readiness.usable is False
    assert "unanswered" in _failed(readiness, "controller_handover_complete").detail


def test_an_incomplete_handover_leaves_the_taint_on_and_the_workspace_unregistered(
    binding, provider_identity, observed_cluster, expected_target
):
    access = FakeClusterAccess(crds=[])
    access.handover = {"complete": False, "holder": "controller-abc", "reconcilers": 1}
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    _assert_interlock_held(outcome, access, store)
    assert "controller_handover_complete" in str(outcome.refusal)


# --- Scoped RBAC: has everything it needs, and only that ------------------------


@pytest.mark.parametrize("pair", REQUIRED_CONTROLLER_PERMISSIONS)
def test_each_required_permission_is_individually_required(pair):
    """Parametrized per permission so a set silently shrinking is a failure.

    Asserting only "some missing permission refuses" would let a future edit drop
    `watch nodepools.superplane.ai` from the required set and keep a green suite.
    """
    access = _installed(FakeClusterAccess())
    access.controller_rbac[pair] = False

    readiness = _readiness(access)

    assert readiness.usable is False
    check = _failed(readiness, "controller_rbac_scoped")
    assert f"{pair[0]} {pair[1]}" in check.detail
    assert "lacks required" in check.detail


@pytest.mark.parametrize("pair", REQUIRED_CONTROLLER_PERMISSIONS)
def test_an_unanswered_permission_is_neither_granted_nor_denied(pair):
    """A missing key is not `False`, and the refusal says so differently.

    `kubectl auth can-i` failing to answer and answering "no" need different operator
    actions, and a Mapping that defaulted absent keys to falsy would collapse them.
    """
    access = _installed(FakeClusterAccess())
    del access.controller_rbac[pair]

    check = _failed(_readiness(access), "controller_rbac_scoped")

    assert "no observation for" in check.detail
    assert f"{pair[0]} {pair[1]}" in check.detail


@pytest.mark.parametrize("pair", FORBIDDEN_CONTROLLER_PERMISSIONS)
def test_each_forbidden_permission_is_individually_refused(pair):
    """The other half of "scoped": a cluster-admin credential satisfies every required
    verb and fails this. A controller that can create ClusterRoleBindings, delete
    namespaces or read secrets can escalate past every boundary this package sets."""
    access = _installed(FakeClusterAccess())
    access.controller_rbac[pair] = True

    readiness = _readiness(access)

    assert readiness.usable is False
    check = _failed(readiness, "controller_rbac_scoped")
    assert f"{pair[0]} {pair[1]}" in check.detail
    assert "must not" in check.detail


def test_an_over_permissioned_controller_leaves_the_taint_on(
    binding, provider_identity, observed_cluster, expected_target
):
    """Through the real entry point. This is the case where everything WORKS — the
    controller reconciles fine with cluster-admin — so only an explicit check stops it,
    and only this assertion shows the check is load-bearing."""
    access = FakeClusterAccess(crds=[])
    access.controller_rbac[FORBIDDEN_CONTROLLER_PERMISSIONS[0]] = True
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    _assert_interlock_held(outcome, access, store)
    assert "controller_rbac_scoped" in str(outcome.refusal)


def test_the_required_and_forbidden_sets_do_not_overlap():
    """An overlap would make the gate unsatisfiable — every cluster would refuse — and
    the failure would look like a broken cluster rather than a broken constant."""
    assert not (
        set(REQUIRED_CONTROLLER_PERMISSIONS) & set(FORBIDDEN_CONTROLLER_PERMISSIONS)
    )


# --- The tenant interlock is re-verified, not assumed ---------------------------


def test_a_relaxed_interlock_is_not_ready():
    """The hazard created by preparing system workloads: a toleration granted too
    broadly, or the taint removed outright to get CoreDNS running.

    Readiness re-asks whether a tenant pod is still unschedulable AFTER placement, so
    that shortcut is caught here rather than by a reviewer.
    """
    access = _installed(FakeClusterAccess(tenant_denied=False))

    readiness = _readiness(access)

    assert readiness.usable is False
    check = _failed(readiness, "tenant_scheduling_still_denied")
    assert "already schedulable" in check.detail
    assert "toleration" in check.detail


def test_an_unanswered_interlock_does_not_establish_that_it_held():
    """`None` models "no schedulable nodes, cannot answer". That is not evidence the
    interlock held, and this is the direction where guessing is unsafe."""
    access = _installed(FakeClusterAccess(tenant_denied=None))

    readiness = _readiness(access)

    assert readiness.usable is False
    assert (
        "no observation" in _failed(readiness, "tenant_scheduling_still_denied").detail
    )


def test_a_relaxed_interlock_leaves_the_workspace_unregistered(
    binding, provider_identity, observed_cluster, expected_target
):
    """The taint assertion is inverted here, and that is the point.

    `tenant_denied=False` means the interlock was ALREADY relaxed before this gate ran,
    so asserting "the taint is still present" would be asserting something the fake
    contradicts. What must hold is the other half: the workspace is not registered, so
    nothing downstream is told this cluster may accept work.
    """
    access = FakeClusterAccess(crds=[], tenant_denied=False)
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert isinstance(outcome.refusal, BootstrapRefused)
    assert outcome.registered is False
    assert store.finalized == []
    assert outcome.taint_cleared is False
    assert access.removed_taints == [], (
        "the interlock was relaxed and bootstrap removed the taint anyway"
    )
    assert "tenant_scheduling_still_denied" in str(outcome.refusal)


def test_readiness_is_established_before_the_taint_is_removed(
    binding, provider_identity, observed_cluster, expected_target
):
    """The ordering F2 asked for, asserted as an ordering rather than inferred.

    Every negative above shows a failed readiness check blocks the taint removal. This
    shows the CLEAN path also does them in that order — otherwise a refactor could move
    the removal earlier and stay green, since a passing readiness check never blocks
    anything.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.ready is True
    calls = access.calls
    placed = calls.index(f"place_system_workloads:{SYSTEM_NAMESPACE}")
    installed = calls.index(f"install_controller:{NAMESPACE}/{CONTROLLER_NAME}")
    verified = calls.index("controller_permissions:" + NAMESPACE)
    interlock = calls.index(f"tenant_scheduling_denied:{NAMESPACE}")
    removed = calls.index(f"remove_bootstrap_taint:{BOOTSTRAP_TAINT_KEY}")

    assert installed < placed < verified, (
        "the controller must exist before system workloads are placed and before "
        "readiness is verified"
    )
    assert verified < removed, "readiness was verified after the interlock was cleared"
    assert interlock < removed, (
        "the tenant interlock was re-verified after it had already been cleared, which "
        "checks nothing"
    )


# --- The readiness records cannot assert what they did not observe ---------------


def test_readiness_with_no_checks_is_not_usable():
    """Vacuous success. An empty check list is what a stubbed-out gate returns, and it
    must not read as ready — the same rule as `IsolationEvidence` with no proofs."""
    assert RuntimeReadiness(namespace=NAMESPACE, checks=()).usable is False


def test_usable_cannot_be_set_directly():
    """`usable` is computed, so no path can claim readiness it did not establish.

    Asserted by attempting the assignment: `dataclasses.replace` with a computed name
    raises, which is the guarantee. A stored boolean is what would let a caller fake it.
    """
    readiness = RuntimeReadiness(
        namespace=NAMESPACE,
        checks=(ReadinessCheck(name="x", verified=False, detail="d"),),
    )

    with pytest.raises(TypeError):
        dataclasses.replace(readiness, usable=True)

    assert readiness.usable is False


def test_an_unverified_check_must_carry_a_detail():
    """An unexplained negative cannot be told apart from a check that never ran, and
    the operator is left with a refusal that names no action."""
    with pytest.raises(BootstrapRefused, match="unverified with no detail"):
        ReadinessCheck(name="workspace_controller_available", verified=False)


def test_a_nameless_check_is_refused():
    with pytest.raises(BootstrapRefused, match="ReadinessCheck.name is required"):
        ReadinessCheck(name="   ", verified=True)


def test_failures_report_every_unverified_check_not_just_the_first():
    """A refusal naming one of three problems sends the operator round the loop twice
    more. Multiple simultaneous failures are the normal case on a half-built cluster."""
    access = _installed(FakeClusterAccess(tenant_denied=False))
    del access.workloads[(SYSTEM_NAMESPACE, "coredns")]
    access.handover = {"complete": False, "holder": "controller-abc"}

    readiness = _readiness(access)

    assert len(readiness.unverified) == 3
    assert len(readiness.failures) == 3
    assert all(": " in failure for failure in readiness.failures), (
        "a failure line carries no detail, so it names a check without naming a cause"
    )


# --- Arguments the gate refuses rather than guessing ----------------------------


def test_readiness_without_a_namespace_is_refused():
    with pytest.raises(BootstrapRefused, match="namespace is required"):
        establish_runtime_readiness(
            access=FakeClusterAccess(), namespace="  ", controller_name=CONTROLLER_NAME
        )


def test_readiness_without_a_controller_name_is_refused():
    """Otherwise the gate reports a ready runtime having never looked for a
    controller — which is F2 restated as an argument default."""
    with pytest.raises(BootstrapRefused, match="controller name is required"):
        establish_runtime_readiness(
            access=_installed(FakeClusterAccess()),
            namespace=NAMESPACE,
            controller_name="",
        )


def test_the_readiness_record_describes_the_namespace_it_was_asked_about():
    """`registration.py` refuses readiness for a different namespace, which only works
    if the record carries the namespace it actually observed."""
    readiness = _readiness(_installed(FakeClusterAccess()))

    assert readiness.namespace == NAMESPACE


def test_the_readiness_record_carries_no_secret_material():
    """Readiness is quoted in issue comments and completion reports, so it goes through
    the real contract screen — the same discipline `test_registration.py` applies to the
    registration record."""
    readiness = _readiness(_installed(FakeClusterAccess()))

    assert_no_secret_material(readiness.namespace, what="readiness.namespace")
    for check in readiness.checks:
        assert_no_secret_material(check.name, what=f"readiness.{check.name}")
        assert_no_secret_material(check.detail, what=f"readiness.{check.name}.detail")
    assert WORKSPACE_ID not in str(readiness.checks)
