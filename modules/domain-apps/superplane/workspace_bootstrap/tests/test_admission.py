"""Isolation proofs and the taint interlock — Issue #5533 (w6-10), AC-01.

The load-bearing test in this file is
`test_the_real_declared_proof_list_is_fully_covered_by_checks`: it reads the actual
`infra/workspaces/outputs.tf` rather than a fixture, so adding a required control
upstream fails here instead of silently passing with the old set of checks.
"""

from __future__ import annotations

import pytest
from superplane_bootstrap.access import ObservedNamespace
from superplane_bootstrap.admission import (
    DECLARED_PROOF_CHECKS,
    ORDERING_PROOFS,
    RESTRICTED_ENFORCE_LABEL,
    RESTRICTED_ENFORCE_VERSION_LABEL,
    AdmissionProof,
    prove_tenant_isolation,
    required_proofs,
    tenant_namespace_labels,
)
from superplane_bootstrap.errors import BootstrapRefused

from .conftest import CNI_ROLE_ARN, ENFORCE_VERSION, NAMESPACE, FakeClusterAccess


def _isolated_namespace(**label_overrides: str) -> ObservedNamespace:
    labels = {
        RESTRICTED_ENFORCE_LABEL: "restricted",
        RESTRICTED_ENFORCE_VERSION_LABEL: ENFORCE_VERSION,
    }
    labels.update(label_overrides)
    return ObservedNamespace(name=NAMESPACE, uid="namespace-uid-0001", labels=labels)


def _access(**overrides) -> FakeClusterAccess:
    access = FakeClusterAccess(**overrides)
    access.namespaces.setdefault(NAMESPACE, _isolated_namespace())
    return access


def _prove(access: FakeClusterAccess, **overrides):
    arguments = {
        "access": access,
        "namespace": NAMESPACE,
        "enforce_version": ENFORCE_VERSION,
        "expected_cni_role_arn": CNI_ROLE_ARN,
        "declared_proofs": required_proofs(),
        **overrides,
    }
    return prove_tenant_isolation(**arguments)


# --- Control ---------------------------------------------------------------------


def test_a_correctly_isolated_namespace_proves_every_declared_control():
    """The control, run against the REAL declared proof list.

    If this fails after an upstream change to `tenant_scheduling_prerequisites`, the
    declared list gained a control nothing here checks — which is the intended
    behaviour, not a broken test.
    """
    evidence = _prove(_access())

    assert evidence.may_clear_taint is True
    assert evidence.unverified == ()


def test_the_real_declared_proof_list_is_fully_covered_by_checks():
    """Every control the infrastructure declares maps to a check or to the ordering rule.

    Read from `infra/workspaces/outputs.tf` as text. This is the drift guard: the
    silent failure it prevents is a workspace registered as usable without a control
    somebody added upstream.
    """
    declared = required_proofs()
    assert len(declared) >= 5

    uncovered = [
        text
        for text in declared
        if not any(fragment in text.lower() for fragment in DECLARED_PROOF_CHECKS)
        and not any(fragment in text.lower() for fragment in ORDERING_PROOFS)
    ]

    assert uncovered == [], (
        "the workspace infrastructure module declares controls this bootstrap has no "
        f"check for: {uncovered}"
    )


def test_an_added_upstream_requirement_is_reported_rather_than_ignored():
    """A control this package does not recognise blocks the interlock.

    The point of the drift guard, exercised directly: an unknown declared entry must
    become a visible unverified proof, not be dropped.
    """
    evidence = _prove(
        _access(),
        declared_proofs=[
            *required_proofs(),
            "Tenant egress restricted to an allowlist",
        ],
    )

    assert evidence.may_clear_taint is False
    assert "declared_requirement_without_a_check" in evidence.unverified


def test_the_ordering_requirement_does_not_deadlock_the_interlock():
    """Regression: the declared "only after these proofs" entry is not a probe.

    An earlier draft mapped declared controls to checks BY POSITION, so this entry —
    an ordering constraint on the caller, not a cluster property — became a proof with
    no check and `may_clear_taint` could never be true for any cluster. The interlock
    would have deadlocked every workspace while looking correct.

    It is discharged structurally by `workspace.bootstrap_workspace`'s call ordering;
    `test_workspace.py` asserts the taint survives every failure mode.
    """
    ordering_entries = [
        text
        for text in required_proofs()
        if any(fragment in text.lower() for fragment in ORDERING_PROOFS)
    ]
    assert ordering_entries, "expected a declared ordering requirement"

    evidence = _prove(_access())

    assert evidence.may_clear_taint is True


# --- Admission labels ------------------------------------------------------------


def test_a_namespace_not_enforcing_restricted_is_refused():
    access = _access()
    access.namespaces[NAMESPACE] = _isolated_namespace(
        **{RESTRICTED_ENFORCE_LABEL: "baseline"}
    )

    evidence = _prove(access)

    assert "restricted_admission_enforced" in evidence.unverified
    assert evidence.may_clear_taint is False


def test_a_namespace_enforcing_a_different_version_is_refused():
    """The proofs were taken against a specific policy version."""
    access = _access()
    access.namespaces[NAMESPACE] = _isolated_namespace(
        **{RESTRICTED_ENFORCE_VERSION_LABEL: "v1.30"}
    )

    evidence = _prove(access)

    assert "restricted_admission_enforced" in evidence.unverified


def test_warn_and_audit_labels_alone_do_not_satisfy_the_proof():
    """They annotate and reject nothing, which is the mistake worth a named test."""
    access = _access()
    access.namespaces[NAMESPACE] = ObservedNamespace(
        name=NAMESPACE,
        uid="namespace-uid-0001",
        labels={
            "pod-security.kubernetes.io/warn": "restricted",
            "pod-security.kubernetes.io/audit": "restricted",
        },
    )

    evidence = _prove(access)

    assert "restricted_admission_enforced" in evidence.unverified


def test_a_tenant_able_to_change_admission_labels_is_refused():
    """An admission policy the constrained tenant can edit is advisory."""
    evidence = _prove(_access(tenant_can_change_labels=True))

    assert "tenant_cannot_weaken_admission" in evidence.unverified
    assert evidence.may_clear_taint is False


def test_a_floating_enforce_version_is_refused_for_a_tenant_namespace():
    """`latest` lets the proved policy change under the namespace at an upgrade."""
    with pytest.raises(BootstrapRefused, match="must be pinned"):
        tenant_namespace_labels("latest")


def test_tenant_namespace_labels_are_the_labels_the_proof_requires():
    """The create path and the verify path must agree, or a namespace fails its own proof.

    `components.py` builds the namespace from `tenant_namespace_labels`; this gate
    checks the same two keys. Restating them separately is how they drift.
    """
    labels = tenant_namespace_labels(ENFORCE_VERSION)

    access = _access()
    access.namespaces[NAMESPACE] = ObservedNamespace(
        name=NAMESPACE, uid="namespace-uid-0001", labels=labels
    )

    assert "restricted_admission_enforced" not in _prove(access).unverified


# --- IMDS ------------------------------------------------------------------------


@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
def test_imds_reachable_over_either_family_is_refused(family):
    """Both families matter. IPv6 is the one that gets forgotten."""
    imds = {"ipv4": False, "ipv6": False}
    imds[family] = True

    evidence = _prove(_access(imds=imds))

    assert "tenant_pod_cannot_reach_imds" in evidence.unverified
    assert evidence.may_clear_taint is False


def test_an_unobserved_imds_family_is_not_treated_as_unreachable():
    """Unknown is not safe.

    The natural implementation returns falsy for an errored probe, which reads as
    "not reachable", which reads as "isolated". A missing observation must refuse.
    """
    evidence = _prove(_access(imds={"ipv4": False}))

    assert "tenant_pod_cannot_reach_imds" in evidence.unverified


def test_no_imds_observations_at_all_is_refused():
    evidence = _prove(_access(imds={}))

    assert "tenant_pod_cannot_reach_imds" in evidence.unverified


# --- Host escape hatches ---------------------------------------------------------


def test_admitting_unsafe_pod_requests_is_refused():
    evidence = _prove(_access(admit_unsafe=True))

    assert "unsafe_pod_requests_rejected" in evidence.unverified
    assert evidence.may_clear_taint is False


def test_every_unsafe_field_is_probed_separately():
    """One dry-run per field.

    A single pod requesting all four proves only that SOMETHING was rejected. A
    cluster rejecting `privileged` while admitting `hostPath` would pass that.
    """
    access = _access()
    _prove(access)

    probed = {
        key
        for name, spec in access.dry_run_calls
        for key in ("hostNetwork", "hostPID", "privileged", "hostPath")
        if key in spec
    }

    assert probed == {"hostNetwork", "hostPID", "privileged", "hostPath"}


def test_a_cluster_that_rejects_everything_does_not_pass_as_isolated():
    """The control inside the control.

    A broken probe, a missing namespace or a restrictive quota rejects the conforming
    pod too. Without a positive case, "rejects everything" reads as perfect isolation.
    """
    access = _access()
    access.reject_everything = True

    evidence = _prove(access)

    assert "unsafe_pod_requests_rejected" in evidence.unverified


# --- CNI credential scope --------------------------------------------------------


def test_a_cni_using_the_node_role_is_refused():
    evidence = _prove(
        _access(
            cni_scope={
                "aws_node_role_arn": "",
                "node_role_has_cni_permissions": True,
                "node_role_has_account_wide_ecr": False,
            }
        )
    )

    assert "cni_credentials_scoped" in evidence.unverified


def test_a_node_role_retaining_cni_permissions_is_refused():
    """A dedicated IRSA role buys nothing while the node role duplicates it."""
    evidence = _prove(
        _access(
            cni_scope={
                "aws_node_role_arn": CNI_ROLE_ARN,
                "node_role_has_cni_permissions": True,
                "node_role_has_account_wide_ecr": False,
            }
        )
    )

    assert "cni_credentials_scoped" in evidence.unverified


def test_a_node_role_with_account_wide_ecr_is_refused():
    evidence = _prove(
        _access(
            cni_scope={
                "aws_node_role_arn": CNI_ROLE_ARN,
                "node_role_has_cni_permissions": False,
                "node_role_has_account_wide_ecr": True,
            }
        )
    )

    assert "cni_credentials_scoped" in evidence.unverified


def test_an_irsa_role_other_than_the_declared_one_is_refused():
    """ "Has some IRSA role" is not "has the role the infrastructure declared"."""
    evidence = _prove(
        _access(
            cni_scope={
                "aws_node_role_arn": "arn:aws:iam::000000000000:role/SomeOtherRole",
                "node_role_has_cni_permissions": False,
                "node_role_has_account_wide_ecr": False,
            }
        )
    )

    assert "cni_credentials_scoped" in evidence.unverified


def test_an_unobserved_cni_scope_field_is_refused():
    evidence = _prove(_access(cni_scope={"aws_node_role_arn": CNI_ROLE_ARN}))

    assert "cni_credentials_scoped" in evidence.unverified


# --- Structural refusals ---------------------------------------------------------


def test_an_absent_namespace_cannot_be_proved():
    access = FakeClusterAccess()

    with pytest.raises(BootstrapRefused, match="is absent"):
        _prove(access)


def test_an_empty_declared_proof_list_is_refused():
    """An empty requirement list would let this gate pass trivially."""
    with pytest.raises(BootstrapRefused, match="no isolation proofs were declared"):
        _prove(_access(), declared_proofs=[])


def test_an_unverified_proof_without_a_detail_cannot_be_constructed():
    """An unexplained negative is indistinguishable from a check that never ran."""
    with pytest.raises(BootstrapRefused, match="unverified with no detail"):
        AdmissionProof(name="something", verified=False)


def test_evidence_with_no_proofs_may_not_clear_the_taint():
    """Vacuous success is the failure mode `bool(self.proofs)` exists to close."""
    from superplane_bootstrap.admission import IsolationEvidence

    empty = IsolationEvidence(namespace=NAMESPACE, enforce_version=ENFORCE_VERSION)

    assert empty.may_clear_taint is False
