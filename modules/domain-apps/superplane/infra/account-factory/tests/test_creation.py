"""Duplicate accounts are impossible and unknown is never read as failure — #5531 (w6-08).

Every test here answers one question: given what is known about a previous attempt, may
CreateAccount be called again? The expensive wrong answers are "yes" when the first attempt
actually succeeded (a second paid AWS account, removable only by a 90-day irreversible
suspension) and "no, it failed" when the outcome was merely unreadable (a real account nobody
is tracking).

## What these tests deliberately do NOT establish

Nothing here calls AWS Organizations, so none of it is evidence that a real `CreateAccount`
behaves as modelled, that a created account lands in the intended OU, or that any account was
ever opened or closed. The AWS answers are constructed by the tests. These establish the
DECISION rule only; live account creation is separately authorized (AC-02) and unproven here.
"""

from __future__ import annotations

import dataclasses

import pytest
from account_factory.creation import (
    INCONCLUSIVE_STATUSES,
    AccountCreationError,
    AttemptDisposition,
    AttemptLedger,
    CreateAccountAttempt,
    CreateAccountFailure,
    CreateAccountObservation,
    CreateAccountStatus,
    assess_attempt,
    intended_attempt,
)
from account_factory.modes import OwnershipMode

from .conftest import BUILDERS, matching_authorization, new_account_request

REQUEST_ID = "car-fixture0000001"
OTHER_REQUEST_ID = "car-fixture0000002"
CREATED_ACCOUNT = "000000000777"


@pytest.fixture
def request_():
    return new_account_request()


@pytest.fixture
def ledger():
    return AttemptLedger()


def _accepted(attempt: CreateAccountAttempt) -> CreateAccountAttempt:
    """An attempt AWS has acknowledged with a request id, still in progress."""
    return dataclasses.replace(attempt, create_account_request_id=REQUEST_ID)


# ── The attempt is recorded before the call, not after ───────────────────────────────


def test_a_fresh_attempt_has_no_request_id_because_aws_has_not_answered(request_):
    """The record must be writable BEFORE the call — that is the entire recovery mechanism.

    A record that required the provider's request id could only be written after the call,
    which is precisely the case it needs to cover: the call happened and the answer was lost.
    """
    attempt = intended_attempt(request_)
    assert attempt.create_account_request_id is None
    assert attempt.status is CreateAccountStatus.IN_PROGRESS
    assert attempt.account_id is None
    assert not attempt.is_conclusive


def test_the_attempt_records_which_account_it_meant_to_create(request_):
    attempt = intended_attempt(request_)
    assert attempt.workspace_id == request_.workspace_id
    assert attempt.organization_id == request_.organization_id
    assert attempt.organizational_unit_id == request_.organizational_unit_id
    assert attempt.account_email == request_.account_email


def test_observing_returns_a_copy_and_never_rewrites_the_recorded_attempt(request_):
    """The pre-call record is what a post-crash reconciliation finds; it stays as written."""
    attempt = _accepted(intended_attempt(request_))
    updated = attempt.observed(
        CreateAccountObservation(
            status=CreateAccountStatus.SUCCEEDED,
            create_account_request_id=REQUEST_ID,
            account_id=CREATED_ACCOUNT,
        )
    )
    assert updated.account_id == CREATED_ACCOUNT
    assert attempt.account_id is None, "the original record was mutated"
    assert attempt.status is CreateAccountStatus.IN_PROGRESS


def test_an_unreadable_answer_keeps_the_recorded_request_id(request_):
    """The id is the only handle for asking about the outcome later.

    An UNKNOWN identifies no request, because AWS was never reached. Overwriting the recorded
    id with `None` on exactly the read that failed would destroy the means of ever resolving
    the attempt, so it is kept.
    """
    attempt = _accepted(intended_attempt(request_))
    updated = attempt.observed(CreateAccountObservation.unreadable("connection reset"))
    assert updated.create_account_request_id == REQUEST_ID
    assert updated.status is CreateAccountStatus.UNKNOWN
    assert not updated.is_conclusive


def test_an_observation_about_another_request_is_refused_not_adopted(request_):
    """A truthful answer about a DIFFERENT attempt would attach another account's outcome."""
    attempt = _accepted(intended_attempt(request_))
    with pytest.raises(AccountCreationError, match="another attempt's outcome"):
        attempt.observed(
            CreateAccountObservation(
                status=CreateAccountStatus.SUCCEEDED,
                create_account_request_id=OTHER_REQUEST_ID,
                account_id=CREATED_ACCOUNT,
            )
        )


# ── Creating an account is a named mode, never a side effect ─────────────────────────


@pytest.mark.parametrize(
    "mode",
    [OwnershipMode.EXISTING_ACCOUNT_MANAGED, OwnershipMode.BRING_EXISTING_CLUSTER],
)
def test_a_mode_that_adopts_an_account_cannot_produce_a_creation_attempt(mode):
    """Design item 3: onboarding an existing account must never open a new one.

    The request reaching here is the only thing between an onboarding flow and an unexpected
    AWS bill, so a non-creating mode is refused rather than quietly treated as creating.
    """
    request = BUILDERS[mode]()
    with pytest.raises(AccountCreationError, match="never a side effect"):
        intended_attempt(request)
    with pytest.raises(AccountCreationError, match="only new-account-managed"):
        assess_attempt(request, AttemptLedger())


def test_an_invalid_request_cannot_have_an_attempt_recorded_for_it():
    """Otherwise the persisted attempt later reads as evidence ADP legitimately tried."""
    request = new_account_request(workspace_id="kube-system")
    with pytest.raises(AccountCreationError, match="does not validate"):
        intended_attempt(request)


def test_an_unauthorized_request_cannot_have_an_attempt_recorded_for_it(request_):
    authorization = matching_authorization(request_, workspace_id="ws-somebody-else")
    with pytest.raises(AccountCreationError, match="does not validate"):
        intended_attempt(request_, authorization)


# ── No prior attempt: creating is a first attempt ────────────────────────────────────


def test_with_no_prior_attempt_creating_is_permitted(request_, ledger):
    decision = assess_attempt(request_, ledger)
    assert decision.disposition is AttemptDisposition.CREATE_PERMITTED
    assert not decision.may_create_account
    assert decision.requires_durable_attempt
    assert decision.attempt is None
    assert not decision.account_unaccounted_for


# ── The duplicate-account fence ──────────────────────────────────────────────────────


def test_a_succeeded_attempt_blocks_creation_and_names_the_account_to_adopt(
    request_, ledger
):
    """The branch whose absence is the duplicate-account bug."""
    ledger.record(
        dataclasses.replace(
            _accepted(intended_attempt(request_)),
            status=CreateAccountStatus.SUCCEEDED,
            account_id=CREATED_ACCOUNT,
        )
    )
    decision = assess_attempt(request_, ledger)
    assert decision.disposition is AttemptDisposition.ALREADY_CREATED
    assert not decision.may_create_account
    assert CREATED_ACCOUNT in decision.reason
    # An account that IS recorded is not unaccounted for, however unwelcome it is.
    assert not decision.account_unaccounted_for


def test_a_replay_of_the_same_request_is_recognised_as_the_same_attempt(
    request_, ledger
):
    """A second call with the same inputs must find the first attempt, not start a new one."""
    ledger.record(_accepted(intended_attempt(request_)))
    replay = new_account_request()
    assert ledger.find(replay) is not None
    assert not assess_attempt(replay, ledger).may_create_account


def test_the_key_ignores_cluster_shape_so_a_corrected_cidr_is_not_a_new_account(
    request_, ledger
):
    """Fixing a VPC CIDR must not make the retry look like a different account request.

    If it did, the corrected retry would sail past the fence and open a second account — the
    exact failure the key exists to prevent.
    """
    ledger.record(_accepted(intended_attempt(request_)))
    corrected = dataclasses.replace(request_, vpc_cidr="10.99.0.0/16")
    assert ledger.find(corrected) is not None
    assert not assess_attempt(corrected, ledger).may_create_account


def test_a_genuinely_different_workspace_is_not_fenced(request_, ledger):
    ledger.record(_accepted(intended_attempt(request_)))
    other = dataclasses.replace(
        request_,
        workspace_id="ws-second",
        account_email="second-workspace@example.invalid",
    )
    assert ledger.find(other) is None
    assert not assess_attempt(other, ledger).may_create_account


def test_a_different_placement_cannot_evade_a_workspace_attempt(request_, ledger):
    """The OU is part of WHICH account is being created, so it belongs in the key."""
    ledger.record(_accepted(intended_attempt(request_)))
    moved = dataclasses.replace(request_, organizational_unit_id="ou-abcd-12345678")
    with pytest.raises(AccountCreationError, match="different approved payload"):
        ledger.find(moved)


def test_the_email_comparison_ignores_case_and_surrounding_space(request_, ledger):
    """`Ops@x` and `ops@x ` are the same AWS root user, so they must be the same attempt."""
    ledger.record(_accepted(intended_attempt(request_)))
    respelled = dataclasses.replace(
        request_, account_email=f"  {request_.account_email.upper()}  "
    )
    assert ledger.find(respelled) is not None


def test_the_ledger_refuses_to_overwrite_a_succeeded_attempt(request_, ledger):
    """That record is the only pointer to a real, paid-for account."""
    succeeded = dataclasses.replace(
        _accepted(intended_attempt(request_)),
        status=CreateAccountStatus.SUCCEEDED,
        account_id=CREATED_ACCOUNT,
    )
    ledger.record(succeeded)
    with pytest.raises(AccountCreationError, match="paid for and untracked"):
        ledger.record(
            dataclasses.replace(
                succeeded,
                account_id="000000000888",
                create_account_request_id=REQUEST_ID,
            )
        )


# ── A retry requires that the prior status was actually read ─────────────────────────


def test_an_in_flight_attempt_with_no_fresh_answer_is_unresolved_not_retryable(
    request_, ledger
):
    """The refusal that matters: no AWS answer must not default to permitting the call."""
    ledger.record(_accepted(intended_attempt(request_)))
    decision = assess_attempt(request_, ledger)
    assert decision.disposition is AttemptDisposition.UNRESOLVED
    assert not decision.may_create_account
    assert decision.account_unaccounted_for


def test_aws_reporting_still_in_progress_permits_neither_retry_nor_release(
    request_, ledger
):
    ledger.record(_accepted(intended_attempt(request_)))
    decision = assess_attempt(
        request_,
        ledger,
        CreateAccountObservation(
            status=CreateAccountStatus.IN_PROGRESS,
            create_account_request_id=REQUEST_ID,
        ),
    )
    assert decision.disposition is AttemptDisposition.IN_FLIGHT
    assert not decision.may_create_account
    assert "read the status again" in decision.reason


def test_an_unreadable_answer_is_unresolved_and_not_a_failure(request_, ledger):
    """The lost-response case. Reading it as failure is how a real account gets orphaned."""
    ledger.record(_accepted(intended_attempt(request_)))
    decision = assess_attempt(
        request_,
        ledger,
        CreateAccountObservation.unreadable(
            "request timed out after the call was sent"
        ),
    )
    assert decision.disposition is AttemptDisposition.UNRESOLVED
    assert not decision.may_create_account
    assert decision.account_unaccounted_for
    assert "not a failure" in decision.reason
    assert "timed out" in decision.reason


def test_a_throttled_answer_that_hides_whether_the_call_landed_blocks_retry(
    request_, ledger
):
    """Throttling is the commonest way the outcome is unknown, and backoff-retry is the trap."""
    ledger.record(_accepted(intended_attempt(request_)))
    decision = assess_attempt(
        request_,
        ledger,
        CreateAccountObservation.unreadable(
            "throttled by Organizations; unclear whether the request was accepted"
        ),
    )
    assert not decision.may_create_account
    assert decision.account_unaccounted_for


def test_an_aws_confirmed_failure_is_the_one_case_a_retry_is_permitted(
    request_, ledger
):
    """Provider-confirmed "nothing was created" is what makes repeating safe."""
    ledger.record(_accepted(intended_attempt(request_)))
    decision = assess_attempt(
        request_,
        ledger,
        CreateAccountObservation(
            status=CreateAccountStatus.FAILED,
            create_account_request_id=REQUEST_ID,
            failure=CreateAccountFailure.INTERNAL_FAILURE,
        ),
    )
    assert decision.disposition is AttemptDisposition.RETRY_PERMITTED
    assert not decision.may_create_account
    assert decision.requires_durable_attempt
    assert "created no account" in decision.reason


def test_a_lost_answer_that_later_resolves_to_success_becomes_already_created(
    request_, ledger
):
    """The full recovery path: unresolved, then read again, and the fence engages."""
    ledger.record(_accepted(intended_attempt(request_)))
    unresolved = assess_attempt(
        request_, ledger, CreateAccountObservation.unreadable("connection reset")
    )
    assert not unresolved.may_create_account

    resolved = assess_attempt(
        request_,
        ledger,
        CreateAccountObservation(
            status=CreateAccountStatus.SUCCEEDED,
            create_account_request_id=REQUEST_ID,
            account_id=CREATED_ACCOUNT,
        ),
    )
    assert resolved.disposition is AttemptDisposition.ALREADY_CREATED
    assert not resolved.may_create_account
    # Persisting the resolved attempt makes the fence durable without a further AWS read.
    ledger.record(resolved.attempt)
    assert (
        assess_attempt(request_, ledger).disposition
        is AttemptDisposition.ALREADY_CREATED
    )


def test_a_settled_outcome_is_not_re_decided(request_, ledger):
    """Re-opening a succeeded attempt is how an account gets created twice."""
    ledger.record(
        dataclasses.replace(
            _accepted(intended_attempt(request_)),
            status=CreateAccountStatus.SUCCEEDED,
            account_id=CREATED_ACCOUNT,
        )
    )
    with pytest.raises(AccountCreationError, match="created twice"):
        assess_attempt(
            request_,
            ledger,
            CreateAccountObservation(
                status=CreateAccountStatus.FAILED,
                create_account_request_id=REQUEST_ID,
                failure=CreateAccountFailure.INTERNAL_FAILURE,
            ),
        )


def test_an_answer_with_no_recorded_attempt_is_unresolved(request_, ledger):
    """The pre-call record is missing, so whether a call already ran cannot be established."""
    decision = assess_attempt(
        request_,
        ledger,
        CreateAccountObservation(
            status=CreateAccountStatus.SUCCEEDED,
            create_account_request_id=REQUEST_ID,
            account_id=CREATED_ACCOUNT,
        ),
    )
    assert decision.disposition is AttemptDisposition.UNRESOLVED
    assert not decision.may_create_account
    assert "pre-call record is missing" in decision.reason


# ── Failures a retry cannot fix are separated from failures it can ───────────────────


@pytest.mark.parametrize(
    "failure",
    [
        CreateAccountFailure.EMAIL_ALREADY_EXISTS,
        CreateAccountFailure.ACCOUNT_LIMIT_EXCEEDED,
        CreateAccountFailure.INVALID_EMAIL,
    ],
)
def test_a_failure_the_same_request_can_never_fix_does_not_permit_a_retry(
    request_, ledger, failure
):
    """Retrying a taken address or an exhausted quota only spends attempts."""
    ledger.record(_accepted(intended_attempt(request_)))
    decision = assess_attempt(
        request_,
        ledger,
        CreateAccountObservation(
            status=CreateAccountStatus.FAILED,
            create_account_request_id=REQUEST_ID,
            failure=failure,
        ),
    )
    assert decision.disposition is AttemptDisposition.REFUSED_INPUT_CANNOT_SUCCEED
    assert not decision.may_create_account
    assert failure.value in decision.reason
    assert not failure.retry_can_succeed


@pytest.mark.parametrize(
    "failure",
    [
        CreateAccountFailure.CONCURRENT_ACCOUNT_MODIFICATION,
        CreateAccountFailure.INTERNAL_FAILURE,
    ],
)
def test_a_transient_failure_permits_a_retry(request_, ledger, failure):
    ledger.record(_accepted(intended_attempt(request_)))
    decision = assess_attempt(
        request_,
        ledger,
        CreateAccountObservation(
            status=CreateAccountStatus.FAILED,
            create_account_request_id=REQUEST_ID,
            failure=failure,
        ),
    )
    assert decision.disposition is AttemptDisposition.RETRY_PERMITTED
    assert not decision.may_create_account
    assert decision.requires_durable_attempt
    assert failure.retry_can_succeed


def test_an_email_conflict_is_reported_rather_than_worked_around(request_, ledger):
    """A taken address may belong to an account in ANOTHER organization.

    Silently suffixing a variant address would open an account under an address the operator
    did not choose, so the conflict surfaces instead.
    """
    ledger.record(_accepted(intended_attempt(request_)))
    decision = assess_attempt(
        request_,
        ledger,
        CreateAccountObservation(
            status=CreateAccountStatus.FAILED,
            create_account_request_id=REQUEST_ID,
            failure=CreateAccountFailure.EMAIL_ALREADY_EXISTS,
        ),
    )
    assert decision.disposition is AttemptDisposition.REFUSED_INPUT_CANNOT_SUCCEED
    assert "input must change" in decision.reason


# ── Records that cannot be acted on are refused at construction ──────────────────────


def test_unknown_is_not_a_conclusive_status():
    assert CreateAccountStatus.UNKNOWN in INCONCLUSIVE_STATUSES
    assert CreateAccountStatus.IN_PROGRESS in INCONCLUSIVE_STATUSES
    assert CreateAccountStatus.FAILED not in INCONCLUSIVE_STATUSES
    assert CreateAccountStatus.SUCCEEDED not in INCONCLUSIVE_STATUSES


def test_success_without_an_account_id_is_refused(request_):
    """Otherwise there is a paid-for account that nothing can identify."""
    attempt = _accepted(intended_attempt(request_))
    with pytest.raises(AccountCreationError, match="nothing can identify"):
        dataclasses.replace(attempt, status=CreateAccountStatus.SUCCEEDED)


def test_an_unconfirmed_attempt_carrying_an_account_id_is_refused(request_):
    attempt = _accepted(intended_attempt(request_))
    with pytest.raises(AccountCreationError, match="look created"):
        dataclasses.replace(attempt, account_id=CREATED_ACCOUNT)


def test_a_failure_without_a_reason_is_refused(request_):
    """Whether repeating can ever succeed is decided entirely by which reason AWS gave."""
    attempt = _accepted(intended_attempt(request_))
    with pytest.raises(AccountCreationError, match="must carry the reason"):
        dataclasses.replace(attempt, status=CreateAccountStatus.FAILED)


def test_a_malformed_request_id_is_refused_because_absent_is_the_honest_value(request_):
    attempt = intended_attempt(request_)
    with pytest.raises(AccountCreationError, match="not an AWS CreateAccountStatus id"):
        dataclasses.replace(attempt, create_account_request_id="")
    with pytest.raises(AccountCreationError, match="Absent is the correct way"):
        dataclasses.replace(attempt, create_account_request_id="12345")


def test_an_unknown_observation_must_say_why_aws_could_not_be_read():
    """It blocks both retry and release, so the reason is the operator's starting point."""
    with pytest.raises(AccountCreationError, match="must say why"):
        CreateAccountObservation(status=CreateAccountStatus.UNKNOWN)


def test_an_unknown_observation_cannot_claim_an_account_or_a_failure():
    with pytest.raises(AccountCreationError, match="reported neither"):
        CreateAccountObservation(
            status=CreateAccountStatus.UNKNOWN,
            detail="timed out",
            account_id=CREATED_ACCOUNT,
        )


def test_an_answered_observation_must_identify_the_request_it_is_about():
    with pytest.raises(AccountCreationError, match="cannot be matched"):
        CreateAccountObservation(
            status=CreateAccountStatus.SUCCEEDED, account_id=CREATED_ACCOUNT
        )


# ── Persisting the request and status ids survives a round trip ──────────────────────


def test_an_attempt_round_trips_through_plain_data(request_):
    """AC-01 asks for the ids to be PERSISTED, so the record must survive storage."""
    attempt = dataclasses.replace(
        _accepted(intended_attempt(request_)),
        status=CreateAccountStatus.SUCCEEDED,
        account_id=CREATED_ACCOUNT,
    )
    restored = CreateAccountAttempt.from_record(attempt.as_record())
    assert restored == attempt


def test_an_absent_request_id_is_omitted_rather_than_stored_as_empty(request_):
    """An empty id would read as an answer identifying nothing; absence says AWS has not
    answered."""
    record = intended_attempt(request_).as_record()
    assert "create_account_request_id" not in record
    assert "account_id" not in record
    assert "failure" not in record
    assert CreateAccountAttempt.from_record(record).create_account_request_id is None


def test_a_ledger_round_trips_and_keeps_fencing(request_):
    ledger = AttemptLedger()
    ledger.record(
        dataclasses.replace(
            _accepted(intended_attempt(request_)),
            status=CreateAccountStatus.SUCCEEDED,
            account_id=CREATED_ACCOUNT,
        )
    )
    restored = AttemptLedger.from_records(ledger.as_records())
    assert restored.find(request_) is not None
    decision = assess_attempt(request_, restored)
    assert decision.disposition is AttemptDisposition.ALREADY_CREATED
    assert not decision.may_create_account


def test_an_empty_ledger_loads_from_nothing():
    assert AttemptLedger.from_records(None).attempts == {}
    assert AttemptLedger.from_records([]).attempts == {}


def test_a_stored_attempt_whose_key_does_not_match_its_fields_is_refused(request_):
    """Otherwise it is findable under one key and fences under another — no fence at all."""
    record = _accepted(intended_attempt(request_)).as_record()
    record["idempotency_key"] = "afk-0000000000000000000000000000ffff"
    with pytest.raises(AccountCreationError, match="silently not engage"):
        AttemptLedger.from_records([record])


def test_a_stored_attempt_with_an_unrecognised_field_is_refused(request_):
    """Dropping it would rebuild a different attempt and then decide a retry from it."""
    record = intended_attempt(request_).as_record()
    record["closed_at"] = "2026-01-01"
    with pytest.raises(AccountCreationError, match="unrecognised field"):
        CreateAccountAttempt.from_record(record)


def test_a_stored_attempt_with_an_unknown_failure_reason_is_refused(request_):
    record = _accepted(intended_attempt(request_)).as_record()
    record["status"] = "failed"
    record["failure"] = "some-new-reason"
    with pytest.raises(AccountCreationError, match="not a known failure reason"):
        CreateAccountAttempt.from_record(record)


def test_a_stored_attempt_claiming_success_with_no_account_id_is_refused_on_load(
    request_,
):
    """Construction invariants apply on load; a corrupt store is not trusted."""
    record = _accepted(intended_attempt(request_)).as_record()
    record["status"] = "succeeded"
    with pytest.raises(AccountCreationError, match="nothing can identify"):
        CreateAccountAttempt.from_record(record)


def test_two_stored_attempts_under_one_key_are_refused_not_resolved_by_order(request_):
    record = _accepted(intended_attempt(request_)).as_record()
    with pytest.raises(AccountCreationError, match="refused rather than resolved"):
        AttemptLedger.from_records([record, dict(record)])


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_email", "another@example.invalid"),
        ("organizational_unit_id", "ou-abcd-12345678"),
    ],
)
def test_payload_edits_cannot_bypass_unresolved_workspace_attempt(
    request_, ledger, field, value
):
    ledger.record(_accepted(intended_attempt(request_)))
    changed = dataclasses.replace(request_, **{field: value})
    with pytest.raises(AccountCreationError, match="different approved payload"):
        assess_attempt(changed, ledger)


def test_offline_decision_is_never_executable_even_with_complete_flags(
    request_, ledger
):
    from .conftest import matching_authorization

    decision = assess_attempt(
        request_, ledger, authorization=matching_authorization(request_)
    )
    assert decision.requires_durable_attempt
    assert not decision.may_create_account
