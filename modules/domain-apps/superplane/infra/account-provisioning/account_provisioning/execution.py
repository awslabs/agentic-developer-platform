"""The durable, fenced execution surface this package requires — Issue #5531 (w6-08).

The accepted design says an account-creation attempt must survive a crash: the intent is
committed before `CreateAccount` is called, the provider's request id is recorded, and the
outcome is reconciled before anyone may retry. That machinery already exists and is not
ours to rebuild — `modules/harness/jobs/` (#5527, w6-04) is the durable operation store,
and `OperationExecutor.execute_provider` is precisely "commit intent under a fence token,
invoke the hook exactly once, record the observation".

## Why this file declares a Protocol instead of importing that package

Two tests assert that `harness_jobs` is **not importable** from this tree:

* `src/superplane-api/tests/test_workspaces.py:908`
* `tests/test_provisioning_adapter.py:828`

Both exist deliberately, with docstrings explaining that composing the real facade is
#5535's (w6-12) work and that importability alone is not live evidence. Adding a hard
import here would fail both — and, worse, would quietly take this wave's composition
decision on #5535's behalf.

So the dependency is **structural**: this package is written against the surface below,
`harness_jobs.execution.OperationExecutor` satisfies it as-is (the names and keyword
arguments are copied from it, not invented), and the composer passes one in. This is the
same technique `superplane_contracts` uses for every port it does not own, and the same
reason `TrustedDeliveryChannel` is a Protocol: the implementation belongs to someone else.

## What this package relies on the real executor for, and therefore cannot check

Committing intent before the call, the fence token that stops a superseded worker from
acting, the advisory lock that serializes dispatch, and the `UNIQUE` constraint that makes
a duplicate refusal a property of PostgreSQL rather than of Python. Those are #5527's
guarantees, tested against a real database in its own lane. `runner.py`'s obligation is to
*use* them correctly: one call per key, a stable key, and never converting an unobtained
answer into a failure. That is what this package's tests establish, and they establish it
by driving the real entry points against a recording double of this surface.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol

__all__ = [
    "CallOutcome",
    "DurableExecutor",
    "OutcomeVocabulary",
    "ProviderCallRecord",
    "ReconciliationStore",
    "as_outcome",
    "outcome_vocabulary",
]


class CallOutcome(str, Enum):
    """What the provider said, including saying nothing.

    Values are byte-identical to `harness_jobs.execution.CallOutcome` because they are
    written to that package's database column and compared across the boundary. A copied
    vocabulary needs a drift test, which `tests/test_contract_agreement.py` provides.

    The distinction that matters most here is `FAILED` versus `UNKNOWN`:

    * `FAILED` is a provider answer establishing that **nothing was created**. An AWS
      validation error naming a duplicate email is this. A timeout is not.
    * `UNKNOWN` is the absence of an answer. An account may exist. Retrying on this is how
      one workspace ends up paying for two accounts, so it never authorizes a retry — a
      later reconciliation pass reads the stored request id instead.

    ## Why every comparison goes through `as_outcome`, and never through `is`

    This class is a *copy* of the authoritative one, for the reason the module docstring
    gives: importing `harness_jobs` here would fail two deliberate non-importability tests
    and would take #5535's composition decision on this wave's behalf. The unavoidable
    consequence is that the objects arriving on a durable record at runtime are members of
    the *harness* enum, not of this one.

    `is` therefore does not hold across the boundary. That is not a stylistic point: a real
    `SUCCEEDED` compared with `is` against this copy fails to match, falls through to the
    unresolved branch, and reports a successfully created account as `unknown` with its id
    discarded — an account that exists, is billing, and that no record names. Exactly the
    failure this story exists to prevent, produced by the comparison operator rather than
    by any provider behaviour.

    Equality *does* hold, because both are `(str, Enum)` over identical values. So the rule
    for this package is: **normalize with `as_outcome` at the boundary, then compare.**
    `tests/test_contract_agreement.py` drives every outcome path with the real harness enum
    to keep that true.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABSENT = "absent"
    UNKNOWN = "unknown"


def as_outcome(value: object) -> CallOutcome | None:
    """Normalize any spelling of an outcome onto this package's member. The only reader.

    Accepts a member of this enum, a member of the authoritative `harness_jobs` enum, the
    bare string stored in the database column, or `None` for "intent committed, not yet
    observed".

    An unrecognised value becomes `UNKNOWN` rather than `None` or an exception. That choice
    is deliberate and is the conservative one: `None` means "no call has been observed",
    which would invite a fresh attempt, and an unrecognised outcome is precisely a state in
    which this code has not established that no account exists. A value it cannot read must
    not be able to authorize a second billable account.
    """
    if value is None:
        return None
    if isinstance(value, CallOutcome):
        return value
    # A member of the harness enum, or any other `(str, Enum)` spelling of it, arrives as a
    # `str` subclass whose value is the shared wire spelling. Read by value, never identity.
    raw = getattr(value, "value", value)
    try:
        return CallOutcome(raw)
    except (ValueError, TypeError):
        return CallOutcome.UNKNOWN


class OutcomeVocabulary(Protocol):
    """The enum class the executor will `isinstance`-check a hook's answer against.

    Structurally satisfied by `harness_jobs.execution.CallOutcome` itself: a caller passes the
    class, not a member.
    """

    SUCCEEDED: object
    FAILED: object
    UNKNOWN: object


def outcome_vocabulary(vocabulary: OutcomeVocabulary | None) -> type[CallOutcome] | OutcomeVocabulary:
    """Resolve which `CallOutcome` class a provider hook must answer in. The OUTBOUND boundary.

    `as_outcome` normalizes outcomes coming IN, off a durable record. This is the mirror going
    OUT, and the mirror is load-bearing for a reason that is easy to miss: the real executor
    validates a hook's answer with

        if not isinstance(outcome, CallOutcome):
            raise ContractViolation("Provider hook must return CallOutcome")

    against **its own** class — and then catches every exception and substitutes
    `CallOutcome.UNKNOWN`. Two distinct enum classes never satisfy `isinstance` across the
    boundary, so a hook returning this package's copy has its answer discarded silently, with no
    log line and no error reaching the caller. Every settled call becomes `UNKNOWN`:

    * a created account is reported as "an account may exist", with its id discarded — the exact
      outcome this story exists to prevent, and the same one `test_contract_agreement.py`
      documents for the INBOUND `is` comparison, produced here by `isinstance` instead;
    * an authoritative AWS refusal (`FAILED`, nothing was created) is reported as "may have
      happened", so it retains budget and blocks the retry it was entitled to; and
    * the executor's own `UNKNOWN` branch marks the operation for cleanup and keeps the intent
      recoverable, so nothing surfaces as broken. It just never converges.

    The fix cannot be an import: two tests assert `harness_jobs` is not importable from this
    tree, and composing the real facade is #5535's (w6-12) decision. So the composer passes the
    class in, exactly as it passes the executor in, and the hook answers in the vocabulary the
    executor will check. `None` falls back to this package's copy, which is what the offline
    tests use and which is correct there because they drive the hook directly.
    """
    return vocabulary if vocabulary is not None else CallOutcome


class ProviderCallRecord(Protocol):
    """The durable record of one provider call, as this package reads it.

    Narrower than `harness_jobs`' `ProviderCall`: only the fields this package needs to
    make a decision are named, so the structural match stays easy to satisfy.
    """

    @property
    def operation_id(self) -> str:
        """Immutable operation identity recorded separately from the call key."""

    @property
    def idempotency_key(self) -> str:
        """The stable key the call was made under."""

    @property
    def provider_ref(self) -> str | None:
        """The provider's own identifier for the call.

        For account creation this is the `car-...` `CreateAccountRequestId`. It is the only
        thing that lets a later pass ask about *that* request rather than start a new one,
        which is why it is recorded even when the outcome is unknown.
        """

    @property
    def target(self) -> str:
        """What the call was recorded as acting on.

        Part of the store's **immutable binding**: `harness_jobs` refuses a re-record of the
        same key with a different `provider`, `operation_kind` or `target`, because a replay
        presenting different values is a misuse of the key rather than a duplicate delivery.

        `creation_runner` exploits that deliberately — it encodes a digest of the
        approval-bound payload here, so "the same operation came back asking for a different
        account" is refused by PostgreSQL rather than only by this package's own check.
        """

    @property
    def outcome(self) -> CallOutcome | None:
        """The recorded outcome, or `None` while intent is committed but unobserved."""


class DurableExecutor(Protocol):
    """The fenced, durable execution surface, as this package consumes it.

    Structurally satisfied by `harness_jobs.execution.OperationExecutor`; the keyword
    names below are that class's. A test double provides exactly this and no more.
    """

    @property
    def operation_id(self) -> str:
        """The operation every effect here is bound to."""

    @property
    def workspace_id(self) -> str:
        """The workspace the operation belongs to, from the lease — not a request body."""

    @property
    def org_id(self) -> str:
        """The organization the operation belongs to, from the lease."""

    async def provider_calls(self, *, provider: str, operation_kind: str) -> tuple[ProviderCallRecord, ...]:
        """Read only this admitted operation's durable provider history."""

    async def observe_success(self, *, idempotency_key: str, detail: str, provider_ref: str) -> object:
        """Persist authoritative successful read-back using the store's own vocabulary."""

    async def execute_provider(
        self,
        *,
        idempotency_key: str,
        provider: str,
        operation_kind: str,
        target: str,
    ) -> tuple[ProviderCallRecord, object]:
        """Commit intent, invoke the composed provider hook once, record what it said.

        The real implementation holds an advisory lock for the whole call, so a second
        concurrent dispatch for the same operation is refused rather than queued, and
        commits the intent row before the hook runs, so a crash leaves evidence.

        Returns the settled call record and a budget disposition this package does not
        interpret — the reservation ledger is the domain's, per #5527's ownership table.
        """

    async def record_intent(
        self,
        *,
        idempotency_key: str,
        provider: str,
        operation_kind: str,
        target: str,
    ) -> ProviderCallRecord:
        """Commit intent for a key without dispatching anything.

        Used by the reconciliation path to learn whether a key already has a record. The
        real implementation returns the EXISTING row when one is present rather than
        creating a second, which is what makes "has this been attempted?" answerable
        after a restart without risking a fresh effect.
        """


class ReconciliationStore(Protocol):
    """Where a reconciled outcome is WRITTEN. The half recovery was missing.

    `reconcile_creation` asks AWS what became of a recorded `CreateAccount` request. Reading
    the answer is only half of recovery: until the answer is on disk the durable row is still
    `intended`, so the next pass re-reads AWS, the operation never converges on the account
    id, and the row that would name a real billable account says only "a call was intended".
    An observation that is not persisted is not recovery — it is a recomputation that has to
    be redone after every restart, and one that is lost for good if the reader dies.

    Structurally satisfied by `harness_jobs.execution.reconcile`, wrapped by the composer so
    the connection stays on the trusted side: that function takes a `Connection` first, and
    this package must never hold one. `execution.py`'s module docstring gives the reason the
    dependency is structural rather than an import; the argument names below are that
    function's.

    ## Why the lease-free primitive is the right one

    `reconcile` deliberately takes no lease. The worker that made the call is, by
    construction, the one that is gone — requiring its lease would make the unrecoverable
    case exactly the case that cannot be recovered. It refuses a row that is already settled,
    including one already `unresolved`, because overwriting a human's unresolved row on a
    timer is a different act from settling an open one.

    That refusal is a normal outcome here, not an error: two recovery passes racing is
    expected, and the second one losing is the store doing its job. `reconcile_creation`
    reports what it read and says the row was already settled rather than raising.
    """

    async def reconcile(
        self,
        *,
        idempotency_key: str,
        outcome: object,
        detail: str | None = None,
        provider_ref: str | None = None,
    ) -> object:
        """Settle the row for `idempotency_key` with an outcome obtained from the provider.

        `outcome` must be a member of the vocabulary the STORE validates against — the
        harness enum, not this package's copy — for the reason `outcome_vocabulary`
        documents at length. The caller resolves it through that function.

        Raises the store's own refusal when the row is absent or already settled. The
        return value is the store's settled record and disposition, which this package does
        not interpret: the reservation ledger is the domain's, per #5527's ownership table.
        """
