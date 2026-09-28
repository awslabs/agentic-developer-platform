"""One approval cannot be spent on a different account — Issue #5531 (w6-08).

## The defect these tests exist to prevent

`account_factory.creation._idempotency_key` derives the fence from four fields — the
organization, the OU, the workspace and the contact address. Three of those four are
caller-mutable, which means **the fence is defeated by editing the request**.

Concretely: a first attempt commits under the key derived from
`team@example.com`/`ou-sandbox`. The answer is lost, so an account may be opening. The
caller edits one character of the email and re-drives. `AttemptLedger.find` derives a
*different* key, finds no record, and `assess_attempt` returns `CREATE_PERMITTED`. A second
`CreateAccount` goes out while the first account is still being opened. Two billable
accounts for one workspace, one of them in no record — and the fence never engaged, because
it was asked about the wrong key.

## The fix, and why it needs both halves

The fence narrows to `creation_key` — the operation id and a constant step, containing
nothing a caller can vary — so the same operation reaches the same key however the body is
edited. That alone would open the mirror-image hole: an approval granted for
`team-a@example.com` in `ou-sandbox` could be re-driven for `team-b@example.com` in
`ou-production`, land on the same key, and be treated as a duplicate delivery of work
already claimed.

So the approved payload travels as a digest in the store's `target`, which `harness_jobs`
treats as part of the immutable binding — it refuses to re-record a key whose `provider`,
`operation_kind` or `target` differs from the stored row. `_require_unchanged_payload` checks
it here as well, so the refusal happens before a credential is obtained rather than after.

Both halves are tested below: the narrow fence (`TestTheFenceIgnoresRequestEdits`) and the
payload binding (`TestAChangedPayloadIsRefused`).

## Two crashes found while building this

Both are in code the reviewed revision shipped, both on the recovery path, and both are
covered by `TestRecoveryPathsThatUsedToCrash`:

* `_ledger_for` passed `provider_ref` — this module's own `request=car-x account=...`
  encoding — straight into `CreateAccountAttempt.create_account_request_id`, which validates
  against `^car-[0-9a-zA-Z]{8,64}$`. Every restart that found a recorded row raised
  `AccountCreationError` instead of answering "was a call already made?".
* `creation_disposition` called `retry_can_succeed`, which is a `@property`, raising
  `TypeError: 'bool' object is not callable` for every established failure.

No AWS is contacted anywhere in this file.
"""

from __future__ import annotations

import pytest
from harness_jobs.execution import CallOutcome as AuthoritativeCallOutcome

from account_factory.creation import (
    AttemptDisposition,
    CreateAccountFailure,
    CreateAccountStatus,
)
from account_provisioning.creation_runner import (
    AccountCreationOutcome,
    CreationRefused,
    _ledger_for,
    create_account,
    creation_disposition,
    creation_key,
    creation_target,
    encode_reference,
    payload_digest,
)

from .conftest import (
    FIXTURE_CREATED_ACCOUNT,
    FIXTURE_ORGANIZATIONAL_UNIT,
    FIXTURE_REQUEST_ID,
    RecordingCredentials,
    RecordingExecutor,
    RecordingOrganizations,
    SettledCall,
    matching_authorization,
    new_account_request,
)


async def _drive(executor: RecordingExecutor, request, *, history=(), organizational_units=None):
    """Run the real entry point with a matching authorization and recording doubles.

    `organizational_units` exists because `_confirm_placement` genuinely reads the tree and
    refuses an OU it cannot find — correctly, since an unconfirmed placement means the
    organization root. A test that edits the placement therefore has to make the new OU
    exist, or it measures the placement check rather than the thing it is about.
    """
    credentials = RecordingCredentials(organizations=RecordingOrganizations(organizational_units=organizational_units))
    return (
        await create_account(
            executor,
            credentials,
            request,
            authorization=matching_authorization(request),
            history=history,
        ),
        credentials,
    )


class TestTheFenceIgnoresRequestEdits:
    """The key is the operation, so no edit to the body can move it."""

    @pytest.mark.parametrize(
        "edit",
        [
            {"account_email": "edited-address@example.invalid"},
            {"organizational_unit_id": "ou-test-elsewhere9"},
            {"vpc_cidr": "10.99.0.0/16"},
            {"node_instance_type": "m6i.xlarge"},
        ],
        ids=["amended-email", "amended-placement", "corrected-cidr", "changed-instance-type"],
    )
    @pytest.mark.asyncio
    async def test_no_request_edit_changes_the_dispatched_fence(self, edit: dict) -> None:
        """The reproduction, measured where it matters: the key actually dispatched under.

        `amended-email` and `amended-placement` are the two that defeated the old
        derived-from-fields key — each moved `_idempotency_key`, so the lookup missed the
        prior attempt and a second `CreateAccount` went out. The other two are here to show
        the narrowing did not overshoot: a corrected CIDR must not look like a different
        account either.

        Asserted against the executor's recorded dispatch rather than against `creation_key`
        in isolation, because what protects an account is the key that reaches the store.
        """
        # Every OU either request might name, so `_confirm_placement` passes in both runs.
        units = [{"Id": FIXTURE_ORGANIZATIONAL_UNIT}, {"Id": "ou-test-elsewhere9"}]

        baseline_executor = RecordingExecutor()
        await _drive(baseline_executor, new_account_request(), organizational_units=units)
        baseline_key = baseline_executor.dispatches[0]["idempotency_key"]

        edited_executor = RecordingExecutor()
        await _drive(edited_executor, new_account_request(**edit), organizational_units=units)

        assert edited_executor.dispatches[0]["idempotency_key"] == baseline_key, (
            f"editing {sorted(edit)} moved the fence, which is how a second account gets opened"
        )
        # And it is the operation's own key, containing nothing derived from the request.
        assert baseline_key == f"{baseline_executor.operation_id}:organizations-create-account"
        assert creation_key(baseline_executor) == baseline_key

    def test_two_operations_never_share_a_fence(self) -> None:
        """The narrowing must not overshoot into one global key.

        A constant key would fence every workspace in the platform behind one account
        creation, which is an availability failure rather than a safety one — but it would
        also make the payload digest the only thing distinguishing two legitimate requests.
        """
        first = creation_key(RecordingExecutor(operation_id="op-one"))
        second = creation_key(RecordingExecutor(operation_id="op-two"))
        assert first != second

    @pytest.mark.asyncio
    async def test_an_edited_email_no_longer_slips_past_an_unresolved_attempt(self) -> None:
        """End to end: the old duplicate-account path, now refused.

        A prior attempt exists whose outcome was never established — an account may be
        opening. The request comes back with one character of the email changed. Under the
        old derived key this found no record and dispatched a second `CreateAccount`.
        """
        original = new_account_request()
        executor = RecordingExecutor()
        recorded = SettledCall(
            creation_key(executor),
            AuthoritativeCallOutcome.UNKNOWN,
            encode_reference(request_id=FIXTURE_REQUEST_ID),
            creation_target(original),
        )

        amended = new_account_request(account_email="edited-address@example.invalid")

        with pytest.raises(CreationRefused) as refusal:
            await _drive(executor, amended, history=(recorded,))

        message = str(refusal.value)
        assert "contact address has changed" in message
        assert executor.dispatches == [], "an amended request dispatched a second CreateAccount while the first was unresolved"

    @pytest.mark.asyncio
    async def test_the_same_payload_resuming_the_same_operation_is_not_refused_by_the_digest(self) -> None:
        """The positive control the digest must not break.

        A genuine resume presents the identical payload, so it must pass the payload check
        and be refused — if at all — for the *state of the attempt* instead. The distinction
        is what an operator does next: "the payload changed" means open a new operation, while
        "the prior attempt is unresolved" means reconcile the recorded request id, and telling
        someone to open a new operation when an account may already be opening is how the
        second account gets created.
        """
        request = new_account_request()
        executor = RecordingExecutor()
        recorded = SettledCall(
            creation_key(executor),
            AuthoritativeCallOutcome.UNKNOWN,
            encode_reference(request_id=FIXTURE_REQUEST_ID),
            creation_target(request),
        )

        with pytest.raises(CreationRefused) as refusal:
            await _drive(executor, request, history=(recorded,))

        message = str(refusal.value)
        assert "has changed" not in message, f"an unchanged payload was refused as changed: {message}"
        assert "unresolved" in message, f"the refusal did not name the unresolved attempt: {message}"
        assert FIXTURE_REQUEST_ID in message, f"the refusal must hand the operator the request id to reconcile, not just a verdict: {message}"
        assert executor.dispatches == []


class TestAChangedPayloadIsRefused:
    """The digest rides in the immutable binding, so a changed approval cannot reuse it."""

    @pytest.mark.parametrize(
        "edit",
        [
            {"account_email": "someone-else@example.invalid"},
            {"organizational_unit_id": "ou-test-production"},
            {"organization_id": "o-otherorg999"},
            {"workspace_id": "ws-other"},
        ],
        ids=["different-address", "different-placement", "different-organization", "different-workspace"],
    )
    def test_each_approval_bound_field_changes_the_digest(self, edit: dict) -> None:
        """All four, because any one of them changes WHICH account is being opened."""
        assert payload_digest(new_account_request()) != payload_digest(new_account_request(**edit))

    @pytest.mark.parametrize(
        "edit",
        [
            {"vpc_cidr": "10.99.0.0/16"},
            {"node_instance_type": "m6i.xlarge"},
            {"cluster_version": "1.32"},
        ],
        ids=["corrected-cidr", "changed-instance-type", "upgraded-cluster-version"],
    )
    def test_the_cluster_inputs_do_not_change_the_digest(self, edit: dict) -> None:
        """Deliberate exclusion, and the reason is a duplicate account.

        These describe what gets built inside the account. If they entered the digest, an
        operator correcting a CIDR after a failed build would be told the approval no longer
        matches and would open a fresh operation — which is exactly how a second account gets
        created for one workspace.
        """
        assert payload_digest(new_account_request()) == payload_digest(new_account_request(**edit))

    def test_the_address_is_compared_case_insensitively_and_untrimmed(self) -> None:
        """Email case and surrounding whitespace do not make a different account.

        AWS treats the address case-insensitively, so `Team@Example.invalid` and
        `team@example.invalid` are the same account to Organizations. Treating them as
        different approvals would refuse a legitimate resume over a capital letter.
        """
        canonical = payload_digest(new_account_request(account_email="team@example.invalid"))
        assert payload_digest(new_account_request(account_email="  Team@Example.invalid  ")) == canonical

    def test_the_digest_does_not_leak_the_contact_address(self) -> None:
        """It lands in a database column and in audit lines, so it must not be readable.

        A joined-values key would carry a real person's address into the `target` of every
        row and every log line that mentions the operation.
        """
        address = "a-real-person@example.invalid"
        digest = payload_digest(new_account_request(account_email=address))
        assert address not in digest
        assert "example.invalid" not in digest
        assert digest.startswith("apd-")

    @pytest.mark.asyncio
    async def test_a_recorded_row_with_no_target_is_refused_rather_than_trusted(self) -> None:
        """A row exists but its binding is unreadable: the conservative branch.

        A call may have been made and this code cannot establish for which payload. Allowing
        the dispatch would be deciding the safe thing on no evidence.
        """
        request = new_account_request()
        executor = RecordingExecutor()
        recorded = SettledCall(creation_key(executor), None, None, "")

        with pytest.raises(CreationRefused) as refusal:
            await _drive(executor, request, history=(recorded,))

        assert "carries no recorded target" in str(refusal.value)
        assert executor.dispatches == []

    @pytest.mark.asyncio
    async def test_the_dispatched_target_carries_the_digest(self) -> None:
        """Without this the whole scheme is inert: the store needs the digest to fence on.

        Asserting on the actual dispatch rather than on `creation_target` alone, because the
        binding only protects anything if the value that reaches `execute_provider` is the
        one that gets stored.
        """
        request = new_account_request()
        executor = RecordingExecutor(
            outcome=AuthoritativeCallOutcome.SUCCEEDED,
            provider_ref=encode_reference(request_id=FIXTURE_REQUEST_ID, account_id=FIXTURE_CREATED_ACCOUNT),
        )

        outcome, _ = await _drive(executor, request)

        assert outcome.succeeded
        assert len(executor.dispatches) == 1
        dispatched = executor.dispatches[0]["target"]
        assert dispatched == creation_target(request)
        assert payload_digest(request) in dispatched
        # The account name stays legible in the row, so an operator reading the store can
        # tell which account it is about without recomputing a hash.
        assert dispatched.startswith("adp-")


class TestRecoveryPathsThatUsedToCrash:
    """Two shipped crashes, both on the path that decides whether a retry is safe."""

    def test_a_recorded_success_rebuilds_the_attempt_instead_of_raising(self) -> None:
        """`_ledger_for` used to raise on every row it was given.

        It passed the whole `request=... account=...` reference into a field validated as an
        AWS status id. The result was `AccountCreationError` in precisely the state the
        function exists to handle — a restart that found evidence of a prior call.
        """
        request = new_account_request()
        recorded = SettledCall(
            "op-fixture:organizations-create-account",
            AuthoritativeCallOutcome.SUCCEEDED,
            encode_reference(request_id=FIXTURE_REQUEST_ID, account_id=FIXTURE_CREATED_ACCOUNT),
            creation_target(request),
        )

        ledger = _ledger_for(request, recorded)
        prior = ledger.find(request)

        assert prior is not None, "a recorded success produced no prior attempt, so a retry would be permitted"
        assert prior.status is CreateAccountStatus.SUCCEEDED
        assert prior.account_id == FIXTURE_CREATED_ACCOUNT
        assert prior.create_account_request_id == FIXTURE_REQUEST_ID
        assert prior.is_conclusive

    def test_a_recorded_failure_rebuilds_with_its_reason(self) -> None:
        """The reason has to survive: it alone decides whether a retry could ever succeed."""
        request = new_account_request()
        recorded = SettledCall(
            "op-fixture:organizations-create-account",
            AuthoritativeCallOutcome.FAILED,
            encode_reference(request_id=FIXTURE_REQUEST_ID, failure=CreateAccountFailure.EMAIL_ALREADY_EXISTS),
            creation_target(request),
        )

        prior = _ledger_for(request, recorded).find(request)

        assert prior is not None
        assert prior.status is CreateAccountStatus.FAILED
        assert prior.failure is CreateAccountFailure.EMAIL_ALREADY_EXISTS
        assert prior.account_id is None

    def test_a_success_with_no_account_id_becomes_unresolved_not_a_crash(self) -> None:
        """The worst state, and it must not be expressible as a conclusive success.

        `CreateAccountAttempt` refuses to hold "succeeded with no id" — correctly, since such
        a record is an assertion rather than an observation. So it is recorded as unresolved,
        which authorizes no retry and routes to reconciliation instead.
        """
        request = new_account_request()
        recorded = SettledCall(
            "op-fixture:organizations-create-account",
            AuthoritativeCallOutcome.SUCCEEDED,
            encode_reference(request_id=FIXTURE_REQUEST_ID),
            creation_target(request),
        )

        prior = _ledger_for(request, recorded).find(request)

        assert prior is not None
        assert prior.status is CreateAccountStatus.UNKNOWN
        assert not prior.is_conclusive

    def test_a_failure_with_no_recorded_reason_becomes_unresolved(self) -> None:
        """An unnamed failure establishes nothing, so it must not read as "no account"."""
        request = new_account_request()
        recorded = SettledCall(
            "op-fixture:organizations-create-account",
            AuthoritativeCallOutcome.FAILED,
            encode_reference(request_id=FIXTURE_REQUEST_ID),
            creation_target(request),
        )

        prior = _ledger_for(request, recorded).find(request)

        assert prior is not None
        assert prior.status is CreateAccountStatus.UNKNOWN
        assert prior.failure is None

    def test_an_absent_row_leaves_the_ledger_empty(self) -> None:
        """`ABSENT` means the provider positively reported nothing under this key."""
        request = new_account_request()
        recorded = SettledCall(
            "op-fixture:organizations-create-account",
            AuthoritativeCallOutcome.ABSENT,
            None,
            creation_target(request),
        )

        assert _ledger_for(request, recorded).find(request) is None

    @pytest.mark.parametrize(
        ("failure", "expected"),
        [
            (CreateAccountFailure.EMAIL_ALREADY_EXISTS, AttemptDisposition.REFUSED_INPUT_CANNOT_SUCCEED),
            (CreateAccountFailure.ACCOUNT_LIMIT_EXCEEDED, AttemptDisposition.REFUSED_INPUT_CANNOT_SUCCEED),
            (CreateAccountFailure.INVALID_EMAIL, AttemptDisposition.REFUSED_INPUT_CANNOT_SUCCEED),
            (CreateAccountFailure.CONCURRENT_ACCOUNT_MODIFICATION, AttemptDisposition.CREATE_PERMITTED),
            (CreateAccountFailure.INTERNAL_FAILURE, AttemptDisposition.CREATE_PERMITTED),
        ],
    )
    def test_a_failed_outcome_maps_to_a_disposition_without_raising(self, failure, expected) -> None:
        """`creation_disposition` called a property, so every failure raised `TypeError`.

        Parametrized over all five reasons rather than one, because the branch the crash hid
        is the one that separates "repeating this can never work" from "this was transient" —
        and getting that backwards either wastes attempts or refuses a recoverable request
        forever.
        """
        outcome = AccountCreationOutcome(
            status=CreateAccountStatus.FAILED,
            create_account_request_id=FIXTURE_REQUEST_ID,
            account_id=None,
            failure=failure,
            detail="",
        )

        assert creation_disposition(outcome) is expected

    def test_an_unresolved_outcome_is_never_retry_permitted(self) -> None:
        """The safety property the disposition mapping exists for."""
        outcome = AccountCreationOutcome(
            status=CreateAccountStatus.UNKNOWN,
            create_account_request_id=FIXTURE_REQUEST_ID,
            account_id=None,
            failure=None,
            detail="",
        )

        assert creation_disposition(outcome) is AttemptDisposition.UNRESOLVED
