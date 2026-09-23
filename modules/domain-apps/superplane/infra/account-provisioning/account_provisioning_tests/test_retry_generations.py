"""A retry arms a new fence before it dispatches — Issue #5531 (w6-08).

## The defect these tests exist to prevent

Narrowing the fence to one key per operation (see `test_payload_fence.py`) fixed the
edit-the-email duplicate and created a new problem at the other end.

`OperationExecutor.execute_provider` commits intent with `fresh=True`, so it REFUSES a key
that already carries a row — correctly, since repeating a call whose intent is already
recorded is how an effect happens twice. With exactly one key per operation, that makes the
first attempt the only attempt. Including after `CONCURRENT_ACCOUNT_MODIFICATION`, which is
AWS saying "another account operation is in flight for this organization, ask again". A
genuinely retryable failure would be unrecoverable except by opening a NEW operation — and a
new operation is a new approval with a fresh fence, which is the exact route that produces
the second billable account the story exists to prevent. Safety that forces the unsafe
workaround is not safety.

So a permitted retry gets its own key: `...:retry-1`, `...:retry-2`. The load-bearing
property is not that the key differs — a uuid would differ. It is that the key is
**derivable from committed rows and from nothing else**:

* Generation N+1 exists only because generation N's row settled as a failure that
  `CreateAccountFailure.retry_can_succeed` allows. Two workers reading the same history
  derive the SAME next key, so the store's `UNIQUE` constraint still decides the race.
* No clock, no counter in process memory, no attempt number from the caller. A restart
  mid-retry re-derives the same key and is refused as a duplicate rather than dispatching
  again.

## The sequence the supervisor asked to see, and where it is

`test_a_lost_retry_response_blocks_the_next_generation` is that exact sequence: confirmed
retryable failure → retry dispatched → the retry's answer is lost → a second retry is
refused. That third step is the one that matters, because the unresolved row sits under the
*retry's* key rather than the original's, so an implementation that only ever looked at
generation 0 would find a settled failure there, conclude a retry is permitted, and dispatch
a third `CreateAccount`.

## Why the refusals are asserted twice over

Two independent mechanisms have to agree before anything dispatches: `_plan_generation`
reasons about durable rows, and `account_factory.assess_attempt` reasons about attempts.
They are kept separate deliberately — a duplicate account requires both to be wrong at the
same time — so these tests assert on `executor.dispatches` being empty and not merely on an
exception type, since an exception raised after `CreateAccount` would still have left an
account behind.

No AWS is contacted anywhere in this file, and nothing here vends, or authorizes vending,
a real account — see `conftest.py`.
"""

from __future__ import annotations

import pytest
from harness_jobs.execution import CallOutcome as AuthoritativeCallOutcome

from account_factory.creation import CreateAccountFailure
from account_provisioning.creation_runner import (
    MAX_CREATION_GENERATIONS,
    AccountCreationError,
    CreationRefused,
    create_account,
    creation_generation_keys,
    creation_key,
    creation_target,
    encode_reference,
)

from .conftest import (
    FIXTURE_CREATED_ACCOUNT,
    FIXTURE_REQUEST_ID,
    RecordingCredentials,
    RecordingExecutor,
    RecordingOrganizations,
    SettledCall,
    matching_authorization,
    new_account_request,
)

# A second `car-...` id, so a test can tell WHICH generation's request id a refusal hands
# back. An operator given the wrong one reconciles the wrong request and learns nothing
# about the account that may be opening.
RETRY_REQUEST_ID = "car-second000000000000000000001"


def _retryable(request_id: str = FIXTURE_REQUEST_ID) -> str:
    """A settled failure AWS says may succeed if asked again.

    `CONCURRENT_ACCOUNT_MODIFICATION` rather than an invented value: it is the reason that
    makes retrying necessary in the first place, and `retry_can_succeed` is `True` for it and
    for `INTERNAL_FAILURE` only. The request id travels even on a failure, because a
    "failure" reported for a call that did allocate an account is the thing reconciliation
    needs an id for.
    """
    return encode_reference(request_id=request_id, failure=CreateAccountFailure.CONCURRENT_ACCOUNT_MODIFICATION)


def _row(
    executor: RecordingExecutor,
    request,
    generation: int,
    outcome: object,
    provider_ref: str,
) -> SettledCall:
    """One durable row for a generation, carrying the payload digest the real store holds."""
    return SettledCall(creation_key(executor, generation), outcome, provider_ref, creation_target(request))


async def _drive(executor: RecordingExecutor, request, history=()):
    """Run the real entry point with a matching authorization and recording doubles."""
    credentials = RecordingCredentials(organizations=RecordingOrganizations())
    outcome = await create_account(
        executor,
        credentials,
        request,
        authorization=matching_authorization(request),
        history=history,
    )
    return outcome, credentials


def _succeeding_executor() -> RecordingExecutor:
    """An executor whose dispatch settles as a created account.

    Used by the positive cases so that "was a dispatch authorized?" is answered by the
    recorded key rather than by whether the call happened to fail afterwards.
    """
    return RecordingExecutor(
        outcome=AuthoritativeCallOutcome.SUCCEEDED,
        provider_ref=encode_reference(request_id=FIXTURE_REQUEST_ID, account_id=FIXTURE_CREATED_ACCOUNT),
    )


class TestAPermittedRetryGetsItsOwnDerivableKey:
    """The liveness half: a retryable failure must be recoverable without a new approval."""

    @pytest.mark.asyncio
    async def test_an_empty_history_dispatches_generation_zero_unsuffixed(self) -> None:
        """Generation 0 is spelled without a suffix, and that is not cosmetic.

        Rows written before generations existed carry `<op>:organizations-create-account`. If
        generation 0 gained a `:retry-0` suffix, every one of those rows would stop matching
        the key the code now derives, the history would read as empty, and the first thing the
        new code did to an in-flight operation would be to dispatch a duplicate `CreateAccount`
        against an account that may already exist. The unsuffixed spelling is what makes the
        change safe to deploy on top of existing rows.
        """
        request = new_account_request()
        executor = _succeeding_executor()

        outcome, _ = await _drive(executor, request)

        assert outcome.succeeded
        assert [dispatch["idempotency_key"] for dispatch in executor.dispatches] == ["op-fixture:organizations-create-account"]

    @pytest.mark.asyncio
    async def test_a_settled_retryable_failure_arms_the_next_generation(self) -> None:
        """`CONCURRENT_ACCOUNT_MODIFICATION` is recoverable, so generation 1 may dispatch.

        Before generations, this state was terminal: the key already had a row, so
        `execute_provider`'s `fresh=True` refused it, and the only way forward was a new
        operation — a new approval, a new fence, and a second account waiting to happen.
        """
        request = new_account_request()
        executor = _succeeding_executor()
        history = (_row(executor, request, 0, AuthoritativeCallOutcome.FAILED, _retryable()),)

        outcome, _ = await _drive(executor, request, history)

        assert outcome.succeeded
        assert [dispatch["idempotency_key"] for dispatch in executor.dispatches] == ["op-fixture:organizations-create-account:retry-1"]

    @pytest.mark.asyncio
    async def test_the_retry_carries_the_same_payload_digest(self) -> None:
        """A new key must not mean a new fence over the payload.

        The digest travels in the store's immutable `target`, and the retry has to present the
        SAME one. If a retry re-derived the target from a freshly supplied request, a caller
        could edit the email, be told the prior generation failed retryably, and spend one
        approval on a different account under a key nothing had yet claimed — the payload hole
        from `test_payload_fence.py`, reopened through the retry door.
        """
        request = new_account_request()
        executor = _succeeding_executor()
        history = (_row(executor, request, 0, AuthoritativeCallOutcome.FAILED, _retryable()),)

        await _drive(executor, request, history)

        assert executor.dispatches[0]["target"] == creation_target(request)
        assert executor.dispatches[0]["target"] == history[0].target, "the retry changed the immutable binding the first generation recorded"

    @pytest.mark.asyncio
    async def test_a_retry_whose_payload_changed_is_refused_before_it_dispatches(self) -> None:
        """The same door, tested from the attacker's side rather than the implementation's.

        Every row in the history is checked, not just the last, so an edited payload is
        refused no matter which generation recorded the approved one. The real store would
        also refuse this at the `target` constraint; the check here happens earlier, before a
        management credential is obtained at all.
        """
        approved = new_account_request()
        executor = _succeeding_executor()
        history = (_row(executor, approved, 0, AuthoritativeCallOutcome.FAILED, _retryable()),)

        amended = new_account_request(account_email="somewhere-else@example.invalid")

        with pytest.raises(CreationRefused) as refusal:
            await _drive(executor, amended, history)

        assert "has changed" in str(refusal.value)
        assert executor.dispatches == [], "an edited payload reached the provider through the retry path"

    @pytest.mark.asyncio
    async def test_two_retryable_failures_reach_generation_two(self) -> None:
        """Generations advance one at a time, and the suffix names which one.

        Worth its own case because an off-by-one here is silent: a second retry that re-derived
        `retry-1` would be refused by the store as a duplicate, an operator would read
        "duplicate" as "already done", and the operation would wedge with no account.
        """
        request = new_account_request()
        executor = _succeeding_executor()
        history = (
            _row(executor, request, 0, AuthoritativeCallOutcome.FAILED, _retryable()),
            _row(executor, request, 1, AuthoritativeCallOutcome.FAILED, _retryable(RETRY_REQUEST_ID)),
        )

        outcome, _ = await _drive(executor, request, history)

        assert outcome.succeeded
        assert [dispatch["idempotency_key"] for dispatch in executor.dispatches] == ["op-fixture:organizations-create-account:retry-2"]


class TestAnUnresolvedGenerationBlocksEveryLaterOne:
    """The safety half: no answer obtained is never converted into permission to ask again."""

    @pytest.mark.asyncio
    async def test_a_lost_retry_response_blocks_the_next_generation(self) -> None:
        """The exact sequence: failure → retry → answer lost → second retry refused.

        This is the case that justifies reading the whole history rather than one key. The
        unresolved row sits under `retry-1`, while `retry-0`'s key holds a settled retryable
        failure. An implementation that consulted only generation 0 — the natural shape before
        generations existed — would find that failure, conclude a retry is permitted, and
        dispatch a THIRD `CreateAccount` while the second may already be opening an account.

        The refusal must also hand back the RETRY's request id, not the original's. An operator
        who reconciles `car-fixture...` learns that the first call failed, which is already
        known, and learns nothing about the account that may exist.
        """
        request = new_account_request()
        executor = _succeeding_executor()
        history = (
            _row(executor, request, 0, AuthoritativeCallOutcome.FAILED, _retryable()),
            _row(executor, request, 1, AuthoritativeCallOutcome.UNKNOWN, encode_reference(request_id=RETRY_REQUEST_ID)),
        )

        with pytest.raises(CreationRefused) as refusal:
            await _drive(executor, request, history)

        message = str(refusal.value)
        assert "unresolved" in message
        assert "generation 1" in message, f"the refusal did not say which generation is unresolved: {message}"
        assert RETRY_REQUEST_ID in message, f"the refusal handed back the wrong request id to reconcile: {message}"
        assert executor.dispatches == [], "a lost retry response authorized a further CreateAccount"

    @pytest.mark.asyncio
    async def test_an_unresolved_generation_zero_blocks_the_first_retry(self) -> None:
        """The original case, restated now that generations exist.

        Narrowing the fence must not have made "unresolved" look like a reason to advance: an
        unobtained answer is not a settled failure, and only a settled retryable failure
        authorizes the next generation.
        """
        request = new_account_request()
        executor = _succeeding_executor()
        history = (_row(executor, request, 0, AuthoritativeCallOutcome.UNKNOWN, encode_reference(request_id=FIXTURE_REQUEST_ID)),)

        with pytest.raises(CreationRefused) as refusal:
            await _drive(executor, request, history)

        assert FIXTURE_REQUEST_ID in str(refusal.value)
        assert executor.dispatches == []

    @pytest.mark.asyncio
    async def test_a_succeeded_generation_refuses_any_further_dispatch(self) -> None:
        """The account exists. A retry here is the duplicate, in its plainest form."""
        request = new_account_request()
        executor = _succeeding_executor()
        history = (
            _row(
                executor,
                request,
                0,
                AuthoritativeCallOutcome.SUCCEEDED,
                encode_reference(request_id=FIXTURE_REQUEST_ID, account_id=FIXTURE_CREATED_ACCOUNT),
            ),
        )

        with pytest.raises(CreationRefused) as refusal:
            await _drive(executor, request, history)

        message = str(refusal.value)
        assert FIXTURE_CREATED_ACCOUNT in message
        # The consequence is named, because "already created" reads as harmless and the
        # remedy for a spare account is a 90-day irreversible suspension.
        assert "90-day" in message
        assert executor.dispatches == []

    @pytest.mark.parametrize(
        "failure",
        [
            CreateAccountFailure.EMAIL_ALREADY_EXISTS,
            CreateAccountFailure.ACCOUNT_LIMIT_EXCEEDED,
            CreateAccountFailure.INVALID_EMAIL,
        ],
    )
    @pytest.mark.asyncio
    async def test_a_non_retryable_failure_authorizes_no_generation(self, failure: CreateAccountFailure) -> None:
        """Each non-retryable reason on its own: repeating the same request cannot succeed.

        Parametrized because the three fail for different reasons and an implementation could
        easily special-case one. `EMAIL_ALREADY_EXISTS` in particular is the tempting one to
        retry — it looks transient — and it is the one where a retry burns a generation against
        an address that will never be accepted.
        """
        request = new_account_request()
        executor = _succeeding_executor()
        history = (
            _row(
                executor,
                request,
                0,
                AuthoritativeCallOutcome.FAILED,
                encode_reference(request_id=FIXTURE_REQUEST_ID, failure=failure),
            ),
        )

        with pytest.raises(CreationRefused) as refusal:
            await _drive(executor, request, history)

        message = str(refusal.value)
        assert failure.value in message, f"the refusal did not name the reason AWS gave: {message}"
        assert executor.dispatches == []

    @pytest.mark.asyncio
    async def test_a_contradictory_history_is_refused_rather_than_continued(self) -> None:
        """An earlier generation that did not settle as a failure means something went wrong.

        A later generation exists, so something dispatched a retry it was not entitled to. The
        safe reading is not "continue from the newest row" — the account generation 0 may have
        opened is exactly what a further dispatch would duplicate — so the whole history is
        refused and an operator has to look.
        """
        request = new_account_request()
        executor = _succeeding_executor()
        history = (
            _row(executor, request, 0, AuthoritativeCallOutcome.UNKNOWN, encode_reference(request_id=FIXTURE_REQUEST_ID)),
            _row(executor, request, 1, AuthoritativeCallOutcome.FAILED, _retryable(RETRY_REQUEST_ID)),
        )

        with pytest.raises(CreationRefused) as refusal:
            await _drive(executor, request, history)

        message = str(refusal.value)
        assert "contradictory" in message
        assert "generation 0" in message, f"the refusal did not identify the contradicting generation: {message}"
        assert executor.dispatches == []


class TestTheGenerationCeiling:
    """An unbounded retryable loop is an unbounded number of possibly-created accounts."""

    @pytest.mark.asyncio
    async def test_the_last_permitted_generation_still_dispatches(self) -> None:
        """The boundary from below, so the ceiling cannot be off by one in the safe-looking
        direction.

        A ceiling that refused one generation early would look correct in every duplicate test
        and would quietly make the last legitimate retry impossible — pushing an operator
        toward a new operation, which is the unsafe workaround.
        """
        request = new_account_request()
        executor = _succeeding_executor()
        history = tuple(
            _row(executor, request, generation, AuthoritativeCallOutcome.FAILED, _retryable()) for generation in range(MAX_CREATION_GENERATIONS - 1)
        )

        outcome, _ = await _drive(executor, request, history)

        assert outcome.succeeded
        assert executor.dispatches[0]["idempotency_key"] == creation_key(executor, MAX_CREATION_GENERATIONS - 1)

    @pytest.mark.asyncio
    async def test_the_ceiling_refuses_a_further_generation(self) -> None:
        """At the ceiling, a retryable failure stops being a reason to retry.

        The reason is not rate limiting. Every generation is a call that may have allocated an
        account despite reporting a failure, so a wedged operation retrying forever is an
        operation nobody ever inspects while the account count climbs.
        """
        request = new_account_request()
        executor = _succeeding_executor()
        history = tuple(
            _row(executor, request, generation, AuthoritativeCallOutcome.FAILED, _retryable()) for generation in range(MAX_CREATION_GENERATIONS)
        )

        with pytest.raises(CreationRefused) as refusal:
            await _drive(executor, request, history)

        assert "ceiling" in str(refusal.value)
        assert executor.dispatches == []

    @pytest.mark.asyncio
    async def test_a_history_longer_than_the_ceiling_is_refused_rather_than_trusted(self) -> None:
        """More rows than generations should exist is itself evidence, and it refuses.

        Reached only if the ceiling was already bypassed — a rolled-back deployment with a
        higher ceiling, or rows assembled wrongly. Either way the code's own invariant is
        already violated, so continuing from such a history would be acting on state it has no
        model for.
        """
        request = new_account_request()
        executor = _succeeding_executor()
        history = tuple(
            _row(executor, request, generation, AuthoritativeCallOutcome.FAILED, _retryable()) for generation in range(MAX_CREATION_GENERATIONS + 1)
        )

        with pytest.raises(CreationRefused) as refusal:
            await _drive(executor, request, history)

        message = str(refusal.value)
        assert str(MAX_CREATION_GENERATIONS + 1) in message and "ceiling" in message
        assert executor.dispatches == []


class TestTheKeysAreDerivedAndNothingElse:
    """Unit-level, because the key spelling is what the store's uniqueness applies to."""

    def test_the_keys_are_stable_across_calls(self) -> None:
        """No clock, no uuid, no counter. Two derivations agree, so a restart is a duplicate.

        Stated directly rather than inferred from the dispatch tests: the moment a key acquires
        any varying component, every duplicate protection in this package silently stops
        applying, and no behavioural test would fail — each run would simply dispatch under a
        key nothing had claimed.
        """
        executor = RecordingExecutor()

        first = creation_generation_keys(executor, MAX_CREATION_GENERATIONS)
        second = creation_generation_keys(executor, MAX_CREATION_GENERATIONS)

        assert first == second
        assert len(set(first)) == len(first), "two generations derived the same key"
        assert first[0] == "op-fixture:organizations-create-account"
        assert first[1:] == (
            "op-fixture:organizations-create-account:retry-1",
            "op-fixture:organizations-create-account:retry-2",
        )

    def test_the_keys_are_scoped_to_the_operation(self) -> None:
        """Two operations share no key, so neither can be mistaken for the other's duplicate."""
        mine = creation_generation_keys(RecordingExecutor(operation_id="op-mine"), MAX_CREATION_GENERATIONS)
        theirs = creation_generation_keys(RecordingExecutor(operation_id="op-theirs"), MAX_CREATION_GENERATIONS)

        assert set(mine).isdisjoint(theirs)

    @pytest.mark.parametrize("generation", [-1, -7])
    def test_a_negative_generation_is_an_error_not_a_key(self, generation: int) -> None:
        """A programming mistake must not silently produce a plausible-looking key.

        `retry--1` would be accepted by the store as a key nothing had claimed, so a sign error
        upstream would present as a fresh dispatch rather than as a crash.
        """
        with pytest.raises(AccountCreationError):
            creation_key(RecordingExecutor(), generation)

    def test_naming_a_negative_number_of_generations_is_an_error(self) -> None:
        """Same reasoning for the plural helper: an empty tuple would read as "no history"."""
        with pytest.raises(AccountCreationError):
            creation_generation_keys(RecordingExecutor(), -1)


@pytest.mark.asyncio
async def test_nonretryable_predecessor_cannot_be_hidden_by_later_retryable_failure():
    request = new_account_request()
    executor = _succeeding_executor()
    history = (
        _row(
            executor,
            request,
            0,
            AuthoritativeCallOutcome.FAILED,
            encode_reference(request_id=FIXTURE_REQUEST_ID, failure=CreateAccountFailure.EMAIL_ALREADY_EXISTS),
        ),
        _row(executor, request, 1, AuthoritativeCallOutcome.FAILED, _retryable(RETRY_REQUEST_ID)),
    )
    with pytest.raises(CreationRefused, match="Only an established, retryable failure"):
        await _drive(executor, request, history)
    assert executor.dispatches == []
