"""The adapter bookkeeping path: record, then call, then reconcile — under B's authority.

Issue #5049 (U11), EPIC #4910. R15, A's half.

## What this is, and the three things it is not

This is the sequencing the contract types exist to enforce, in one place, so an
adapter author gets the ordering by calling it rather than by remembering it. Two
entry points: `perform_operation` (record a handle, then call) and
`release_allocation` (re-check the provider, then report).

It is **not a job lifecycle.** There is no state machine over an operation's life,
no queue, no timer, no scheduler and no thread. Every method here runs inside the
caller's call and returns.

It is **not a service.** No routes, no server, no listener.

It is **not a writer of domain records.** `HandleStore` is a boundary A calls
across, implemented upstream by U11c. A never writes the domain database.

## Why the ordering lives in code rather than in a docstring

The failure is an ordering failure, and ordering is the one thing a type system
does not check. `perform_operation` therefore closes the window structurally:

1. Build the handle from locally-available identity (available before any call —
   upstream already chooses `clusterName` at `onboarder.go:170`).
2. Persist it and require the store's acknowledgement.
3. `authorize_provider_call` — refuse to proceed without durability.
4. Only now invoke the provider.

A crash at any point after step 2 leaves a findable record, which is what makes
step 4's lost response reconcilable instead of invisible.

## The ambiguity path, and the one thing it will not do

An ambiguous call routes to `reconciliation.reconcile` with a provider
observation, and `perform_operation` **never repeats the operation itself**. It
returns the decision and lets the caller act on
`decision.may_repeat_operation`. The reason is the boundary: retry ordering and
attempt fencing are B's, and an adapter that looped internally would be making
retry decisions B owns — the same second-lifecycle-owner problem as adding a
scheduler, one layer down.

## B's authority, mocked and recorded as a mock

`OperationAuthority` is B's contract for "this operation is active and you may act
under it". **No lease, fencing or `attempt_id` implementation exists in ADP
today**, so:

* the Protocol below is the shape A calls, not an implementation A ships;
* every test supplies a mock authority, and the suite records it as a mock;
* R15 acceptance 8 (stops and cleanup with the agent process gone) is satisfied by
  B's independent-lifetime driver calling `release_allocation`. A supplies the
  verified release path; A does not supply the thing that outlives the agent.

The live criteria for all of this stay deferred: a mock authority verifies the
mock, exactly as a mock provider re-check verifies the mock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .accounting import AllocationResources, ReleaseAssessment, assess_release
from .handles import (
    CallDecision,
    CallOutcome,
    HandleRecord,
    OperationKind,
    ProviderHandle,
    authorize_provider_call,
)
from .health import ContractViolation
from .provider_truth import Finding, ReleaseIntent, TeardownReport
from .reconciliation import (
    ProviderObservation,
    ProviderPresence,
    ReconcileDecision,
    ReconcileRequest,
    ReconcileResult,
    reconcile,
)


class HandleStore(Protocol):
    """Durable storage for provider handles. Implemented upstream by U11c.

    A Protocol rather than a class because A does not implement this: domain
    records are written by the upstream API. `record` must not return until the
    handle is durable, and its returned `confirmed_at` is the acknowledgement
    `HandleRecord` requires — so a store that returns eagerly is refusing to make
    the claim rather than quietly making a false one.
    """

    def record(self, handle: ProviderHandle) -> datetime:
        """Persist `handle` durably and return when persistence acknowledged it."""
        ...

    def attach_provider_reference(self, handle: ProviderHandle, reference: str) -> None:
        """Record the provider's own identifier against an existing handle."""
        ...


class OperationAuthority(Protocol):
    """B's authority for an active operation. Mocked here; B owns the real one.

    A calls `authority_for` and acts only under what it returns. A does not mint,
    extend, cache or infer authority: an adapter that could would be an adapter
    that can act on an operation B has cancelled.
    """

    def authority_for(self, allocation_id: str) -> str | None:
        """Return B's authority token, or None when no operation is active."""
        ...


class ProviderClient(Protocol):
    """The provider operations A drives. Responses come from the provider.

    `observe` is the provider re-check R15 acceptance 1 turns on. It is a separate
    method from the operation calls precisely because it must be a fresh query to
    the provider rather than a cached result of an earlier call.
    """

    def invoke(self, handle: ProviderHandle) -> tuple[CallOutcome, str | None]:
        """Perform the operation. Returns the outcome and the provider's reference."""
        ...

    def observe(self, handle: ProviderHandle) -> ProviderObservation:
        """Re-check the provider for the resource this handle identifies."""
        ...

    def observe_allocation(self, allocation_id: str) -> dict[str, ProviderObservation]:
        """Re-check every resource of an allocation: compute, storage and network."""
        ...


@dataclass(frozen=True)
class OperationResult:
    """What happened, what established it, and whether a repeat is authorized.

    `decision` is `None` only when the handle was never durably recorded, i.e. the
    provider was never called — there is no outcome to reconcile because no
    operation was made.
    """

    record: HandleRecord
    call: CallDecision
    outcome: CallOutcome | None = None
    decision: ReconcileDecision | None = None

    @property
    def may_repeat_operation(self) -> bool:
        """True only when a provider observation established absence.

        Deliberately False when the call was refused for want of a durable
        handle: that refusal means nothing ran, but it also means nothing was
        recorded, so the safe action is to fix the recording and start over rather
        than to proceed with an unrecordable operation.
        """
        return self.decision is not None and self.decision.may_repeat_operation

    @property
    def resources_possibly_created(self) -> bool:
        """True while a resource may exist that this adapter cannot account for.

        The question `onboarder.go`'s timeout branch cannot answer. It is False
        when the call never happened and False when the provider established
        absence; True whenever the outcome remains unresolved.
        """
        return self.decision is not None and self.decision.resources_unresolved


class ProviderAdapter:
    """Adapter-side bookkeeping and provider-truth reporting under B's authority.

    Holds no operation state between calls: every method takes what it needs and
    returns a value. That is what keeps this a bookkeeping path rather than a
    lifecycle — there is nothing here for a second owner to own.
    """

    def __init__(
        self,
        store: HandleStore,
        provider: ProviderClient,
        authority: OperationAuthority,
        provider_name: str,
    ) -> None:
        if not provider_name.strip():
            raise ContractViolation("provider_name must be a non-empty string")
        self._store = store
        self._provider = provider
        self._authority = authority
        self._provider_name = provider_name

    def _record_handle(self, handle: ProviderHandle) -> HandleRecord:
        """Persist the handle, tolerating a store that fails to acknowledge.

        A store failure produces a non-durable record rather than an exception,
        because the caller's next step must be a refusal to call the provider —
        which is a decision the caller should see, not an error it might catch and
        proceed past.
        """
        try:
            confirmed_at = self._store.record(handle)
        except Exception:
            return HandleRecord(handle=handle, durable=False)
        if confirmed_at is None:
            return HandleRecord(handle=handle, durable=False)
        return HandleRecord(handle=handle, durable=True, confirmed_at=confirmed_at)

    def perform_operation(
        self,
        operation: OperationKind,
        resource_name: str,
        idempotency_key: str,
        allocation_id: str,
        workspace: str,
    ) -> OperationResult:
        """Record the handle, then call the provider, then establish the outcome.

        Returns rather than retries. When `may_repeat_operation` is True the
        caller may repeat the operation; when `resources_possibly_created` is True
        it must not, because a resource it cannot see may exist.
        """
        authority = self._authority.authority_for(allocation_id)
        if not authority:
            # No active operation means no authority to act under. Refusing here
            # rather than at the provider call keeps A from recording a handle for
            # an operation B never authorized.
            return OperationResult(
                record=HandleRecord(
                    handle=ProviderHandle(
                        operation=operation,
                        provider=self._provider_name,
                        resource_name=resource_name,
                        idempotency_key=idempotency_key,
                        allocation_id=allocation_id,
                        workspace=workspace,
                    ),
                    durable=False,
                ),
                call=CallDecision(
                    permitted=False,
                    reason="no active operation authority from B for this allocation",
                ),
            )

        handle = ProviderHandle(
            operation=operation,
            provider=self._provider_name,
            resource_name=resource_name,
            idempotency_key=idempotency_key,
            allocation_id=allocation_id,
            workspace=workspace,
        )

        # Step 1 — durability BEFORE the call. This is the ordering the whole
        # unit exists for: after this point a crash is reconcilable.
        record = self._record_handle(handle)
        call = authorize_provider_call(record)
        if not call.permitted:
            return OperationResult(record=record, call=call)

        # Step 2 — the call whose response can be lost.
        try:
            outcome, reference = self._provider.invoke(handle)
        except TimeoutError:
            # The case the upstream code classifies as failure. A timeout is the
            # absence of an answer, so it becomes AMBIGUOUS and goes to the
            # re-check rather than to a replacement launch.
            outcome, reference = CallOutcome.AMBIGUOUS, None
        except Exception:
            outcome, reference = CallOutcome.AMBIGUOUS, None

        if reference:
            record = HandleRecord(
                handle=handle.with_provider_reference(reference),
                durable=record.durable,
                confirmed_at=record.confirmed_at,
            )
            try:
                self._store.attach_provider_reference(handle, reference)
            except Exception:
                # The pre-call record is already durable, so the resource stays
                # findable by name and idempotency key. Losing the provider's own
                # identifier degrades reconciliation; it does not break it.
                pass

        # Step 3 — establish what actually happened.
        decision = self._reconcile_outcome(record, outcome, authority)
        return OperationResult(
            record=record, call=call, outcome=outcome, decision=decision
        )

    def _reconcile_outcome(
        self, record: HandleRecord, outcome: CallOutcome, authority: str
    ) -> ReconcileDecision:
        """Resolve an outcome, re-checking the provider when it was ambiguous."""
        request = ReconcileRequest(
            handle=record.handle, outcome=outcome, operation_authority=authority
        )
        if outcome is not CallOutcome.AMBIGUOUS:
            return reconcile(request, None)

        try:
            observation = self._provider.observe(record.handle)
        except Exception as exc:
            # A re-check that itself failed establishes nothing. Passing None
            # yields UNRESOLVED, which authorizes no repeat — the conservative
            # direction, and the one a caller cannot mistake for absence.
            observation = ProviderObservation(
                presence=ProviderPresence.UNKNOWN,
                queried_by=record.handle.resource_name,
                detail=f"provider re-check failed: {type(exc).__name__}",
            )
        return reconcile(request, observation)

    def release_allocation(
        self,
        allocation_id: str,
        intent: ReleaseIntent,
        *,
        allocation_resources: AllocationResources,
    ) -> TeardownReport:
        """Re-check the provider, then report the truth with a non-zero result on failure.

        This is the path B's independent-lifetime driver invokes, which is how
        R15 acceptance 8 is met without A owning a lifetime. A credential failure
        during the re-check becomes an `UNKNOWN` observation and therefore an
        unresolved report — never a claim that cleanup succeeded.
        """
        if (
            not isinstance(allocation_resources, AllocationResources)
            or allocation_resources.allocation_id != allocation_id
        ):
            raise ContractViolation(
                "release inventory does not belong to this allocation"
            )
        if not self._authority.authority_for(allocation_id):
            raise ContractViolation(
                "release reporting requires B's active operation authority"
            )
        credential_failure = False
        try:
            observations = self._provider.observe_allocation(allocation_id)
        except PermissionError:
            credential_failure = True
            observations = {
                name: ProviderObservation(
                    presence=ProviderPresence.UNKNOWN,
                    queried_by=name,
                    detail="credential failure during provider re-check",
                )
                for name in allocation_resources.resource_ids
            }
        except Exception as exc:
            observations = {
                name: ProviderObservation(
                    presence=ProviderPresence.UNKNOWN,
                    queried_by=name,
                    detail=f"provider re-check failed: {type(exc).__name__}",
                )
                for name in allocation_resources.resource_ids
            }

        assessment: ReleaseAssessment = assess_release(
            observations, allocation=allocation_resources
        )
        findings = tuple(
            Finding(
                resource=name,
                detail=(
                    "provider omitted an expected allocation resource"
                    if name not in observations
                    else "provider reported a resource outside the allocation inventory"
                    if name not in allocation_resources.resource_ids
                    else "provider observation identified a different resource"
                    if observations[name].queried_by != name
                    else observations[name].detail
                    or f"provider reports {observations[name].presence.value}"
                ),
            )
            for name in assessment.unresolved_resources
        )
        return TeardownReport(
            allocation_id=allocation_id,
            assessment=assessment,
            intent=intent,
            findings=findings,
            credential_failure=credential_failure,
        )

    def reconcile_recorded_handle(
        self, record: HandleRecord, allocation_id: str
    ) -> ReconcileDecision:
        """Resolve a handle found in storage after a crash.

        The recovery entry point: B's recovery worker (B's, not built here) finds
        a durable handle whose operation never reported an outcome and calls this.
        The outcome is `AMBIGUOUS` by construction — that is exactly what an
        unreported operation is — so this always re-checks the provider.
        """
        if (
            record.handle.allocation_id != allocation_id
            or record.handle.provider != self._provider_name
        ):
            raise ContractViolation(
                "recorded handle does not belong to this allocation/provider"
            )
        authority = self._authority.authority_for(allocation_id)
        if not authority:
            raise ContractViolation(
                "reconciliation requires B's authority for an active operation"
            )
        if not record.durable:
            # A non-durable record cannot have been read back from storage. This
            # would mean the caller assembled one locally and is asking A to treat
            # an unrecorded operation as recoverable.
            raise ContractViolation(
                "only a durably recorded handle can be reconciled after a crash"
            )
        return self._reconcile_outcome(record, CallOutcome.AMBIGUOUS, authority)


__all__ = [
    "HandleStore",
    "OperationAuthority",
    "OperationResult",
    "ProviderAdapter",
    "ProviderClient",
    "ReconcileResult",
]
