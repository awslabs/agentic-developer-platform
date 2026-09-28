"""The approval gate: binding, recheck, expiry, revocation and envelope.

Issue #5526 (w6-03), EPIC #4910, Wave 6. Covers AC-01's offline half -- replay is in
`test_admission_postgres.py`, because single-use is a database constraint and asserting
it against anything else would be evidence about the fake.

Every test here is a **negative** case except the two that establish the baseline. That
ratio is deliberate: the permissive path is one value and the denials are the contract.
A suite weighted the other way would pass while any individual denial regressed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs.approval import (
    APPROVAL_PERMISSION,
    NON_PERMISSIVE_RESULTS,
    ApprovalBinding,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalResult,
    ApproverStatus,
    SpendEnvelope,
    evaluate_approval,
    requires_distinct_approver,
    utc_now,
)
from harness_jobs.identity import (
    ContractViolation,
    OperationRequest,
    ResolvedPrincipal,
)

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
REQUESTER = "user:alice"
APPROVER = "user:boss"


def principal(subject: str = REQUESTER, **kwargs: object) -> ResolvedPrincipal:
    defaults: dict[str, object] = {
        "org_id": "org-1",
        "workspace_id": "ws-1",
        "subject": subject,
        "permissions": frozenset({"workspace:provision"}),
    }
    defaults.update(kwargs)
    return ResolvedPrincipal(**defaults)  # type: ignore[arg-type]


def request(**kwargs: object) -> OperationRequest:
    defaults: dict[str, object] = {
        "action": "provision",
        "idempotency_key": "key-1",
        "parameters": {"instance_type": "a100"},
    }
    defaults.update(kwargs)
    return OperationRequest(**defaults)  # type: ignore[arg-type]


def envelope(**kwargs: int) -> SpendEnvelope:
    defaults = {
        "max_resource_units": 4,
        "max_runtime_seconds": 3600,
        "max_cost_micros": 5_000_000,
    }
    defaults.update(kwargs)
    return SpendEnvelope(**defaults)


def approver_ok(subject: str = APPROVER, **kwargs: object) -> ApproverStatus:
    defaults: dict[str, object] = {
        "subject": subject,
        "is_member": True,
        "permissions": frozenset({APPROVAL_PERMISSION}),
        "revoked": False,
    }
    defaults.update(kwargs)
    return ApproverStatus(**defaults)  # type: ignore[arg-type]


def record(**kwargs: object) -> ApprovalRecord:
    actor = principal()
    defaults: dict[str, object] = {
        "approval_id": "appr-1",
        "binding": ApprovalBinding.for_request(actor, request()),
        "envelope": envelope(),
        "result": ApprovalResult.ALLOWED_ONCE,
        "approvers": frozenset({APPROVER}),
        "decided_by": APPROVER,
        "decided_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(hours=1),
    }
    defaults.update(kwargs)
    return ApprovalRecord(**defaults)  # type: ignore[arg-type]


def evaluate(
    rec: ApprovalRecord | None = None,
    *,
    actor: ResolvedPrincipal | None = None,
    req: OperationRequest | None = None,
    requested: SpendEnvelope | None = None,
    statuses: dict[str, ApproverStatus] | None = None,
    now: datetime = NOW,
) -> ApprovalDecision:
    return evaluate_approval(
        record() if rec is None and statuses is None else rec,
        principal=actor or principal(),
        request=req or request(),
        requested_envelope=requested or envelope(),
        approver_statuses={APPROVER: approver_ok()} if statuses is None else statuses,
        now=now,
    )


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_a_current_approval_for_this_exact_request_permits():
    """The one permissive path, so the denials below are not vacuously green.

    Without this, every assertion in this file could pass because the gate refuses
    everything unconditionally -- which is a fail-closed bug, not a fail-closed design.
    """
    decision = evaluate()
    assert decision.permitted is True
    assert decision.reason == ""


def test_a_narrower_request_is_still_covered():
    """Asking for less than was approved is inside what the human agreed to."""
    decision = evaluate(
        record(),
        requested=envelope(max_resource_units=1, max_cost_micros=1),
    )
    assert decision.permitted is True


# ---------------------------------------------------------------------------
# Absence and the answer vocabulary
# ---------------------------------------------------------------------------


def test_absence_of_an_approval_is_a_denial_not_a_default():
    """#5524 §2: an unanswered question is not permission."""
    decision = evaluate_approval(
        None,
        principal=principal(),
        request=request(),
        requested_envelope=envelope(),
        approver_statuses={APPROVER: approver_ok()},
        now=NOW,
    )
    assert decision.permitted is False
    assert "absence is not permission" in decision.reason


@pytest.mark.parametrize(
    "result", sorted(NON_PERMISSIVE_RESULTS, key=lambda r: r.value)
)
def test_no_result_other_than_allowed_once_permits(result: ApprovalResult):
    """Driven from the set rather than a hand-written list.

    A member added to `ApprovalResult` without being added to the permissive branch is
    covered automatically; one added *to* the permissive branch fails here.
    """
    decision = evaluate(record(result=result))
    assert decision.permitted is False
    assert result.value in decision.reason


def test_unavailable_is_distinguishable_from_rejected():
    """The distinction the HITL contract exists to preserve (#4178).

    Both deny, and they are different facts: `unavailable` may warrant a retry,
    `rejected` never does. A gate whose reason string collapsed them would make that
    undecidable downstream.
    """
    rejected = evaluate(record(result=ApprovalResult.REJECTED))
    unavailable = evaluate(record(result=ApprovalResult.UNAVAILABLE))
    assert rejected.reason != unavailable.reason


# ---------------------------------------------------------------------------
# Expiry and revocation
# ---------------------------------------------------------------------------


def test_an_expired_approval_does_not_permit():
    decision = evaluate(record(), now=NOW + timedelta(hours=2))
    assert decision.permitted is False
    assert "expired" in decision.reason


def test_expiry_is_exclusive_at_the_boundary():
    """An approval expiring exactly now does not permit.

    The boundary belongs to the denying side: an approval whose validity ends at this
    instant is not evidence about this instant.
    """
    decision = evaluate(record(expires_at=NOW))
    assert decision.permitted is False
    assert "expired" in decision.reason


def test_a_revoked_approval_does_not_permit_even_before_expiry():
    decision = evaluate(record(revoked=True))
    assert decision.permitted is False
    assert "revoked" in decision.reason


def test_revocation_is_reported_as_revocation_not_expiry():
    """Two different remedies, so they must not share a reason.

    An operator told "expired" re-requests approval; one told "revoked" finds out why it
    was revoked first.
    """
    decision = evaluate(record(revoked=True, expires_at=NOW - timedelta(hours=1)))
    assert "revoked" in decision.reason


# ---------------------------------------------------------------------------
# The binding: changed plans, tenants and requesters
# ---------------------------------------------------------------------------


def test_a_changed_parameter_invalidates_the_approval():
    """ "Same key, bigger machine" -- the retry-as-budget-increase case (#5524 §3.2)."""
    decision = evaluate(record(), req=request(parameters={"instance_type": "h100"}))
    assert decision.permitted is False
    assert "different plan" in decision.reason


def test_an_added_parameter_invalidates_the_approval():
    """A parameter the approver never saw changes what was asked for."""
    decision = evaluate(
        record(), req=request(parameters={"instance_type": "a100", "count": "8"})
    )
    assert decision.permitted is False
    assert "different plan" in decision.reason


def test_a_changed_action_invalidates_the_approval():
    """An approval to provision is not an approval to tear down."""
    decision = evaluate(record(), req=request(action="teardown"))
    assert decision.permitted is False
    assert "different plan" in decision.reason


def test_a_changed_idempotency_key_invalidates_the_approval():
    """The key is inside the digest, so one approval cannot cover two admissions.

    This is the property that makes single-use enforceable at all: if the key were
    outside the binding, one approval would legitimately match an unlimited number of
    distinct requests.
    """
    decision = evaluate(record(), req=request(idempotency_key="key-2"))
    assert decision.permitted is False
    assert "different plan" in decision.reason


def test_an_approval_for_another_workspace_does_not_permit():
    decision = evaluate(record(), actor=principal(workspace_id="ws-2"))
    assert decision.permitted is False
    assert "different workspace" in decision.reason


def test_an_approval_for_another_organization_does_not_permit():
    """Checked separately from the workspace: a workspace id may repeat across orgs."""
    decision = evaluate(record(), actor=principal(org_id="org-2", subject=REQUESTER))
    assert decision.permitted is False
    assert "different organization" in decision.reason


def test_an_approval_granted_to_another_requester_does_not_permit():
    """A held approval is not transferable to whoever presents it."""
    decision = evaluate(record(), actor=principal(subject="user:mallory"))
    assert decision.permitted is False
    assert "different requester" in decision.reason


# ---------------------------------------------------------------------------
# Self-issued authority
# ---------------------------------------------------------------------------


def test_a_requester_cannot_approve_its_own_request():
    decision = evaluate(
        record(approvers=frozenset({REQUESTER}), decided_by=REQUESTER),
        statuses={REQUESTER: approver_ok(REQUESTER)},
    )
    assert decision.permitted is False
    assert "its own requester" in decision.reason


def test_the_admitting_principal_cannot_also_be_the_approver():
    """An approver admitting its own approval is refused.

    Reached through the requester check rather than a separate branch, and that is a
    property worth pinning rather than an accident: the binding already requires the
    admitting principal to *be* the recorded requester (the test above this one), so
    "the admitting principal is the approver" and "the requester is the approver" are
    the same condition by the time self-approval is evaluated.

    An earlier revision of this gate had a second branch for the admitting principal.
    It was unreachable for exactly that reason, and this test is what established it --
    the branch could not be made to fire with any input. It was removed rather than
    left as reassuring dead code.
    """
    rec = record(
        binding=ApprovalBinding.for_request(principal(subject=APPROVER), request()),
    )
    decision = evaluate(rec, actor=principal(subject=APPROVER))
    assert decision.permitted is False
    assert "its own requester" in decision.reason


def test_requires_distinct_approver_is_directly_testable():
    """The predicate holds both clauses, since it is callable without the binding check.

    Called on its own, nothing has established that the admitting principal is the
    recorded requester, so the second clause is live here even though the gate
    reaches it only in the state where the two coincide.
    """
    assert requires_distinct_approver(principal(), record()) is True
    assert (
        requires_distinct_approver(
            principal(),
            record(approvers=frozenset({REQUESTER}), decided_by=REQUESTER),
        )
        is False
    )
    # The second clause in isolation: an approver admitting a record that names a
    # different requester. `evaluate_approval` refuses this earlier, at the binding.
    assert requires_distinct_approver(principal(subject=APPROVER), record()) is False


# ---------------------------------------------------------------------------
# Current approver authority, rechecked
# ---------------------------------------------------------------------------


def test_an_approver_who_lost_membership_no_longer_authorizes():
    """Permission loss between decision and admission (this story's design 1)."""
    decision = evaluate(record(), statuses={APPROVER: approver_ok(is_member=False)})
    assert decision.permitted is False
    assert "current authority" in decision.reason


def test_an_approver_who_lost_the_approval_permission_no_longer_authorizes():
    decision = evaluate(
        record(), statuses={APPROVER: approver_ok(permissions=frozenset())}
    )
    assert decision.permitted is False
    assert "current authority" in decision.reason


def test_an_approver_whose_membership_was_revoked_no_longer_authorizes():
    decision = evaluate(record(), statuses={APPROVER: approver_ok(revoked=True)})
    assert decision.permitted is False
    assert "current authority" in decision.reason


def test_provision_permission_alone_does_not_confer_approval_authority():
    """The blast-radius separation `policy.py:178-190` states.

    If PROVISION were enough to approve, every principal able to request an operation
    could approve one, and the distinct-approver rule would be the only thing left --
    satisfiable with two colluding or two compromised requester accounts.
    """
    decision = evaluate(
        record(),
        statuses={
            APPROVER: approver_ok(permissions=frozenset({"workspace:provision"}))
        },
    )
    assert decision.permitted is False
    assert "current authority" in decision.reason


def test_an_unread_approver_status_is_a_denial():
    """An unverified approver does not authorize.

    The mapping being empty means the caller could not establish current authority. That
    is not the same as establishing that authority is absent, and both deny -- but a
    gate that treated a missing entry as "nothing disqualifying found" would turn a
    failed membership lookup into a pass.
    """
    decision = evaluate(record(), statuses={})
    assert decision.permitted is False
    assert "could not be established" in decision.reason


def test_every_selected_approver_is_rechecked_not_only_the_one_who_answered():
    """A policy that selected two approvers is not satisfied by one readable status.

    The second approver's status is missing here; the gate must refuse rather than stop
    at `decided_by`.
    """
    decision = evaluate(
        record(approvers=frozenset({APPROVER, "user:second"})),
        statuses={APPROVER: approver_ok()},
    )
    assert decision.permitted is False
    assert "could not be established" in decision.reason


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    ["max_resource_units", "max_runtime_seconds", "max_cost_micros"],
)
def test_exceeding_any_envelope_dimension_denies(field_name: str):
    """Every dimension independently, so a check taking the best of three fails here."""
    approved = envelope()
    requested = envelope(**{field_name: getattr(approved, field_name) + 1})
    decision = evaluate(record(envelope=approved), requested=requested)
    assert decision.permitted is False
    assert "exceeds the approved envelope" in decision.reason


def test_covers_is_one_directional():
    small = envelope(max_resource_units=1, max_runtime_seconds=1, max_cost_micros=1)
    assert envelope().covers(small) is True
    assert small.covers(envelope()) is False


def test_an_envelope_rejects_a_boolean_ceiling():
    """`True` is an `int`, and would otherwise pass as a ceiling of 1."""
    with pytest.raises(ContractViolation):
        SpendEnvelope(
            max_resource_units=True,  # type: ignore[arg-type]
            max_runtime_seconds=60,
            max_cost_micros=1,
        )


def test_an_envelope_rejects_a_negative_ceiling():
    with pytest.raises(ContractViolation):
        SpendEnvelope(max_resource_units=-1, max_runtime_seconds=60, max_cost_micros=1)


# ---------------------------------------------------------------------------
# Construction-time refusals
# ---------------------------------------------------------------------------


def test_a_binding_cannot_be_built_from_a_caller_supplied_digest():
    """There is no such constructor, and that is the anti-smuggling property.

    Asserted structurally: `for_request` is the only classmethod, and it derives the
    digest. A future `from_digest` helper fails this test rather than passing review.
    """
    constructors = [
        name
        for name, value in vars(ApprovalBinding).items()
        if isinstance(value, classmethod)
    ]
    assert constructors == ["for_request"]


def test_an_answer_from_an_unselected_approver_cannot_be_recorded():
    """Refused at construction, so no such record exists to be rechecked."""
    with pytest.raises(ContractViolation):
        record(approvers=frozenset({APPROVER}), decided_by="user:stranger")


def test_a_naive_datetime_is_refused_at_construction():
    """Not at the expiry comparison, where it would raise instead of denying."""
    with pytest.raises(ContractViolation):
        record(expires_at=datetime(2026, 9, 21, 12, 0, 0))


def test_an_empty_approver_set_is_refused():
    """A record nobody was selected for cannot be satisfied by anybody."""
    with pytest.raises(ContractViolation):
        record(approvers=frozenset(), decided_by=APPROVER)


def test_a_naive_now_is_a_contract_violation_not_a_denial():
    """A malformed input is a programming error, and must not read as a refusal."""
    with pytest.raises(ContractViolation):
        evaluate(record(), now=datetime(2026, 9, 20, 12, 0, 0))


def test_a_permitted_decision_cannot_carry_a_reason():
    with pytest.raises(ContractViolation):
        ApprovalDecision(permitted=True, reason="looks fine")


def test_a_refusal_must_carry_a_reason():
    with pytest.raises(ContractViolation):
        ApprovalDecision(permitted=False, reason="")


def test_utc_now_is_timezone_aware():
    assert utc_now().tzinfo is not None
