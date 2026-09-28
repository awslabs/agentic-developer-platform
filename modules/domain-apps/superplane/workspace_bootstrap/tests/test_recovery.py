"""Reservation, taint restoration and interrupted recovery — findings F5 and F6.

F5 verbatim: "registration refusal happens after the taint is removed; conflict returns
failure with `taint_cleared=True` and no taint-restore."

F6 verbatim: "component failures discard the cleanup state just created — a CRD failure
after namespace creation leaves `cleanup=None`."

The two are one story from the operator's side, which is why they share a file. F5 is
about the window between "nodes made schedulable" and "workspace registered"; F6 is
about what the refusal in that window can tell you afterwards. A refusal that restored
the taint but produced no cleanup plan still leaves objects nobody will delete, and a
refusal with a plan but schedulable nodes is the worse half. Both have to hold together.

The assertion that carries the most weight here is `outcome.nodes_left_schedulable`.
It is the single fact that distinguishes a safe refusal from the state the review found
being reported as a tidy failure — tenant work able to schedule onto a cluster whose
bootstrap was rejected. Its false-positive direction is asserted too: a clean refusal
before the taint ever came off must NOT raise the alarm, or the alarm means nothing.
"""

from __future__ import annotations

import pytest
from superplane_bootstrap.components import ComponentInstallation
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.prerequisites import ExpectedPrerequisites
from superplane_bootstrap.registration import reserve_registration
from superplane_bootstrap.state import (
    BootstrapState,
    claim_fingerprint,
    state_from_mapping,
)
from superplane_bootstrap.target import verify_target
from superplane_bootstrap.workspace import (
    BOOTSTRAP_TAINT_KEY,
    bootstrap_workspace,
    recover_interrupted_bootstrap,
)
from superplane_contracts.secrets import assert_no_secret_material

from .conftest import (
    ACCOUNT_ID,
    CLUSTER_ARN,
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


@pytest.fixture
def target(binding, provider_identity, observed_cluster, expected_target):
    return verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adp-created",
        **expected_target,
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


def _interrupted_state(claim: str | None = None) -> FakeStateStore:
    """A store holding the exact F5 interruption: taint cleared, never registered.

    Built by saving a real `BootstrapState` rather than by hand-writing a mapping, so
    the record has to survive `to_mapping`/`state_from_mapping` — the round trip the
    fake performs on every save. A hand-written payload could carry a field the real
    serialization drops, and then this whole file would be testing a state the
    production store can never produce.

    F13: the record names the claim it holds, defaulting to a fingerprint of the token
    `FakeRegistrationStore` issues — so the default is the LEGITIMATE recovery case, an
    attempt whose own record identifies its own claim. Pass `claim` to get the stale case:
    a record naming a claim the store does not hold.
    """
    store = FakeStateStore()
    store.save(
        BootstrapState(
            workspace_id=WORKSPACE_ID,
            cluster_arn=CLUSTER_ARN,
            prerequisites_recorded=True,
            registration_reserved=True,
            registration_claim=(
                claim_fingerprint(FakeRegistrationStore.issued_token)
                if claim is None
                else claim
            ),
            taint_cleared=True,
            registration_finalized=False,
        )
    )
    return store


def _store_holding_the_recorded_claim() -> FakeRegistrationStore:
    """A store holding the reservation that `_interrupted_state` names.

    The token matters as much as the reservation now (F13): recovery presents a
    fingerprint, and the store releases only the claim that fingerprint identifies. A
    reservation with no token is a claim held by nobody, which the production schema makes
    unrepresentable — so a test setting one without the other would be asserting against a
    row that cannot exist.
    """
    store = FakeRegistrationStore()
    store.reservations[WORKSPACE_ID] = {"cluster_arn": CLUSTER_ARN}
    store.tokens[WORKSPACE_ID] = store.issued_token
    return store


# --- F5: the conflict decision happens before any mutation -----------------------


def test_a_conflicting_workspace_is_refused_before_the_cluster_is_touched(
    binding, provider_identity, observed_cluster, expected_target
):
    """The F5 defect, inverted. This is the case the review named.

    Previously the conflict was decided after the taint came off, so this exact input
    produced `taint_cleared=True`, no restore, and a "failure" that had already made the
    nodes schedulable. Now the reservation is taken first, so the refusal lands with the
    cluster untouched and nothing to undo.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore(conflict="already bound to another cluster")

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert isinstance(outcome.refusal, BootstrapRefused)
    assert outcome.taint_cleared is False
    assert _taint_present(access)
    assert access.removed_taints == []
    assert access.created_namespaces == [], (
        "the cluster was mutated before this workspace's claim was established"
    )
    assert outcome.registered is False
    assert outcome.nodes_left_schedulable is False
    # The store's own conflict text is carried through, because it is the only thing that
    # knows WHICH conflict this is — a rebinding here, a live claim in the F10 tests
    # below — and a refusal an operator cannot act on is barely better than the race.
    assert "already bound to another cluster" in str(outcome.refusal)
    assert "nothing needs to be undone" in str(outcome.refusal)


def test_an_unanswered_reservation_is_not_treated_as_a_held_one(
    binding, provider_identity, observed_cluster, expected_target
):
    """A store that returns no `reserved` key has not granted a claim.

    The natural failure is a store erroring and returning `{}`; `{}.get("reserved")` is
    falsy, which happens to be safe — but the check is explicit so a future edit that
    defaults it the other way is a test failure rather than a silent claim on a cluster
    this bootstrap has no reservation for.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore(reserve_answers=False)

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert isinstance(outcome.refusal, BootstrapRefused)
    assert "unanswered reservation is not a held one" in str(outcome.refusal)
    assert access.created_namespaces == []
    assert _taint_present(access)


def test_the_reservation_is_taken_before_the_first_cluster_mutation(
    binding, provider_identity, observed_cluster, expected_target
):
    """Asserted as an ordering on the clean path, not inferred from the refusals.

    Every test above shows a FAILED reservation stops the mutation. None of them shows
    the reservation happens first when it succeeds — a refactor could move it after the
    namespace creation and they would all still pass.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.ready is True
    reserved = store.calls.index(f"reserve:{WORKSPACE_ID}")
    finalized = store.calls.index(f"finalize:{WORKSPACE_ID}")
    assert reserved < finalized
    assert access.calls, (
        "the cluster was never touched, so there is no ordering to test"
    )
    # The reservation is not a cluster call, so the ordering is asserted across the two
    # fakes: the store's reserve must precede the cluster's first mutation. Recorded
    # calls are the only way to see this, since both operations succeed.
    assert store.calls[0] == f"reserve:{WORKSPACE_ID}", (
        "the first store operation was not the reservation, so a mutation may precede "
        "the claim"
    )


def test_a_reservation_held_for_another_workspace_cannot_finalize(target):
    """The reservation and the target must name the same workspace.

    Finalizing would complete one workspace's claim with another's identity — a
    registration that passes every field check and is bound to the wrong thing.
    """
    store = FakeRegistrationStore()
    reservation = reserve_registration(store=store, target=target, namespace=NAMESPACE)
    foreign = type(reservation)(
        workspace_id="another-workspace",
        identity=reservation.identity,
        replayed=False,
        # A genuinely held claim — the token is what makes it one (F10). Constructing this
        # without one now refuses, which would make the test pass for the wrong reason:
        # the check under test is the workspace-id mismatch, and it must be reached.
        attempt_token=reservation.attempt_token,
    )

    from superplane_bootstrap.registration import finalize_registration

    with pytest.raises(BootstrapRefused, match="reservation is held for workspace"):
        finalize_registration(
            store=store,
            reservation=foreign,
            target=target,
            installation=ComponentInstallation(
                workspace_id=WORKSPACE_ID,
                cluster_arn=CLUSTER_ARN,
                namespace=NAMESPACE,
                namespace_uid="namespace-uid-0001",
                namespace_owned=True,
            ),
            evidence=None,  # type: ignore[arg-type]
            readiness=None,
            credential_reference_id=CREDENTIAL_ID,
            contract_version="v1",
            screen=assert_no_secret_material,
        )

    assert store.finalized == []


# --- F5: a failure after the taint came off restores it --------------------------


def test_a_registration_write_failure_after_the_taint_cleared_restores_the_taint(
    binding, provider_identity, observed_cluster, expected_target
):
    """The other half of F5: the window cannot be closed, so it must be recoverable.

    The registration write is the last step and it can fail — a dropped connection, a
    constraint violation. At that moment the nodes ARE schedulable. Restoring the taint
    is the only way the refusal is honest, and `nodes_left_schedulable` must be false
    because the restoration succeeded.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore(finalize_fails=True)

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.registered is False
    assert outcome.taint_cleared is True, (
        "the scenario did not reach the window it is about"
    )
    assert outcome.taint_restored is True
    assert outcome.restore_failed is False
    assert outcome.nodes_left_schedulable is False
    assert access.restored_taints == [BOOTSTRAP_TAINT_KEY]
    assert _taint_present(access), "the taint was reported restored but is not present"


def test_an_unexpected_error_in_the_window_is_wrapped_rather_than_escaping(
    binding, provider_identity, observed_cluster, expected_target
):
    """The failures this package does not raise itself are the ones the window is
    widest for, and they were escaping.

    The registration write is the last step and it fails with whatever the store raises —
    an OSError, a driver error, a timeout. The handler caught only `BootstrapRefused`, so
    such a failure propagated out of `bootstrap_workspace` with the taint already removed,
    the reservation still held, and no outcome produced at all. That is worse than the
    conflict case the review named: there was not even a report to act on.

    The original is chained rather than swallowed, because "unexpected" is exactly the
    case where the operator needs the underlying error — but chained is the whole of it.
    This test previously asserted the store's message was IN the refusal string, which
    was F9 stated as a requirement: `cli.py::_report` serializes that string to stdout,
    and a real store raises with a DSN or a request body in its text. The need the
    assertion was reaching for is diagnosability, and `__cause__` is what actually
    serves it — a traceback carries the full error to a debugger without printing it.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore(finalize_fails=True)

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert isinstance(outcome.refusal, BootstrapRefused)
    assert "unexpected OSError" in str(outcome.refusal)
    assert isinstance(outcome.refusal.__cause__, OSError), (
        "the underlying error was discarded, leaving the operator with a refusal that "
        "cannot be diagnosed"
    )
    # F9: the type is named, the text is not. Reachable through the chain, not stdout.
    assert "synthetic registration write failure" not in str(outcome.refusal)
    assert "synthetic registration write failure" in str(outcome.refusal.__cause__)


def test_the_reservation_is_released_when_the_bootstrap_it_claimed_for_failed(
    binding, provider_identity, observed_cluster, expected_target
):
    """A held reservation for an abandoned attempt blocks every retry.

    Without the release, the conflict check added for F5 would refuse the workspace's
    own next attempt — the fix would have turned a recoverable failure into a permanent
    one.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore(finalize_fails=True)

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.reservation_released is True
    assert store.released == [WORKSPACE_ID]
    assert store.reservations == {}, (
        "the reservation outlived the attempt that took it, so a retry would refuse "
        "itself as a conflict"
    )


def test_a_failed_restoration_is_reported_as_the_alarm_it_is(
    binding, provider_identity, observed_cluster, expected_target
):
    """The worst state this package can reach, and it must not read as a tidy failure.

    Nodes schedulable, workspace unregistered, taint un-restorable. There is no
    automatic way out, so the only correct behaviour is to say so loudly —
    `nodes_left_schedulable` is the machine-readable form and the refusal text is the
    operator-readable one.
    """
    access = FakeClusterAccess(crds=[], restore_fails=True)
    store = FakeRegistrationStore(finalize_fails=True)

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.taint_cleared is True
    assert outcome.taint_restored is False
    assert outcome.restore_failed is True
    assert outcome.nodes_left_schedulable is True
    assert not _taint_present(access)


def test_a_clean_early_refusal_does_not_raise_the_schedulable_alarm(
    binding, provider_identity, observed_cluster, expected_target
):
    """The alarm's false-positive direction. Without this, it could be hardcoded true
    for every refusal and every test above would still pass — and an alarm that fires
    on every failure is one an operator learns to ignore."""
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore(conflict="already bound elsewhere")

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert isinstance(outcome.refusal, BootstrapRefused)
    assert outcome.nodes_left_schedulable is False
    assert outcome.restore_failed is False
    assert access.restored_taints == [], (
        "a taint that was never removed was 'restored', which would mask a real "
        "restoration failure elsewhere"
    )


def test_a_successful_bootstrap_never_reports_the_alarm(
    binding, provider_identity, observed_cluster, expected_target
):
    """`taint_cleared and not registered` is the alarm condition, so the clean path —
    cleared AND registered — must be silent."""
    access = FakeClusterAccess(crds=[])

    outcome = _run(
        access,
        FakeRegistrationStore(),
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
    )

    assert outcome.ready is True
    assert outcome.nodes_left_schedulable is False
    assert outcome.taint_restored is False


# --- F5: recovering an interrupted attempt ---------------------------------------


def test_an_interrupted_attempt_is_detected_from_the_durable_record_alone():
    """The cluster looks clean, which is the whole problem.

    After an interruption the taint is simply gone — indistinguishable from a successful
    bootstrap by observation. Only the record showing the registration never happened
    can tell them apart, which is why this state lives in `state.py` and not on the
    cluster.
    """
    state_store = _interrupted_state()
    access = FakeClusterAccess(crds=[], taints=[])
    store = _store_holding_the_recorded_claim()

    outcome = recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert isinstance(outcome.refusal, BootstrapRefused)
    assert "interrupted between removing the bootstrap taint" in str(outcome.refusal)
    assert outcome.taint_restored is True
    assert outcome.nodes_left_schedulable is False
    assert _taint_present(access), "recovery did not put the interlock back"


def test_recovery_releases_the_stranded_reservation():
    """The interrupted attempt's claim is still held, and it would block the retry that
    recovery exists to enable."""
    state_store = _interrupted_state()
    store = _store_holding_the_recorded_claim()

    outcome = recover_interrupted_bootstrap(
        access=FakeClusterAccess(crds=[], taints=[]),
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert outcome.reservation_released is True
    assert store.reservations == {}


def test_recovery_records_the_restoration_durably():
    """Otherwise a second recovery would see the same interrupted state and try again —
    harmless once, but it means the record never converges on the truth."""
    state_store = _interrupted_state()
    store = _store_holding_the_recorded_claim()
    before = len(state_store.history)

    recover_interrupted_bootstrap(
        access=FakeClusterAccess(crds=[], taints=[]),
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert len(state_store.history) > before
    assert state_store.history[-1]["taint_restored"] is True
    assert state_store.history[-1]["registration_reserved"] is False


def test_a_reservation_that_could_not_be_released_stays_recorded_as_held():
    """The inverse, and the safer direction of the same write.

    Recovery records `registration_reserved` from whether the release SUCCEEDED, not from
    having asked. A claim whose fate is unknown may still be out there, and recording it as
    released would leave a stranded reservation nothing ever revisits — so the recorded
    value stays true.

    F13: "unknown" specifically means the STORE COULD NOT BE REACHED, which is why the fake
    is made to raise rather than simply hold no reservation. A store that answers and
    reports no matching claim is a different case entirely — the claim is settled, and this
    record is stale — and conflating the two is what made recovery refuse a workspace
    forever over a claim nothing held. The two are asserted separately; see
    `test_a_record_naming_a_claim_the_registry_does_not_have_is_stale`.
    """
    state_store = _interrupted_state()
    store = FakeRegistrationStore(recovery_unreachable=True)

    outcome = recover_interrupted_bootstrap(
        access=FakeClusterAccess(crds=[], taints=[]),
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert outcome.reservation_released is False
    assert state_store.history[-1]["registration_reserved"] is True
    assert state_store.history[-1]["registration_claim"] != "", (
        "the fingerprint was dropped while the claim's fate was still unknown, so a "
        "later recovery could not identify the claim it must retry"
    )
    assert state_store.history[-1]["taint_restored"] is False, (
        "an unreachable registry cannot authorize a mutation over a possible successor"
    )


def test_a_record_naming_a_claim_the_registry_does_not_have_is_stale():
    """**The F13 case at this layer: the record is stale, so no cluster write happens.**

    The store ANSWERS and reports no claim matching this record's fingerprint. That means
    either the claim was already cleared or another attempt holds the workspace now — and
    from here those are indistinguishable, so one of the possibilities is a live bootstrap.

    Two things must therefore not happen. The interlock must not go back on, because
    re-tainting every node while a successor is mid-bootstrap makes that successor's
    workloads unschedulable, and a taint write cannot be undone by later realising the
    record was stale. And the record must not keep asserting the claim, or recovery would
    refuse this workspace forever over a claim nothing holds.

    The real-database counterpart, with a genuine live successor on a second connection, is
    in `test_recovery_postgres.py`. This pins the decision; that one pins the consequence.
    """
    state_store = _interrupted_state()
    store = FakeRegistrationStore()  # answers, and holds no matching claim
    access = FakeClusterAccess(crds=[], taints=[])

    outcome = recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert outcome.reservation_released is False
    assert access.restored_taints == [], (
        "a stale record re-applied the interlock, which would make a live successor's "
        "nodes unschedulable mid-bootstrap"
    )
    assert state_store.history[-1]["registration_reserved"] is False
    assert state_store.history[-1]["registration_claim"] == ""
    assert "is no longer in the registry" in str(outcome.refusal)
    assert "left as it is" in str(outcome.refusal), (
        "the refusal must say the interlock was NOT touched; an operator reading a "
        "recovery failure needs to know whether nodes are schedulable right now"
    )


def test_a_record_that_cannot_name_its_claim_releases_nothing_and_writes_nothing():
    """**A pre-fingerprint record has no authority, and the fence is what says so.**

    This is the F13 defect in its original form: a record asserting `registration_reserved`
    with no way to say WHICH reservation. Every record written before the fingerprint
    existed looks like this, so the case is real rather than hypothetical, and the tempting
    reading — "the boolean authorized a release before, so honour it" — is exactly the
    unfenced delete the finding is about.

    Nothing may be inferred from the boolean alone. The store is not asked (asking would
    mean presenting a blank fingerprint, which `release_claim` refuses by contract), the
    interlock is not written, and the record keeps asserting the claim it cannot identify,
    because an operator has not yet resolved it and a recovery that forgot would lose the
    only evidence that a reservation is outstanding.

    This is pinned offline deliberately. Reverting the authority check to the pre-F13
    boolean leaves the entire offline suite green without it — the real-database tests catch
    it, but a fence whose only guard needs PostgreSQL is a fence that gets removed by
    someone running the fast suite and believing it.
    """
    state_store = _interrupted_state(claim="")  # the pre-fingerprint record
    store = _store_holding_the_recorded_claim()  # a claim IS held — and must survive
    access = FakeClusterAccess(crds=[], taints=[])

    outcome = recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert outcome.reservation_released is False
    assert store.released == [], (
        "an unidentifiable record released a claim anyway, which is the unfenced delete "
        "F13 is about — the claim it dropped could belong to a live successor"
    )
    assert WORKSPACE_ID in store.reservations, "the held reservation was deleted"
    assert access.restored_taints == [], (
        "the interlock was re-applied on a record that cannot prove the attempt holding "
        "this workspace is gone, which would strand a live attempt's nodes"
    )
    assert state_store.history[-1]["registration_reserved"] is True, (
        "the outstanding reservation was forgotten, so nothing would ever revisit it"
    )
    assert "cannot identify WHICH claim" in str(outcome.refusal)
    assert "An operator must confirm" in str(outcome.refusal), (
        "the refusal must name the human action; this state cannot be resolved "
        "automatically by design, so a refusal without it is a dead end"
    )


def test_recovery_reports_the_alarm_when_the_taint_cannot_be_restored():
    """Recovery can fail too, and then tenant work may schedule onto an unverified
    cluster until an operator intervenes. The refusal says exactly that.

    The store here holds the claim this record names, so the release succeeds and the
    restoration is genuinely attempted — the alarm is about the taint and must not be
    reachable by a record that declined to touch the cluster at all.
    """
    outcome = recover_interrupted_bootstrap(
        access=FakeClusterAccess(crds=[], taints=[], restore_fails=True),
        store=_store_holding_the_recorded_claim(),
        state_store=_interrupted_state(),
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert outcome.restore_failed is True
    assert outcome.nodes_left_schedulable is True
    assert "STILL SCHEDULABLE" in str(outcome.refusal)


def test_recovery_on_a_clean_state_does_nothing():
    """Safe to call unconditionally at the start of every attempt — which is the only
    way it gets called reliably. A version that refused when there was nothing to
    recover could not be wired in that way."""
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore()

    outcome = recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=FakeStateStore(),
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert outcome.refusal is None
    assert outcome.taint_cleared is False
    assert access.restored_taints == []
    assert store.released == []


def test_recovery_does_nothing_for_a_completed_bootstrap():
    """`taint_cleared and registration_finalized` is success, not an interruption.

    Getting this wrong would restore the taint on a healthy registered workspace and
    stop every tenant pod on it — a self-inflicted outage triggered by a routine
    recovery call.
    """
    state_store = FakeStateStore()
    state_store.save(
        BootstrapState(
            workspace_id=WORKSPACE_ID,
            cluster_arn=CLUSTER_ARN,
            taint_cleared=True,
            registration_finalized=True,
        )
    )
    access = FakeClusterAccess(crds=[], taints=[])

    outcome = recover_interrupted_bootstrap(
        access=access,
        store=FakeRegistrationStore(),
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert outcome.refusal is None
    assert access.restored_taints == []
    assert not _taint_present(access), (
        "recovery re-tainted a registered workspace's nodes, which would evict nothing "
        "but would stop everything new from scheduling"
    )


def test_recovery_refuses_a_state_record_naming_another_cluster():
    """A workspace previously bootstrapped against another cluster needs an operator
    decision, not a silent rebind — and recovery must not be the place that quietly
    performs one."""
    state_store = _interrupted_state()

    with pytest.raises(BootstrapRefused, match="different cluster"):
        recover_interrupted_bootstrap(
            access=FakeClusterAccess(crds=[], taints=[]),
            store=FakeRegistrationStore(),
            state_store=state_store,
            workspace_id=WORKSPACE_ID,
            cluster_arn=CLUSTER_ARN.replace("cluster/", "cluster/other-"),
        )


def test_the_interruption_property_is_computed_not_stored():
    """`interrupted_after_taint_cleared` is derived from two recorded facts, so no
    writer can record "not interrupted" for a state that is."""
    interrupted = BootstrapState(
        workspace_id=WORKSPACE_ID, cluster_arn=CLUSTER_ARN, taint_cleared=True
    )
    completed = BootstrapState(
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
        taint_cleared=True,
        registration_finalized=True,
    )
    untouched = BootstrapState(workspace_id=WORKSPACE_ID, cluster_arn=CLUSTER_ARN)

    assert interrupted.interrupted_after_taint_cleared is True
    assert completed.interrupted_after_taint_cleared is False
    assert untouched.interrupted_after_taint_cleared is False


# --- F12: recovery converges, and each half retries only its own work -------------
#
# F12 verbatim: "`interrupted_after_taint_cleared` ignores `taint_restored`. After
# recovery records `taint_restored=True`, the property remains true because
# `taint_cleared=True` and `registration_finalized=False`. Every subsequent recovery
# therefore re-applies the taint and reports the old interruption again, contradicting
# the recovery test's stated convergence requirement."
#
# The contradiction the review points at is real and it is in this file:
# `test_recovery_records_the_restoration_durably` above says "otherwise a second recovery
# would see the same interrupted state and try again ... it means the record never
# converges on the truth" — and then asserts only that a write happened. It never called
# recovery twice, so it could not notice that the write it checked did not converge
# anything. That is the gap these tests close, and the reason they call recovery twice is
# that convergence is not observable from one call at all.


def _pending_release_state() -> FakeStateStore:
    """A claim stranded by an attempt that refused BEFORE the taint ever came off.

    Reachable and not hypothetical: the reservation is taken at step 3 and the first
    cluster mutation is step 4, so anything that refuses in between — a CRD failure, an
    unavailable CoreDNS, a killed process — leaves exactly this. `taint_cleared` is false,
    so the interlock is still on and nothing unverified can schedule; the only outstanding
    problem is a claim that will refuse every later attempt as a conflict.

    This state is why `reservation_release_pending` is not conditioned on
    `taint_cleared`: a recovery that only looked at the post-taint window would never see
    it, and the workspace would stay permanently unbootstrappable with a perfectly safe
    cluster.
    """
    store = FakeStateStore()
    store.save(
        BootstrapState(
            workspace_id=WORKSPACE_ID,
            cluster_arn=CLUSTER_ARN,
            prerequisites_recorded=True,
            registration_reserved=True,
            registration_claim=claim_fingerprint(FakeRegistrationStore.issued_token),
            taint_cleared=False,
            registration_finalized=False,
        )
    )
    return store


def test_a_second_recovery_after_a_successful_one_does_nothing_at_all():
    """**The F12 test.** Recovery twice; the second call must find nothing to do.

    The first call restores the taint and releases the claim, and records both. The
    second must then be a genuine no-op — no taint write, no release call, no refusal —
    because the record now says the cluster is safe and the claim is gone.

    Re-applying the taint is not harmless, which is why this is a blocker rather than a
    tidiness point. `restore_bootstrap_taint` writes to every node in the cluster, and an
    operator who runs `recover` as a routine check would see the same alarm reported
    forever with no way to distinguish a stuck record from a cluster that keeps losing
    its taint.
    """
    state_store = _interrupted_state()
    store = _store_holding_the_recorded_claim()
    access = FakeClusterAccess(crds=[], taints=[])

    first = recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert first.taint_restored is True
    assert first.reservation_released is True
    assert access.restored_taints == [BOOTSTRAP_TAINT_KEY]
    assert store.released == [WORKSPACE_ID]

    writes_after_first = len(state_store.history)

    second = recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert second.refusal is None, (
        "the second recovery re-reported an interruption that had already been recovered"
    )
    assert access.restored_taints == [BOOTSTRAP_TAINT_KEY], (
        "the second recovery re-applied a taint that was already on every node"
    )
    assert store.calls.count(f"release_claim:{WORKSPACE_ID}") == 1, (
        "the second recovery re-released a reservation that was already gone"
    )
    # F10: recovery uses the UNFENCED release, and it is the only path that may. The
    # fenced `release` requires the token of the attempt that took the claim, and recovery
    # by definition does not have it — the process holding it is the one that died.
    assert not any(call.startswith("release:") for call in store.calls), (
        "recovery called the fenced release, which cannot succeed without a token it "
        "does not have"
    )
    assert len(state_store.history) == writes_after_first, (
        "the second recovery wrote state despite having nothing to record"
    )


def test_a_release_that_failed_is_retried_while_the_restored_taint_is_not():
    """The two halves retry independently — the second thing F12 asked for.

    The first call restores the taint (succeeds) and tries to release the claim (fails).
    The second call must therefore do EXACTLY the release, and must not touch the taint:
    the interlock is already on, and the whole reason the halves were separated is that
    the restoration's success was masking the release's failure.

    Asserted on the fakes' recorded calls rather than on the outcome, because the outcome
    cannot distinguish "did not need to" from "did it again and it was idempotent". Only
    the call record can.
    """
    state_store = _interrupted_state()
    store = _store_holding_the_recorded_claim()
    store.release_fails_for = 1
    access = FakeClusterAccess(crds=[], taints=[])

    first = recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert first.taint_restored is True
    assert first.reservation_released is False
    assert state_store.history[-1]["registration_reserved"] is True, (
        "a failed release recorded as released would strand the claim forever"
    )
    assert state_store.history[-1]["taint_restored"] is True, (
        "the restoration succeeded and must be recorded even though the release did not"
    )

    second = recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert second.reservation_released is True
    assert store.calls.count(f"release_claim:{WORKSPACE_ID}") == 2, (
        "the release was not retried"
    )
    assert access.restored_taints == [BOOTSTRAP_TAINT_KEY], (
        "the second call re-applied an interlock that was already restored"
    )
    assert state_store.history[-1]["registration_reserved"] is False
    assert second.refusal is None, (
        "with the interlock on and the claim dropped there is nothing left to report"
    )

    third = recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert third.refusal is None
    assert store.calls.count(f"release_claim:{WORKSPACE_ID}") == 2, (
        "recovery did not converge: a third call still had work to do"
    )


def test_a_claim_stranded_before_the_taint_came_off_is_released_without_a_taint_write():
    """The reservation half is reachable on its own, and must not touch the interlock.

    An attempt that refused between step 3 (reserve) and step 8 (clear taint) leaves a
    held claim on a cluster whose nodes were never made schedulable. Recovery has to drop
    the claim — otherwise the workspace is permanently unbootstrappable — and must not
    report an interruption alarm, because there is nothing unsafe about the cluster.

    A recovery gated on the post-taint window alone would skip this state entirely, which
    is the reason `reservation_release_pending` does not read `taint_cleared`.
    """
    state_store = _pending_release_state()
    store = _store_holding_the_recorded_claim()
    access = FakeClusterAccess(crds=[])

    outcome = recover_interrupted_bootstrap(
        access=access,
        store=store,
        state_store=state_store,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert outcome.reservation_released is True
    assert store.reservations == {}
    assert access.restored_taints == [], (
        "recovery wrote a taint for a cluster whose interlock never came off"
    )
    assert outcome.refusal is None, (
        "a safe cluster with a dropped claim is not an interruption to report"
    )
    assert outcome.nodes_left_schedulable is False
    assert _taint_present(access), (
        "the interlock was never cleared and must still be on"
    )


def test_a_stranded_claim_that_cannot_be_released_says_what_it_blocks():
    """The refusal for the release-only path names the consequence, not the mechanism.

    "A reservation could not be released" tells an operator nothing actionable. "Every
    later bootstrap attempt for this workspace will be refused as a conflict" tells them
    what they will see next and why, which is the difference between a message that ends
    an investigation and one that starts it.

    It also states that the interlock is in place, because the obvious operator fear on
    reading any recovery failure is "are nodes schedulable right now" — and here they are
    not.
    """
    outcome = recover_interrupted_bootstrap(
        access=FakeClusterAccess(crds=[]),
        store=FakeRegistrationStore(recovery_unreachable=True),
        state_store=_pending_release_state(),
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )

    assert isinstance(outcome.refusal, BootstrapRefused)
    assert "refused as a conflict" in str(outcome.refusal)
    assert "nothing unverified can schedule" in str(outcome.refusal)
    assert outcome.nodes_left_schedulable is False
    assert outcome.restore_failed is False, (
        "the interlock was never cleared, so no restoration failed"
    )


def test_failed_restoration_retains_claim_until_safe_release():
    state_store = _interrupted_state()
    store = _store_holding_the_recorded_claim()
    access = FakeClusterAccess(crds=[], taints=[], restore_fails_for=1)

    def recover():
        return recover_interrupted_bootstrap(
            access=access,
            store=store,
            state_store=state_store,
            workspace_id=WORKSPACE_ID,
            cluster_arn=CLUSTER_ARN,
        )

    first = recover()
    assert first.nodes_left_schedulable
    assert not first.reservation_released
    assert state_store.current.registration_reserved
    assert not store.released
    assert WORKSPACE_ID in store.tokens
    second = recover()
    assert second.taint_restored and second.reservation_released
    assert not second.nodes_left_schedulable
    assert not state_store.current.registration_reserved
    assert recover().refusal is None
    assert access.restored_taints == [BOOTSTRAP_TAINT_KEY] * 2
    assert store.calls.count(f"release_claim:{WORKSPACE_ID}") == 1


def test_each_recovery_action_retries_until_its_own_success():
    state_store = _interrupted_state()
    store = _store_holding_the_recorded_claim()
    store.release_fails_for = 1
    access = FakeClusterAccess(crds=[], taints=[], restore_fails_for=1)

    def recover():
        return recover_interrupted_bootstrap(
            access=access,
            store=store,
            state_store=state_store,
            workspace_id=WORKSPACE_ID,
            cluster_arn=CLUSTER_ARN,
        )

    first = recover()
    assert not first.taint_restored and not first.reservation_released
    second = recover()
    assert second.taint_restored and not second.reservation_released
    assert state_store.current.registration_reserved
    third = recover()
    assert third.taint_restored and third.reservation_released
    assert recover().refusal is None
    assert access.restored_taints == [BOOTSTRAP_TAINT_KEY] * 2
    assert store.calls.count(f"release_claim:{WORKSPACE_ID}") == 2


def test_the_recovery_decision_reads_the_restoration_and_the_historical_fact_does_not():
    """The property split, asserted directly on the state model.

    Both properties are needed and they answer different questions.
    `interrupted_after_taint_cleared` is the HISTORY — "this workspace was interrupted
    mid-bootstrap once" — and an operator reading `state` wants it regardless of whether
    it has since been recovered. `recovery_pending` is the DECISION, and it is the one
    that has to go false once the work is done.

    F12 was these two being the same property. Pinning them apart here means a future
    edit that re-merges them fails on this test rather than silently re-opening a loop
    that only shows up as a repeated alarm in production.
    """
    recovered = BootstrapState(
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
        taint_cleared=True,
        taint_restored=True,
        registration_reserved=False,
        registration_finalized=False,
    )

    assert recovered.interrupted_after_taint_cleared is True, (
        "the interruption did happen and the record should still say so"
    )
    assert recovered.recovery_pending is False, (
        "F12: a fully recovered state must not ask to be recovered again"
    )
    assert recovered.interlock_restoration_pending is False
    assert recovered.reservation_release_pending is False

    half_recovered = BootstrapState(
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
        taint_cleared=True,
        taint_restored=True,
        registration_reserved=True,
        registration_finalized=False,
    )

    assert half_recovered.recovery_pending is True
    assert half_recovered.interlock_restoration_pending is False
    assert half_recovered.reservation_release_pending is True

    registered = BootstrapState(
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
        registration_reserved=True,
        taint_cleared=True,
        registration_finalized=True,
    )

    assert registered.recovery_pending is False, (
        "a completed registration holds its reservation row as the REGISTRATION; "
        "releasing it would delete the record the workspace's usability rests on"
    )


# --- F6: a refusal after a mutation carries a cleanup plan -----------------------


def test_a_crd_failure_after_namespace_creation_still_produces_a_cleanup_plan(
    binding, provider_identity, observed_cluster, expected_target
):
    """The F6 defect, named exactly: this case used to return `cleanup=None`.

    The namespace exists and is owned. A refusal that discards that fact leaves an
    object nothing will delete and no record that it should be — which makes the next
    attempt's adoption logic see a namespace it cannot account for.
    """
    access = FakeClusterAccess(crds=[], establish_crds_result=[])
    store = FakeRegistrationStore()

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert isinstance(outcome.refusal, BootstrapRefused)
    assert outcome.installation is not None, (
        "the partial installation was discarded, so the created namespace is invisible"
    )
    assert outcome.cleanup is not None, "the F6 defect: cleanup=None after a mutation"
    assert outcome.cleanup.remove_namespace == ""
    assert outcome.cleanup.retained_namespace == NAMESPACE
    assert outcome.cleanup.retained_namespace_uid, (
        "the outstanding namespace obligation lost its immutable identity"
    )
    assert any("cascading" in reason for reason in outcome.cleanup.preserved)


def test_the_namespace_creation_is_recorded_durably_before_the_next_mutation(
    binding, provider_identity, observed_cluster, expected_target
):
    """F6's underlying rule, asserted on the write SEQUENCE rather than the end state.

    A store keeping only the latest value could not distinguish an implementation that
    records progress as it happens from one that writes everything at the end — and only
    the first survives a process killed mid-install. `FakeStateStore.history` keeps every
    write for exactly this question.
    """
    state_store = FakeStateStore()
    access = FakeClusterAccess(crds=[], establish_crds_result=[])

    _run(
        access,
        FakeRegistrationStore(),
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        state_store=state_store,
    )

    namespace_writes = [
        index
        for index, payload in enumerate(state_store.history)
        if payload.get("namespace")
    ]
    crd_writes = [
        index
        for index, payload in enumerate(state_store.history)
        if payload.get("crds_established")
    ]
    assert namespace_writes, "the namespace creation was never recorded durably"
    # The namespace must be recorded in a write of its own, strictly before anything the
    # CRD step records. An implementation that recorded both facts in one write at the end
    # of `install_components` would satisfy a final-state assertion and still lose the
    # namespace if the process died during the CRD apply — which is the F6 scenario.
    assert not crd_writes or namespace_writes[0] < crd_writes[0], (
        "the namespace and the CRDs were recorded in the same write or the wrong order, "
        "so a process killed between the two mutations leaves no record of the namespace"
    )


def test_a_recorded_namespace_survives_the_serialization_round_trip(
    binding, provider_identity, observed_cluster, expected_target
):
    """A record that cannot be re-read is not durable.

    The fake round-trips every save through `to_mapping`/`state_from_mapping`, so this
    asserts the ownership facts survive it — the uid above all, since ownership is
    exactly "durable record and live uid agree" and a dropped uid silently converts an
    owned namespace to an adopted one on the next attempt.
    """
    state_store = FakeStateStore()

    _run(
        FakeClusterAccess(crds=[]),
        FakeRegistrationStore(),
        binding,
        provider_identity,
        observed_cluster,
        expected_target,
        state_store=state_store,
    )

    reloaded = state_from_mapping(state_store.history[-1])

    assert reloaded.namespace is not None
    assert reloaded.namespace.name == NAMESPACE
    assert reloaded.namespace.uid.strip()
    assert reloaded.namespace.created_by_bootstrap is True
    assert reloaded.controller_installed is True
    assert reloaded.prerequisites_recorded is True


def test_a_refusal_before_any_mutation_plans_no_cleanup(
    binding, provider_identity, observed_cluster, expected_target
):
    """F6's opposite direction, and it matters as much.

    "Always return a plan" would be the lazy fix, and a plan naming a namespace that was
    never created is worse than no plan: executing it would delete somebody else's
    namespace of the same name. No mutation means no plan.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore(conflict="already bound elsewhere")

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.installation is None
    assert outcome.cleanup is None
    assert access.created_namespaces == []


def test_a_failure_after_the_taint_cleared_carries_both_the_plan_and_the_restoration(
    binding, provider_identity, observed_cluster, expected_target
):
    """F5 and F6 together, which is the case an operator actually meets.

    A late failure has to do both jobs: put the interlock back AND describe what exists
    so it can be removed. An outcome doing only one of the two is still an incident.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeRegistrationStore(finalize_fails=True)

    outcome = _run(
        access, store, binding, provider_identity, observed_cluster, expected_target
    )

    assert outcome.taint_restored is True
    assert outcome.nodes_left_schedulable is False
    assert outcome.cleanup is not None
    assert outcome.cleanup.remove_namespace == ""
    assert outcome.cleanup.retained_namespace == NAMESPACE
    assert outcome.cleanup.preserves_cluster is True
    assert outcome.inventory is not None, (
        "the plan was built without the prerequisite inventory, so it cannot revoke the "
        "access path this attempt established"
    )


def test_unreachable_registry_never_authorizes_interlock_mutation():
    access = FakeClusterAccess(crds=[], taints=[])
    state = _interrupted_state()
    result = recover_interrupted_bootstrap(
        access=access,
        store=FakeRegistrationStore(recovery_unreachable=True),
        state_store=state,
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
    )
    assert result.nodes_left_schedulable and not result.reservation_released
    assert state.current.registration_reserved
    assert access.restored_taints == []
