"""An ambiguous outcome is reconciled against the recorded handle, never retried blind.

Issue #5049 (U11), EPIC #4910. R15 acceptance 6.

## The failure this prevents

`Provisioner.provisionNode` iterates cloud options and, on a failed onboard, does
this (`controllers/provisioner.go:265-272`):

```go
// Onboarding failed - log and try next cloud.
logger.Info("onboarding failed, trying next option", ...)
if _, termErr := option.Adapter.TerminateNode(ctx, clusterName); termErr != nil {
    logger.Error(termErr, "failed to terminate failed cluster", ...)
}
```

The termination attempt is real, and it is why this is subtle rather than
obvious: upstream *does* try to clean up. But the attempt is fire-and-forget —
its error is logged and the loop proceeds to the next cloud regardless — and the
`clusterName` it terminates is only meaningful if the launch got far enough to
use it. Behind both is the classification error: `onboarder.go`'s timeout branch
returns `Success: false`, so a lost response arrives here as a **failure**, and
the next iteration launches on another provider. If the first launch did come up,
its cost is now invisible.

So the rule is not "retry more carefully". It is that the outcome must be
*established* before anything decides what to do next, and the only thing that
can establish it is the provider, queried by the identity recorded before the
call.

## Why this cannot be answered from an internal status field

R15 acceptance 1 says a re-check "against the provider (not an internal status
field)", and the same reasoning applies to acceptance 6. The internal state after
a lost response says "failed", because that is what the code wrote when the call
returned an error. Consulting it returns the wrong answer with full confidence.
`reconcile` therefore takes a `ProviderObservation` — what a query to the
provider actually returned — and has no path that reaches a conclusion without
one.

## The four-way result, and the one that is usually collapsed

`RECONCILED_EXISTS` and `RECONCILED_ABSENT` are the two everyone models. The
other two are the ones that matter:

* `RETRY_PERMITTED` is issued **only** after a provider observation established
  absence. It is a different fact from "the call failed" and is the only route to
  a repeat of the operation.
* `UNRESOLVED` is issued when the provider could not be reached to answer. It
  permits no retry and, critically, permits no release either — an unreachable
  provider is the case where both "give up" and "try again" are unsafe, and it is
  exactly the case a two-way split has nowhere to put.

## What is mocked, and recorded as a mock

B owns the operation lifecycle, cancellation ordering, leases/fencing and the
recovery worker. **No lease, fencing or `attempt_id` implementation exists in ADP
today**, so this module models B's boundary as an input it will not cross:

* `ReconcileRequest.operation_authority` is B's authority for an active
  operation, supplied by B. A does not mint it, does not extend it and refuses to
  act without it (`superplane_contracts.leases` from U8 is the lease *shape*; the
  driver that grants one is B's and is not built here).
* There is no scheduler, timer or queue in this package. R15 acceptance 8 —
  stops and cleanup working after the agent process is gone — is met by B's
  independent-lifetime driver invoking `adapter.release_allocation`, not by A
  acquiring a lifecycle of its own. A second lifecycle owner is precisely what
  the boundary forbids.

Tests exercise these through a mock authority and record it as a mock. That is
legitimate for an unbuilt dependency; the live criteria stay deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .handles import CallOutcome, ProviderHandle
from .health import ContractViolation


class ProviderPresence(str, Enum):
    """What a query to the provider established about a resource.

    `UNKNOWN` is not a synonym for `ABSENT`. It is the answer when the provider
    itself could not be consulted — an API error, an expired credential, a
    network partition. Collapsing it into `ABSENT` is how "we could not check"
    becomes "there is nothing there", which is the same substitution that makes
    the upstream vault-sync check report `synced` when listing secrets failed
    (see `health.py`).
    """

    # The provider confirmed the resource exists. It may still be starting.
    PRESENT = "present"

    # The provider confirmed the resource does not exist.
    ABSENT = "absent"

    # The provider could not be consulted. Establishes nothing either way.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderObservation:
    """What the provider said when queried by a recorded handle.

    This is the provider-truth primitive the whole module turns on: it is
    constructed from a provider response, and every conclusion below is derived
    from one of these rather than from local state.

    `queried_by` records which identifier the query used. A query by the wrong
    identity can be answered confidently and still be about the wrong resource,
    so the report says what was asked, not only what came back.
    """

    presence: ProviderPresence
    queried_by: str
    provider_state: str | None = None
    detail: str = ""
    observation_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.observation_id, str) or (
            self.observation_id
            and (
                len(self.observation_id) != 32
                or any(c not in "0123456789abcdef" for c in self.observation_id)
            )
        ):
            raise ContractViolation(
                "observation_id must be empty or a trusted query nonce"
            )
        if not isinstance(self.presence, ProviderPresence):
            raise ContractViolation("presence must be a ProviderPresence")
        if not isinstance(self.queried_by, str) or not self.queried_by.strip():
            raise ContractViolation("queried_by must be a non-empty string")
        if self.presence is ProviderPresence.PRESENT and not self.provider_state:
            # "It exists" with no state is not an observation of the provider; it
            # is an assertion. The provider's own status string is the evidence.
            raise ContractViolation(
                "a PRESENT observation must carry the provider's reported state"
            )
        if self.presence is ProviderPresence.UNKNOWN:
            if self.provider_state:
                raise ContractViolation(
                    "an UNKNOWN observation cannot carry a provider state — "
                    "the provider was not successfully consulted"
                )
            if not self.detail.strip():
                # An unresolved observation blocks both retry and release, so why
                # it could not be answered is the operator's whole starting point.
                raise ContractViolation(
                    "an UNKNOWN observation must say why the provider could not "
                    "be consulted"
                )


class ReconcileResult(str, Enum):
    """The conclusion of reconciling an ambiguous outcome."""

    # The provider holds the resource. The operation ran. Adopt the handle;
    # launching a replacement here is the duplicate-spend bug.
    RECONCILED_EXISTS = "reconciled_exists"

    # The provider confirmed absence. The operation left nothing behind.
    RECONCILED_ABSENT = "reconciled_absent"

    # Absence was established, so repeating the operation is now safe. This is
    # the ONLY value that authorizes a repeat.
    RETRY_PERMITTED = "retry_permitted"

    # The provider could not be consulted. No retry, no release, and the
    # allocation stays on the books as unresolved.
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ReconcileRequest:
    """An ambiguous operation, its recorded handle, and B's authority to act.

    `operation_authority` is B's token for an active operation. A holds no
    authority of its own: it acts only under B's execution authority, so a blank
    authority is refused rather than defaulted to "the caller is presumably
    allowed". The lifecycle that issues it is B's and is mocked here.
    """

    handle: ProviderHandle
    outcome: CallOutcome
    operation_authority: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_authority, str)
            or not self.operation_authority.strip()
        ):
            raise ContractViolation(
                "operation_authority must be a non-empty string — A acts only "
                "under B's execution authority for an active operation"
            )


@dataclass(frozen=True)
class ReconcileDecision:
    """The conclusion, plus what established it.

    `observation` is the provider evidence the conclusion rests on. It is `None`
    only for the `SUCCEEDED`/`FAILED` short-circuits, where the provider already
    answered on the call itself. Every conclusion drawn from an ambiguous outcome
    carries the observation that produced it, so a report can be audited back to
    a provider response rather than to a local status field.
    """

    result: ReconcileResult
    handle: ProviderHandle
    observation: ProviderObservation | None = None
    reason: str = ""

    @property
    def may_repeat_operation(self) -> bool:
        """True only when provider-established absence authorizes a repeat.

        The single place the "may I launch again?" question is answered. A caller
        that asks this instead of testing `outcome != SUCCEEDED` cannot reproduce
        the blind-retry bug, because no unresolved or existing-resource path
        reaches `RETRY_PERMITTED`.
        """
        return self.result is ReconcileResult.RETRY_PERMITTED

    @property
    def resources_unresolved(self) -> bool:
        """True while the provider's answer leaves resources unaccounted for.

        Both `UNRESOLVED` and `RECONCILED_EXISTS` are unresolved for accounting
        purposes: in the first the provider could not be consulted, and in the
        second it confirmed a resource is still there. The adapter exposes this
        flag; `accounting.py` independently assesses allocation observations before permitting release (R15 acceptance 7).
        """
        return self.result in (
            ReconcileResult.UNRESOLVED,
            ReconcileResult.RECONCILED_EXISTS,
        )


def reconcile(
    request: ReconcileRequest, observation: ProviderObservation | None
) -> ReconcileDecision:
    """Decide what an ambiguous provider outcome actually was.

    Requires a provider observation for the ambiguous case and refuses to reach
    any conclusion without one. That refusal is the acceptance criterion: a
    caller with no provider answer gets `UNRESOLVED`, which authorizes nothing,
    rather than a default that authorizes the replacement launch.

    The unambiguous outcomes short-circuit: the provider already answered on the
    call, so re-querying would add nothing. Only `AMBIGUOUS` needs the re-check.
    """
    handle = request.handle
    if observation is not None and observation.queried_by not in {
        handle.resource_name,
        handle.idempotency_key,
        handle.provider_reference,
    }:
        return ReconcileDecision(
            result=ReconcileResult.UNRESOLVED,
            handle=handle,
            reason="provider observation does not identify the recorded operation",
        )

    if request.outcome is CallOutcome.SUCCEEDED:
        return ReconcileDecision(
            result=ReconcileResult.RECONCILED_EXISTS,
            handle=handle,
            observation=observation,
            reason="the provider confirmed the operation on the call itself",
        )

    if request.outcome is CallOutcome.FAILED:
        # A refusal is the provider's own answer that the operation did not run,
        # so a repeat is permitted without a re-check. This is the one path where
        # upstream's classification is right — and the reason the timeout branch
        # sharing it is the whole defect.
        return ReconcileDecision(
            result=ReconcileResult.RETRY_PERMITTED,
            handle=handle,
            observation=observation,
            reason="the provider refused the operation; it did not run",
        )

    if observation is None:
        return ReconcileDecision(
            result=ReconcileResult.UNRESOLVED,
            handle=handle,
            reason="ambiguous outcome with no provider observation; a repeat "
            "here would risk duplicating a resource that may exist",
        )

    if observation.presence is ProviderPresence.PRESENT:
        return ReconcileDecision(
            result=ReconcileResult.RECONCILED_EXISTS,
            handle=handle,
            observation=observation,
            reason=f"the provider holds the resource in state "
            f"{observation.provider_state!r}; the lost response was a success",
        )

    if observation.presence is ProviderPresence.ABSENT:
        # Absence established by the provider is what makes a repeat safe. For a
        # release operation the same observation means the release completed, so
        # the caller reads `RETRY_PERMITTED` as "nothing is there" — which is
        # what `accounting.py` needs and what a re-check must show.
        return ReconcileDecision(
            result=ReconcileResult.RETRY_PERMITTED,
            handle=handle,
            observation=observation,
            reason="the provider confirmed no resource for this handle; "
            "repeating the operation cannot duplicate one",
        )

    return ReconcileDecision(
        result=ReconcileResult.UNRESOLVED,
        handle=handle,
        observation=observation,
        reason=f"the provider could not be consulted ({observation.detail}); "
        "the allocation stays unresolved and is not erased",
    )
