"""Reconciliation WRITES what it read, or says it did not — Issue #5531 (w6-08).

## The defect these tests exist to prevent

`reconcile_creation` asked AWS what became of a recorded `CreateAccount` request, built an
`AccountCreationOutcome` from the answer, and returned it. It never touched the durable row.

That is not recovery. It is a recomputation, and the difference is visible in four places:

* the row stays `intended`, so the next pass asks AWS the same question again — forever;
* the operation never converges: nothing on disk ever names the account that was created,
  which is the exact end state this whole story exists to prevent;
* the observation dies with the process that made it, so a crash between the read and the
  caller's next step loses the only answer anyone obtained; and
* `_plan_generation` reads committed rows only, so an `intended` row keeps reporting
  "generation 0 is unresolved, an account may exist" about an account AWS already confirmed.

So the answer is now persisted through `execution.ReconciliationStore`, and the tests below
are about what gets written, what deliberately does not, and what the caller is told when the
write did not happen.

## What is real here and what is substituted

This file is the OFFLINE half: it asserts the mapping from an AWS answer to a settlement —
which key, which outcome, which reference — using `RecordingReconciliationStore`, which
performs no write. That the write is durable, that the row really moves out of `intended`,
and that a second pass then reads the settled row are claims about PostgreSQL and are
asserted against a real server in `test_durability_postgres.py`.

No AWS API is called and no account is created anywhere in this file. A live vend against a
real organization is a LIVE criterion needing separate named authorization; offline evidence
never closes one.
"""

from __future__ import annotations

import pytest
from harness_jobs.execution import CallOutcome as AuthoritativeCallOutcome
from harness_jobs.execution import ProviderCallRefused

from account_factory.creation import CreateAccountFailure, CreateAccountStatus
from account_provisioning.creation_runner import (
    CREATION_STEP,
    _observation_from_status,
    creation_target,
    encode_reference,
    reconcile_creation,
    reconciled_outcome,
)
from account_provisioning.execution import CallOutcome as LocalCallOutcome

from .conftest import (
    FIXTURE_CREATED_ACCOUNT,
    FIXTURE_REQUEST_ID,
    RecordingCredentials,
    RecordingExecutor,
    RecordingOrganizations,
    RecordingReconciliationStore,
    SettledCall,
    failed_response,
    in_progress_response,
    new_account_request,
    succeeded_response,
)

RECORDED_KEY = f"op-fixture:{CREATION_STEP}#0"


def _intended_row(reference: str | None = None) -> SettledCall:
    """The row a lost reply leaves: intent committed, no outcome, the request id recorded.

    Built with the real `encode_reference` rather than a hand-written string, so a change to
    the reference format cannot leave these tests asserting against a spelling production no
    longer writes.
    """
    return SettledCall(
        RECORDED_KEY,
        None,
        encode_reference(request_id=FIXTURE_REQUEST_ID) if reference is None else reference,
        creation_target(new_account_request()),
    )


async def _reconcile(describe, *, store=None, refusal_types=(ProviderCallRefused,), recorded=None):
    """Drive the real `reconcile_creation` over one scripted `DescribeCreateAccountStatus`.

    The outcome vocabulary is the executor's OWN class, because the STORE validates against
    it and this package holds only a copy — see `execution.outcome_vocabulary`. Passing the
    local copy here is the bug that class's docstring describes, so the tests pass the real
    one and one test below asserts that is what arrives.
    """
    organizations = RecordingOrganizations(describe_results=[describe])
    credentials = RecordingCredentials(organizations=organizations)
    executor = RecordingExecutor()
    outcome = await reconcile_creation(
        executor,
        credentials,
        recorded=recorded if recorded is not None else _intended_row(),
        store=store,
        outcomes=AuthoritativeCallOutcome,
        refusal_types=refusal_types,
    )
    return outcome, organizations


# ─────────────────────────────────────────────────────────────────────────────────────────
# A confirmed account is written down
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestAConfirmedAccountIsPersisted:
    """The case the unpersisted version got wrong most expensively.

    AWS says the account exists and names it. If that does not reach the durable row, the
    system holds an account that is billing and no record anywhere identifies it.
    """

    @pytest.mark.asyncio
    async def test_the_row_is_settled_under_the_key_that_was_read(self) -> None:
        store = RecordingReconciliationStore()
        outcome, _ = await _reconcile(succeeded_response(), store=store)

        assert outcome.succeeded
        assert len(store.settlements) == 1, (
            "the confirmed creation was read and not written: the durable row is still "
            "awaiting reconciliation, so the next pass re-reads AWS and nothing on disk "
            "ever names the account that exists"
        )
        settled = store.settlements[0]
        assert settled["idempotency_key"] == RECORDED_KEY, (
            "the settlement was written under a different key than the row that was read, "
            "so the unresolved row stays unresolved and some other call gets overwritten"
        )

    @pytest.mark.asyncio
    async def test_the_persisted_reference_names_the_account(self) -> None:
        """The whole point of settling it: the account id has to be ON the row.

        An outcome of `succeeded` with a reference that does not name the account is worse
        than an unresolved row, because `disposition_for` releases the reservation for a
        `SUCCEEDED` call — so the budget is given back for an account that is still billing.
        """
        store = RecordingReconciliationStore()
        await _reconcile(succeeded_response(), store=store)

        reference = store.settlements[0]["provider_ref"]
        assert FIXTURE_CREATED_ACCOUNT in reference, f"the row was settled as succeeded without recording the account id: {reference!r}"
        assert FIXTURE_REQUEST_ID in reference, (
            f"the request handle was dropped while settling, so the settlement cannot be traced back to the call it is about: {reference!r}"
        )

    @pytest.mark.asyncio
    async def test_the_outcome_is_written_in_the_stores_own_vocabulary(self) -> None:
        """`isinstance`, not equality, is what the real store checks — see `reconcile`.

        `harness_jobs.execution.reconcile` raises `ContractViolation` for an outcome that is
        not a member of ITS `CallOutcome`. This package's copy is a different class, so
        settling with the copy is not a type nicety — it is a recovery pass that raises
        instead of writing, leaving every reconciled row `intended` forever.
        """
        store = RecordingReconciliationStore()
        await _reconcile(succeeded_response(), store=store)

        written = store.settlements[0]["outcome"]
        assert isinstance(written, AuthoritativeCallOutcome), (
            f"the outcome was settled as {type(written)!r}; the real store `isinstance`-checks against its own CallOutcome and refuses anything else"
        )
        assert written is AuthoritativeCallOutcome.SUCCEEDED

    @pytest.mark.asyncio
    async def test_the_caller_is_told_the_row_was_settled(self) -> None:
        """The status is what AWS said; the detail also says what happened to the row.

        Two separate facts. A confirmed account whose row could not be settled needs a
        different action from a confirmed account whose row was settled, and a caller that
        cannot tell them apart cannot take either.
        """
        store = RecordingReconciliationStore()
        outcome, _ = await _reconcile(succeeded_response(), store=store)

        assert outcome.status is CreateAccountStatus.SUCCEEDED
        assert "settled" in outcome.detail
        assert FIXTURE_CREATED_ACCOUNT in outcome.detail


# ─────────────────────────────────────────────────────────────────────────────────────────
# An established failure is written down too
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestAnEstablishedFailureIsPersisted:
    """`FAILED` with a reason AWS named is the only answer that releases the reservation.

    It also has to be persisted, for a different reason from success: until it is, the row
    reads as "a call may have been made" and `_plan_generation` refuses the retry the failure
    positively authorized. An unpersisted failure is a stuck operation.
    """

    @pytest.mark.asyncio
    async def test_a_named_failure_settles_as_failed_with_its_reason(self) -> None:
        store = RecordingReconciliationStore()
        outcome, _ = await _reconcile(failed_response("EMAIL_ALREADY_EXISTS"), store=store)

        assert outcome.status is CreateAccountStatus.FAILED
        assert outcome.failure is CreateAccountFailure.EMAIL_ALREADY_EXISTS
        settled = store.settlements[0]
        assert settled["outcome"] is AuthoritativeCallOutcome.FAILED, (
            "an established failure was not settled as FAILED, so the reservation is retained "
            "for an account AWS says does not exist and the authorized retry stays blocked"
        )
        assert CreateAccountFailure.EMAIL_ALREADY_EXISTS.value in settled["provider_ref"], (
            "the failure reason was not persisted; whether a retry can ever succeed is decided "
            "entirely by which reason AWS gave, so a row without it authorizes nothing"
        )
        assert FIXTURE_CREATED_ACCOUNT not in (settled["provider_ref"] or ""), "a failed settlement must not carry an account id"

    @pytest.mark.asyncio
    async def test_a_failure_whose_reason_aws_did_not_name_is_not_settled_as_failed(self) -> None:
        """An unmapped reason establishes nothing, so it must not release the reservation.

        `FAILED` is a claim that NOTHING was created. This code can only make that claim for
        a reason it recognises; for any other it must persist `UNKNOWN`, which keeps the
        budget retained and puts the row in front of a person.
        """
        store = RecordingReconciliationStore()
        outcome, _ = await _reconcile(failed_response("SOME_REASON_AWS_ADDED_LATER"), store=store)

        assert outcome.status is CreateAccountStatus.UNKNOWN
        assert store.settlements[0]["outcome"] is AuthoritativeCallOutcome.UNKNOWN, (
            "a FAILED reply with an unrecognised reason was settled as an established failure; "
            "that releases the reservation and permits a retry on evidence that establishes "
            "nothing about whether an account exists"
        )


# ─────────────────────────────────────────────────────────────────────────────────────────
# What must NOT be written
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestAnUnfinishedOrUnreadableAnswerIsNotSettled:
    """The half where writing is the error.

    Every case here shares one property: nothing has established whether an account exists.
    The row must either stay open for another sweep or be parked for a human, and neither is
    a settlement that frees the budget.
    """

    @pytest.mark.asyncio
    async def test_an_in_progress_creation_writes_nothing_at_all(self) -> None:
        """AWS is still working. Settling here would close a call that has not finished.

        Not even `unresolved`: `unresolved_calls` selects `intended` rows only, so parking an
        in-flight creation would remove it from automatic recovery and require a person to
        finish a job that was going to finish by itself.
        """
        store = RecordingReconciliationStore()
        outcome, _ = await _reconcile(in_progress_response(), store=store)

        assert outcome.status is CreateAccountStatus.IN_PROGRESS
        assert store.settlements == [], (
            "an in-progress creation was settled. The call has not finished, and a settled "
            "row is excluded from the unresolved sweep — so the operation is now waiting for "
            "a human instead of for AWS"
        )

    @pytest.mark.asyncio
    async def test_a_success_with_no_account_id_is_parked_not_settled_as_success(self) -> None:
        """The worst possible row, and the one a naive mapping writes.

        AWS said SUCCEEDED and did not name the account. Persisting that as `SUCCEEDED`
        releases the reservation and reports the operation complete while nothing anywhere
        identifies the account that is billing.
        """
        store = RecordingReconciliationStore()
        outcome, _ = await _reconcile(succeeded_response(account_id=None), store=store)

        assert outcome.may_exist
        settled = store.settlements[0]
        assert settled["outcome"] is AuthoritativeCallOutcome.UNKNOWN, (
            "a SUCCEEDED reply with no AccountId was settled as a success, releasing the "
            "reservation for an account that exists and that no record names"
        )
        assert FIXTURE_REQUEST_ID in settled["provider_ref"], "the handle must survive: it is the only way anyone can ask AWS again"

    @pytest.mark.asyncio
    async def test_an_unreadable_status_leaves_the_row_intended_for_another_sweep(self) -> None:
        """The read itself failed. Nothing is written, deliberately.

        Parking it `unresolved` would take the row out of the sweepable set because one read
        failed once — turning a transient AWS problem into a permanent operator ticket. The
        row stays `intended`, which is exactly where the next sweep will find it.
        """
        from account_provisioning.ports import ProviderUnavailable

        store = RecordingReconciliationStore()
        outcome, _ = await _reconcile(ProviderUnavailable("the endpoint timed out"), store=store)

        assert outcome.status is CreateAccountStatus.UNKNOWN
        assert outcome.create_account_request_id == FIXTURE_REQUEST_ID
        assert store.settlements == [], (
            "a failed READ was written to the row as a settled outcome, removing it from automatic recovery over a transient error"
        )

    @pytest.mark.asyncio
    async def test_a_row_with_no_request_handle_is_never_settled(self) -> None:
        """No handle means AWS was never asked, so there is nothing to persist.

        This is the row that needs a person: a call may have been made and nothing identifies
        it. Writing any outcome here would be recording a guess as an observation.
        """
        store = RecordingReconciliationStore()
        organizations = RecordingOrganizations(describe_results=[succeeded_response()])
        credentials = RecordingCredentials(organizations=organizations)
        outcome = await reconcile_creation(
            RecordingExecutor(),
            credentials,
            recorded=_intended_row(reference=""),
            store=store,
            outcomes=AuthoritativeCallOutcome,
            refusal_types=(ProviderCallRefused,),
        )

        assert outcome.may_exist
        assert store.settlements == [], "a row with no recorded request id was settled from an answer about some other request"
        assert organizations.calls == [], "AWS was asked about a request id that was never recorded"

    @pytest.mark.asyncio
    async def test_the_same_unnameable_success_keeps_its_handle_on_the_dispatch_path(self) -> None:
        """The other caller of the parser, where the same reply used to lose the handle.

        `creation_hook` and `reconcile_creation` share `_observation_from_status`, so a reply
        AWS is entitled to send has to be survivable on BOTH. Here it is the first reply rather
        than a recovery read, and the failure mode was worse: the parser raised, the executor's
        blanket catch turned it into `UNKNOWN` with no `provider_ref` at all, and the request id
        for an account that exists was gone from the only place it is ever recorded.

        Asserted through the real hook, because the reference it returns is exactly what the
        executor writes to the row.
        """
        from account_provisioning.creation_runner import creation_hook

        organizations = RecordingOrganizations(create_result=succeeded_response(account_id=None))
        credentials = RecordingCredentials(organizations=organizations)
        hook = creation_hook(credentials, new_account_request(), outcomes=AuthoritativeCallOutcome)

        outcome, detail, reference = await hook(_intended_row())

        assert outcome is AuthoritativeCallOutcome.UNKNOWN, "a success AWS did not name must not be recorded as a success"
        assert FIXTURE_REQUEST_ID in (reference or ""), (
            f"the reply said an account was created and the row was left with no handle to it: {reference!r}"
        )
        assert "does not name it" in (detail or "")


# ─────────────────────────────────────────────────────────────────────────────────────────
# When the write does not happen, the caller is told
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestTheCallerIsToldWhetherTheRowWasSettled:
    """A reported outcome that is silent about the row is the defect in a subtler form.

    The unpersisted version returned a correct-looking success. Anything that can return a
    correct-looking success while the row is still `intended` has to SAY so, or the next
    reader draws the same wrong conclusion the original defect did.
    """

    @pytest.mark.asyncio
    async def test_an_uncomposed_store_reads_and_says_it_did_not_persist(self) -> None:
        """A composition that forgot to wire the store must not look like convergence."""
        outcome, _ = await _reconcile(succeeded_response(), store=None)

        assert outcome.succeeded, "the read itself still works without a store"
        assert "NOT persisted" in outcome.detail, (
            f"reconciliation ran with no store and reported a plain success, which is exactly "
            f"how the unpersisted defect read from the outside: {outcome.detail!r}"
        )

    @pytest.mark.asyncio
    async def test_a_store_refusal_is_reported_not_raised(self) -> None:
        """Two recovery passes racing is expected; the loser losing is the store working.

        `reconcile` refuses a row that is already settled — including one already
        `unresolved`, because overwriting a human's row on a timer is a different act from
        settling an open one. Raising here would turn a normal race into a recovery pass that
        crashes, and a crashing sweep is one that stops reconciling everything after it.
        """
        store = RecordingReconciliationStore(refuse_with=ProviderCallRefused("already settled"))
        outcome, _ = await _reconcile(succeeded_response(), store=store)

        assert outcome.succeeded, "what AWS said is still reported"
        assert outcome.account_id == FIXTURE_CREATED_ACCOUNT
        assert "not settled by this pass" in outcome.detail
        assert "already settled" in outcome.detail, "the store's own words are what tell an operator which row won"

    @pytest.mark.asyncio
    async def test_an_unnamed_store_failure_is_not_swallowed(self) -> None:
        """Only the refusal named by `refusal_types` is normal. Everything else propagates.

        A blanket `except Exception` here would hide a genuine write failure — a lost
        connection, a constraint violation — behind a sentence about a race, and the row
        would silently stay `intended` while the caller read a settled success.
        """
        store = RecordingReconciliationStore(refuse_with=RuntimeError("the connection died mid-write"))

        with pytest.raises(RuntimeError, match="connection died"):
            await _reconcile(succeeded_response(), store=store, refusal_types=(ProviderCallRefused,))


# ─────────────────────────────────────────────────────────────────────────────────────────
# The mapping, on its own
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestTheSettlementMapping:
    """`reconciled_outcome` is the translation, tested directly and without a database.

    Separate from the tests above because the mapping is the part a future reader is most
    likely to change, and a table of "this answer settles as that outcome" is where a wrong
    change shows up as a failing line rather than as a subtly different sentence.
    """

    def test_an_unspecified_vocabulary_falls_back_to_the_local_copy(self) -> None:
        """Offline callers pass nothing, and the fallback must still be a real member.

        The fallback is correct for the offline tests, which never hand the answer to a real
        store. It is NOT correct in production, and that is why the executor-facing callers
        pass the class in — see `execution.outcome_vocabulary`.
        """
        from account_factory.creation import CreateAccountObservation

        outcome, _, _ = reconciled_outcome(
            CreateAccountObservation(
                status=CreateAccountStatus.SUCCEEDED,
                create_account_request_id=FIXTURE_REQUEST_ID,
                account_id=FIXTURE_CREATED_ACCOUNT,
            )
        )
        assert outcome is LocalCallOutcome.SUCCEEDED

    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            (succeeded_response(), "SUCCEEDED"),
            (succeeded_response(account_id=None), "UNKNOWN"),
            (failed_response("EMAIL_ALREADY_EXISTS"), "FAILED"),
            (failed_response("SOME_REASON_AWS_ADDED_LATER"), "UNKNOWN"),
            ({"Id": FIXTURE_REQUEST_ID, "State": "SOMETHING_NEW"}, "UNKNOWN"),
        ],
    )
    def test_each_aws_reply_settles_as_exactly_one_outcome(self, reply: dict, expected: str) -> None:
        """The whole table in one place, from AWS's words to the store's vocabulary.

        Driven from the response dict through the real parser rather than from hand-built
        observations, because the parser is the only thing that produces one and because two
        of these five shapes CANNOT be expressed as an observation at all — which is the
        subject of the next test.

        Note the asymmetry, which is the safety rule rather than an inconsistency: only two
        rows settle as anything other than `UNKNOWN`, and both of them are answers that
        positively established what happened. Every other shape of answer keeps the budget
        retained, because releasing it is what permits a second billable account.
        """
        status = reply.get("CreateAccountStatus", reply)
        outcome, detail, reference = reconciled_outcome(_observation_from_status(status), AuthoritativeCallOutcome)

        assert outcome is getattr(AuthoritativeCallOutcome, expected)
        assert detail, "every settlement carries a sentence a human can read off the row"
        assert FIXTURE_REQUEST_ID in reference, "the handle is on every settlement, whatever the outcome"

    @pytest.mark.parametrize(
        ("status", "failure", "account_id"),
        [
            (CreateAccountStatus.SUCCEEDED, None, None),
            (CreateAccountStatus.FAILED, None, None),
        ],
    )
    def test_the_two_assertions_masquerading_as_observations_cannot_be_built(self, status, failure, account_id) -> None:
        """Why the parser must downgrade, rather than pass AWS's words through.

        `CreateAccountObservation` refuses a success with no account id and a failure with no
        reason, because each is a claim rather than something AWS reported. An earlier revision
        built the first one anyway, and the raise did not surface as a clear error: on the hook
        path the executor's blanket catch turned it into `UNKNOWN` with NO reference, discarding
        the request id for an account that exists; on the recovery path it aborted the sweep, so
        every row behind the offending one went unreconciled.

        This test is the reason the downgrade lives in `_observation_from_status` and not in a
        caller: if the constraint is ever relaxed, this fails and points at the parser.
        """
        from account_factory.creation import AccountCreationError, CreateAccountObservation

        with pytest.raises(AccountCreationError):
            CreateAccountObservation(
                status=status,
                create_account_request_id=FIXTURE_REQUEST_ID,
                account_id=account_id,
                failure=failure,
                detail="AWS said so",
            )


@pytest.mark.asyncio
async def test_different_provider_request_cannot_settle_recorded_creation():
    store = RecordingReconciliationStore()
    response = succeeded_response()
    response["CreateAccountStatus"]["Id"] = "car-other-operation"
    outcome, _ = await _reconcile(response, store=store)
    assert outcome.may_exist
    assert outcome.account_id is None
    assert outcome.create_account_request_id == FIXTURE_REQUEST_ID
    assert store.settlements == []


@pytest.mark.asyncio
async def test_recovery_refuses_another_operations_record_before_credentials():
    from account_provisioning.creation_runner import CreationRefused

    row = _intended_row()
    row.operation_id = "other-operation"
    credentials = RecordingCredentials(organizations=RecordingOrganizations())
    with pytest.raises(CreationRefused, match="different operation"):
        await reconcile_creation(RecordingExecutor(), credentials, recorded=row)
    assert credentials.management_calls == []
