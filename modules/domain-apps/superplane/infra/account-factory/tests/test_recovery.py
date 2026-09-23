"""Recovery and retention for a half-built account — Issue #5531 (w6-08), EPIC #4910.

The state under test is **create-succeeded, bootstrap-failed**: a real, billable AWS account
whose roles or prerequisites are partly absent. These tests pin the properties that keep a
report from recommending either of the two wrong moves — a retry that could open a second
account, or a teardown that permanently suspends a real one — and the `NOT_CHECKED` state that
keeps "nobody looked" from reading as either "present" or "absent".

## What these tests deliberately do NOT establish

Nothing here reads an account, repairs anything, retries anything, or closes anything. Step
states are OBSERVATIONS the tests construct; no AWS call is made and no `iam get-role` is
performed. That a live account's real state matches any report, and that a real recovery action
succeeds, are LIVE criteria needing separate named authorization (AC-02). Mocked or offline
evidence never closes a live criterion.
"""

from __future__ import annotations

import dataclasses

import pytest
from account_factory import recovery
from account_factory.bootstrap import (
    AUTOSCALING_STEP,
    BOOTSTRAP_ROLE_STEP,
    BootstrapError,
    BootstrapStep,
    PresenceRule,
    bootstrap_plan,
)
from account_factory.creation import (
    AttemptDecision,
    AttemptDisposition,
    CreateAccountAttempt,
    CreateAccountStatus,
    account_identity_key,
)
from account_factory.recovery import (
    RecoveryError,
    StepState,
    blocking_prerequisites,
    recovery_report,
)

from .conftest import (
    FIXTURE_ORG_ID,
    FIXTURE_ORGANIZATIONAL_UNIT,
    FIXTURE_TARGET_ACCOUNT,
    FIXTURE_WORKSPACE,
    new_account_request,
)


@pytest.fixture
def plan():
    return bootstrap_plan(new_account_request())


FIXTURE_EMAIL = "fixture-workspace@example.invalid"
"""The same address `new_account_request` uses, so an attempt built here is about the account
the `plan` fixture describes."""


def _attempt_key(**overrides) -> str:
    """The identity key a real attempt for the `plan` fixture's request would carry.

    Derived rather than written as a literal, because a literal is not a key: `intended_attempt`
    always derives it, and `recovery_report` now refuses an attempt whose recorded key does not
    re-derive from its own fields — a record a ledger lookup would miss. Fixtures that hardcode
    one are describing a record the production path cannot produce.

    `overrides` is how the cross-account tests build a key for a DIFFERENT account, so a
    mismatch test exercises a well-formed attempt about another subject rather than a corrupt
    one — those are separate failures and the report names them separately.
    """
    return account_identity_key(new_account_request(**overrides))


def _succeeded_attempt(
    account_id: str = FIXTURE_TARGET_ACCOUNT,
) -> CreateAccountAttempt:
    """A recorded attempt AWS confirmed created this account."""
    return CreateAccountAttempt(
        workspace_id=FIXTURE_WORKSPACE,
        organization_id=FIXTURE_ORG_ID,
        organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
        account_email=FIXTURE_EMAIL,
        idempotency_key=_attempt_key(),
        create_account_request_id="car-fixture0000001",
        status=CreateAccountStatus.SUCCEEDED,
        account_id=account_id,
    )


def _created() -> AttemptDecision:
    """The create-succeeded half of create-succeeded/bootstrap-failed."""
    return AttemptDecision(
        disposition=AttemptDisposition.ALREADY_CREATED,
        reason="a recorded attempt succeeded and its account id is known",
        attempt=_succeeded_attempt(),
    )


def _unresolved() -> AttemptDecision:
    """The costly unknown: the creation outcome could not be established."""
    return AttemptDecision(
        disposition=AttemptDisposition.UNRESOLVED,
        reason="the recorded attempt's outcome could not be read",
        attempt=CreateAccountAttempt(
            workspace_id=FIXTURE_WORKSPACE,
            organization_id=FIXTURE_ORG_ID,
            organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
            account_email=FIXTURE_EMAIL,
            idempotency_key=_attempt_key(),
            create_account_request_id="car-fixture0000001",
        ),
    )


def _all_established(plan) -> dict[str, StepState]:
    return {step.name: StepState.ESTABLISHED for step in plan.steps}


# ─────────────────────────────────────────────────────────────────────────────────────────
# Unchecked is not absent, and not present
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_a_step_nobody_observed_is_not_checked_rather_than_absent(plan):
    """The distinction the module turns on, applied to bootstrap.

    "The role is not there" invites creating it; "I did not look" invites looking. Conflating
    them means either creating something that already exists or assuming something that does
    not.
    """
    report = recovery_report(plan, _created(), observed={})
    assert report.unchecked == tuple(step.name for step in plan.steps)
    assert report.established == ()
    assert not report.every_step_accounted_for


def test_an_unchecked_step_is_not_something_a_retry_would_act_on(plan):
    """Acting on an unread step is how a create lands on something that already exists."""
    report = recovery_report(plan, _created(), observed={})
    assert report.retry_would_repeat == ()
    assert set(report.retry_cannot_advance) == {step.name for step in plan.steps}


def test_an_unchecked_step_is_reported_as_incomplete_not_as_fine(plan):
    """The account is not KNOWN to be ready, and that is what incomplete means."""
    observed = _all_established(plan)
    del observed[AUTOSCALING_STEP]
    report = recovery_report(plan, _created(), observed)
    assert AUTOSCALING_STEP in report.incomplete
    assert not report.account_is_usable


def test_a_partially_read_account_is_never_reported_usable(plan):
    """ "No failures were observed" is not "everything was observed to be fine"."""
    observed = _all_established(plan)
    del observed[BOOTSTRAP_ROLE_STEP]
    report = recovery_report(plan, _created(), observed)
    assert not report.account_is_usable
    assert "never checked" in report.summary


def test_the_next_action_for_an_unread_step_is_to_read_it(plan):
    report = recovery_report(plan, _created(), observed={})
    finding = next(f for f in report.findings if f.name == AUTOSCALING_STEP)
    assert "READ IT FIRST" in finding.next_action


def test_a_fully_read_and_established_account_is_reported_complete(plan):
    """The positive control: the refusals above would be vacuous if nothing ever passed."""
    report = recovery_report(plan, _created(), _all_established(plan))
    assert report.every_step_accounted_for
    assert report.account_is_usable
    assert report.summary.startswith("COMPLETE")
    assert report.incomplete == ()


# ─────────────────────────────────────────────────────────────────────────────────────────
# What exists: an unresolved creation is neither "exists" nor "does not"
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_a_recorded_success_is_reported_as_an_existing_account(plan):
    report = recovery_report(plan, _created(), _all_established(plan))
    assert report.account_exists
    assert not report.account_may_exist_untracked
    assert report.account_id == FIXTURE_TARGET_ACCOUNT


def test_an_unresolved_creation_is_reported_as_possibly_untracked_not_as_existing(plan):
    """Reporting a possible account as existing would invite adopting an unconfirmed id;
    reporting it as absent would abandon a real one."""
    report = recovery_report(plan, _unresolved(), _all_established(plan))
    assert report.account_may_exist_untracked
    assert not report.account_exists


def test_an_unresolved_creation_dominates_the_summary(plan):
    """Even with every bootstrap step established, the creation unknown is the headline.

    A report that led with "bootstrap is complete" would be describing an account nobody has
    confirmed is the only one.
    """
    report = recovery_report(plan, _unresolved(), _all_established(plan))
    assert report.summary.startswith("UNRESOLVED")
    assert "nothing is tracking" in report.summary


# ─────────────────────────────────────────────────────────────────────────────────────────
# What a retry would and would not repeat
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_a_retry_repeats_only_the_verified_absent_steps(plan):
    observed = _all_established(plan)
    observed[AUTOSCALING_STEP] = StepState.ABSENT
    report = recovery_report(plan, _created(), observed)
    assert report.retry_would_repeat == (AUTOSCALING_STEP,)
    assert BOOTSTRAP_ROLE_STEP in report.retry_would_skip


def test_what_a_retry_would_skip_is_named_before_the_retry_not_after_it(plan):
    """So "the retry did nothing about X" is visible in advance."""
    observed = _all_established(plan)
    observed[AUTOSCALING_STEP] = StepState.ABSENT
    report = recovery_report(plan, _created(), observed)
    assert set(report.retry_would_repeat) | set(report.retry_would_skip) == {
        step.name for step in plan.steps
    }
    assert not set(report.retry_would_repeat) & set(report.retry_would_skip)


def test_a_bootstrap_retry_is_safe_only_when_nothing_is_blocked_and_creation_is_settled(
    plan,
):
    observed = _all_established(plan)
    observed[AUTOSCALING_STEP] = StepState.ABSENT
    report = recovery_report(plan, _created(), observed)
    assert report.bootstrap_retry_is_safe
    assert not report.needs_operator


def test_a_retry_is_never_safe_while_the_creation_outcome_is_unknown(plan):
    """The failure mode this module exists for: a retry presented as safe under an unknown
    outcome. Every bootstrap step here is verified absent, so the bootstrap side alone would
    say "go" — the creation unknown must veto it."""
    observed = {step.name: StepState.ABSENT for step in plan.steps}
    report = recovery_report(plan, _unresolved(), observed)
    assert not report.bootstrap_retry_is_safe
    assert report.needs_operator


def test_a_retry_is_not_safe_when_a_step_was_never_read(plan):
    """The safe action for an unread step is unknown, so the retry's outcome is not
    completion."""
    observed = _all_established(plan)
    del observed[AUTOSCALING_STEP]
    report = recovery_report(plan, _created(), observed)
    assert not report.bootstrap_retry_is_safe
    assert AUTOSCALING_STEP in report.retry_cannot_advance


def test_a_retry_is_not_safe_over_a_denial(plan):
    """A retry loop on a permission denial is an infinite loop that looks like progress."""
    observed = _all_established(plan)
    observed[AUTOSCALING_STEP] = StepState.DENIED
    report = recovery_report(
        plan,
        _created(),
        observed,
        details={AUTOSCALING_STEP: "AccessDenied on iam:CreateServiceLinkedRole"},
    )
    assert not report.bootstrap_retry_is_safe
    assert report.denied == (AUTOSCALING_STEP,)
    assert report.summary.startswith("BLOCKED")


def test_a_denial_reports_the_steps_own_remediation_rather_than_a_second_copy(plan):
    """Two copies of a remediation is one that can disagree with itself."""
    observed = _all_established(plan)
    observed[AUTOSCALING_STEP] = StepState.DENIED
    report = recovery_report(
        plan, _created(), observed, details={AUTOSCALING_STEP: "AccessDenied"}
    )
    finding = next(f for f in report.findings if f.name == AUTOSCALING_STEP)
    assert finding.step.denial_remediation in finding.next_action


def test_a_denied_finding_must_say_what_was_refused():
    """The remedy depends on WHICH permission was refused, and a detail-free denial cannot be
    told from a guess."""
    step = BootstrapStep(
        name="fixture-step",
        tier=None,
        reason="a fixture step",
        command=("aws", "sts", "get-caller-identity"),
        presence=PresenceRule.REUSE_IF_PRESENT,
        scope="child account (fixture)",
        denial_remediation="a fixture remediation",
    )
    with pytest.raises(RecoveryError, match="must say what was refused"):
        recovery.StepFinding(step=step, state=StepState.DENIED)


# ─────────────────────────────────────────────────────────────────────────────────────────
# A name already taken by something else is neither present nor absent
# ─────────────────────────────────────────────────────────────────────────────────────────


CONFLICT_DETAIL = (
    "AdpWorkspaceController exists but trusts arn:aws:iam::210987654321:root, "
    "which is not the reviewed trusted principal"
)
"""A conflict detail shaped like the real one: the thing exists, and what differs is named."""


def test_a_role_that_exists_but_differs_is_neither_established_nor_absent(plan):
    """The state this whole member exists for.

    `ESTABLISHED` would reuse a role nobody reviewed; `ABSENT` would try to create it, which
    fails against the thing already there. Both answers are wrong in a way that a boolean
    present/absent cannot express, which is why the vocabulary needed a fifth word.
    """
    observed = _all_established(plan)
    observed["controller-role"] = StepState.CONFLICT
    report = recovery_report(
        plan, _created(), observed, details={"controller-role": CONFLICT_DETAIL}
    )

    assert report.conflicting == ("controller-role",)
    assert "controller-role" not in report.established
    assert "controller-role" in report.incomplete
    # Not absent: a retry must not try to create over it.
    assert "controller-role" not in report.retry_would_repeat
    # Not a permission problem: it does not belong in the denial list.
    assert report.denied == ()
    # Not unread: a read is what established the conflict.
    assert report.unchecked == ()
    assert report.every_step_accounted_for


def test_a_conflicting_step_blocks_a_retry_rather_than_inviting_one(plan):
    """Re-running bootstrap cannot resolve a conflict and must not try."""
    observed = _all_established(plan)
    observed["controller-role"] = StepState.CONFLICT
    report = recovery_report(
        plan, _created(), observed, details={"controller-role": CONFLICT_DETAIL}
    )

    assert not report.account_is_usable
    assert not report.ready_for_workspace_provisioning
    assert not report.bootstrap_retry_is_safe
    assert "controller-role" in report.retry_cannot_advance
    assert report.needs_operator


def test_the_summary_of_a_conflict_does_not_read_as_establishable_by_a_retry(plan):
    """The regression this branch closes.

    A conflicting step is `is_known` and is not `DENIED`, so before the CONFLICT branch the
    summary fell through to the plain INCOMPLETE line — "verified absent and establishable by
    re-running bootstrap". That advice is wrong twice: the step is not absent, and the re-run it
    recommends fails against the existing role. The failure then reads as a permissions problem
    and sends the operator to fix an IAM policy that is working as intended.
    """
    observed = _all_established(plan)
    observed["controller-role"] = StepState.CONFLICT
    report = recovery_report(
        plan, _created(), observed, details={"controller-role": CONFLICT_DETAIL}
    )

    assert report.summary.startswith("CONFLICT")
    assert "controller-role" in report.summary
    assert "establishable" not in report.summary
    assert "verified absent" not in report.summary


def test_a_conflict_is_reported_ahead_of_a_denial_elsewhere(plan):
    """With both present the conflict leads, because it is the one a grant cannot fix.

    An operator who reads BLOCKED first resolves the permission, re-runs, and lands back in the
    same place — having learned nothing about the role that is actually in the way.
    """
    observed = _all_established(plan)
    observed["controller-role"] = StepState.CONFLICT
    observed[AUTOSCALING_STEP] = StepState.DENIED
    report = recovery_report(
        plan,
        _created(),
        observed,
        details={
            "controller-role": CONFLICT_DETAIL,
            AUTOSCALING_STEP: "AccessDenied on iam:CreateServiceLinkedRole",
        },
    )

    assert report.summary.startswith("CONFLICT")
    # Still reported, just not as the headline.
    assert report.denied == (AUTOSCALING_STEP,)


def test_the_next_action_for_a_conflict_is_to_decide_not_to_re_run(plan):
    """Bootstrap does not re-scope or replace an existing identity on its own: both are
    destructive to whatever is using it today."""
    observed = _all_established(plan)
    observed["controller-role"] = StepState.CONFLICT
    report = recovery_report(
        plan, _created(), observed, details={"controller-role": CONFLICT_DETAIL}
    )
    finding = next(f for f in report.findings if f.name == "controller-role")

    assert "DECIDE" in finding.next_action
    assert CONFLICT_DETAIL in finding.next_action
    assert "authorization" in finding.next_action


def test_a_conflicting_thing_is_reported_as_retained_even_though_it_is_not_established(
    plan,
):
    """Recovery does not remove it, and a reader who sees only "incomplete" would assume
    nothing is there — then be surprised when their own create fails against it."""
    observed = _all_established(plan)
    observed["controller-role"] = StepState.CONFLICT
    report = recovery_report(
        plan, _created(), observed, details={"controller-role": CONFLICT_DETAIL}
    )

    retained = "\n".join(report.retained)
    assert "controller-role" in retained
    assert CONFLICT_DETAIL in retained
    assert "does not remove or re-scope it" in retained


def test_a_conflicting_finding_must_say_what_differed():
    """Re-scoping and replacing the existing identity are different decisions, and a bare
    "conflict" reads as a transient failure — inviting exactly the re-run that cannot work."""
    step = BootstrapStep(
        name="fixture-step",
        tier=None,
        reason="a fixture step",
        command=("aws", "sts", "get-caller-identity"),
        presence=PresenceRule.REUSE_IF_PRESENT,
        scope="child account (fixture)",
        denial_remediation="a fixture remediation",
    )
    with pytest.raises(RecoveryError, match="must say what differed"):
        recovery.StepFinding(step=step, state=StepState.CONFLICT)


def test_a_conflict_on_a_blocking_prerequisite_blocks_workspace_provisioning(plan):
    """A conflicting blocking step is as blocking as a missing one — more so, because the
    remedy is not available to the retry at all."""
    observed = _all_established(plan)
    observed[AUTOSCALING_STEP] = StepState.CONFLICT
    report = recovery_report(
        plan,
        _created(),
        observed,
        details={
            AUTOSCALING_STEP: "a role of this name exists outside aws-service-role/"
        },
    )
    assert AUTOSCALING_STEP in blocking_prerequisites(report)


def test_recovery_never_reports_re_running_account_creation_as_safe(plan):
    """The account already exists in this state; calling CreateAccount again is the
    duplicate-account bug, and it is not a bootstrap report's call to imply otherwise."""
    for decision in (_created(), _unresolved()):
        report = recovery_report(plan, decision, _all_established(plan))
        assert report.creation_retry_is_safe is False


def test_a_retry_is_not_safe_without_a_known_account_id(plan):
    """A retry with no target is a search, and a search can act on the wrong account."""
    decision = AttemptDecision(
        disposition=AttemptDisposition.ALREADY_CREATED,
        reason="a recorded attempt succeeded",
        attempt=_succeeded_attempt(),
    )
    report = recovery_report(plan, decision, _all_established(plan))
    assert report.account_id == FIXTURE_TARGET_ACCOUNT

    no_attempt = AttemptDecision(
        disposition=AttemptDisposition.ALREADY_CREATED,
        reason="no attempt record accompanies this disposition",
    )
    without = recovery_report(plan, no_attempt, _all_established(plan))
    assert without.account_id is None
    assert not without.bootstrap_retry_is_safe


# ─────────────────────────────────────────────────────────────────────────────────────────
# What is retained
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_the_account_is_always_reported_as_retained(plan):
    """Recovery never closes an account. "Start over" is not a reset: it is an irreversible
    90-day suspension plus a second account."""
    report = recovery_report(plan, _created(), _all_established(plan))
    account_line = report.retained[0]
    assert FIXTURE_TARGET_ACCOUNT in account_line
    assert "never closes" in account_line
    assert "90-day" in account_line


def test_retention_is_reported_even_when_the_account_id_is_unknown(plan):
    """An absent id must not make the retained-account line disappear — that would read as
    nothing being retained."""
    decision = AttemptDecision(
        disposition=AttemptDisposition.ALREADY_CREATED, reason="no attempt record"
    )
    report = recovery_report(plan, decision, _all_established(plan))
    assert "id not recorded" in report.retained[0]


def test_established_steps_are_reported_as_retained_not_recreated(plan):
    """ "This was not removed" has to be visible: an account-wide role a reader assumes was
    cleaned up is one they will look for and not find."""
    observed = _all_established(plan)
    observed[AUTOSCALING_STEP] = StepState.ABSENT
    report = recovery_report(plan, _created(), observed)
    retained = " ".join(report.retained)
    assert BOOTSTRAP_ROLE_STEP in retained
    assert "neither deletes nor re-creates" in retained
    # The absent one is not claimed as retained; it does not exist to retain.
    assert AUTOSCALING_STEP not in retained


def test_an_established_step_needs_no_action(plan):
    report = recovery_report(plan, _created(), _all_established(plan))
    for finding in report.findings:
        assert finding.next_action.startswith("nothing")


def test_a_must_already_exist_step_is_not_offered_as_something_to_create(plan):
    """Bootstrap does not create the audit trail, so recovery must not suggest it does."""
    report = recovery_report(plan, _created(), _all_established(plan))
    finding = next(f for f in report.findings if f.name == "baseline-audit-logging")
    assert "does not create it" in finding.next_action


# ─────────────────────────────────────────────────────────────────────────────────────────
# Blocking prerequisites are distinguished from gaps
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_a_missing_service_linked_role_blocks_workspace_provisioning(plan):
    """Not a gap to close later: the next workspace KMS key creation FAILS, with an error
    about a policy principal that does not name the real cause."""
    observed = _all_established(plan)
    observed[AUTOSCALING_STEP] = StepState.ABSENT
    report = recovery_report(plan, _created(), observed)
    assert AUTOSCALING_STEP in blocking_prerequisites(report)


def test_a_missing_baseline_control_is_a_gap_rather_than_a_block(plan):
    """The two differ in kind, and a report that called everything blocking would tell the
    reader nothing."""
    observed = _all_established(plan)
    observed["baseline-public-access-block"] = StepState.ABSENT
    report = recovery_report(plan, _created(), observed)
    assert "baseline-public-access-block" in report.incomplete
    assert blocking_prerequisites(report) == ()


def test_an_unread_blocking_step_still_blocks(plan):
    """Unchecked is not established, so it cannot clear a blocking prerequisite."""
    observed = _all_established(plan)
    del observed[AUTOSCALING_STEP]
    report = recovery_report(plan, _created(), observed)
    assert AUTOSCALING_STEP in blocking_prerequisites(report)


def test_a_fully_established_account_blocks_nothing(plan):
    report = recovery_report(plan, _created(), _all_established(plan))
    assert blocking_prerequisites(report) == ()


def test_exactly_the_steps_a_workspace_build_cannot_start_without_are_blocking(plan):
    """The blocking/gap distinction is only as good as which steps carry the flag.

    Asserted as the whole set rather than one member, because the failure mode is an edit that
    flips one: flag everything and the distinction conveys nothing, flag too little and a
    workspace build is scheduled against an account whose next KMS key creation fails.

    `workload-role` is deliberately NOT blocking — it is what tenant work runs as, so the
    workspace can be built before it exists — and the two baseline controls are gaps to close
    rather than prerequisites.
    """
    blocking = {s.name for s in plan.steps if s.blocks_workspace_provisioning}
    assert blocking == {"bootstrap-role", "controller-role", AUTOSCALING_STEP}


def test_a_step_claiming_to_block_must_say_what_would_fail():
    """What it blocks IS the justification for blocking; a step that halts a build without
    naming what would fail is one an operator overrides."""
    with pytest.raises(BootstrapError, match="does not state what would fail"):
        BootstrapStep(
            name="fixture-step",
            tier=None,
            reason="a fixture step",
            command=("aws", "sts", "get-caller-identity"),
            presence=PresenceRule.REUSE_IF_PRESENT,
            scope="child account (fixture)",
            denial_remediation="a fixture remediation",
            blocks_workspace_provisioning=True,
        )


# ─────────────────────────────────────────────────────────────────────────────────────────
# The report covers the plan it was given, and only that plan
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_the_report_covers_every_step_in_the_plan(plan):
    """A caller cannot shrink the report by supplying fewer observations."""
    report = recovery_report(plan, _created(), observed={})
    assert [f.name for f in report.findings] == [s.name for s in plan.steps]


def test_an_observation_about_a_step_the_plan_lacks_is_refused(plan):
    """It is about something else, and reporting it would describe a different account."""
    with pytest.raises(RecoveryError, match="not in this plan"):
        recovery_report(
            plan, _created(), {"some-other-account-step": StepState.ESTABLISHED}
        )


def test_a_detail_about_a_step_the_plan_lacks_is_refused_too(plan):
    """A misspelled detail key must not silently drop the operator's own account of what they saw.

    Details are checked on the same footing as observations, because a detail keyed to a name
    the plan does not have is the one part of a report a reader cannot reconstruct from
    elsewhere — and its most likely cause is a typo in the step name.
    """
    with pytest.raises(RecoveryError, match="not in this plan"):
        recovery_report(
            plan,
            _created(),
            {"bootstrap-role": StepState.ESTABLISHED},
            {"bootstrap-roll": "reused the existing role"},
        )


def test_a_detail_for_a_planned_step_is_carried_into_its_finding(plan):
    """The positive control: the check above refuses typos, not details as such."""
    report = recovery_report(
        plan,
        _created(),
        {"bootstrap-role": StepState.ESTABLISHED},
        {"bootstrap-role": "read at 2026-09-22, arn matched"},
    )
    finding = next(f for f in report.findings if f.name == "bootstrap-role")
    assert finding.detail == "read at 2026-09-22, arn matched"


def test_the_account_id_comes_from_the_recorded_attempt_not_from_a_caller(plan):
    """A caller-supplied id could name an account the ledger never recorded ADP opening — and
    that is exactly the id a recovery action would then act on.

    Asserted structurally: there is no parameter to pass one through.
    """
    import inspect

    assert "account_id" not in inspect.signature(recovery_report).parameters


def test_clean_observations_about_no_recorded_account_are_not_readiness(plan):
    """A clean step list must not stand in for a real account.

    Every step observed established while nothing recorded an account means the observations
    are about some other account, or the attempt was never recorded. Neither is a state to
    build a workspace in, and calling it COMPLETE would let a reader conclude otherwise.
    """
    no_record = AttemptDecision(
        disposition=AttemptDisposition.CREATE_PERMITTED,
        reason="no prior attempt exists for this request",
    )
    report = recovery_report(plan, no_record, _all_established(plan))
    # The steps themselves are fine; the account is the problem, and the two are separate.
    assert report.account_is_usable
    assert not report.ready_for_workspace_provisioning
    assert report.summary.startswith("NO RECORDED ACCOUNT")


def test_readiness_requires_both_a_recorded_account_and_established_steps(plan):
    """The positive control, and the other half of the conjunction."""
    ready = recovery_report(plan, _created(), _all_established(plan))
    assert ready.ready_for_workspace_provisioning

    observed = _all_established(plan)
    observed[AUTOSCALING_STEP] = StepState.ABSENT
    not_ready = recovery_report(plan, _created(), observed)
    assert not_ready.account_exists
    assert not not_ready.ready_for_workspace_provisioning


def test_the_report_names_the_workspace_and_organization_it_describes(plan):
    report = recovery_report(plan, _created(), _all_established(plan))
    assert report.workspace_id == FIXTURE_WORKSPACE
    assert report.organization_id == FIXTURE_ORG_ID


# ─────────────────────────────────────────────────────────────────────────────────────────
# The plan and the attempt must be about the SAME account
# ─────────────────────────────────────────────────────────────────────────────────────────
#
# A report is assembled from two independently-sourced halves: the step findings describe the
# PLAN's account, while the account id and every disposition derived from it come from the
# creation ATTEMPT. Nothing compared the two, so a report could present one account's evidence
# as proof about another — and every safety property in the module returned the reassuring
# answer, because each is individually correct about the half it can see.
#
# These are the negative cases. `test_readiness_requires_both_a_recorded_account_and_established
# _steps` above is the positive control that a matching pair still reports normally.


def _attempt_for(account_id: str = FIXTURE_TARGET_ACCOUNT, **request_overrides):
    """A well-formed succeeded attempt about the account `request_overrides` describes.

    Well-formed is the point: its key re-derives from its own fields, so these tests exercise
    the cross-account comparison rather than the record-integrity one. A corrupt record and a
    record about someone else are different failures and the report distinguishes them.
    """
    request = new_account_request(**request_overrides)
    return AttemptDecision(
        disposition=AttemptDisposition.ALREADY_CREATED,
        reason="a recorded attempt succeeded and its account id is known",
        attempt=CreateAccountAttempt(
            workspace_id=request.workspace_id,
            organization_id=request.organization_id,
            organizational_unit_id=request.organizational_unit_id,
            account_email=request.account_email,
            idempotency_key=account_identity_key(request),
            create_account_request_id="car-fixture0000002",
            status=CreateAccountStatus.SUCCEEDED,
            account_id=account_id,
        ),
    )


def test_an_attempt_for_another_workspace_is_refused(plan):
    """The cross-tenant case: one workspace's plan, another's account.

    Refused rather than reported, because the report it would otherwise produce is actively
    misleading in both directions: this workspace gets cleared for provisioning because a
    DIFFERENT workspace's account exists, and a bootstrap retry gets aimed at that account —
    writing account-wide IAM roles into a tenant that never asked for them.
    """
    other = _attempt_for(account_id="000000000123", workspace_id="ws-someone-else")

    with pytest.raises(RecoveryError) as raised:
        recovery_report(plan, other, _all_established(plan))

    message = str(raised.value)
    assert "different accounts" in message
    # Names both values, because the operator has to see WHICH two things disagreed.
    assert FIXTURE_WORKSPACE in message
    assert "ws-someone-else" in message


def test_an_attempt_from_another_organization_is_refused(plan):
    """Same workspace id in a different organization is still a different account."""
    other = _attempt_for(account_id="000000000124", organization_id="o-otherorg999")

    with pytest.raises(RecoveryError) as raised:
        recovery_report(plan, other, _all_established(plan))

    assert "o-otherorg999" in str(raised.value)
    assert FIXTURE_ORG_ID in str(raised.value)


def test_an_attempt_placed_in_another_organizational_unit_is_refused(plan):
    """The case only the identity key can catch.

    Workspace and organization both match here; the attempt differs solely in WHERE the
    account was placed. Comparing only the two fields the plan carried in the clear would pass
    this, which is why the plan carries a key derived from all four.
    """
    other = _attempt_for(
        account_id="000000000125", organizational_unit_id="ou-production-99"
    )

    with pytest.raises(RecoveryError) as raised:
        recovery_report(plan, other, _all_established(plan))

    assert "identity" in str(raised.value)
    # The OU and address travel as a digest, so the refusal says the identities disagree
    # without reprinting the contact address into a log line.
    assert "organizational unit" in str(raised.value)


def test_an_attempt_opened_with_another_contact_address_is_refused(plan):
    """The fourth field. A different root-user address is a different AWS account.

    It is also the field the duplicate-account defect turns on: `_idempotency_key`'s own
    docstring notes that editing the address by one character changes the derived key. An
    attempt carrying a different address is therefore one a lookup for this request would not
    find, and must not be reported on as though it were this plan's.
    """
    other = _attempt_for(
        account_id="000000000126", account_email="someone-else@example.invalid"
    )

    with pytest.raises(RecoveryError) as raised:
        recovery_report(plan, other, _all_established(plan))

    assert "identity" in str(raised.value)
    # The address itself is deliberately NOT echoed: it travels inside the digest.
    assert "someone-else@example.invalid" not in str(raised.value)


def test_a_stale_attempt_whose_key_no_longer_matches_its_fields_is_refused(plan):
    """Record INTEGRITY, which is a different failure from a mismatch of subjects.

    Everything the attempt records about itself matches this plan; only its stored key is
    stale. That matters on its own because the stored key is what `AttemptLedger.find` looks a
    prior attempt up under: a record whose key no longer derives from its own fields is one a
    lookup for this request MISSES, so it reports no prior attempt and permits a create while
    this attempt's account may already exist.

    Reported as `integrity` rather than as a differing account, because the remedy differs —
    nobody should go looking for a second tenant's account here.
    """
    stale = AttemptDecision(
        disposition=AttemptDisposition.ALREADY_CREATED,
        reason="a recorded attempt succeeded and its account id is known",
        attempt=CreateAccountAttempt(
            workspace_id=FIXTURE_WORKSPACE,
            organization_id=FIXTURE_ORG_ID,
            organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
            account_email=FIXTURE_EMAIL,
            # Well-formed and plausible, and derived from an earlier revision of the request.
            idempotency_key=_attempt_key(account_email="old-address@example.invalid"),
            create_account_request_id="car-fixture0000003",
            status=CreateAccountStatus.SUCCEEDED,
            account_id=FIXTURE_TARGET_ACCOUNT,
        ),
    )

    with pytest.raises(RecoveryError) as raised:
        recovery_report(plan, stale, _all_established(plan))

    assert "integrity" in str(raised.value)


def test_the_refusal_happens_before_any_report_is_built(plan):
    """No partial report escapes — the caller gets an exception, not a degraded object.

    Stated because the alternative design is tempting: carrying the mismatch as a finding or a
    flag would leave `account_exists` and `bootstrap_retry_is_safe` readable and TRUE on a
    report about two different accounts, and a caller reading only those would act on it.
    """
    other = _attempt_for(account_id="000000000127", workspace_id="ws-someone-else")

    with pytest.raises(RecoveryError):
        recovery_report(plan, other, _all_established(plan))


def test_a_decision_with_no_attempt_is_not_a_mismatch(plan):
    """The never-recorded case must stay reportable.

    This is the state the module most needs to describe — clean observations with no attempt
    saying this workspace has an account at all. Refusing it as a mismatch would make the
    report unable to say so, which is worse than the gap being closed.
    """
    no_record = AttemptDecision(
        disposition=AttemptDisposition.CREATE_PERMITTED,
        reason="no prior attempt exists for this request",
    )

    report = recovery_report(plan, no_record, _all_established(plan))

    assert report.summary.startswith("NO RECORDED ACCOUNT")
    assert not report.ready_for_workspace_provisioning


def test_an_unresolved_attempt_for_this_plan_still_reports(plan):
    """The comparison must not break the costly-unknown path.

    `_unresolved` carries an attempt with no account id yet. It is about THIS plan's account,
    so it passes the comparison and still reports UNRESOLVED — the check gates on identity,
    not on how settled the outcome is.
    """
    report = recovery_report(plan, _unresolved(), _all_established(plan))

    assert report.account_may_exist_untracked
    assert not report.bootstrap_retry_is_safe


def test_a_plan_with_no_recorded_identity_still_compares_what_it_has(plan):
    """Absent identity means that comparison was NOT MADE, not that it passed.

    A directly-constructed plan carries no `account_identity_key`. The OU and address can then
    not be compared — but the workspace and organization still can, and still are. Treating an
    absent key as a blanket pass would be the "verified and never checked look alike" failure
    `ValidationAuthorization` exists to prevent.
    """
    keyless = dataclasses.replace(plan, account_identity_key=None)

    # Still caught: the workspace comparison does not depend on the key.
    with pytest.raises(RecoveryError):
        recovery_report(
            keyless,
            _attempt_for(account_id="000000000128", workspace_id="ws-someone-else"),
            _all_established(keyless),
        )

    # Not caught, and honestly so: without the key there is nothing to compare the OU against.
    tolerated = recovery_report(
        keyless,
        _attempt_for(
            account_id="000000000129", organizational_unit_id="ou-production-99"
        ),
        _all_established(keyless),
    )
    assert tolerated.account_id == "000000000129"


def test_a_real_plan_records_the_identity_it_is_about(plan):
    """The plan half of the fix, asserted directly.

    Without this the comparison above silently degrades to workspace-and-organization only,
    and the OU and contact-address cases stop being checked while their tests still pass
    through the `None` branch.
    """
    assert plan.account_identity_key == account_identity_key(new_account_request())
    # A digest, not the fields: the contact address must not ride along on the plan.
    assert "example.invalid" not in (plan.account_identity_key or "")
