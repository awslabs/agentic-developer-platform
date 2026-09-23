"""Registration readiness, replay safety and credential discipline — AC-01, design item 2.

AC-01's "partial registration" and "replayed registration" cases are here, along with
the "cluster ready but bootstrap failed" case — which is the one worth reading first,
because the cluster reports ACTIVE throughout and only the bootstrap is incomplete.

The credential tests use the GENUINE `assert_no_secret_material` from
`superplane_contracts.secrets`, not a stand-in. A stand-in would let this package's
idea of what counts as secret material drift from the contract's, invisibly.
"""

from __future__ import annotations

import dataclasses

import pytest
from superplane_bootstrap.admission import AdmissionProof, IsolationEvidence
from superplane_bootstrap.components import ComponentInstallation, InstalledObject
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.readiness import ReadinessCheck, RuntimeReadiness
from superplane_bootstrap.registration import (
    _ARN_BEARING_FIELDS,
    WorkspaceTarget,
    finalize_registration,
    reserve_registration,
)
from superplane_bootstrap.target import verify_target
from superplane_contracts.connections import CredentialReference
from superplane_contracts.health import ContractViolation
from superplane_contracts.secrets import assert_no_secret_material

from .conftest import (
    CLUSTER_ARN,
    CREDENTIAL_ID,
    ENFORCE_VERSION,
    NAMESPACE,
    WORKSPACE_ID,
    FakeRegistrationStore,
)

CONTRACT_VERSION = "v1"
NAMESPACE_UID = "namespace-uid-0001"


@pytest.fixture
def target(binding, provider_identity, observed_cluster, expected_target):
    return verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adp-created",
        **expected_target,
    )


@pytest.fixture
def installation(target):
    return ComponentInstallation(
        workspace_id=target.workspace_id,
        cluster_arn=target.cluster_arn,
        namespace=NAMESPACE,
        namespace_uid=NAMESPACE_UID,
        namespace_owned=True,
        objects=(
            InstalledObject(
                kind="Namespace", name=NAMESPACE, owned=True, uid=NAMESPACE_UID
            ),
        ),
    )


def _evidence(
    *, verified: bool = True, namespace: str = NAMESPACE
) -> IsolationEvidence:
    proofs = (
        AdmissionProof(
            name="restricted_admission_enforced",
            verified=verified,
            detail="" if verified else "namespace enforces baseline",
        ),
        AdmissionProof(name="tenant_pod_cannot_reach_imds", verified=True),
    )
    return IsolationEvidence(
        namespace=namespace, enforce_version=ENFORCE_VERSION, proofs=proofs
    )


def _readiness(*, usable: bool = True, namespace: str = NAMESPACE) -> RuntimeReadiness:
    """A readiness object, verified or not.

    `RuntimeReadiness.usable` is a computed property, so an unusable one is produced by
    giving it a failed check rather than by setting a flag — which is the point of
    computing it: no test, and no production path, can assert readiness it did not
    establish.
    """
    return RuntimeReadiness(
        namespace=namespace,
        checks=(
            ReadinessCheck(
                name="workspace_controller_available",
                verified=usable,
                detail="" if usable else "the workspace controller is absent",
            ),
        ),
    )


def _register(store, target, installation, **overrides):
    """Reserve then finalize, which is what registration now is (F5).

    The single `register_workspace` call this replaces took its conflict decision AFTER
    the taint had been cleared, so a conflicting workspace returned failure with
    `taint_cleared=True` and nothing restored. The claim is now taken before any mutation
    and completed after readiness, so a helper that wants a finished registration has to
    do both halves — and a test that wants only the conflict decision calls
    `reserve_registration` directly.

    `reservation` is an override rather than always freshly taken so the mismatched-
    reservation tests can supply one held for another workspace.
    """
    arguments = {
        "store": store,
        "target": target,
        "installation": installation,
        "evidence": _evidence(),
        "readiness": _readiness(),
        "credential_reference_id": CREDENTIAL_ID,
        "contract_version": CONTRACT_VERSION,
        "screen": assert_no_secret_material,
        **overrides,
    }
    reservation = arguments.pop(
        "reservation",
        None,
    )
    if reservation is None:
        reservation = reserve_registration(
            store=store, target=target, namespace=installation.namespace
        )
    return finalize_registration(reservation=reservation, **arguments)


# --- Control ---------------------------------------------------------------------


def test_a_ready_workspace_is_registered_once(target, installation):
    store = FakeRegistrationStore()

    registration = _register(store, target, installation)

    assert registration.replayed is False
    assert len(store.finalized) == 1
    assert registration.target.workspace_id == WORKSPACE_ID
    assert registration.target.cluster_arn == CLUSTER_ARN
    assert registration.target.namespace_uid == NAMESPACE_UID


# --- Readiness (AC-01: cluster ready but bootstrap failed) -----------------------


def test_an_unproved_workspace_is_not_registered(target, installation):
    """The named AC-01 case. The cluster is ACTIVE the whole time.

    What is not ready is the bootstrap, not the cluster — and the cluster's own
    status cannot distinguish them, which is why readiness comes from evidence.
    """
    store = FakeRegistrationStore()

    with pytest.raises(BootstrapRefused, match="isolation is not proved"):
        _register(store, target, installation, evidence=_evidence(verified=False))

    assert store.finalized == []


def test_evidence_with_no_proofs_does_not_register(target, installation):
    """Vacuous success must not read as readiness."""
    store = FakeRegistrationStore()
    empty = IsolationEvidence(namespace=NAMESPACE, enforce_version=ENFORCE_VERSION)

    with pytest.raises(BootstrapRefused, match="no proofs ran"):
        _register(store, target, installation, evidence=empty)

    assert store.finalized == []


def test_evidence_for_a_different_namespace_does_not_register(target, installation):
    """Proofs about one namespace say nothing about another.

    Without this check, a bootstrap could prove isolation for a preflight namespace
    and register a different one as usable on that basis.
    """
    store = FakeRegistrationStore()

    with pytest.raises(BootstrapRefused, match="does not describe the namespace"):
        _register(
            store, target, installation, evidence=_evidence(namespace="somewhere-else")
        )

    assert store.finalized == []


def test_readiness_is_checked_before_the_existing_record_is_read(target, installation):
    """Ordering matters: a failed bootstrap must not be turned into a success by a
    previous run's record being present."""
    store = FakeRegistrationStore()
    store.records[WORKSPACE_ID] = _register(
        FakeRegistrationStore(), target, installation
    ).target

    with pytest.raises(BootstrapRefused, match="isolation is not proved"):
        _register(store, target, installation, evidence=_evidence(verified=False))


# --- Partial registration (AC-01) ------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    [
        "workspace_id",
        "org_id",
        "account_id",
        "region",
        "cluster_name",
        "cluster_arn",
        "endpoint",
        "namespace",
        "namespace_uid",
        "cluster_ownership",
        "credential_reference_id",
        "contract_version",
    ],
)
def test_no_field_of_a_registration_may_be_blank(field_name):
    """ "Non-empty" is in the design item because the partial-registration failure
    mode is a record that exists with blank identity fields — downstream reads it as
    present and cannot tell it apart from a complete one."""
    complete = {
        "workspace_id": WORKSPACE_ID,
        "org_id": "org",
        "account_id": "000000000000",
        "region": "us-east-1",
        "cluster_name": "cluster",
        "cluster_arn": CLUSTER_ARN,
        "endpoint": "https://example.invalid",
        "namespace": NAMESPACE,
        "namespace_uid": NAMESPACE_UID,
        "cluster_ownership": "adp-created",
        "credential_reference_id": CREDENTIAL_ID,
        "contract_version": CONTRACT_VERSION,
    }

    with pytest.raises(BootstrapRefused, match=f"WorkspaceTarget.{field_name}"):
        WorkspaceTarget(**{**complete, field_name: "   "})


def test_a_registration_missing_the_namespace_uid_is_refused(target, installation):
    """Without the uid, cleanup could only identify the namespace by name — and a
    namespace with the right name may be a different object."""
    store = FakeRegistrationStore()
    partial = dataclasses.replace(installation, namespace_uid="")

    with pytest.raises(BootstrapRefused, match="namespace_uid"):
        _register(store, target, partial)

    assert store.finalized == []


def test_a_mismatched_installation_and_target_are_refused(target, installation):
    store = FakeRegistrationStore()
    foreign = dataclasses.replace(installation, workspace_id="another-workspace")

    with pytest.raises(BootstrapRefused, match="different workspace"):
        _register(store, target, foreign)

    assert store.finalized == []


def test_an_installation_on_a_different_cluster_is_refused(target, installation):
    store = FakeRegistrationStore()
    foreign = dataclasses.replace(
        installation, cluster_arn=CLUSTER_ARN.replace("cluster/", "cluster/other-")
    )

    with pytest.raises(BootstrapRefused, match="different cluster"):
        _register(store, target, foreign)


# --- Replay vs conflict (AC-01) --------------------------------------------------


def test_an_exact_replay_does_not_write_twice(target, installation):
    """The same operation re-delivered. Writing again would duplicate rows or bump a
    record downstream consumers treat as immutable."""
    store = FakeRegistrationStore()

    first = _register(store, target, installation)
    second = _register(store, target, installation)

    assert first.replayed is False
    assert second.replayed is True
    assert len(store.finalized) == 1


def test_a_replay_returns_the_same_identity(target, installation):
    store = FakeRegistrationStore()

    first = _register(store, target, installation)
    second = _register(store, target, installation)

    assert second.target.immutable_identity == first.target.immutable_identity


def test_rebinding_a_workspace_to_a_different_cluster_is_refused(
    target, installation, binding, provider_identity, observed_cluster, expected_target
):
    """Rebinding silently re-points every tenant's work at a different cluster.

    `installation_bootstrap.py` sets this precedent with "cluster binding already
    differs" and refuses organization rebinding outright.
    """
    store = FakeRegistrationStore()
    _register(store, target, installation)

    other_arn = CLUSTER_ARN.replace("cluster/", "cluster/other-")
    other_cluster = dataclasses.replace(observed_cluster, arn=other_arn)
    other_target = verify_target(
        binding=binding,
        provider=provider_identity,
        observed=other_cluster,
        cluster_ownership="adp-created",
        **{**expected_target, "expected_cluster_arn": other_arn},
    )
    other_installation = dataclasses.replace(installation, cluster_arn=other_arn)

    # Matched on the divergent FIELD rather than on "refusing to rebind": the reservation
    # refusal now covers two conflicts — a rebinding and an F10 live claim — so the shared
    # prefix says "already claimed" and the store's conflict text distinguishes them. The
    # field name is the part an operator acts on, and the part this test is about.
    with pytest.raises(BootstrapRefused, match="different cluster_arn"):
        _register(store, other_target, other_installation)

    assert len(store.finalized) == 1


def test_a_record_that_cannot_be_compared_is_treated_as_a_conflict(
    target, installation
):
    """A record with no comparable identity has not been shown to be the same record,
    so it must not be accepted as a replay."""
    store = FakeRegistrationStore()
    store.records[WORKSPACE_ID] = object()

    with pytest.raises(BootstrapRefused, match="binds it differently"):
        _register(store, target, installation)

    assert store.finalized == []


def test_a_namespace_recreated_under_a_new_uid_is_a_conflict(target, installation):
    """Same workspace, same cluster, different namespace object.

    The namespace was deleted and recreated between runs, so the registered uid no
    longer identifies anything. Reconciling silently would leave cleanup holding a
    precondition that can never match.
    """
    store = FakeRegistrationStore()
    _register(store, target, installation)

    recreated = dataclasses.replace(installation, namespace_uid="a-different-uid")

    with pytest.raises(BootstrapRefused, match="namespace_uid"):
        _register(store, target, recreated)


# --- Credential discipline (design item 2) ---------------------------------------


def test_the_record_holds_a_credential_reference_id_and_no_credential(
    target, installation
):
    """The registration record is what gets quoted in issue comments and completion
    reports, which is the artifact design item 2 is about."""
    registration = _register(
        store=FakeRegistrationStore(), target=target, installation=installation
    )

    for spec in dataclasses.fields(registration.target):
        if spec.name in _ARN_BEARING_FIELDS:
            continue
        assert_no_secret_material(
            getattr(registration.target, spec.name), what=f"registered.{spec.name}"
        )
    assert registration.target.credential_reference_id == CREDENTIAL_ID


def test_a_credential_reference_refuses_an_arn():
    """Shown against the real contract: an ARN is a complete-enough pointer that
    leaking it is a disclosure on its own."""
    with pytest.raises(ContractViolation, match="not an ARN"):
        CredentialReference(
            credential_id="arn:aws:secretsmanager:us-east-1:000000000000:secret:x",
            service="eks",
            label="workspace",
        )


def test_the_cluster_arn_is_carried_despite_the_contract_treating_arns_as_secret(
    target, installation
):
    """The one deliberate exemption, asserted rather than left implicit.

    `superplane_contracts.secrets` treats every ARN as secret-shaped — right for
    credential payloads. But `cluster_arn` is a public Terraform output and a
    workspace target that could not name its own cluster would be useless, so the
    exemption is by explicit field name. This test is what makes widening that set a
    visible decision rather than a quiet one.
    """
    registration = _register(FakeRegistrationStore(), target, installation)

    assert registration.target.cluster_arn == CLUSTER_ARN
    assert _ARN_BEARING_FIELDS == {"cluster_arn"}, (
        "the ARN screen exemption was widened; every added field is a field a "
        "credential could be carried through"
    )


def test_the_secret_screen_is_actually_consulted(target, installation):
    """A screen that is imported but never called is the failure a passing test
    would otherwise hide, so this asserts the call happened for every screened field."""
    screened: list[str] = []

    def recording_screen(payload, *, what="payload"):
        screened.append(what)
        assert_no_secret_material(payload, what=what)

    _register(FakeRegistrationStore(), target, installation, screen=recording_screen)

    expected = len(dataclasses.fields(WorkspaceTarget)) - len(_ARN_BEARING_FIELDS)
    assert len(screened) == expected
    assert any("credential_reference_id" in what for what in screened)
    assert not any("cluster_arn" in what for what in screened)


def test_a_credential_arriving_through_any_field_is_refused(target, installation):
    """The screen runs over the whole record, not just the credential reference —
    which is the only way a credential could arrive."""
    store = FakeRegistrationStore()

    with pytest.raises(ContractViolation):
        _register(
            store,
            target,
            installation,
            credential_reference_id=(
                "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKC\n"
                "-----END RSA PRIVATE KEY-----"
            ),
        )

    assert store.finalized == []
