"""Duplicate fencing and durability, against a REAL PostgreSQL server — #5531 (w6-08).

The claim this story rests on is that ADP opens an AWS account **at most once, ever**, and
that the intent to open it is committed before the provider is called so a lost answer is
recoverable rather than repeatable. Every part of that is a property of the database:

* "two concurrent workers cannot both open an account" is a PRIMARY KEY conflict;
* "the record survived the crash" is a claim about what is on disk;
* "a superseded worker cannot publish an outcome" is an advisory lock and a fence token.

A fake executor cannot establish any of it. The rest of this suite uses `HookExecutor`, a
double whose own docstring says it does NOT reproduce the advisory lock, the fence token or
the UNIQUE constraint — which is exactly the list of things that do the fencing. So this
file drives this package's real entry points through the REAL
`harness_jobs.execution.OperationExecutor` against a real server, on a fresh schema per
test.

## What is real here and what is substituted

Real: PostgreSQL, the harness DDL, `OperationStore.admit`, `leases.acquire`,
`OperationExecutor`, the advisory lock, the fence token, the
`harness_provider_call_intent` primary key, this package's `create_account` and
`creation_hook`, and `account_factory`'s decision layer.

Substituted: the AWS clients only. `RecordingOrganizations` stands in for Organizations,
because the thing under test is what the DATABASE permits, not what AWS returns — and
contacting AWS is neither authorized nor necessary to establish that a second dispatch is
refused. Every assertion about "how many accounts were opened" reads
`organizations.create_calls`, which is a list of the actual calls made.

## What these tests do NOT establish

No AWS account is created and no AWS API is called. That a real vend against a real
organization behaves as described is a LIVE criterion requiring separate named
authorization, and it belongs to the Wave 6 operations gate. Offline evidence — however
real the database — never closes a live criterion.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from harness_jobs import OperationStore
from harness_jobs.execution import (
    BudgetDisposition,
    CallStage,
    OperationExecutor,
    ProviderCallRefused,
    disposition_for,
    read_call,
    reconcile,
    unresolved_calls,
)
from harness_jobs.execution import CallOutcome as AuthoritativeCallOutcome
from harness_jobs.identity import (
    REQUIRED_PERMISSION,
    OperationRequest,
    ResolvedPrincipal,
)
from harness_jobs.leases import acquire
from harness_jobs.recovery import sweep_expired_leases

from account_factory.creation import CreateAccountFailure, CreateAccountStatus
from account_provisioning.creation_runner import (
    CreationRefused,
    _settled_outcome,
    create_account,
    creation_hook,
    creation_key,
    creation_target,
    reconcile_creation,
)

from .conftest import (
    FIXTURE_CREATED_ACCOUNT,
    FIXTURE_ORG_ID,
    FIXTURE_REQUEST_ID,
    FIXTURE_WORKSPACE,
    RecordingCredentials,
    RecordingOrganizations,
    failed_response,
    in_progress_response,
    matching_authorization,
    new_account_request,
    requires_postgres,
    succeeded_response,
)

pytestmark = requires_postgres


# ─────────────────────────────────────────────────────────────────────────────────────────
# Building a real, admitted, leased operation
# ─────────────────────────────────────────────────────────────────────────────────────────


def _principal() -> ResolvedPrincipal:
    """A principal in the tenant the request fixtures name.

    The org and workspace must match the request, because `create_account` compares the
    authorization against the executor's own LEASE — server-resolved identity. A mismatch is
    refused before any provider call, which is `test_authorization_required.py`'s subject,
    not this file's.
    """
    return ResolvedPrincipal(
        org_id=FIXTURE_ORG_ID,
        workspace_id=FIXTURE_WORKSPACE,
        subject="user:tester",
        permissions=frozenset({REQUIRED_PERMISSION}),
    )


async def _mark_paid(connection, operation_id: str, *, org_id: str, workspace_id: str) -> None:
    """The approval-consumption row the spend gate would have written.

    `leases.acquire` refuses an operation that did not pass the approval gate, so without
    this no lease is grantable and nothing here could run. It is a stand-in for the gate,
    not a way around it — the same role `modules/harness/jobs/tests/conftest.py::mark_paid`
    plays for that package's own tests, and it is deliberately not used by anything
    asserting the gate itself.
    """
    approval = f"prov-{uuid.uuid4().hex}"
    await connection.execute(
        """
        INSERT INTO harness_approval_consumption (
            approval_id, operation_id, org_id, workspace_id, plan_digest,
            requester, approved_by, max_resource_units, max_runtime_seconds,
            max_cost_micros, reservation_id, reservation_state
        )
        VALUES ($1,$2,$3,$4,'account-factory-digest','user:tester','user:approver',
                4, 3600, 5000000, $5, 'confirmed')
        """,
        approval,
        operation_id,
        org_id,
        workspace_id,
        f"res-{approval}",
    )


async def _leased(pool, *, key: str, holder: str = "worker-1", attempt: str = "attempt-1", **kwargs):
    """An admitted, paid-for operation with a live lease — the real thing, in the database.

    Going through `admit` and `acquire` rather than inserting rows keeps the operation
    subject to the same state, tenancy and approval checks the executor enforces, so a
    refusal anywhere below is a real refusal rather than an artefact of hand-built rows.
    """
    store = OperationStore()
    async with pool.acquire() as connection, connection.transaction():
        admitted = await store.admit(
            connection,
            _principal(),
            OperationRequest(action="provision", idempotency_key=key, parameters={}),
        )
        await _mark_paid(
            connection,
            admitted.record.operation_id,
            org_id=admitted.record.org_id,
            workspace_id=admitted.record.workspace_id,
        )
        return await acquire(
            connection,
            operation_id=admitted.record.operation_id,
            holder=holder,
            attempt_id=attempt,
            **kwargs,
        )


def _executor(lease, pool, *, hook):
    """The REAL executor, bound to the real pool.

    `provider_call` is the composition seam the harness provides for trusted code; handing a
    hook built by `creation_hook` to it is the composition #5535 (w6-12) owns in production.
    The vocabulary passed to `creation_hook` is the executor's OWN `CallOutcome` class,
    because the executor `isinstance`-checks against it and silently substitutes `UNKNOWN`
    for anything else — see `execution.outcome_vocabulary`.
    """
    return OperationExecutor(lease, connect=pool.acquire, provider_call=hook)


def _composed(pool, lease, organizations):
    """The triple a real dispatch needs: credentials, request, executor."""
    request = new_account_request()
    credentials = RecordingCredentials(organizations=organizations)
    hook = creation_hook(credentials, request, outcomes=AuthoritativeCallOutcome)
    return credentials, request, _executor(lease, pool, hook=hook)


async def _row(pool, key: str):
    async with pool.acquire() as connection:
        return await read_call(connection, idempotency_key=key)


class _PoolReconciliationStore:
    """The composer's `execution.ReconciliationStore`, over the real pool.

    `harness_jobs.execution.reconcile` takes a `Connection` as its first argument, and this
    package must never hold one — that is exactly why the dependency is a Protocol and why the
    adapter lives on the trusted side. This is that adapter, three lines of it, written here
    because the production composition is #5535's (w6-12) work and writing it in this package
    would be taking that decision on their behalf.

    It is deliberately NOT a double. `reconcile` runs for real, against the real DDL, so the
    `stage='intended'` predicate, the already-settled refusal and the durability of what it
    wrote are the actual store's behaviour rather than an imitation of it.
    """

    def __init__(self, pool) -> None:
        self._pool = pool
        self.settlements: list[str] = []

    async def reconcile(self, *, idempotency_key: str, outcome, detail=None, provider_ref=None):
        self.settlements.append(idempotency_key)
        async with self._pool.acquire() as connection, connection.transaction():
            return await reconcile(
                connection,
                idempotency_key=idempotency_key,
                outcome=outcome,
                detail=detail,
                provider_ref=provider_ref,
            )


async def _rows_for(pool, operation_id: str) -> list:
    async with pool.acquire() as connection:
        return await connection.fetch(
            "SELECT idempotency_key, stage, outcome, provider_ref, target, fence_token "
            "FROM harness_provider_call_intent WHERE operation_id=$1 "
            "ORDER BY idempotency_key",
            operation_id,
        )


# ─────────────────────────────────────────────────────────────────────────────────────────
# Intent is committed BEFORE the provider is called
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestIntentIsCommittedBeforeTheCall:
    """The ordering that makes a lost answer recoverable instead of repeatable.

    A record written AFTER the call is absent in exactly the case it exists for: the call
    happened and the answer was lost.
    """

    @pytest.mark.asyncio
    async def test_the_row_is_on_disk_while_the_hook_is_still_running(self, pool):
        """Observed from a SEPARATE connection, which is what makes it a durability claim.

        Reading the row on the caller's own connection would prove only that a session can
        see its own uncommitted work. A second connection can see it only if it committed.
        """
        lease = await _leased(pool, key="dur-before")
        request = new_account_request()
        credentials = RecordingCredentials(organizations=RecordingOrganizations(create_result=succeeded_response()))
        seen: list = []

        async def hook(call):
            # Mid-flight: intent is committed, the provider has not answered.
            seen.extend(await _rows_for(pool, lease.operation_id))
            return (
                AuthoritativeCallOutcome.SUCCEEDED,
                "created",
                f"request={FIXTURE_REQUEST_ID} account={FIXTURE_CREATED_ACCOUNT}",
            )

        executor = _executor(lease, pool, hook=hook)
        await create_account(executor, credentials, request, authorization=matching_authorization(request))

        assert len(seen) == 1, (
            "no committed intent row was visible from another connection while the provider "
            "call was in flight — a crash here would leave no record that a call may have "
            "been made, and the next pass would open a second account"
        )
        assert seen[0]["stage"] == CallStage.INTENDED.value
        assert seen[0]["idempotency_key"] == creation_key(executor)

    @pytest.mark.asyncio
    async def test_the_committed_row_outlives_the_caller(self, pool):
        """The crash case: nothing in the caller's memory is needed to find the row again.

        Simulated by abandoning the executor rather than killing a process. What matters is
        that the key is re-derivable from the operation id alone, which is why
        `creation_key` contains no uuid, timestamp or attempt number.
        """
        lease = await _leased(pool, key="dur-crash")
        request = new_account_request()
        credentials = RecordingCredentials(organizations=RecordingOrganizations(create_result=in_progress_response()))

        async def lost(call):
            raise TimeoutError("the reply never came")

        executor = _executor(lease, pool, hook=lost)
        outcome = await create_account(executor, credentials, request, authorization=matching_authorization(request))

        # Not reported as a failure: an account may exist.
        assert outcome.status is CreateAccountStatus.UNKNOWN
        assert outcome.may_exist

        recovered = await _row(pool, creation_key(executor))
        assert recovered is not None, (
            "the intent row did not survive, so a recovery pass would conclude no call was made and could open a second account"
        )
        assert recovered.operation_id == lease.operation_id
        assert recovered.stage is CallStage.INTENDED


# ─────────────────────────────────────────────────────────────────────────────────────────
# The duplicate fence
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestASecondCallUnderTheSameKeyIsRefused:
    """The constraint that stops the second billable account.

    `execute_provider` commits intent with `fresh=True`, so a key that already has a row is
    refused by the stored row rather than by a Python flag — which is what makes the refusal
    hold across a restart and across two workers that cannot see each other.
    """

    @pytest.mark.asyncio
    async def test_a_repeated_dispatch_calls_aws_exactly_once(self, pool):
        """The redelivery case: the same operation driven again. It must not reach AWS."""
        lease = await _leased(pool, key="dur-dup")
        organizations = RecordingOrganizations(create_result=succeeded_response())
        credentials, request, executor = _composed(pool, lease, organizations)

        first = await create_account(executor, credentials, request, authorization=matching_authorization(request))
        assert first.succeeded

        with pytest.raises(ProviderCallRefused) as refused:
            await create_account(executor, credentials, request, authorization=matching_authorization(request))
        assert "already exists" in str(refused.value)

        assert len(organizations.create_calls) == 1, (
            f"AWS CreateAccount was called {len(organizations.create_calls)} times for one "
            f"operation. Each extra call is a real, billable account, and removing the spare "
            f"is a 90-day irreversible suspension"
        )
        assert len(await _rows_for(pool, lease.operation_id)) == 1

    @pytest.mark.asyncio
    async def test_two_concurrent_workers_open_one_account(self, pool):
        """The race, run for real on separate connections.

        A pool rather than one connection is the point: two coroutines sharing a connection
        are serialized by the driver, so this test would pass without exercising the lock at
        all. Here both workers genuinely contend.

        Exactly one must win. The loser must be REFUSED, not silently deduplicated into a
        success — a caller told "fine" for a call it never made will not go looking for the
        account it believes it created.
        """
        lease = await _leased(pool, key="dur-race")
        organizations = RecordingOrganizations(create_result=succeeded_response())
        request = new_account_request()
        credentials = RecordingCredentials(organizations=organizations)
        hook = creation_hook(credentials, request, outcomes=AuthoritativeCallOutcome)
        entered = asyncio.Event()

        async def slow(call):
            # Widen the window so both workers are genuinely in flight together.
            entered.set()
            await asyncio.sleep(0.05)
            return await hook(call)

        async def worker():
            try:
                return await create_account(
                    _executor(lease, pool, hook=slow),
                    credentials,
                    request,
                    authorization=matching_authorization(request),
                )
            except (ProviderCallRefused, CreationRefused) as exc:
                return exc

        results = await asyncio.gather(worker(), worker())
        won = [r for r in results if not isinstance(r, Exception)]
        lost = [r for r in results if isinstance(r, Exception)]

        assert len(organizations.create_calls) == 1, (
            f"two concurrent workers made {len(organizations.create_calls)} CreateAccount "
            f"calls. This is the duplicate-account defect, and it is billable"
        )
        assert len(won) == 1, f"expected exactly one winner, got {results}"
        assert len(lost) == 1, "the loser was not refused, so it may believe it created an account"
        assert len(await _rows_for(pool, lease.operation_id)) == 1

    @pytest.mark.asyncio
    async def test_the_key_is_re_derived_identically_by_a_second_executor(self, pool):
        """The key survives losing all process memory.

        A key carrying a uuid or a timestamp would differ per process, so the fence would
        never engage after a restart — it would look present and do nothing.
        """
        lease = await _leased(pool, key="dur-key")
        first = _executor(lease, pool, hook=None)
        second = _executor(lease, pool, hook=None)
        assert creation_key(first) == creation_key(second)
        assert lease.operation_id in creation_key(first)


# ─────────────────────────────────────────────────────────────────────────────────────────
# A superseded worker cannot act
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestAStaleWorkerCannotAct:
    """The fence token, which is the half a Python-level check cannot provide.

    The expensive case is not a stale worker failing — it is a stale worker SUCCEEDING at
    something while its successor is re-making the same call.
    """

    @pytest.mark.asyncio
    async def test_a_worker_whose_lease_was_taken_over_never_reaches_aws(self, pool):
        lease = await _leased(pool, key="dur-fenced", duration=dt.timedelta(milliseconds=1))
        await asyncio.sleep(0.05)
        async with pool.acquire() as connection:
            await sweep_expired_leases(connection)
            async with connection.transaction():
                await acquire(
                    connection,
                    operation_id=lease.operation_id,
                    holder="worker-2",
                    attempt_id="attempt-2",
                )

        organizations = RecordingOrganizations(create_result=succeeded_response())
        credentials, request, stale = _composed(pool, lease, organizations)

        with pytest.raises(ProviderCallRefused):
            await create_account(stale, credentials, request, authorization=matching_authorization(request))

        assert organizations.create_calls == [], (
            "a superseded worker reached AWS. Its successor is entitled to make this call, so this is the duplicate-account path"
        )

    @pytest.mark.asyncio
    async def test_a_second_concurrent_dispatch_is_refused_not_queued(self, pool):
        """The advisory lock is per operation, and refusing beats queueing.

        Queueing would mean making the call again once the first released, which is the
        thing being prevented.
        """
        lease = await _leased(pool, key="dur-lock")
        organizations = RecordingOrganizations(create_result=succeeded_response())
        credentials, request, _ = _composed(pool, lease, organizations)
        inside = asyncio.Event()
        release = asyncio.Event()

        async def blocking(call):
            inside.set()
            await release.wait()
            return AuthoritativeCallOutcome.SUCCEEDED, "created", f"account={FIXTURE_CREATED_ACCOUNT}"

        held = asyncio.create_task(
            _executor(lease, pool, hook=blocking).execute_provider(
                idempotency_key=f"{lease.operation_id}:probe-a",
                provider="aws-organizations",
                operation_kind="create-account",
                target=creation_target(request),
            )
        )
        await inside.wait()
        try:
            with pytest.raises(ProviderCallRefused) as refused:
                await _executor(lease, pool, hook=blocking).execute_provider(
                    idempotency_key=f"{lease.operation_id}:probe-b",
                    provider="aws-organizations",
                    operation_kind="create-account",
                    target=creation_target(request),
                )
            assert "in flight" in str(refused.value)
        finally:
            release.set()
            await held


# ─────────────────────────────────────────────────────────────────────────────────────────
# Outcomes are persisted as what they are
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestOutcomesArePersistedAsWhatTheyAre:
    """The FAILED/UNKNOWN distinction, written to disk and read back."""

    @pytest.mark.asyncio
    async def test_an_established_refusal_is_persisted_as_a_failure(self, pool):
        """AWS said no, so nothing was created and the row says so conclusively."""
        lease = await _leased(pool, key="dur-failed")
        organizations = RecordingOrganizations(create_result=failed_response("EMAIL_ALREADY_EXISTS"))
        credentials, request, executor = _composed(pool, lease, organizations)

        outcome = await create_account(executor, credentials, request, authorization=matching_authorization(request))

        assert outcome.status is CreateAccountStatus.FAILED
        assert not outcome.may_exist
        assert outcome.failure is CreateAccountFailure.EMAIL_ALREADY_EXISTS

        row = await _row(pool, creation_key(executor))
        assert row.stage is CallStage.OBSERVED
        assert row.outcome is AuthoritativeCallOutcome.FAILED
        # The request id is preserved even on an established failure: it is what an operator
        # uses to confirm in the Organizations console that nothing was allocated.
        assert FIXTURE_REQUEST_ID in (row.provider_ref or "")

    @pytest.mark.asyncio
    async def test_a_confirmed_account_is_persisted_with_its_id(self, pool):
        """The success path, read back off disk.

        The account id specifically: losing it is the untracked-account failure, and it is
        the field the `is`-comparison defect described in `execution.py` silently discarded.
        """
        lease = await _leased(pool, key="dur-ok")
        organizations = RecordingOrganizations(create_result=succeeded_response())
        credentials, request, executor = _composed(pool, lease, organizations)

        outcome = await create_account(executor, credentials, request, authorization=matching_authorization(request))

        assert outcome.succeeded
        assert outcome.account_id == FIXTURE_CREATED_ACCOUNT

        row = await _row(pool, creation_key(executor))
        assert row.stage is CallStage.OBSERVED
        assert row.outcome is AuthoritativeCallOutcome.SUCCEEDED
        assert FIXTURE_CREATED_ACCOUNT in (row.provider_ref or ""), (
            "the account id is not on disk. An account exists, is billing, and no durable record names it"
        )
        assert FIXTURE_REQUEST_ID in (row.provider_ref or "")

    @pytest.mark.asyncio
    async def test_the_normal_first_reply_is_never_recorded_as_an_established_failure(self, pool):
        """`IN_PROGRESS` must not settle as `FAILED`, and the row must stay swept.

        The direction of this mistake is the expensive one. `FAILED` means AWS established
        that NOTHING was created, and `CONCURRENT_ACCOUNT_MODIFICATION`-style failures are
        retryable — so a normal `IN_PROGRESS` recorded as a failure both reports "no account"
        while one is opening AND authorizes a further generation, which opens the second.

        Asserted on the durable STAGE, not only on the returned outcome: `observed` is
        terminal and excluded from `unresolved_calls`, so a wrongly-settled row is never
        swept again. That is the difference between a slow recovery and no recovery.

        This test exists because reinjecting exactly this defect left the rest of this class
        green — the success and failure paths both still behaved, and only the reconciliation
        test noticed. A defect that one test catches by side effect is a defect the suite
        does not really assert.
        """
        lease = await _leased(pool, key="dur-inflight-stage")
        organizations = RecordingOrganizations(create_result=in_progress_response())
        credentials, request, executor = _composed(pool, lease, organizations)

        outcome = await create_account(executor, credentials, request, authorization=matching_authorization(request))

        assert outcome.status is not CreateAccountStatus.FAILED, (
            "an accepted, still-opening account creation was reported as an established "
            "failure. AWS said IN_PROGRESS, which is the normal first reply"
        )
        assert outcome.may_exist, "an unconfirmed creation must report that an account may exist"
        assert outcome.failure is None, "no AWS failure reason was returned, so none may be recorded"

        row = await _row(pool, creation_key(executor))
        assert row.outcome is not AuthoritativeCallOutcome.FAILED
        assert row.stage is CallStage.INTENDED, (
            f"the row settled as {row.stage!r}; a terminal stage is excluded from the "
            f"unresolved sweep, so the account AWS is opening would never be reconciled"
        )

    @pytest.mark.asyncio
    async def test_the_recorded_target_is_the_payload_digest_and_leaks_no_address(self, pool):
        lease = await _leased(pool, key="dur-target")
        organizations = RecordingOrganizations(create_result=succeeded_response())
        credentials, request, executor = _composed(pool, lease, organizations)

        await create_account(executor, credentials, request, authorization=matching_authorization(request))

        row = await _row(pool, creation_key(executor))
        assert row.target == creation_target(request)
        assert request.account_email not in row.target, (
            "the contact address is stored in the clear in a database column; the binding is supposed to be a digest"
        )


# ─────────────────────────────────────────────────────────────────────────────────────────
# Retries arm a NEW fence; a changed payload cannot reuse one
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestRetriesAndTheImmutableBinding:
    @pytest.mark.asyncio
    async def test_a_retryable_failure_arms_a_new_fence_and_succeeds(self, pool):
        """A permitted retry gets its OWN key, so it is dispatchable rather than refused.

        Sharing generation 0's key would make every retry permanently undispatchable; a key
        that varied per attempt would make every retry a fresh account. The generation
        suffix is the narrow path between those two.
        """
        lease = await _leased(pool, key="dur-retry")
        organizations = RecordingOrganizations(create_result=failed_response("CONCURRENT_ACCOUNT_MODIFICATION"))
        credentials, request, executor = _composed(pool, lease, organizations)

        first = await create_account(executor, credentials, request, authorization=matching_authorization(request))
        assert first.status is CreateAccountStatus.FAILED
        assert first.failure is CreateAccountFailure.CONCURRENT_ACCOUNT_MODIFICATION
        assert first.failure.retry_can_succeed

        generation_0 = await _row(pool, creation_key(executor, 0))

        organizations.create_result = succeeded_response()
        second = await create_account(
            executor,
            credentials,
            request,
            authorization=matching_authorization(request),
            history=(generation_0,),
        )

        assert second.succeeded
        assert second.account_id == FIXTURE_CREATED_ACCOUNT
        assert len(organizations.create_calls) == 2
        generation_1 = await _row(pool, creation_key(executor, 1))
        assert generation_1 is not None, "the retry did not arm a second fence of its own"
        assert generation_1.outcome is AuthoritativeCallOutcome.SUCCEEDED

    @pytest.mark.asyncio
    async def test_a_succeeded_generation_refuses_any_further_dispatch(self, pool):
        """The account exists. Nothing may call CreateAccount for this operation again."""
        lease = await _leased(pool, key="dur-done")
        organizations = RecordingOrganizations(create_result=succeeded_response())
        credentials, request, executor = _composed(pool, lease, organizations)

        await create_account(executor, credentials, request, authorization=matching_authorization(request))
        settled = await _row(pool, creation_key(executor, 0))

        with pytest.raises(CreationRefused) as refused:
            await create_account(
                executor,
                credentials,
                request,
                authorization=matching_authorization(request),
                history=(settled,),
            )
        assert FIXTURE_CREATED_ACCOUNT in str(refused.value)
        assert len(organizations.create_calls) == 1

    @pytest.mark.asyncio
    async def test_a_changed_payload_cannot_spend_this_operations_approval(self, pool):
        """The approval-substitution case, refused against the row's immutable binding.

        An operation approved to open one account, re-driven for another. Both directions of
        harm are real: the approval was granted for one account and would be spent on
        another, and the account that WAS approved may still be opening.
        """
        lease = await _leased(pool, key="dur-swap")
        organizations = RecordingOrganizations(create_result=in_progress_response())
        credentials, approved, executor = _composed(pool, lease, organizations)

        await create_account(executor, credentials, approved, authorization=matching_authorization(approved))
        committed = await _row(pool, creation_key(executor, 0))
        calls_so_far = len(organizations.create_calls)

        substituted = new_account_request(account_email="someone-else@example.invalid")
        with pytest.raises(CreationRefused) as refused:
            await create_account(
                executor,
                credentials,
                substituted,
                authorization=matching_authorization(substituted),
                history=(committed,),
            )
        assert "different account" in str(refused.value)
        assert len(organizations.create_calls) == calls_so_far, "a second, different account was dispatched under an approval granted for the first"

    @pytest.mark.asyncio
    async def test_a_changed_payload_is_refused_even_when_a_retry_is_otherwise_permitted(self, pool):
        """The case where the payload check is the ONLY thing standing in the way.

        In the unresolved case above, `_plan_generation` refuses on its own, so that test
        passes even with the payload binding removed entirely — verified by reinjection. A
        *retryable failure* is different: the generation logic positively authorizes another
        dispatch, and the recorded target is then the single remaining check between one
        approval and an account it was never granted for.

        This is also the genuinely tempting case to allow. `EMAIL_ALREADY_EXISTS` makes a
        corrected address look like the obvious fix — but `_require_unchanged_payload` refuses
        it unconditionally, because it cannot distinguish that from an unresolved attempt
        without trusting the outcome field. A corrected address is a new approval.
        """
        lease = await _leased(pool, key="dur-swap-retry")
        organizations = RecordingOrganizations(create_result=failed_response("CONCURRENT_ACCOUNT_MODIFICATION"))
        credentials, approved, executor = _composed(pool, lease, organizations)

        first = await create_account(executor, credentials, approved, authorization=matching_authorization(approved))
        assert first.failure is not None and first.failure.retry_can_succeed, (
            "this test needs a history that positively authorizes another generation"
        )
        committed = await _row(pool, creation_key(executor, 0))
        calls_so_far = len(organizations.create_calls)

        # A retry of the SAME payload is permitted here — established by the sibling test —
        # so a refusal below is attributable to the changed payload and nothing else.
        substituted = new_account_request(organizational_unit_id="ou-test-elsewhere1")
        with pytest.raises(CreationRefused) as refused:
            await create_account(
                executor,
                credentials,
                substituted,
                authorization=matching_authorization(substituted),
                history=(committed,),
            )
        assert "different account" in str(refused.value)
        assert len(organizations.create_calls) == calls_so_far, (
            "an approval granted for one account was spent dispatching another, on a history that authorized a retry of the original"
        )


# ─────────────────────────────────────────────────────────────────────────────────────────
# Request handles survive uncertain replies and executor reconstruction
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestTheRequestIdSurvivesTheNormalFirstReply:
    """The normal asynchronous reply must remain recoverable from committed state."""

    @pytest.mark.asyncio
    async def test_an_in_progress_creation_stays_reconcilable(self, pool):
        lease = await _leased(pool, key="dur-inprogress")
        organizations = RecordingOrganizations(create_result=in_progress_response())
        credentials, request, executor = _composed(pool, lease, organizations)

        outcome = await create_account(executor, credentials, request, authorization=matching_authorization(request))

        assert outcome.status is CreateAccountStatus.UNKNOWN
        assert outcome.may_exist
        assert outcome.needs_reconciliation
        assert outcome.create_account_request_id == FIXTURE_REQUEST_ID
        row = await _row(pool, creation_key(executor))
        assert row.stage is CallStage.INTENDED
        assert FIXTURE_REQUEST_ID in row.provider_ref

    @pytest.mark.asyncio
    async def test_reconstructed_executor_recovers_without_another_create(self, pool):
        lease = await _leased(pool, key="dur-restart")
        organizations = RecordingOrganizations(create_result=in_progress_response(), describe_results=[succeeded_response()])
        credentials, request, executor = _composed(pool, lease, organizations)
        await create_account(executor, credentials, request, authorization=matching_authorization(request))
        del executor, credentials

        # Rebuild the executor and credential adapter; read the handle from another
        # connection, with none of the first dispatch's returned objects available.
        credentials, request, restarted = _composed(pool, lease, organizations)
        row = await _row(pool, creation_key(restarted))
        recovered = await reconcile_creation(restarted, credentials, recorded=row)
        assert recovered.succeeded
        assert recovered.account_id == FIXTURE_CREATED_ACCOUNT
        assert recovered.create_account_request_id == FIXTURE_REQUEST_ID
        reads = [kwargs for name, kwargs in organizations.calls if name == "describe_create_account_status"]
        assert reads == [{"CreateAccountRequestId": FIXTURE_REQUEST_ID}]

        # The original intent still fences a repeated dispatch even if the caller
        # omits history; recovery reads cannot authorize a second create.
        with pytest.raises(ProviderCallRefused):
            await create_account(restarted, credentials, request, authorization=matching_authorization(request))
        assert len(organizations.create_calls) == 1
        assert len(await _rows_for(pool, lease.operation_id)) == 1


# ─────────────────────────────────────────────────────────────────────────────────────────
# What reconciliation WRITES, on disk
# ─────────────────────────────────────────────────────────────────────────────────────────


class TestReconciliationSettlesTheRowOnDisk:
    """The half the sibling class above does not establish, and the defect it missed.

    `test_reconstructed_executor_recovers_without_another_create` asserts that the RETURNED
    value is right. It was, and the row was still `intended` — so every restart asked AWS the
    same question again, the operation never converged on the account id, and the row naming a
    real billable account said only "a call was intended". A returned value is not recovery
    until it is on disk.

    Every assertion below reads the row back on a separate connection after the reconciling
    objects are gone, because "it is persisted" is a claim about what another process can see.
    """

    @pytest.mark.asyncio
    async def test_a_confirmed_account_is_reconciled_onto_the_row(self, pool):
        """INTENDED → reconciled/succeeded, with the account id in the reference."""
        lease = await _leased(pool, key="dur-settle-ok")
        organizations = RecordingOrganizations(create_result=in_progress_response(), describe_results=[succeeded_response()])
        credentials, request, executor = _composed(pool, lease, organizations)
        await create_account(executor, credentials, request, authorization=matching_authorization(request))
        key = creation_key(executor)
        assert (await _row(pool, key)).stage is CallStage.INTENDED, "this test needs an unresolved row to reconcile"
        del executor, credentials

        credentials, request, restarted = _composed(pool, lease, organizations)
        store = _PoolReconciliationStore(pool)
        recovered = await reconcile_creation(
            restarted,
            credentials,
            recorded=await _row(pool, key),
            store=store,
            outcomes=AuthoritativeCallOutcome,
            refusal_types=(ProviderCallRefused,),
        )
        assert recovered.succeeded

        settled = await _row(pool, key)
        assert settled.stage is CallStage.RECONCILED, (
            f"the row is {settled.stage.value!r} after a confirmed creation was reconciled. "
            f"An `intended` row means the next pass re-reads AWS forever and the operation "
            f"never converges on the account id"
        )
        assert settled.outcome is AuthoritativeCallOutcome.SUCCEEDED
        assert FIXTURE_CREATED_ACCOUNT in (settled.provider_ref or ""), (
            f"the settled row does not name the account that exists: {settled.provider_ref!r}. "
            f"An account is billing and nothing on disk identifies it"
        )
        assert FIXTURE_REQUEST_ID in (settled.provider_ref or ""), "the handle must remain on the row it settled"

    @pytest.mark.asyncio
    async def test_the_settled_row_survives_another_restart_without_asking_aws_again(self, pool):
        """The convergence claim: the second restart reads disk, not AWS.

        `describe_results` is scripted with exactly ONE answer and the double raises when it
        runs out, so a second `DescribeCreateAccountStatus` fails the test by construction
        rather than by an assertion someone could weaken. That is the property the unpersisted
        version could not have: it re-read AWS on every pass, forever.
        """
        lease = await _leased(pool, key="dur-settle-restart")
        organizations = RecordingOrganizations(create_result=in_progress_response(), describe_results=[succeeded_response()])
        credentials, request, executor = _composed(pool, lease, organizations)
        await create_account(executor, credentials, request, authorization=matching_authorization(request))
        key = creation_key(executor)

        credentials, request, first_pass = _composed(pool, lease, organizations)
        await reconcile_creation(
            first_pass,
            credentials,
            recorded=await _row(pool, key),
            store=_PoolReconciliationStore(pool),
            outcomes=AuthoritativeCallOutcome,
            refusal_types=(ProviderCallRefused,),
        )
        del first_pass, credentials

        # A second restart. Nothing from the first pass is in memory, and the only thing it
        # has to work from is the row.
        row = await _row(pool, key)
        settled = _settled_outcome(row)
        assert settled.succeeded, (
            f"the account id could not be read back off the row after a restart: "
            f"{row.stage.value!r}/{row.provider_ref!r}. This is the convergence the story asks "
            f"for — after recovery, the operation knows which account it created"
        )
        assert settled.account_id == FIXTURE_CREATED_ACCOUNT

        reads = [kwargs for name, kwargs in organizations.calls if name == "describe_create_account_status"]
        assert len(reads) == 1, f"AWS was consulted {len(reads)} times for one settled creation; the row is the authority once it is written"

    @pytest.mark.asyncio
    async def test_the_settled_row_is_no_longer_offered_to_the_sweep(self, pool):
        """`unresolved_calls` selects `intended` only, which is what ends the retry loop.

        The enumerable set is how recovery is driven at all. A row that reconciliation read and
        did not write stays in it permanently: a sweep that re-reads the same creation on every
        pass, for the lifetime of the deployment, and that never finishes converging.
        """
        lease = await _leased(pool, key="dur-settle-sweep")
        organizations = RecordingOrganizations(create_result=in_progress_response(), describe_results=[succeeded_response()])
        credentials, request, executor = _composed(pool, lease, organizations)
        await create_account(executor, credentials, request, authorization=matching_authorization(request))
        key = creation_key(executor)

        async with pool.acquire() as connection:
            before = await unresolved_calls(connection)
        assert [call.idempotency_key for call in before] == [key], "the unresolved row must start in the sweepable set"

        credentials, request, restarted = _composed(pool, lease, organizations)
        await reconcile_creation(
            restarted,
            credentials,
            recorded=await _row(pool, key),
            store=_PoolReconciliationStore(pool),
            outcomes=AuthoritativeCallOutcome,
            refusal_types=(ProviderCallRefused,),
        )

        async with pool.acquire() as connection:
            after = await unresolved_calls(connection)
        assert [call.idempotency_key for call in after] == [], (
            f"the reconciled creation is still offered to the sweep: {[c.idempotency_key for c in after]}. "
            f"Recovery that does not remove its own work from the queue re-does it forever"
        )

    @pytest.mark.asyncio
    async def test_an_established_failure_is_reconciled_and_releases_the_reservation(self, pool):
        """INTENDED → reconciled/failed, and the ledger disposition that follows from it.

        The disposition is asserted because it is the consequence that makes this row's
        correctness matter in money: `disposition_for` releases the reservation for a `FAILED`
        call and retains it for an `UNKNOWN` one. Settling the wrong one of those two either
        leaks a reservation forever or gives budget back for an account that exists.
        """
        lease = await _leased(pool, key="dur-settle-failed")
        organizations = RecordingOrganizations(
            create_result=in_progress_response(),
            describe_results=[failed_response("EMAIL_ALREADY_EXISTS")],
        )
        credentials, request, executor = _composed(pool, lease, organizations)
        await create_account(executor, credentials, request, authorization=matching_authorization(request))
        key = creation_key(executor)

        credentials, request, restarted = _composed(pool, lease, organizations)
        store = _PoolReconciliationStore(pool)
        recovered = await reconcile_creation(
            restarted,
            credentials,
            recorded=await _row(pool, key),
            store=store,
            outcomes=AuthoritativeCallOutcome,
            refusal_types=(ProviderCallRefused,),
        )
        assert recovered.status is CreateAccountStatus.FAILED
        assert recovered.failure is CreateAccountFailure.EMAIL_ALREADY_EXISTS

        settled = await _row(pool, key)
        assert settled.stage is CallStage.RECONCILED
        assert settled.outcome is AuthoritativeCallOutcome.FAILED, (
            f"an established failure settled as {settled.outcome!r}; the reservation is then "
            f"retained forever for an account AWS says was never created"
        )
        assert CreateAccountFailure.EMAIL_ALREADY_EXISTS.value in (settled.provider_ref or ""), (
            "the reason must be on the row: whether a retry can ever succeed is decided entirely by which reason AWS gave"
        )
        assert disposition_for(settled) is BudgetDisposition.RELEASE

    @pytest.mark.asyncio
    async def test_an_unnameable_success_is_parked_unresolved_and_keeps_the_budget(self, pool):
        """`UNKNOWN` → `unresolved`: the state that says a person has to look.

        AWS said SUCCEEDED and did not name the account. Settling that as a success would
        RELEASE the reservation for an account that is billing and that nothing identifies, so
        it is parked instead — out of the automatic sweep, with the budget retained.
        """
        lease = await _leased(pool, key="dur-settle-unnamed")
        organizations = RecordingOrganizations(
            create_result=in_progress_response(),
            describe_results=[succeeded_response(account_id=None)],
        )
        credentials, request, executor = _composed(pool, lease, organizations)
        await create_account(executor, credentials, request, authorization=matching_authorization(request))
        key = creation_key(executor)

        credentials, request, restarted = _composed(pool, lease, organizations)
        recovered = await reconcile_creation(
            restarted,
            credentials,
            recorded=await _row(pool, key),
            store=_PoolReconciliationStore(pool),
            outcomes=AuthoritativeCallOutcome,
            refusal_types=(ProviderCallRefused,),
        )
        assert recovered.may_exist

        settled = await _row(pool, key)
        assert settled.stage is CallStage.UNRESOLVED, (
            f"the row is {settled.stage.value!r}; a success AWS could not name must be parked for a person, not settled either way"
        )
        assert settled.outcome is AuthoritativeCallOutcome.UNKNOWN
        assert FIXTURE_REQUEST_ID in (settled.provider_ref or ""), "the handle is the operator's only starting point"
        assert disposition_for(settled) is BudgetDisposition.RETAIN, "the reservation was released for a creation that may have opened an account"

    @pytest.mark.asyncio
    async def test_a_second_recovery_pass_is_refused_by_the_row_and_reported(self, pool):
        """Two sweeps racing. The loser must report, not crash and not overwrite.

        `reconcile` updates `WHERE stage='intended'`, so the second pass changes no row — which
        is the whole protection for an `unresolved` row that records a decision to involve a
        human. A recovery pass that raised here would abort the sweep, and every row behind the
        contended one would go unreconciled.
        """
        lease = await _leased(pool, key="dur-settle-race")
        organizations = RecordingOrganizations(
            create_result=in_progress_response(),
            describe_results=[succeeded_response(), succeeded_response()],
        )
        credentials, request, executor = _composed(pool, lease, organizations)
        await create_account(executor, credentials, request, authorization=matching_authorization(request))
        key = creation_key(executor)
        recorded = await _row(pool, key)

        credentials, request, restarted = _composed(pool, lease, organizations)
        store = _PoolReconciliationStore(pool)
        first = await reconcile_creation(
            restarted,
            credentials,
            recorded=recorded,
            store=store,
            outcomes=AuthoritativeCallOutcome,
            refusal_types=(ProviderCallRefused,),
        )
        assert first.succeeded
        won = await _row(pool, key)

        # The same stale row, reconciled again — exactly what a second sweep holds.
        second = await reconcile_creation(
            restarted,
            credentials,
            recorded=recorded,
            store=store,
            outcomes=AuthoritativeCallOutcome,
            refusal_types=(ProviderCallRefused,),
        )
        assert second.succeeded, "the loser still reports what AWS said"
        assert "not settled by this pass" in second.detail

        unchanged = await _row(pool, key)
        assert (unchanged.stage, unchanged.outcome, unchanged.provider_ref) == (won.stage, won.outcome, won.provider_ref), (
            "the second recovery pass overwrote a row that was already settled"
        )

    @pytest.mark.asyncio
    async def test_an_in_progress_creation_is_left_in_the_sweep(self, pool):
        """AWS is still working, so the row must stay `intended` and stay sweepable.

        The mirror of the settlement tests, and the reason "persist whatever you read" is the
        wrong rule: parking an in-flight creation takes it out of `unresolved_calls` and makes
        a person finish a job that was going to finish by itself.
        """
        lease = await _leased(pool, key="dur-settle-inflight")
        organizations = RecordingOrganizations(create_result=in_progress_response(), describe_results=[in_progress_response()])
        credentials, request, executor = _composed(pool, lease, organizations)
        await create_account(executor, credentials, request, authorization=matching_authorization(request))
        key = creation_key(executor)

        credentials, request, restarted = _composed(pool, lease, organizations)
        store = _PoolReconciliationStore(pool)
        outcome = await reconcile_creation(
            restarted,
            credentials,
            recorded=await _row(pool, key),
            store=store,
            outcomes=AuthoritativeCallOutcome,
            refusal_types=(ProviderCallRefused,),
        )
        assert outcome.status is CreateAccountStatus.IN_PROGRESS
        assert store.settlements == [], "an in-flight creation was written to the durable row"

        row = await _row(pool, key)
        assert row.stage is CallStage.INTENDED
        async with pool.acquire() as connection:
            assert [call.idempotency_key for call in await unresolved_calls(connection)] == [key], (
                "an in-progress creation was taken out of the sweep, so nothing will ever finish it automatically"
            )


@pytest.mark.asyncio
async def test_registration_loads_only_the_operations_durable_account(pool):
    from account_provisioning.registration import create_from_durable_history, render_created_account

    from .conftest import FIXTURE_ORGANIZATIONAL_UNIT

    lease = await _leased(pool, key="registration-durable")
    organizations = RecordingOrganizations(
        create_result=succeeded_response(), parents={"Parents": [{"Id": FIXTURE_ORGANIZATIONAL_UNIT, "Type": "ORGANIZATIONAL_UNIT"}]}
    )
    credentials, request, executor = _composed(pool, lease, organizations)
    assert (await create_from_durable_history(executor, credentials, request, authorization=matching_authorization(request))).succeeded
    # Reconstruct the service after a restart. No account ID or history crosses
    # the registration entry point; both are loaded by the real leased executor.
    restarted = _executor(lease, pool, hook=None)
    rendered = await render_created_account(restarted, credentials, request, authorization=matching_authorization(request))
    import json

    assert FIXTURE_CREATED_ACCOUNT in json.dumps(rendered.objects)
    assert len(organizations.create_calls) == 1
    other_lease = await _leased(pool, key="other-registration-operation", attempt="other-attempt")
    other = _executor(other_lease, pool, hook=None)
    with pytest.raises(CreationRefused, match="exactly one durable successful"):
        await render_created_account(other, credentials, request, authorization=matching_authorization(request))


@pytest.mark.asyncio
async def test_registration_refuses_changed_payload_and_unplaced_account(pool):
    from dataclasses import replace

    from account_provisioning.registration import create_from_durable_history, load_created_account

    lease = await _leased(pool, key="registration-binding")
    organizations = RecordingOrganizations(create_result=succeeded_response())
    credentials, request, executor = _composed(pool, lease, organizations)
    await create_from_durable_history(executor, credentials, request, authorization=matching_authorization(request))
    with pytest.raises(CreationRefused, match="approved OU"):
        await load_created_account(executor, credentials, request, authorization=matching_authorization(request))
    changed = replace(request, account_email="changed@example.invalid")
    with pytest.raises(CreationRefused, match="changed since the attempt"):
        await load_created_account(executor, credentials, changed, authorization=matching_authorization(changed))
    assert len(organizations.create_calls) == 1


@pytest.mark.asyncio
async def test_registration_refuses_unresolved_or_cancelled_operation(pool):
    from account_provisioning.registration import create_from_durable_history, load_created_account

    lease = await _leased(pool, key="registration-unresolved")
    credentials, request, executor = _composed(pool, lease, RecordingOrganizations(create_result=in_progress_response()))
    await create_from_durable_history(executor, credentials, request, authorization=matching_authorization(request))
    with pytest.raises(CreationRefused, match="unresolved durable history"):
        await load_created_account(executor, credentials, request, authorization=matching_authorization(request))
    await executor.cancel(reason="test cancels registration authority")
    with pytest.raises(ProviderCallRefused, match="Cancelled or terminal"):
        await load_created_account(executor, credentials, request, authorization=matching_authorization(request))


@pytest.mark.asyncio
async def test_creation_resolves_nested_paginated_approved_ou(pool):
    from account_provisioning.registration import create_from_durable_history

    from .conftest import FIXTURE_ORGANIZATIONAL_UNIT

    class NestedOrganizations(RecordingOrganizations):
        def list_roots(self, **kwargs):
            return {"Roots": [], "NextToken": "roots-page-2"} if not kwargs else {"Roots": [{"Id": "r-test"}]}

        def list_organizational_units_for_parent(self, **kwargs):
            if kwargs["ParentId"] == "r-test":
                return {"OrganizationalUnits": [{"Id": "ou-parent-fixture"}]}
            if not kwargs.get("NextToken"):
                return {"OrganizationalUnits": [], "NextToken": "children-page-2"}
            return {"OrganizationalUnits": [{"Id": FIXTURE_ORGANIZATIONAL_UNIT}]}

    lease = await _leased(pool, key="nested-ou-create")
    credentials, request, executor = _composed(pool, lease, NestedOrganizations(create_result=succeeded_response()))
    assert (await create_from_durable_history(executor, credentials, request, authorization=matching_authorization(request))).succeeded


@pytest.mark.asyncio
async def test_retry_hook_requests_only_the_original_operations_credentials(pool):
    from account_provisioning.registration import create_from_durable_history

    lease = await _leased(pool, key="retry-credential-binding")
    organizations = RecordingOrganizations(create_result=failed_response("CONCURRENT_ACCOUNT_MODIFICATION"))
    request = new_account_request()

    class BoundCredentials(RecordingCredentials):
        async def management(self, *, operation_id):
            assert operation_id == lease.operation_id, "retry key is not a credential operation identity"
            return await super().management(operation_id=operation_id)

    credentials = BoundCredentials(organizations=organizations)
    executor = _executor(lease, pool, hook=creation_hook(credentials, request, outcomes=AuthoritativeCallOutcome))
    first = await create_from_durable_history(executor, credentials, request, authorization=matching_authorization(request))
    assert first.status is CreateAccountStatus.FAILED
    organizations.create_result = succeeded_response()
    second = await create_from_durable_history(executor, credentials, request, authorization=matching_authorization(request))
    assert second.succeeded
    assert len(organizations.create_calls) == 2
    assert set(credentials.management_calls) == {lease.operation_id}
