"""Durable provider handles — an operation cannot be made before it is findable.

Issue #5049 (U11), EPIC #4910. R15 acceptance 5.

## The failure this prevents, from the pinned snapshot

`Onboarder.Onboard` (`provisioner/onboarder.go`) launches a cluster like this:

```go
reqID, err := o.skyClient.Launch(ctx, launchReq)   // :179
if err != nil {
    return &OnboardResult{
        Success:     false,
        ClusterName: clusterName,
        Error:       fmt.Sprintf("SkyPilot launch failed: %v", err),
        // no RequestID — there is none to report
    }, nil                                          // :180-186
}
```

The launch identifier exists **only on the success path**. When the call times
out, `err` is non-nil, `reqID` is the zero string, and the returned result
carries no provider reference at all. `Provisioner.provisionNode` then logs
"onboarding failed, trying next option" and moves to the next cloud
(`controllers/provisioner.go:265-272`).

A timeout is not evidence that nothing was created. So the sequence is: ask a
provider for a machine, lose the response, conclude failure, ask a *different*
provider for a machine — and the first one, if it came up, is running and billing
with nothing in the system holding a reference to it. Nothing reports that as an
error, because from the controller's point of view the launch failed.

## The rule, and why it is an ordering rule rather than a validation rule

The identity has to exist **before** the call, because the crash this must
survive can happen during the call. Recording the handle afterwards leaves the
exact window the bug lives in: a resource created by a call whose response never
came back, and no record that the call was ever made.

The upstream code already chooses `clusterName` before building the request
(`onboarder.go:170`), so a reconcilable identity is available before the launch —
it is simply not written down anywhere durable until the call returns. That is
the gap this closes.

## Why durability is asserted by the caller rather than checked here

A is not the persistence layer. Domain records are written by the upstream API
(U11c), so this package cannot verify that a record reached storage — it can only
refuse to authorize the provider call until something that *does* know says so.
That is what `HandleRecord.durable` is: the persistence layer's acknowledgement,
supplied to `authorize_provider_call`, which refuses without it.

An in-process flag would satisfy the type and none of the requirement. So
`durable=True` additionally requires `confirmed_at` — the instant persistence
acknowledged — which a caller cannot produce by simply setting a boolean, and
which lands in the provider-truth report as the evidence for the claim.

## What is deliberately absent

**No retry counter, no backoff state, no attempt sequencing.** An `attempt_id`
and the fencing around it are B's; no lease, fencing or attempt implementation
exists in ADP today (see `reconciliation.py` on the mocked boundary). A handle
that carried its own attempt counter would be the first half of a second retry
authority living in the adapter, and the operation lifecycle is not A's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .health import ContractViolation


class OperationKind(str, Enum):
    """The class of provider operation a handle identifies.

    `str`-valued for the same reason as `CheckStatus`: the wire form is the
    member value, so a receiver in another language compares a stable string
    rather than an ordinal that moves when a member is inserted.
    """

    # Capacity is being created — the case where a lost response costs money.
    PROVISION = "provision"

    # Work is being submitted to existing capacity.
    SUBMIT = "submit"

    # Capacity is being released. Ambiguity here is not free either: a lost
    # response read as failure leads to a second release attempt, and read as
    # success leads to a resource nobody is looking for.
    RELEASE = "release"


@dataclass(frozen=True)
class ProviderHandle:
    """The identity of a provider operation, chosen before the operation runs.

    Two identifiers, because they answer different questions:

    * `resource_name` is what the caller asked the provider to call the thing.
      It is chosen locally and is therefore available before the call. It is what
      a later re-check queries by (the upstream client's `Status(clusterName)`).
    * `idempotency_key` is what makes a repeat of the *same* operation
      distinguishable from a new one, so a reconciliation can tell "my earlier
      attempt" from "someone else's resource with a similar name".

    `provider_reference` is the identifier the provider returns — SkyPilot's
    `request_id`, EC2's instance id. It is `None` on this type by design: it does
    not exist yet at the moment the handle must be recorded, and a field that
    could only be filled in after the call would put the whole record after the
    call.
    """

    operation: OperationKind
    provider: str
    resource_name: str
    idempotency_key: str
    allocation_id: str
    workspace: str
    provider_reference: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "provider",
            "resource_name",
            "idempotency_key",
            "allocation_id",
            "workspace",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"{field_name} must be a non-empty string")
        if self.provider_reference is not None and not self.provider_reference.strip():
            # Blank is worse than absent: absent says "the provider has not
            # answered yet", blank reads as an answer that carries no identifier.
            raise ContractViolation(
                "provider_reference must be a non-empty string when present"
            )

    def with_provider_reference(self, reference: str) -> ProviderHandle:
        """Return a copy carrying the identifier the provider returned.

        A new value rather than a mutation, so the recorded pre-call handle stays
        exactly as it was written. The pre-call form is the one a reconciliation
        after a crash will find, and it must not be retroactively edited into
        something that looks like it always had the provider's answer.
        """
        if self.provider_reference is not None and self.provider_reference != reference:
            # Two different references for one operation means either the
            # idempotency key was reused across operations or two attempts ran.
            # Either way the adapter cannot decide which resource it owns.
            raise ContractViolation(
                "provider_reference already recorded with a different value"
            )
        return ProviderHandle(
            operation=self.operation,
            provider=self.provider,
            resource_name=self.resource_name,
            idempotency_key=self.idempotency_key,
            allocation_id=self.allocation_id,
            workspace=self.workspace,
            provider_reference=reference,
        )


@dataclass(frozen=True)
class HandleRecord:
    """A handle plus whether persistence has acknowledged storing it.

    `durable` is not A's own opinion. It is the upstream API's acknowledgement
    (U11c), passed in, and `confirmed_at` is when that acknowledgement arrived.
    Requiring the timestamp alongside the flag is what stops `durable=True` from
    being a boolean a caller can assert its way past: the constructor demands the
    instant, and the instant is what the provider-truth report cites.
    """

    handle: ProviderHandle
    durable: bool
    confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.durable:
            if self.confirmed_at is None:
                raise ContractViolation(
                    "a durable handle record requires confirmed_at — the instant "
                    "persistence acknowledged the record"
                )
            if self.confirmed_at.tzinfo is None:
                raise ContractViolation("confirmed_at must be timezone-aware")
        elif self.confirmed_at is not None:
            # A confirmation time on a record that is not durable asserts two
            # contradictory things about the same write.
            raise ContractViolation(
                "confirmed_at must be absent when the record is not durable"
            )


@dataclass(frozen=True)
class CallDecision:
    """Whether the provider call may be made, and why not when it may not.

    `reason` names the missing precondition and nothing else. It does not echo
    the handle back, so a refusal cannot be used to read recorded state.
    """

    permitted: bool
    reason: str = ""


def authorize_provider_call(record: HandleRecord) -> CallDecision:
    """Refuse the provider call until the handle is durably recorded.

    This is R15 acceptance 5 stated as a gate rather than as a convention: the
    call the adapter is about to make is the call whose response can be lost, so
    the record has to already be findable when it is made. Refusing here is the
    only point at which "recorded before the call counts as made" is enforceable
    — afterwards, the operation has already happened.

    Fail-closed: a record that is not durable produces no permission, and there
    is no branch that permits the call because persistence was merely attempted.
    """
    if not record.durable:
        return CallDecision(
            permitted=False,
            reason="handle is not durably recorded; a lost response would leave "
            "no reference to reconcile against",
        )
    return CallDecision(permitted=True)


class CallOutcome(str, Enum):
    """How a provider call ended, with ambiguity as its own outcome.

    The three-way split is the point. Upstream has two: `err == nil` and
    everything else, which is what makes a timeout indistinguishable from a
    refusal. `AMBIGUOUS` exists so the difference survives into the code that
    decides what to do next.
    """

    # The provider answered, and the answer was success.
    SUCCEEDED = "succeeded"

    # The provider answered, and the answer was a refusal. The operation did not
    # run — the provider said so.
    FAILED = "failed"

    # No answer arrived: timeout, dropped connection, lost response, or the
    # caller crashed between the call and reading the reply. The operation may
    # have fully succeeded. This is an unknown, not a negative.
    AMBIGUOUS = "ambiguous"
