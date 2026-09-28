"""The provisioning provider adapter, reachable only through B's facade.

Issue #5052 (U17a), EPIC #4910. R14, ADP half, acceptance 2.

## The one design decision worth reading

The adapter has **no public method that provisions**. ``ProvisioningAdapter.run``
requires an ``OperationBinding``, and the only thing that produces a real binding
is B's facade. So "invoked directly instead of through the facade" is not a policy
the adapter enforces at run time and hopes callers respect — there is no entry
point that omits the binding.

That matters because the alternative shape is the common one and it fails
quietly: an adapter with ``provision(workspace_id, params)`` plus a separate
``authorize()`` the caller is expected to have called first. Every such pair
eventually ships a call site that forgot the second half, and the failure is
invisible because provisioning still works — it just works without an operation
record, so there is nothing to cancel, observe or bound.

## Where the facade ends and the provider begins

Two collaborators, injected, and the separation is deliberate:

* ``facade`` — B's scoped trusted-operation contract. Issues the binding, reports
  progress, accepts cancellation. **Does not exist in ADP today**; the tests pass
  a mock and record it as one.
* ``provider`` — the thing that actually creates infrastructure. Also injected,
  because this unit's job is the *authority* path; a real provider client is the
  consuming lane's (U2 builds, U3 rolls out).

The adapter never asks the provider what happened. It asks the **facade**. The
provider's return value is deliberately discarded for the purpose of reporting
outcome — see ``_report`` below. This is the "progress read from an internal status
field" failure inverted: even the provider's own claim is not the operation's
outcome, because the operation is the facade's record and the facade is what a
consumer can observe.

## What the adapter must never do, stated as code

* Read a raw secret value, or call a vault credential-**management** endpoint
  (``POST``/``DELETE /auth/credentials``). Those administer a *user's* stored
  credentials: no run binding, no expiry, no run-tied revocation. There is no HTTP
  client and no secrets client in this module at all, so the property holds by
  absence rather than by a check that could be removed.
* Mint a credential. A credential this side minted is one no revocation reaches.
* Write a domain record. Domain writes are the upstream API's
  (``repo-path-allocation.md``); this module has no database handle.
* Accept a caller-supplied identity. Enforced in ``_check_intent``, and the
  rejection stands **even when the smuggled value matches the bound principal** —
  see that method's comment for why the matching case is the important one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from .health import ContractViolation
from .provisioning import (
    REQUIRED_PERMISSION,
    OperationBinding,
    OperationState,
    ProvisioningIntent,
    ProvisioningProgress,
    forbidden_parameters,
)


class ProvisioningRefused(PermissionError):
    """The adapter refused to run. Carries a caller-safe reason.

    A ``PermissionError`` subclass, matching ``AuthorizationDeniedError`` in U9's
    policy, so a caller that treats authorization failures uniformly catches this
    too. Distinct from ``ContractViolation`` (a malformed shape) because a
    well-formed request the adapter is not willing to run is a different outcome
    from an unconstructable one.
    """


@runtime_checkable
class OperationFacade(Protocol):
    """B's scoped trusted-operation contract, as this adapter consumes it.

    A ``Protocol`` rather than a base class: B owns the implementation and it does
    not exist yet, so there is nothing here for an implementation to inherit from.
    Structural typing also means the mock in the tests satisfies exactly the
    surface the adapter uses and nothing more, so the mock cannot drift into
    providing conveniences a real facade would not.

    ``runtime_checkable`` so a caller can assert the shape at a boundary. Note
    that a runtime ``isinstance`` check against a Protocol verifies *method
    presence only*, not signatures — which is why the tests assert the double is a
    mock explicitly rather than inferring liveness from a passing isinstance.
    """

    def report_progress(self, operation_id: str) -> ProvisioningProgress:
        """The facade's current report for one operation.

        The **only** way this adapter or its caller learns an outcome.
        """
        ...

    def record_started(self, operation_id: str) -> None:
        """Tell the facade work has begun, so its record reflects reality."""
        ...

    def record_finished(self, operation_id: str, *, failed: bool) -> None:
        """Tell the facade work stopped, and whether the attempt raised.

        Deliberately **not** ``record_succeeded``. The adapter reports that its
        attempt ended and whether it threw; whether the *operation* succeeded is
        the facade's determination, resolved against the provider. An adapter that
        could declare success would be the "reports success while the provider has
        provisioned nothing" failure with extra steps.
        """
        ...


@runtime_checkable
class ProvisioningProvider(Protocol):
    """The thing that actually creates or destroys infrastructure."""

    def provision(
        self, parameters: dict[str, str], *, binding: OperationBinding
    ) -> None:
        """Create capacity for the bound workspace."""
        ...

    def teardown(
        self, parameters: dict[str, str], *, binding: OperationBinding
    ) -> None:
        """Release capacity for the bound workspace."""
        ...


@dataclass(frozen=True)
class ProvisioningAdapter:
    """Runs provisioning under an authorized operation, and only under one.

    Frozen: the adapter holds no mutable state, and specifically no status field.
    That is not tidiness — a mutable ``self.state`` is exactly what a caller or a
    test would read instead of asking the facade, and the story names reading an
    adapter-internal status field as a failure mode. There is nothing to read.

    ``clock`` is injected for the same reason U8's contracts take a receiver
    ``now``: expiry is decided by a clock the adapter does not own, and every
    branch stays testable without patching time.
    """

    facade: OperationFacade
    provider: ProvisioningProvider
    clock: Callable[[], datetime]

    def run(
        self, binding: OperationBinding, intent: ProvisioningIntent
    ) -> ProvisioningProgress:
        """Perform a provisioning operation under ``binding``, or refuse.

        Returns the facade's progress report — never a value this adapter composed
        about its own success. The return type is the observation channel, so a
        caller cannot end up with an outcome that did not come through the facade.

        Order matters and is the same discipline as ``authorize_request`` in U9's
        policy: every authority check runs before the provider is touched, so a
        refusal cannot happen after infrastructure was already created.
        """
        self._check_binding(binding)
        self._check_intent(binding, intent)

        if binding.action != intent.action:
            # The caller asked for something other than what was authorized. A
            # teardown running under a binding authorized for provision (or the
            # reverse) is a destructive mismatch, and refusing is the only safe
            # resolution: honouring the binding would silently do something the
            # caller did not ask for, and honouring the intent would perform an
            # unauthorized action.
            raise ProvisioningRefused(
                "intent action does not match the authorized operation"
            )

        return self._execute(binding, intent)

    # ------------------------------------------------------------------
    # Authority checks
    # ------------------------------------------------------------------

    def _check_binding(self, binding: OperationBinding) -> None:
        """Refuse anything that is not a currently-valid operation binding."""
        if binding is None:  # pragma: no cover - defensive; typing forbids it
            # Kept despite being unreachable through the annotated signature,
            # because "no binding" is THE failure this adapter exists to prevent
            # and Python annotations do not enforce themselves. A caller passing
            # None from untyped code must be refused, not crash with an
            # AttributeError that a broad `except` upstream could swallow into a
            # retry.
            raise ProvisioningRefused(
                "provisioning requires an authorized operation binding"
            )
        if not isinstance(binding, OperationBinding):
            # A duck-typed stand-in carrying the right attribute names is not a
            # binding. This refuses the shape a caller would reach for to skip the
            # facade: a small local object with an operation_id and a principal.
            raise ProvisioningRefused(
                "operation binding must be an OperationBinding issued by the facade"
            )
        if binding.permission != REQUIRED_PERMISSION:
            # Unreachable via the constructor, which refuses the same thing.
            # Re-checked because this function's contract is "the binding
            # authorizes provisioning", and it should not depend on a validation
            # rule in another module staying where it is — the same reasoning
            # `authorize_submit` gives for its second workspace check.
            raise ProvisioningRefused(
                "operation binding does not authorize provisioning"
            )

        now = self.clock()
        if binding.is_expired(now):
            raise ProvisioningRefused("operation authorization has expired")

    def _check_intent(
        self, binding: OperationBinding, intent: ProvisioningIntent
    ) -> None:
        """Refuse an intent that asserts an identity.

        The important case is the one that looks harmless: a parameter map
        carrying an ``org_id`` that **matches** the bound principal's org. It is
        still refused, and the reason is worth stating because "it matched, so no
        harm done" is the argument that removes this check.

        A caller-supplied identity that agrees with the binding today is a code
        path that reads the caller's value. Once that path exists, the only thing
        preventing a mismatched value from being honoured is that something else
        happens to compare them — and comparisons get reordered, cached, or made
        conditional. Refusing regardless means there is no path that reads
        caller-supplied identity at all, which is a property rather than a
        coincidence. Design §6 lines 398-407 forbid the field being authority; the
        way to guarantee that is for the field to be a refusal.
        """
        offending = forbidden_parameters(intent)
        if offending:
            raise ProvisioningRefused(
                "provisioning parameters may not assert an identity; "
                f"remove: {', '.join(sorted(offending))}. "
                "The principal is resolved from the operation binding."
            )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute(
        self, binding: OperationBinding, intent: ProvisioningIntent
    ) -> ProvisioningProgress:
        """Invoke the provider between the facade's start and finish records."""
        parameters = dict(intent.parameters)

        self.facade.record_started(binding.operation_id)
        failed = False
        try:
            if intent.action == "provision":
                self.provider.provision(parameters, binding=binding)
            else:
                self.provider.teardown(parameters, binding=binding)
        except Exception:
            # Marked failed and re-raised. Not swallowed: a caller must be able to
            # distinguish "the adapter could not run" from "the operation is in
            # some state", and the facade's record must reflect that the attempt
            # ended even though this raises.
            #
            # `record_finished` is called in the except branch AND after the try
            # body rather than in a `finally`, because a bare `finally` would also
            # fire on GeneratorExit/KeyboardInterrupt and report a finish the
            # facade should not record for an interrupted process.
            failed = True
            self.facade.record_finished(binding.operation_id, failed=True)
            raise
        if not failed:
            self.facade.record_finished(binding.operation_id, failed=False)

        return self._report(binding)

    def _report(self, binding: OperationBinding) -> ProvisioningProgress:
        """Ask the facade what happened. The provider's own claim is not consulted.

        Note the provider's return value is discarded in ``_execute`` — the calls
        are made for effect and the outcome is read here. A ``provision()`` that
        returned ``{"status": "ok"}`` would be the provider's assertion about
        itself, one indirection away from the adapter-internal status field the
        story forbids, and it would still be true if the provider created nothing.
        """
        progress = self.facade.report_progress(binding.operation_id)
        if not isinstance(progress, ProvisioningProgress):
            # A facade returning something else is a contract breach on B's side,
            # and it must surface here rather than being handed to a caller who
            # would read `.state` off an arbitrary object. Raised as a violation
            # rather than a refusal: nothing was denied, the report is malformed.
            raise ContractViolation(
                "facade progress report is not a ProvisioningProgress"
            )
        if progress.operation_id != binding.operation_id:
            # Progress for a different operation. The named-vs-positional rule the
            # HITL contract makes explicit: a report that does not name its
            # operation could be about any concurrent one, and accepting it here
            # would let one operation's success be read as another's.
            raise ContractViolation(
                "facade reported progress for a different operation"
            )
        return progress

    # ------------------------------------------------------------------
    # Cancellation and observation
    # ------------------------------------------------------------------

    def observe(self, binding: OperationBinding) -> ProvisioningProgress:
        """Read current progress for an operation, through the facade.

        Exposed so a caller polling for completion has a supported way to do it
        that is not "read a field on the adapter". Deliberately re-validates the
        binding: an expired authorization does not entitle a caller to keep
        observing, because progress for a tenant's operation is information about
        that tenant's estate — the same reasoning ``scoping.py`` gives for treating
        read authorization as a first-class check rather than the lesser half of
        write authorization.
        """
        self._check_binding(binding)
        return self._report(binding)


def summarize(progress: ProvisioningProgress) -> str:
    """A one-line, caller-safe description of an operation's reported state.

    Provided so a consumer logging progress does not hand-roll a string that
    conflates states — in particular, one that renders ``UNKNOWN`` as a failure.
    Reveals no parameters and no principal: an operation summary is likely to be
    logged, and a log line is the least controlled place a tenant identifier can
    end up.
    """
    if progress.state is OperationState.UNKNOWN:
        return (
            f"operation {progress.operation_id} outcome unresolved "
            f"(not a failure): {progress.detail or 'no detail supplied'}"
        )
    return f"operation {progress.operation_id} is {progress.state.value}"
