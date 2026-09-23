"""The gate sequence: reserve, verify, install, prepare, prove, clear, finalize.

Issue #5533 (w6-10), EPIC #4910.

## The order is the design

Each step's safety depends on the previous one having happened, so the ordering is
not stylistic. The sequence below is the repaired one; the numbering differences from
the first revision are the F2, F4 and F5 fixes and are called out where they occur.

1. **Verify the target** (`target.py`) — before any mutation, because installing
   against the wrong cluster succeeds silently and no later gate catches it.
2. **Verify the access prerequisites** (`prerequisites.py`) — F4. The scoped access
   entry and the security-group rules were an unverified optional argument; they are
   now a mandatory gate, and it runs here because it is the cheapest gate to fail and
   a failure leaves the cluster untouched.
3. **Reserve the registration** (`registration.reserve_registration`) — F5. The
   conflict decision moved in front of every mutation. A workspace already bound to a
   different cluster is refused while refusing is still free.
4. **Install components** (`components.py`) — creates the namespace with the
   admission labels the next steps will require, and refuses if another controller is
   already reconciling this cluster.
5. **Prepare the system workloads** (`readiness.prepare_system_workloads`) — F2. The
   cluster's own services, CoreDNS above all, which the live handoff records as
   unschedulable behind the bootstrap taint.
6. **Establish and verify runtime readiness** (`readiness.py`) — F2. Scoped RBAC,
   controller availability, completed single-controller handover, system workloads
   actually available, and the tenant interlock still in force.
7. **Prove isolation** (`admission.py`) — against the namespace that now exists.
   Cannot run earlier: there is nothing to probe until the namespace is there.
8. **Clear the bootstrap taint** — ONLY if every declared proof verified AND the
   runtime is usable. This is the one action here whose effect is immediate: it makes
   the nodes schedulable for tenant work.
9. **Finalize the registration** (`registration.finalize_registration`) — last,
   because a registration is the domain's statement that work may be scheduled, and
   steps 1-8 are what make that true.

Step 8 is what the workspace infrastructure module's `tenant_scheduling_prerequisites`
output demands in its own words: *"Only after these proofs, remove the bootstrap taint
through the bounded bootstrap owner."* That declared requirement is discharged
structurally, by this function's call ordering, rather than by a probe — see
`admission.ORDERING_PROOFS` and the note in `admission.py`'s docstring about why
positional mapping of declared controls to checks was wrong.

## Why the irreversible action is now next-to-last, and what happens if the last step fails

The first revision cleared the taint and then did the whole of registration — read the
existing record, refuse a rebinding, write. Review finding F5: a conflict was therefore
discovered with the nodes already schedulable, the failure was returned with
`taint_cleared=True` and no attempt to put the taint back, and a store exception in the
same window escaped identically.

Two changes. The conflict decision moved to step 3, before anything is mutated, so the
common refusal now costs nothing. And the narrow window that remains — between clearing
the taint at step 8 and the store write at step 9 — is explicitly recovered:
`_restore_interlock` re-applies the taint and VERIFIES it is present again. If the
restoration itself fails, the outcome says so rather than reporting a tidy failure over
nodes that are still schedulable. `BootstrapOutcome.nodes_left_schedulable` is the
property an operator and a test read for that case.

The window cannot be eliminated, only made small and recorded. Durable state
(`state.py`) is what makes an interruption inside it recoverable on the next run:
`BootstrapState.recovery_pending` is true exactly while something is still outstanding,
and `recover_interrupted_bootstrap` restores the interlock from the record alone, without
needing to re-observe anything. F12: that decision is `recovery_pending` and not
`interrupted_after_taint_cleared`, which records that an interruption happened and
therefore stays true after it has been recovered.

## Why a failure returns an outcome instead of only raising

A partial bootstrap needs cleanup, and cleanup needs to know what was created. If
this function raised and discarded its progress, the caller would have to guess —
and guessing wrong in either direction is bad: guessing too much means planning to
delete objects that were never created (which, for a namespace identified by name,
can hit an unrelated object), and guessing too little leaves things permanent by
accident.

So `bootstrap_workspace` catches the refusal, records what had been established at
that point, and returns a `BootstrapOutcome` carrying both the failure and the
cleanup plan. The refusal is preserved on the outcome rather than swallowed: a caller
that ignores `registered` still has the exception to surface. `raise_for_failure()`
is provided for callers that want the exception.

F6: a refusal raised after the namespace was created carries the partial installation
on the exception (`BootstrapRefused.installation`), and the handler below prefers its
own local value but falls back to that one. Without the fallback, a CRD failure
produced `cleanup=None` for a cluster with an owned namespace on it.

## What "ready" means on the outcome

`BootstrapOutcome.ready` is true only when the taint was actually cleared and the
registration actually happened. It is computed, not stored, so no code path can
construct an outcome claiming readiness it did not reach — the same reason
`IsolationEvidence.may_clear_taint` is a property.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .access import ClusterAccess, ProviderIdentity, RegistrationStore
from .admission import IsolationEvidence, prove_tenant_isolation
from .components import ComponentInstallation, install_components
from .errors import BootstrapRefused, failure_kind
from .inventory import PrerequisiteInventory
from .prerequisites import (
    ExpectedPrerequisites,
    PrerequisiteAccess,
    verify_prerequisites,
)
from .readiness import (
    REQUIRED_SYSTEM_WORKLOADS,
    RuntimeReadiness,
    establish_runtime_readiness,
    prepare_system_workloads,
)
from .registration import (
    RegistrationReservation,
    WorkspaceRegistration,
    finalize_registration,
    reserve_registration,
)
from .retire import CleanupPlan, plan_cleanup
from .state import (
    BootstrapState,
    StateStore,
    claim_fingerprint,
    load_state,
    record,
)
from .target import VerifiedTarget, verify_target

# The taint the workspace infrastructure module puts on every new node, from
# `tenant_scheduling_prerequisites.bootstrap_taint_key`. Not restated as a literal
# anywhere else in this package.
BOOTSTRAP_TAINT_KEY = "superplane.aws-e/bootstrap"

# The default name of the workspace controller Deployment whose availability readiness
# requires. A parameter on `bootstrap_workspace` rather than a hard-coded literal
# because the release owner chooses it, but defaulted so a caller cannot accidentally
# pass a blank and skip the check — `establish_runtime_readiness` refuses a blank name.
WORKSPACE_CONTROLLER_NAME = "superplane-controller"


class SecretScreen(Protocol):
    """See `registration.SecretScreen` — same signature as the contract's function."""

    def __call__(self, payload: object, *, what: str = ...) -> None: ...


@dataclass(frozen=True)
class BootstrapOutcome:
    """Everything one bootstrap attempt established, whether or not it succeeded.

    Populated progressively, so a failure at step 4 still carries the verified target,
    the inventory and the installation record — which is exactly what cleanup needs.
    """

    target: VerifiedTarget | None = None
    inventory: PrerequisiteInventory | None = None
    reservation: RegistrationReservation | None = None
    installation: ComponentInstallation | None = None
    readiness: RuntimeReadiness | None = None
    evidence: IsolationEvidence | None = None
    taint_cleared: bool = False
    taint_restored: bool = False
    restore_failed: bool = False
    reservation_released: bool = False
    registration: WorkspaceRegistration | None = None
    cleanup: CleanupPlan | None = None
    refusal: BootstrapRefused | None = None

    @property
    def ready(self) -> bool:
        """True only when the interlock was cleared AND the workspace registered."""
        return self.taint_cleared and self.registration is not None

    @property
    def registered(self) -> bool:
        return self.registration is not None

    @property
    def nodes_left_schedulable(self) -> bool:
        """The F5 alarm: the interlock was cleared for a bootstrap that then failed.

        True when the taint was removed, the workspace was NOT registered, and the
        taint was not successfully put back. This is the state the review identified as
        being reported as a tidy failure — tenant work can now schedule onto a cluster
        whose bootstrap was rejected. A caller that ignores everything else must not be
        able to ignore this, so it is a named property rather than an inference from
        three other fields.
        """
        return self.taint_cleared and not self.registered and not self.taint_restored

    def raise_for_failure(self) -> None:
        """Re-raise the refusal that stopped this attempt, if there was one."""
        if self.refusal is not None:
            raise self.refusal


def _require_interlock_proofs(evidence, readiness):
    if not evidence.may_clear_taint:
        raise BootstrapRefused(
            "refusing to remove the bootstrap taint: unproved controls ("
            + (", ".join(evidence.unverified) or "no proofs ran")
            + ")"
        )
    if not readiness.usable:
        raise BootstrapRefused(
            "refusing to remove the bootstrap taint: the workspace runtime is not "
            "usable ("
            + ("; ".join(readiness.failures) or "no readiness checks ran")
            + "). Making nodes schedulable before the controller and the cluster's own "
            "system services are available would admit tenant work that cannot run"
        )


def _clear_interlock(
    access: ClusterAccess,
    evidence: IsolationEvidence,
    readiness: RuntimeReadiness,
    taint_key: str,
) -> bool:
    """Remove the bootstrap taint, but only against proved isolation AND readiness.

    Re-checks both `may_clear_taint` and `usable` immediately before the call rather
    than trusting the caller's control flow. The checks are cheap and this is the action
    that makes nodes schedulable for tenant work; a future refactor that reorders the
    caller should fail closed here rather than silently clear the interlock.

    The readiness half is the F2 fix at its last possible point: even if every earlier
    guard were removed, a cluster with no available CoreDNS or no reconciling controller
    cannot get past this line.
    """
    _require_interlock_proofs(evidence, readiness)
    remaining = access.remove_bootstrap_taint(taint_key)
    still_present = [taint for taint in remaining if taint.get("key") == taint_key]
    if still_present:
        raise BootstrapRefused(
            f"the bootstrap taint {taint_key!r} is still present after removal was "
            "requested; the nodes are not schedulable, so registering the workspace "
            "as usable would be false"
        )
    return True


def _restore_interlock(access: ClusterAccess, taint_key: str) -> bool:
    """Put the bootstrap taint back after a post-removal failure. Verifies it took.

    **This is the F5 recovery.** Returns whether the nodes are unschedulable again,
    and does NOT raise on failure: it is called from a refusal path, and an exception
    here would replace the original diagnosis with a second one while losing the fact
    that the first failure happened. The caller records both.

    Verification matters as much as the attempt. A restore that silently did nothing
    and a restore that worked look identical from the call alone, and the difference is
    whether tenant work can schedule onto a cluster whose bootstrap was rejected.
    """
    try:
        present = access.restore_bootstrap_taint(taint_key)
    except Exception:
        return False
    return any(taint.get("key") == taint_key for taint in present)


def _release_reservation(
    store: RegistrationStore, workspace_id: str, attempt_token: str
) -> bool:
    """Drop THIS attempt's claim after a refusal. Never raises, for the same reason.

    A reservation left behind blocks every later attempt for this workspace, so the
    outcome reports whether the release succeeded rather than assuming it.

    F10: the token is required, so this releases only the claim this attempt holds. The
    finding's second half is a losing attempt dropping the winner's reservation mid-flight,
    and this is the call site where that would have happened — a bootstrap refused at step
    4 released "the workspace's" claim, not its own.
    """
    try:
        return bool(store.release(workspace_id, attempt_token))
    except Exception:
        return False


def recover_interrupted_bootstrap(
    *,
    access: ClusterAccess,
    store: RegistrationStore,
    state_store: StateStore,
    workspace_id: str,
    cluster_arn: str,
    taint_key: str = BOOTSTRAP_TAINT_KEY,
    authority_factory=None,
    binding=None,
    target=None,
) -> BootstrapOutcome:
    """Restore then release under the exact claim's exclusive database lock.

    A successor cannot reserve between restoration and release. Failed restoration
    retains the claim. Missing/stale claims and an unreachable database authorize
    no cluster mutation; the outcome retains the unresolved interlock alarm.
    """
    state = load_state(state_store, workspace_id=workspace_id, cluster_arn=cluster_arn)
    if not state.recovery_pending:
        return BootstrapOutcome()
    from .adapters import KubectlClusterAccess

    if authority_factory is None and isinstance(access, KubectlClusterAccess):
        return BootstrapOutcome(
            taint_cleared=state.taint_cleared or state.taint_clear_pending,
            refusal=BootstrapRefused(
                "production recovery requires trusted authority composition"
            ),
        )
    if authority_factory is not None:
        try:
            if target is None or (target.workspace_id, target.cluster_arn) != (
                workspace_id,
                cluster_arn,
            ):
                raise BootstrapRefused(
                    "authority recovery target differs from the durable state"
                )
            authority = authority_factory.recover(
                binding=binding,
                target=target,
                store=store,
                state_store=state_store,
                claim=state.registration_claim,
            )
            authority.revoke()
            access = authority.supervisor
        except Exception as error:
            refusal = BootstrapRefused(
                "bootstrap authority recovery is unresolved; claim retained"
            )
            refusal.__cause__ = error
            return BootstrapOutcome(
                taint_cleared=state.taint_cleared or state.taint_clear_pending,
                restore_failed=state.interlock_restoration_pending,
                refusal=refusal,
            )
    unidentified_claim = not state.reservation_release_authorized
    released = False
    restored = state.taint_restored
    claim_not_found = False
    if not unidentified_claim:

        def restore():
            nonlocal restored
            if state.interlock_restoration_pending:
                restored = _restore_interlock(access, taint_key)
                return restored
            return True

        try:
            matched, _restoration_ok, released = store.recover_claim(
                workspace_id, state.registration_claim, restore=restore
            )
            claim_not_found = not matched
        except Exception:
            pass  # Retain claim and alarm; no unknown outcome becomes released.

    # F13. `claim_not_found` settles the claim as surely as `released` does, and for a
    # better reason: the database was asked and reported that this record's claim is not
    # there. Continuing to record it as held would make recovery refuse this workspace
    # forever over a claim nothing holds.
    #
    # A failed restoration or unknown database outcome keeps the claim held.
    claim_settled = released or claim_not_found
    record(
        state_store,
        state,
        taint_restored=restored,
        registration_reserved=state.registration_reserved and not claim_settled,
        # Cleared with the claim it identifies, so a settled claim leaves no fingerprint for
        # a later call to present again. Keeping it would be harmless today — the row is
        # gone, so the compare-and-delete matches nothing — but it would leave the record
        # asserting a claim that does not exist, and this package's whole discipline is that
        # the record says only what is true.
        registration_claim=("" if claim_settled else state.registration_claim),
    )

    # The interlock is the alarm; the reservation is bookkeeping. A call that only had a
    # release to retry does not claim an interruption was found, because by then the
    # cluster is already safe and saying otherwise would send an operator looking at
    # nodes that are correctly tainted.
    if unidentified_claim:
        # F13. Deliberately first: it describes why the other two branches' work did not
        # happen, so reporting either of them instead would be reporting a state this call
        # did not establish.
        refusal = BootstrapRefused(
            f"the durable record for workspace {workspace_id!r} says a registration "
            "reservation is held but cannot identify WHICH claim it is, so recovery "
            "cannot prove the attempt that took it is gone. Releasing the reservation "
            "anyway would delete whichever claim exists — including a live attempt's, "
            "which would admit a second concurrent bootstrap of this workspace — and "
            "re-applying the bootstrap taint would make a live attempt's nodes "
            "unschedulable, so neither was done. This record predates claim "
            "fingerprinting. An operator must confirm no bootstrap is running for this "
            "workspace and then clear the reservation directly"
        )
    elif claim_not_found:
        # F13. The record named a claim; the database does not have it. Reported as its own
        # case because the previous two branches would both be WRONG here: there is no
        # interruption to announce (the nodes were deliberately left alone) and the claim
        # was not "unreleasable" (there was nothing to release).
        refusal = BootstrapRefused(
            f"the registration reservation this record names for workspace "
            f"{workspace_id!r} is no longer in the registry, so this record is stale: "
            "either the claim was already cleared, or another bootstrap attempt holds "
            "this workspace now. Nothing was released and the bootstrap interlock was "
            "left as it is — re-applying it would make a live attempt's nodes "
            "unschedulable, and this record cannot establish that no attempt is running. "
            "If no bootstrap is in progress, check the node taints for this cluster "
            "directly"
        )
    elif state.interlock_restoration_pending:
        refusal = BootstrapRefused(
            "a previous bootstrap attempt was interrupted between removing the "
            f"bootstrap taint and registering workspace {workspace_id!r}. The nodes "
            + (
                "were schedulable for a bootstrap that never completed; the taint has "
                "been restored"
                if restored
                else "ARE STILL SCHEDULABLE for a bootstrap that never completed and "
                "the taint could not be restored; tenant work may schedule onto an "
                "unverified cluster until an operator re-applies it"
            )
        )
    elif not released:
        refusal = BootstrapRefused(
            f"the registration reservation for workspace {workspace_id!r} is still "
            "recorded as held and could not be released. The interlock is in place, so "
            "nothing unverified can schedule, but every later bootstrap attempt for "
            "this workspace will be refused as a conflict until the claim is dropped"
        )
    else:
        # The release succeeded and the interlock needed nothing. Converged: the next
        # call finds `recovery_pending` false and returns immediately.
        refusal = None

    return BootstrapOutcome(
        taint_cleared=state.taint_cleared or state.taint_clear_pending,
        taint_restored=restored,
        restore_failed=(state.taint_cleared or state.taint_clear_pending)
        and not restored,
        reservation_released=released,
        refusal=refusal,
    )


def bootstrap_workspace(
    *,
    binding: object,
    provider: ProviderIdentity,
    access: ClusterAccess,
    prerequisite_access: PrerequisiteAccess,
    store: RegistrationStore,
    state_store: StateStore,
    observed_cluster: object,
    expected_account_id: str,
    expected_region: str,
    expected_cluster_name: str,
    expected_cluster_arn: str,
    expected_certificate_authority_data: str,
    expected_cni_role_arn: str,
    expected_prerequisites: ExpectedPrerequisites,
    cluster_ownership: str,
    namespace: str,
    enforce_version: str,
    credential_reference_id: str,
    contract_version: str,
    screen: SecretScreen,
    controller_name: str = WORKSPACE_CONTROLLER_NAME,
    required_system_workloads: Sequence[str] = REQUIRED_SYSTEM_WORKLOADS,
    required_crds: Sequence[str] | None = None,
    declared_proofs: Sequence[str] | None = None,
    taint_key: str = BOOTSTRAP_TAINT_KEY,
    authority_factory=None,
) -> BootstrapOutcome:
    """Run the full gate sequence. Returns an outcome; never partially registers.

    On refusal at any step, returns an outcome carrying the refusal and a cleanup
    plan scoped to what had actually been created. The taint is cleared only after
    every declared isolation proof verified AND the runtime was shown usable, and the
    registration is finalized only after the taint is confirmed gone. A refusal after
    the taint was cleared restores it and reports whether the restoration succeeded.

    `inventory` is no longer a parameter: the prerequisite inventory is BUILT here by
    the mandatory gate at step 2 (F4), from authoritative reads, rather than supplied
    by the caller. A caller-supplied inventory was unverifiable — it is the record that
    authorizes revocation, so accepting one from the caller would let the caller
    authorize its own cleanup.
    """
    target: VerifiedTarget | None = None
    inventory: PrerequisiteInventory | None = None
    reservation: RegistrationReservation | None = None
    installation: ComponentInstallation | None = None
    readiness: RuntimeReadiness | None = None
    evidence: IsolationEvidence | None = None
    taint_cleared = False
    # A placeholder so the refusal handler always has something to record against. It
    # is replaced by the real record as soon as the target establishes the trusted
    # workspace id — see the note below on why it cannot be loaded before that.
    state: BootstrapState | None = None
    authority = None

    try:
        # 1. Identity and target, before any mutation.
        target = verify_target(
            binding=binding,
            provider=provider,
            observed=observed_cluster,
            expected_account_id=expected_account_id,
            expected_region=expected_region,
            expected_cluster_name=expected_cluster_name,
            expected_cluster_arn=expected_cluster_arn,
            expected_certificate_authority_data=expected_certificate_authority_data,
            cluster_ownership=cluster_ownership,
        )

        access.bind_target(target, binding)

        # The durable record is keyed on the TRUSTED workspace id, which only exists
        # once `verify_target` has read it from the operation binding's principal.
        # Loading it earlier would mean keying it on a caller-supplied id — the
        # identity rule this whole package follows in reverse.
        state = load_state(
            state_store,
            workspace_id=target.workspace_id,
            cluster_arn=target.cluster_arn,
        )

        from .adapters import KubectlClusterAccess
        from .authority_runtime import BootstrapAuthorityFactory

        if authority_factory is None and isinstance(access, KubectlClusterAccess):
            raise BootstrapRefused(
                "production bootstrap requires trusted temporary-authority composition"
            )
        if authority_factory is not None:
            if not isinstance(authority_factory, BootstrapAuthorityFactory):
                raise BootstrapRefused(
                    "bootstrap authority must come from trusted service composition"
                )
            from .components import WORKSPACE_CRDS

            release = authority_factory.release
            if (
                release.namespace,
                release.controller,
                release.enforce_version,
                release.crds,
                release.system_workloads,
            ) != (
                namespace,
                controller_name,
                enforce_version,
                tuple(required_crds or WORKSPACE_CRDS),
                tuple(required_system_workloads),
            ):
                raise BootstrapRefused(
                    "trusted bootstrap grant plan differs from the installation release"
                )
            from .prerequisites import verify_network_prerequisites

            verify_network_prerequisites(
                access=prerequisite_access,
                target=target,
                expected=expected_prerequisites,
                provider_account_id=provider.account_id,
            )
            reservation = reserve_registration(
                store=store, target=target, namespace=namespace
            )
            if reservation.replayed:
                # A completed registration carries no mutation claim. Return its
                # canonical record instead of reinstalling or acquiring new grants.
                existing = store.read(target.workspace_id)
                if existing is None:
                    raise BootstrapRefused(
                        "completed registration is missing its canonical target"
                    )
                return BootstrapOutcome(
                    target=target,
                    reservation=reservation,
                    taint_cleared=False,
                    registration=WorkspaceRegistration(target=existing, replayed=True),
                )
            state = record(
                state_store,
                state,
                registration_reserved=True,
                registration_claim=claim_fingerprint(reservation.attempt_token),
            )
            authority = authority_factory.create(
                binding=binding,
                target=target,
                reservation=reservation,
                store=store,
                state_store=state_store,
            )
            authority.acquire()
            access = authority.installer
            state = load_state(
                state_store,
                workspace_id=target.workspace_id,
                cluster_arn=target.cluster_arn,
            )

        # 2. F4: the mandatory, attributed, recorded prerequisite gate. Runs before
        # the first mutation so a missing access path refuses against an untouched
        # cluster.
        inventory, state = verify_prerequisites(
            access=prerequisite_access,
            target=target,
            expected=expected_prerequisites,
            principal_arn=provider.principal_arn,
            namespace=namespace,
            store=state_store,
            state=state,
            provider_account_id=provider.account_id,
            authority=authority,
        )

        # 3. F5: the conflict decision, in front of every mutation.
        if reservation is None:
            reservation = reserve_registration(
                store=store, target=target, namespace=namespace
            )
        # Recorded as held only when a claim actually IS held. A replay of a completed
        # registration carries no token and leaves no `reserved` row, so recording it here
        # would make `reservation_release_pending` true forever: recovery would keep trying
        # to release a claim that does not exist, get False from a `registered` row it must
        # not delete, and report an unreleasable claim on every call. That is the F12
        # convergence failure arriving through the F10 fence.
        # F13: the claim's IDENTITY is recorded in the same write as the boolean, as a
        # one-way fingerprint of the token — never the token, which would make the state
        # file a copy of the permission to publish this workspace. Written here, in the
        # same `record` call, because a boolean that outlives its fingerprint is precisely
        # the state F13 is about: recovery would know a claim was held and not which one.
        state = record(
            state_store,
            state,
            registration_reserved=bool(reservation.attempt_token),
            registration_claim=(
                claim_fingerprint(reservation.attempt_token)
                if reservation.attempt_token
                else ""
            ),
        )

        from .target import _binding_identity

        _binding_identity(binding)
        # 4. First mutation. Ownership recorded durably as it happens (F3/F6).
        install_kwargs = {
            "access": access,
            "target": target,
            "namespace": namespace,
            "enforce_version": enforce_version,
            "store": state_store,
            "state": state,
            "controller_name": controller_name,
        }
        if required_crds is not None:
            install_kwargs["required_crds"] = required_crds
        installation, state = install_components(**install_kwargs)

        # 5 and 6. F2: make the runtime exist, then verify it does. The system
        # workloads are placed while tenant scheduling is still denied, and step 6
        # re-verifies that it still is.
        prepare_system_workloads(access=access, required=required_system_workloads)
        readiness = establish_runtime_readiness(
            access=access,
            namespace=installation.namespace,
            controller_name=controller_name,
            required_system_workloads=required_system_workloads,
        )

        # 7. Isolation proofs against the namespace that now exists.
        evidence = prove_tenant_isolation(
            access=access,
            namespace=installation.namespace,
            enforce_version=enforce_version,
            expected_cni_role_arn=expected_cni_role_arn,
            declared_proofs=declared_proofs,
        )

        if authority is not None:
            # Remove installer RBAC/EKS entries and registrar administration before
            # the final inventory, readiness, interlock and registration decisions.
            authority.revoke(retain_workspace=True)
            access = authority.supervisor
            if access.can_tenant_change_admission_labels(installation.namespace):
                raise BootstrapRefused(
                    "tenant authority remains after installer revocation"
                )
            readiness = establish_runtime_readiness(
                access=access,
                namespace=installation.namespace,
                controller_name=controller_name,
                required_system_workloads=required_system_workloads,
            )

        # 8. The declared "only after these proofs" requirement, discharged by this
        # ordering, now also gated on readiness.
        # Write intent first: a killed process cannot report whether the cluster
        # mutation completed. Recovery treats that window as possibly schedulable.
        _binding_identity(binding)
        _require_interlock_proofs(evidence, readiness)
        state = record(state_store, state, taint_clear_pending=True)
        taint_cleared = _clear_interlock(access, evidence, readiness, taint_key)
        state = record(
            state_store, state, taint_cleared=True, taint_clear_pending=False
        )

        # 9. The single write, completing the claim taken at step 3.
        registration = finalize_registration(
            store=store,
            reservation=reservation,
            target=target,
            installation=installation,
            evidence=evidence,
            readiness=readiness,
            credential_reference_id=credential_reference_id,
            contract_version=contract_version,
            screen=screen,
        )
        state = record(state_store, state, registration_finalized=True)
    except Exception as error:
        # Deliberately `Exception` and not `BootstrapRefused`. The recovery below is what
        # closes the F5 window, and the window is widest for the failures this package
        # does NOT raise itself: the registration write is the last step and it fails with
        # whatever the store raises — an OSError, a driver error, a timeout. Catching only
        # refusals meant such a failure escaped with the taint already removed, the
        # reservation still held and nothing restored, which is the F5 hazard in its worst
        # form and strictly worse than the conflict case the review named, because no
        # operator-facing outcome was produced at all.
        #
        # An unexpected error is still not silently converted into a tidy failure: it is
        # wrapped, so the outcome refuses and the original is chained as `__cause__`.
        if isinstance(error, BootstrapRefused):
            refusal = error
        else:
            # F9: the type, not the text. This handler catches EVERY unexpected error in
            # the sequence, so it is the widest of the leak paths — a store or SDK
            # failure at step 9 arrives here with whatever string its driver chose, and
            # `cli.py::_report` puts `str(refusal)` on stdout. `__cause__` is set below,
            # so nothing is lost to a debugger.
            refusal = BootstrapRefused(
                f"the bootstrap failed with an unexpected {failure_kind(error)}; the "
                "gates below were run to leave the cluster safe, but this is not a "
                "refusal this package anticipated and needs an operator"
            )
            refusal.__cause__ = error

        # F6: a refusal raised after the namespace existed carries the partial
        # installation. Prefer the local value; fall back to the exception's, which is
        # the CRD-failure case that previously produced cleanup=None.
        if installation is None:
            carried = getattr(refusal, "installation", None)
            if isinstance(carried, ComponentInstallation):
                installation = carried

        # F5 recovery: if the interlock was already cleared, put it back and verify.
        taint_restored = False
        restore_failed = False
        if taint_cleared or (state is not None and state.taint_clear_pending):
            taint_cleared = True  # conservatively report the uncertain mutation
            taint_restored = _restore_interlock(access, taint_key)
            restore_failed = not taint_restored

        reservation_released = False
        if authority is not None:
            try:
                # Durable retention/cleanup policy survives a failed response or
                # process restart. No recovery path reacquires a revoked grant.
                authority.revoke()
            except Exception as recovery_error:
                refusal = BootstrapRefused(
                    "temporary bootstrap authority remains unresolved; recovery is required"
                )
                refusal.__cause__ = recovery_error
        if reservation is not None and reservation.attempt_token and not restore_failed:
            # Fenced on this attempt's own token (F10). A reservation with no token is a
            # replay of a COMPLETED registration — there is no claim to drop, and an
            # unfenced release here would unpublish a live workspace on a refusal path.
            reservation_released = _release_reservation(
                store, reservation.workspace_id, reservation.attempt_token
            )

        # `state` is None only when step 1 refused, which is before any record exists
        # and before anything was mutated — there is nothing to record in that case.
        if state is not None:
            # RE-READ before writing. `state` is this frame's local, and the steps that
            # mutate the cluster record their progress through their OWN local copy:
            # `install_components` writes the namespace record and returns an updated
            # state, but when it then REFUSES — the CRD-failure case F6 is about — that
            # return never happens and this frame's `state` is still the pre-namespace
            # value. Writing it back here overwrote the durable namespace record with
            # null, so the very recovery path that exists to preserve the cleanup plan
            # destroyed the record the plan is built from: the retry could no longer
            # recognise its OWN namespace and adopted it instead, which means a
            # bootstrap that failed halfway would leave its namespace behind forever.
            #
            # A stale write is worse than no write, so the persisted record wins for
            # everything except the two facts this handler is the sole author of.
            persisted = state_store.load() or state
            still_holds_claim = (
                reservation is not None
                and bool(reservation.attempt_token)
                and not reservation_released
            )
            record(
                state_store,
                persisted,
                taint_restored=taint_restored,
                registration_reserved=still_holds_claim,
                # F13: the fingerprint tracks the boolean exactly. A refusal that released
                # its own claim must leave no fingerprint behind — otherwise the record
                # would name a claim that no longer exists — and a refusal that could NOT
                # release it must keep the fingerprint, because that record is the only
                # thing that will ever let recovery identify the claim it stranded.
                registration_claim=(
                    claim_fingerprint(reservation.attempt_token)
                    if still_holds_claim and reservation is not None
                    else ""
                ),
            )

        return BootstrapOutcome(
            target=target,
            inventory=inventory,
            reservation=reservation,
            installation=installation,
            readiness=readiness,
            evidence=evidence,
            taint_cleared=taint_cleared,
            taint_restored=taint_restored,
            restore_failed=restore_failed,
            reservation_released=reservation_released,
            registration=None,
            cleanup=(
                plan_cleanup(
                    target=target, installation=installation, inventory=inventory
                )
                if target is not None
                and installation is not None
                and inventory is not None
                else None
            ),
            refusal=refusal,
        )

    return BootstrapOutcome(
        target=target,
        inventory=inventory,
        reservation=reservation,
        installation=installation,
        readiness=readiness,
        evidence=evidence,
        taint_cleared=taint_cleared,
        registration=registration,
        cleanup=plan_cleanup(
            target=target, installation=installation, inventory=inventory
        ),
        refusal=None,
    )
