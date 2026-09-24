"""The gate sequence and the taint interlock — Issue #5533 (w6-10), AC-01 and AC-02.

The tests that matter most here are the ones asserting what did NOT happen: that the
bootstrap taint is still on the nodes and the workspace is still unregistered after
each failure mode. Clearing that taint is the action that makes nodes schedulable for
tenant work, so "the interlock held" is the property worth the most tests.

This is also where the declared ordering requirement — "Only after these proofs,
remove the bootstrap taint through the bounded bootstrap owner" — is discharged. It is
not a probe, so `admission.py` cannot check it; it is a property of this call
sequence, and these tests are its evidence.
"""

from __future__ import annotations

import dataclasses

import pytest
from superplane_bootstrap.access import ObservedNamespace
from superplane_bootstrap.admission import (
    RESTRICTED_ENFORCE_LABEL,
    RESTRICTED_ENFORCE_VERSION_LABEL,
)
from superplane_bootstrap.components import BOOTSTRAP_OWNER_LABEL
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.prerequisites import ExpectedPrerequisites
from superplane_bootstrap.workspace import BOOTSTRAP_TAINT_KEY, bootstrap_workspace
from superplane_contracts.secrets import assert_no_secret_material

from .conftest import (
    ACCOUNT_ID,
    CLUSTER_SG_ID,
    CNI_ROLE_ARN,
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


def _run(
    access,
    store,
    binding,
    provider_identity,
    observed_cluster,
    expected_target,
    **overrides,
):
    """Call the real entry point with every mandatory seam supplied.

    `prerequisite_access`, `state_store` and `expected_prerequisites` are no longer
    optional (F4, F6): the prerequisite gate runs before any cluster mutation and the
    durable store records progress after each one, so a caller cannot obtain an outcome
    without providing them. A test that wants the missing-seam refusal passes the
    override explicitly rather than relying on a default.
    """
    arguments = {
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
    return bootstrap_workspace(**arguments)


def _access(**overrides) -> FakeClusterAccess:
    """A cluster that bootstraps cleanly: no CRDs yet, correctly isolated, tainted.

    The namespace is deliberately NOT pre-created. An earlier revision seeded it so the
    proof gate's read would find the right labels, but under F3 a namespace that already
    exists with no durable creation record is ADOPTED — so seeding it would have quietly
    converted every one of these tests from the creation path to the adoption path, and
    the cleanup-plan assertions would have been testing the wrong branch. The fake's
    `create_namespace` stores what it created, so the later read finds it anyway.
    """
    return FakeClusterAccess(crds=[], **overrides)


def _taint_present(access) -> bool:
    return any(t.get("key") == BOOTSTRAP_TAINT_KEY for t in access.node_taints())


# --- Control ---------------------------------------------------------------------


def test_a_clean_bootstrap_clears_the_taint_and_registers(
    binding, provider_identity, observed_cluster, expected_target
):
    access, store = _access(), FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.refusal is None
    assert outcome.ready is True
    assert outcome.taint_cleared is True
    assert _taint_present(access) is False
    assert len(store.finalized) == 1


def test_the_taint_is_removed_only_after_the_proofs_ran(
    binding, provider_identity, observed_cluster, expected_target
):
    """The declared ordering requirement, asserted directly.

    `infra/workspaces/outputs.tf` declares "Only after these proofs, remove the
    bootstrap taint". Evidence exists on the outcome and the removal happened, in that
    order — the taint cannot have been cleared before the proofs produced a result.
    """
    access, store = _access(), FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.evidence is not None
    assert outcome.evidence.may_clear_taint is True
    assert access.removed_taints == [BOOTSTRAP_TAINT_KEY]


def test_registration_happens_after_the_taint_is_cleared(
    binding, provider_identity, observed_cluster, expected_target
):
    """A registration says work may be scheduled here. If the nodes are still
    unschedulable that statement is false."""
    access, store = _access(), FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.taint_cleared is True
    assert outcome.registered is True


# --- The interlock holds: every failure leaves the taint on ----------------------


def test_a_wrong_certificate_leaves_the_taint_and_registers_nothing(
    binding, provider_identity, observed_cluster, expected_target
):
    """Gate 1 fails, so nothing is created at all."""
    access, store = _access(), FakeRegistrationStore()
    wrong = dataclasses.replace(
        observed_cluster, certificate_authority_data="c3Vic3RpdHV0ZWQ="
    )

    outcome = _run(access, store, binding, provider_identity, wrong, expected_target)

    assert isinstance(outcome.refusal, BootstrapRefused)
    assert outcome.ready is False
    assert _taint_present(access) is True
    assert store.finalized == []
    assert access.established == []


def test_an_inactive_cluster_leaves_the_taint_and_registers_nothing(
    binding, provider_identity, observed_cluster, expected_target
):
    access, store = _access(), FakeRegistrationStore()
    creating = dataclasses.replace(observed_cluster, status="CREATING")

    outcome = _run(access, store, binding, provider_identity, creating, expected_target)

    assert outcome.ready is False
    assert _taint_present(access) is True
    assert store.finalized == []


def test_a_duplicate_controller_leaves_the_taint_and_registers_nothing(
    binding, provider_identity, observed_cluster, expected_target
):
    """AC-01's duplicate-controller case, end to end."""
    access = _access(controller_images=["registry.example/superplane-controller:v1"])
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.ready is False
    assert _taint_present(access) is True
    assert store.finalized == []


def test_missing_crds_leave_the_taint_and_register_nothing(
    binding, provider_identity, observed_cluster, expected_target
):
    """AC-01's missing-CRD case, end to end."""
    access = _access(establish_crds_result=[])
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.ready is False
    assert _taint_present(access) is True
    assert store.finalized == []


@pytest.mark.parametrize(
    "failure",
    [
        {"imds": {"ipv4": True, "ipv6": False}},
        {"imds": {"ipv6": False}},
        {"admit_unsafe": True},
        {"tenant_can_change_labels": True},
        {
            "cni_scope": {
                "aws_node_role_arn": "",
                "node_role_has_cni_permissions": True,
                "node_role_has_account_wide_ecr": False,
            }
        },
    ],
)
def test_any_unproved_isolation_control_leaves_the_taint_in_place(
    binding, provider_identity, observed_cluster, expected_target, failure
):
    """The most important test in this file, parametrized over every control.

    A failed proof must leave the nodes unschedulable. AC-01's "cluster ready but
    bootstrap failed" is this case: the cluster is ACTIVE, the namespace exists, the
    CRDs are established — and the workspace is still not usable.
    """
    access, store = _access(**failure), FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.ready is False
    assert outcome.taint_cleared is False
    assert _taint_present(access) is True
    assert access.removed_taints == []
    assert store.finalized == []


def test_a_taint_that_survives_removal_prevents_registration(
    binding, provider_identity, observed_cluster, expected_target
):
    """If the removal did not take effect the nodes are not schedulable, so
    registering the workspace as usable would be false."""

    class StubbornAccess(FakeClusterAccess):
        def remove_bootstrap_taint(self, key):
            self.removed_taints.append(key)
            return [dict(t) for t in self.taints]

    access = StubbornAccess(crds=[])
    access.create_namespace(
        NAMESPACE,
        {
            RESTRICTED_ENFORCE_LABEL: "restricted",
            RESTRICTED_ENFORCE_VERSION_LABEL: ENFORCE_VERSION,
            BOOTSTRAP_OWNER_LABEL: WORKSPACE_ID,
        },
    )
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.ready is False
    assert store.finalized == []
    assert "still present" in str(outcome.refusal)


# --- Partial bootstrap produces a usable cleanup plan ----------------------------


def test_a_failure_after_install_still_yields_a_cleanup_plan(
    binding, provider_identity, observed_cluster, expected_target
):
    """Cleanup needs to know what was created. If the outcome discarded its progress
    the caller would have to guess, and guessing wrong in either direction is bad."""
    access, store = _access(admit_unsafe=True), FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.cleanup is not None
    assert outcome.cleanup.remove_namespace == ""
    assert outcome.cleanup.retained_namespace == NAMESPACE
    assert outcome.cleanup.preserves_cluster is True


def test_a_failure_before_install_yields_no_cleanup_plan(
    binding, provider_identity, observed_cluster, expected_target
):
    """Nothing was created, so there is nothing to plan — and a plan naming a
    namespace that was never created could hit an unrelated object of that name."""
    access, store = _access(), FakeRegistrationStore()
    wrong = dataclasses.replace(observed_cluster, account_id="999999999999")

    outcome = _run(access, store, binding, provider_identity, wrong, expected_target)

    assert outcome.cleanup is None
    assert outcome.installation is None


def test_the_refusal_is_preserved_and_re_raisable(
    binding, provider_identity, observed_cluster, expected_target
):
    """A caller that ignores `ready` still has the exception to surface."""
    access, store = _access(admit_unsafe=True), FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    with pytest.raises(BootstrapRefused):
        outcome.raise_for_failure()


def test_a_byoc_failure_plans_cleanup_that_preserves_the_supplied_cluster(
    binding, provider_identity, observed_cluster, expected_target
):
    """AC-02 under the failure path, which is when cleanup actually runs."""
    access = FakeClusterAccess(crds=[], admit_unsafe=True)
    access.namespaces[NAMESPACE] = ObservedNamespace(
        name=NAMESPACE,
        uid="pre-existing-uid",
        labels={
            RESTRICTED_ENFORCE_LABEL: "restricted",
            RESTRICTED_ENFORCE_VERSION_LABEL: ENFORCE_VERSION,
        },
    )
    store = FakeRegistrationStore()

    outcome = _run(
        access,
        store,
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        cluster_ownership="adopted",
    )

    assert outcome.ready is False
    assert outcome.cleanup is not None
    assert outcome.cleanup.preserves_cluster is True
    assert outcome.cleanup.remove_namespace == ""
    assert outcome.cleanup.deletes_nothing is True


def test_the_verified_inventory_is_carried_into_the_cleanup_plan(
    binding, provider_identity, observed_cluster, expected_target
):
    """The inventory in the plan is the one the gate VERIFIED, not one a caller passed.

    Before the F4 repair `inventory` was a parameter defaulting to None, so a caller
    could obtain a cleanup plan for a workspace whose access prerequisites were recorded
    nowhere. Now it is built by the mandatory gate from authoritative reads, and these
    entries are in the plan because they were observed on AWS and attributed to this
    workspace.

    They appear under `preserved` rather than `remove_prerequisites` because the fake's
    observation omits `created_by_bootstrap` — exactly as a real `aws` read does, since
    AWS cannot report who created a rule. That is the safe default, and the assertion
    pins it: an adopted rule is never planned for deletion.
    """
    access, store = _access(admit_unsafe=True), FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.inventory is not None
    assert outcome.cleanup is not None
    preserved = " ".join(outcome.cleanup.preserved)
    assert "EksAccessEntry" in preserved
    assert "SecurityGroupRule/cluster-endpoint" in preserved
    assert outcome.cleanup.remove_prerequisites == ()


def test_an_absent_access_entry_refuses_before_the_cluster_is_touched(
    binding, provider_identity, observed_cluster, expected_target
):
    """F4's core case: the prerequisite gate runs BEFORE any mutation.

    The finding was that these prerequisites were optional and unverified. Now a missing
    scoped access entry refuses against an untouched cluster — no namespace created, no
    CRDs established, no reservation taken.
    """
    access, store = _access(), FakeRegistrationStore()

    outcome = _run(
        access,
        store,
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        prerequisite_access=FakePrerequisiteAccess(entry_exists=False),
    )

    assert outcome.ready is False
    assert outcome.inventory is None
    assert access.created_namespaces == []
    assert access.established == []
    assert store.reservations == {}
    assert _taint_present(access) is True


# --- Idempotence -----------------------------------------------------------------


def test_a_second_run_against_a_bootstrapped_workspace_is_a_replay(
    binding, provider_identity, observed_cluster, expected_target
):
    """Bootstrap must be retry-safe (design item 3). A re-run recognises the existing
    registration rather than writing twice.

    Both runs share the durable state store as well as the registration store, because
    that is what a retry on one workspace actually looks like. It also keeps the second
    run on the OWNERSHIP path: the state carries the namespace uid recorded at creation,
    so `namespace_ownership` finds a durable record whose uid matches the live object and
    the re-run still owns what it created. With a fresh state store the same namespace
    would be adopted — correct behaviour for an unknown namespace, but not a retry.
    """
    store = FakeRegistrationStore()
    state_store = FakeStateStore()
    access = _access()

    first = _run(
        access,
        store,
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        state_store=state_store,
    )
    second = _run(
        access,
        store,
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        state_store=state_store,
    )

    assert first.registered and second.registered
    assert second.registration.replayed is True
    assert len(store.finalized) == 1
    # The retry owned the namespace it created rather than adopting it, and created it
    # only once.
    assert second.installation.namespace_owned is True
    assert len(access.created_namespaces) == 1


def test_a_re_applied_taint_is_cleared_again_on_re_run(
    binding, provider_identity, observed_cluster, expected_target
):
    """A Terraform re-apply can restore the taint, so the proofs must be re-runnable
    rather than one-shot."""
    store = FakeRegistrationStore()
    state_store = FakeStateStore()
    access = _access()
    _run(
        access,
        store,
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        state_store=state_store,
    )

    # Terraform re-applies the node group and the pending taint comes back.
    access.taints.append(
        {"key": BOOTSTRAP_TAINT_KEY, "value": "pending", "effect": "NoSchedule"}
    )
    access.tenant_denied = True

    outcome = _run(
        access,
        store,
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        state_store=state_store,
    )

    assert _taint_present(access) is False
    assert outcome.taint_cleared is True


def test_the_outcome_cannot_claim_readiness_without_both_steps(
    binding, provider_identity, observed_cluster, expected_target
):
    """`ready` is computed from the taint and the registration, not stored, so no code
    path can construct an outcome claiming readiness it did not reach."""
    from superplane_bootstrap.workspace import BootstrapOutcome

    assert BootstrapOutcome(taint_cleared=True, registration=None).ready is False
    assert BootstrapOutcome(taint_cleared=False, registration=None).ready is False


def test_process_exit_after_taint_mutation_keeps_durable_recovery_intent(
    binding,
    provider_identity,
    observed_cluster,
    expected_target,
):
    import pytest
    from superplane_bootstrap.workspace import recover_interrupted_bootstrap
    from .conftest import CLUSTER_ARN

    class KilledAfterMutation(FakeClusterAccess):
        def remove_bootstrap_taint(self, key):
            super().remove_bootstrap_taint(key)
            raise SystemExit("synthetic process kill after cluster mutation")

    access = KilledAfterMutation()
    state_store = FakeStateStore()
    store = FakeRegistrationStore()
    with pytest.raises(SystemExit):
        _run(
            access,
            store,
            binding,
            provider_identity,
            observed_cluster,
            expected_target,
            state_store=state_store,
        )
    state = state_store.load()
    assert state.taint_clear_pending and not state.taint_cleared
    assert state.interlock_restoration_pending
    result = recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )
    assert result.taint_restored
    assert not state_store.load().recovery_pending
