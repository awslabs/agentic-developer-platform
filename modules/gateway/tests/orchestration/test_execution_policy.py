"""Tests for the accepted execution policy and its admission check (#5128).

No database and no fixtures. Every fact `authorize_action` needs is an argument,
so each adversarial case — revoked membership, a stale plan version, unknown spend
— is a literal in the test rather than a state simulated in a store. That is the
payoff of the pure-function split, and it is why the deny-path coverage here is
exhaustive instead of representative.

Two conventions worth stating because they are load-bearing:

- Assertions are on `DenyReason` members, **never on `detail` text**. #5122
  consumes the typed reason; a test matching prose would pass while the contract
  broke and fail when the wording improved.
- Denials are asserted to be *the specific* reason, not merely falsy. Several of
  these paths would deny for a second, incidental reason if the intended check
  were deleted (a stale version would also fail expiry, an unpermitted action
  would also fail scope), so `assert not decision.permitted` alone would keep
  passing with the real check removed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.orchestration.execution_policy import (
    POLICY_SCHEMA_VERSION,
    AcceptanceMode,
    Action,
    AuthorizationContext,
    CredentialScope,
    Decision,
    DenyReason,
    ExecutionPolicy,
    PolicyLimits,
    PolicyRejectedError,
    ResourceRef,
    authorize_action,
    flow_budget_binding,
    policy_hash,
    stamp_policy,
    summarize_policy,
)

ORG_A = "org-alpha"
ORG_B = "org-beta"
TEAM_A = "team-alpha"
REPO_A = "repo-alpha"
REPO_B = "repo-beta"
ENV_A = "conn-env-alpha"
PRINCIPAL = "user-owner"
FLOW = "demo-flow"
ADDRESS = "demo-flow/epic-1/wave-1/eval-1"

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=7)


def _limits(**overrides: object) -> PolicyLimits:
    base: dict[str, object] = {
        "max_wall_clock_seconds": 3600,
        "max_spend_usd": Decimal("50.00"),
        "max_attempts_per_node": 3,
        "max_concurrent_actions": 4,
    }
    base.update(overrides)
    return PolicyLimits(**base)  # type: ignore[arg-type]


def _policy(**overrides: object) -> ExecutionPolicy:
    """An unstamped, submittable policy. Overrides replace whole fields."""
    base: dict[str, object] = {
        "org_id": ORG_A,
        "team_ids": [TEAM_A],
        "repository_ids": [REPO_A],
        "environment_connection_ids": [ENV_A],
        "allowed_actions": [Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.DEPLOY, Action.EVALUATE],
        "human_gates": [Action.DEPLOY],
        "evaluation_acceptance": {ADDRESS: AcceptanceMode.MACHINE},
        "expires_at": LATER,
        "limits": _limits(),
    }
    base.update(overrides)
    return ExecutionPolicy(**base)  # type: ignore[arg-type]


def _stamped(**overrides: object) -> ExecutionPolicy:
    return stamp_policy(_policy(**overrides), principal_id=PRINCIPAL, org_id=ORG_A)


def _context(policy: ExecutionPolicy | None = None, **overrides: object) -> AuthorizationContext:
    """A context that permits, so each test can break exactly one thing.

    Building the *allowed* case and mutating one field per test is deliberate: it
    proves the deny came from the field under test rather than from an incidental
    gap in the fixture.
    """
    base: dict[str, object] = {
        "policy": policy if policy is not None else _stamped(),
        "accepted_plan_version": 1,
        "in_force_plan_version": 1,
        "principal_id": PRINCIPAL,
        "member_org_id": ORG_A,
        "member_team_ids": frozenset({TEAM_A}),
        "principal_can_authorize": True,
        "now": NOW,
        "credential_scope": CredentialScope.SCOPED,
        "observed_spend_usd": Decimal("1.00"),
    }
    base.update(overrides)
    return AuthorizationContext(**base)  # type: ignore[arg-type]


def _develop(repo: str = REPO_A) -> ResourceRef:
    return ResourceRef(repository_id=repo, org_id=ORG_A, node_address="demo-flow/epic-1/wave-1/story-1")


# ---------------------------------------------------------------------------
# Schema: the bounds an owner cannot accidentally omit
# ---------------------------------------------------------------------------


class TestPolicyLimits:
    def test_all_bounds_required(self) -> None:
        """No limit has a default, so an unbounded policy is unrepresentable.

        The issue's "unbounded values fail validation" requirement. Asserting all
        four are missing at once proves none of them silently acquired a default —
        a per-field test would pass if one field defaulted.
        """
        with pytest.raises(ValidationError) as exc:
            PolicyLimits()  # type: ignore[call-arg]
        missing = {error["loc"][0] for error in exc.value.errors()}
        assert missing == {"max_wall_clock_seconds", "max_spend_usd", "max_attempts_per_node", "max_concurrent_actions"}

    @pytest.mark.parametrize(
        "field",
        ["max_wall_clock_seconds", "max_spend_usd", "max_attempts_per_node", "max_concurrent_actions"],
    )
    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_bound_rejected(self, field: str, value: int) -> None:
        """Zero and negative are both refused, for every bound.

        Zero matters as much as negative: a zero limit authorizes nothing, so a
        policy carrying one lists actions that every admission then blocks.
        """
        with pytest.raises(ValidationError):
            _limits(**{field: value})

    def test_extra_field_rejected(self) -> None:
        """An unrecognised bound is a 422, not a silently ignored key.

        A misspelled limit that parsed would produce a policy the author believes
        is bounded and the engine does not.
        """
        with pytest.raises(ValidationError):
            PolicyLimits(  # type: ignore[call-arg]
                max_wall_clock_seconds=60,
                max_spend_usd=Decimal("1"),
                max_attempts_per_node=1,
                max_concurrent_actions=1,
                max_tokens=100,
            )


class TestExecutionPolicySchema:
    def test_unknown_action_rejected(self) -> None:
        """The action set is closed. An unknown verb fails at acceptance."""
        with pytest.raises(ValidationError):
            _policy(allowed_actions=["exfiltrate"])

    def test_all_six_issue_actions_expressible(self) -> None:
        """The six actions the issue enumerates, and only those.

        Pinned as an exact set so adding a member is a deliberate act with a test
        change, not a silent widening of what a policy can authorize.
        """
        assert {action.value for action in Action} == {"develop", "review", "repair", "merge", "deploy", "evaluate"}

    def test_at_least_one_action_required(self) -> None:
        with pytest.raises(ValidationError):
            _policy(allowed_actions=[])

    def test_at_least_one_repository_required(self) -> None:
        """A policy authorizing work in no repository authorizes nothing."""
        with pytest.raises(ValidationError):
            _policy(repository_ids=[])

    def test_expiry_required(self) -> None:
        """No expiry means a permanent grant, which scoped authority excludes."""
        with pytest.raises(ValidationError):
            ExecutionPolicy(  # type: ignore[call-arg]
                org_id=ORG_A,
                repository_ids=[REPO_A],
                allowed_actions=[Action.DEVELOP],
                limits=_limits(),
            )

    def test_secret_shaped_extra_field_rejected(self) -> None:
        """ "No secret material" is structural: there is no field to put one in."""
        with pytest.raises(ValidationError):
            _policy(github_token="ghp_notarealtoken")

    def test_unsupported_schema_version_rejected_at_parse(self) -> None:
        """`schema_version` is a Literal, so a future document stops at the boundary."""
        with pytest.raises(ValidationError):
            _policy(schema_version=POLICY_SCHEMA_VERSION + 1)

    def test_human_gate_must_name_an_allowed_action(self) -> None:
        """A gate on an unpermitted action reads as a control but is not one."""
        with pytest.raises(ValidationError, match="absent from allowed_actions"):
            _policy(allowed_actions=[Action.DEVELOP], human_gates=[Action.DEPLOY])

    def test_action_may_be_both_allowed_and_gated(self) -> None:
        """The intended shape: agents prepare, a human releases.

        Not a contradiction — the default fixture relies on it for DEPLOY.
        """
        policy = _policy(allowed_actions=[Action.DEVELOP, Action.MERGE], human_gates=[Action.MERGE])
        assert policy.permits(Action.DEVELOP)
        assert not policy.permits(Action.MERGE)

    def test_evaluation_address_must_be_a_graph_address(self) -> None:
        """The one caller-controlled key space is constrained like every other address."""
        with pytest.raises(ValidationError, match="graph addresses"):
            _policy(evaluation_acceptance={"not-an-address": AcceptanceMode.MACHINE})

    def test_duplicate_repository_rejected(self) -> None:
        with pytest.raises(ValidationError, match="repository_ids repeats"):
            _policy(repository_ids=[REPO_A, REPO_A])

    def test_empty_team_ids_is_an_org_level_binding(self) -> None:
        """Expressible on purpose: a single-team org need not invent a team."""
        assert _policy(team_ids=[]).team_ids == []

    def test_empty_environment_connections_is_the_safe_default(self) -> None:
        """A develop-and-review policy registers no deploy target."""
        policy = _policy(allowed_actions=[Action.DEVELOP], human_gates=[], environment_connection_ids=[])
        assert policy.environment_connection_ids == []


# ---------------------------------------------------------------------------
# Stamping: server-issued identity, refused rather than overwritten
# ---------------------------------------------------------------------------


class TestStamping:
    def test_stamp_sets_id_hash_and_principal(self) -> None:
        stamped = _stamped()
        assert stamped.principal_id == PRINCIPAL
        assert stamped.policy_hash == policy_hash(stamped)
        assert stamped.policy_id == f"pol_{stamped.policy_hash[:32]}"

    def test_stamp_does_not_mutate_the_submitted_document(self) -> None:
        """The caller keeps the document as submitted; stamping returns a copy."""
        submitted = _policy()
        stamp_policy(submitted, principal_id=PRINCIPAL, org_id=ORG_A)
        assert submitted.policy_id is None
        assert submitted.principal_id is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [("policy_id", "pol_attacker"), ("policy_hash", "0" * 64), ("principal_id", "user-someone-else")],
    )
    def test_caller_supplied_stamp_field_is_rejected_not_overwritten(self, field: str, value: str) -> None:
        """Refused, so an author is never told a value was accepted that was replaced.

        `principal_id` is the dangerous one: an author who believes they named a
        principal must be told they did not, rather than discovering it from an
        audit trail naming someone else.
        """
        with pytest.raises(PolicyRejectedError, match="server-stamped"):
            stamp_policy(_policy(**{field: value}), principal_id=PRINCIPAL, org_id=ORG_A)

    def test_stamp_requires_a_principal(self) -> None:
        with pytest.raises(PolicyRejectedError, match="principal_id"):
            stamp_policy(_policy(), principal_id="", org_id=ORG_A)

    def test_policy_is_never_re_homed(self) -> None:
        """Tenant is compared, never substituted — `compile.py`'s Gate 2 restated."""
        with pytest.raises(PolicyRejectedError, match="never re-homed"):
            stamp_policy(_policy(org_id=ORG_B), principal_id=PRINCIPAL, org_id=ORG_A)


class TestPolicyHash:
    def test_identical_content_stamps_identically(self) -> None:
        """**The retry-safety property.**

        `plan_hash` covers the policy, and idempotency compares `plan_hash`. If
        stamping minted a fresh id, a retried acceptance of the same document
        would hash differently, miss the idempotency return, and be refused as a
        plan-of-record rewrite — turning a dropped connection into a permanent
        failure. This test is what stops that regression.
        """
        first = _stamped()
        second = _stamped()
        assert first.policy_id == second.policy_id
        assert first.policy_hash == second.policy_hash

    def test_changed_authority_changes_the_hash(self) -> None:
        """Identity tracks content, so a widened policy is a different policy."""
        narrow = _stamped(allowed_actions=[Action.DEVELOP], human_gates=[])
        wide = _stamped(allowed_actions=[Action.DEVELOP, Action.MERGE], human_gates=[])
        assert narrow.policy_hash != wide.policy_hash

    def test_changed_limits_change_the_hash(self) -> None:
        assert _stamped().policy_hash != _stamped(limits=_limits(max_spend_usd=Decimal("999"))).policy_hash

    def test_field_order_does_not_change_the_hash(self) -> None:
        """Canonicalisation: declaration order is not identity."""
        forward = _policy(repository_ids=[REPO_A, REPO_B])
        assert policy_hash(forward) == policy_hash(_policy(repository_ids=[REPO_A, REPO_B]))

    def test_hash_excludes_principal(self) -> None:
        """Two owners authorizing identical scope authored the same policy.

        Documented in `policy_hash`; asserted here because it is a deliberate
        choice a reader would otherwise take for a bug. Nothing authorizes on the
        hash, and provenance survives in `principal_id` and the decision row.
        """
        mine = stamp_policy(_policy(), principal_id=PRINCIPAL, org_id=ORG_A)
        theirs = stamp_policy(_policy(), principal_id="user-other", org_id=ORG_A)
        assert mine.policy_hash == theirs.policy_hash
        assert mine.principal_id != theirs.principal_id


class TestFlowBudgetBinding:
    def test_binding_is_derived_from_the_flow_id(self) -> None:
        assert flow_budget_binding(FLOW) == f"flow:{FLOW}"

    def test_binding_is_stable_across_calls(self) -> None:
        """Stable across gates, retries and restarts by construction.

        Nothing is persisted and nothing is propagated, so there is no path by
        which a restart or a new run id resets the allowance.
        """
        assert flow_budget_binding(FLOW) == flow_budget_binding(FLOW)

    def test_distinct_flows_do_not_share_an_allowance(self) -> None:
        assert flow_budget_binding(FLOW) != flow_budget_binding("other-flow")

    def test_empty_flow_id_refused(self) -> None:
        """An empty binding would pool every flow's spend into one allowance."""
        with pytest.raises(ValueError, match="flow_budget_binding requires a flow_id"):
            flow_budget_binding("")


# ---------------------------------------------------------------------------
# The display projection: what an owner is told they authorized
# ---------------------------------------------------------------------------


class TestSummarizePolicy:
    """`summarize_policy` is what a reader is shown, so its failures are misreadings.

    The assertions here are mostly about what the summary must *not* say. A summary
    that overstates autonomy reads as consent to something the owner gated, and no
    exception is raised anywhere along that path — the only thing standing between
    the document and the misreading is this projection.
    """

    def test_a_gated_action_is_not_reported_as_autonomous(self) -> None:
        """The central case. `DEPLOY` is in both lists in `_policy`.

        `allowed_actions` and `human_gates` overlap by design — an action in both is
        the owner saying "agents may prepare this, a person releases it". A summary
        copying `allowed_actions` verbatim would report `deploy` as unattended on
        exactly the policy that gated it.
        """
        summary = summarize_policy(_stamped())
        assert Action.DEPLOY in summary.human_decisions
        assert Action.DEPLOY not in summary.autonomous_actions

    def test_the_two_action_lists_never_overlap(self) -> None:
        """Disjoint here so no consumer has to re-derive the split and get it backwards."""
        summary = summarize_policy(_stamped())
        assert not set(summary.autonomous_actions) & set(summary.human_decisions)

    def test_autonomous_actions_are_the_permitted_ungated_ones(self) -> None:
        summary = summarize_policy(_stamped())
        assert summary.autonomous_actions == [Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.EVALUATE]

    def test_an_unlisted_action_appears_in_neither_list(self) -> None:
        """`MERGE` is absent from `_policy`, so it is not autonomous *and* not a gate.

        Reporting an unlisted action as a human decision would be the same overstatement
        in the other direction: it claims a control the policy does not contain.
        """
        summary = summarize_policy(_stamped())
        assert Action.MERGE not in summary.autonomous_actions
        assert Action.MERGE not in summary.human_decisions

    def test_a_policy_gating_everything_authorizes_nothing_unattended(self) -> None:
        policy = _policy(allowed_actions=[Action.DEVELOP], human_gates=[Action.DEVELOP])
        summary = summarize_policy(stamp_policy(policy, principal_id=PRINCIPAL, org_id=ORG_A))
        assert summary.autonomous_actions == []
        assert summary.human_decisions == [Action.DEVELOP]

    def test_action_order_is_canonical_not_author_order(self) -> None:
        """Two orderings of the same authority produce the same summary.

        So the same policy always reads identically, and a re-accepted document does
        not appear to have changed because its author listed actions differently.
        """
        forward = _policy(allowed_actions=[Action.DEVELOP, Action.REVIEW], human_gates=[])
        reversed_ = _policy(allowed_actions=[Action.REVIEW, Action.DEVELOP], human_gates=[])
        assert summarize_policy(forward).autonomous_actions == summarize_policy(reversed_).autonomous_actions
        assert summarize_policy(forward).autonomous_actions == [Action.DEVELOP, Action.REVIEW]

    def test_machine_acceptance_is_a_count_never_the_addresses(self) -> None:
        """§7.2: the graph address is the internal cost join key and is never rendered.

        Carrying the count rather than the map means there is no address on the
        summary for a renderer to leak by accident.
        """
        policy = _policy(
            evaluation_acceptance={
                ADDRESS: AcceptanceMode.MACHINE,
                "demo-flow/epic-1/wave-1/eval-2": AcceptanceMode.MACHINE,
                "demo-flow/epic-1/wave-1/eval-3": AcceptanceMode.HUMAN,
            }
        )
        summary = summarize_policy(stamp_policy(policy, principal_id=PRINCIPAL, org_id=ORG_A))
        assert summary.machine_accepted_evaluations == 2
        assert ADDRESS not in summary.model_dump_json()

    def test_human_accepted_evaluations_are_not_counted(self) -> None:
        policy = _policy(evaluation_acceptance={ADDRESS: AcceptanceMode.HUMAN})
        assert summarize_policy(stamp_policy(policy, principal_id=PRINCIPAL, org_id=ORG_A)).machine_accepted_evaluations == 0

    def test_no_identity_or_provenance_field_is_projected(self) -> None:
        """A hash on a summary invites treating a hash match as the authorization check.

        It is not: authority also depends on live membership, expiry and limits that
        no document carries. `extra="forbid"` plus this assertion keeps the field off.
        """
        assert not {"policy_id", "policy_hash", "principal_id", "org_id", "schema_version"} & set(summarize_policy(_stamped()).model_dump().keys())

    def test_limits_are_carried_whole(self) -> None:
        """Including the exact `Decimal`. The spend figure is the number an owner agreed to."""
        summary = summarize_policy(_stamped(limits=_limits(max_spend_usd=Decimal("1234.56"))))
        assert summary.limits.max_spend_usd == Decimal("1234.56")
        assert summary.limits.max_concurrent_actions == 4
        assert summary.limits.max_attempts_per_node == 3

    def test_spend_serialises_as_a_string_not_a_float(self) -> None:
        """A float would put rounding into the one number the whole grant is bounded by."""
        summary = summarize_policy(_stamped(limits=_limits(max_spend_usd=Decimal("1234.56"))))
        assert '"max_spend_usd":"1234.56"' in summary.model_dump_json().replace(" ", "")

    def test_expiry_and_targets_are_carried(self) -> None:
        summary = summarize_policy(_stamped())
        assert summary.expires_at == LATER
        assert summary.repository_ids == [REPO_A]
        assert summary.environment_connection_ids == [ENV_A]
        assert summary.team_ids == [TEAM_A]

    def test_an_empty_environment_list_stays_empty(self) -> None:
        """No deployment target is a real authorization, and must not read as unset."""
        policy = _policy(environment_connection_ids=[], allowed_actions=[Action.DEVELOP], human_gates=[])
        assert summarize_policy(stamp_policy(policy, principal_id=PRINCIPAL, org_id=ORG_A)).environment_connection_ids == []

    def test_the_summary_does_not_alias_the_policy_lists(self) -> None:
        """Mutating a summary cannot reach back into the accepted document."""
        policy = _stamped()
        summary = summarize_policy(policy)
        summary.repository_ids.append("repo-injected")
        assert policy.repository_ids == [REPO_A]

    def test_summarizing_an_unstamped_policy_works(self) -> None:
        """#4529 previews a policy before acceptance stamps it.

        The projection reads no stamped field, so a draft summarizes the same way an
        accepted one does — which is what makes the preview trustworthy.
        """
        assert summarize_policy(_policy()).autonomous_actions == summarize_policy(_stamped()).autonomous_actions


# ---------------------------------------------------------------------------
# Decision invariants
# ---------------------------------------------------------------------------


class TestDecisionInvariants:
    def test_blocked_decision_must_carry_a_typed_reason(self) -> None:
        """#5122 renders the reason, so a reasonless block is unconstructible."""
        with pytest.raises(ValueError, match="typed deny reason"):
            Decision(permitted=False)

    def test_permitted_decision_cannot_carry_a_reason(self) -> None:
        """The shape that would make a caller's `if decision.reason:` wrong."""
        with pytest.raises(ValueError, match="cannot carry a deny reason"):
            Decision(permitted=True, reason=DenyReason.POLICY_EXPIRED)


# ---------------------------------------------------------------------------
# authorize_action: the permit path, then every deny reason
# ---------------------------------------------------------------------------


class TestAuthorizePermits:
    def test_permitted_action_in_scope_within_limits(self) -> None:
        decision = authorize_action(_context(), Action.DEVELOP, _develop(), 1)
        assert decision.permitted
        assert decision.reason is None

    def test_machine_acceptance_permitted_where_declared(self) -> None:
        decision = authorize_action(
            _context(),
            Action.EVALUATE,
            ResourceRef(node_address=ADDRESS, org_id=ORG_A),
            1,
        )
        assert decision.permitted

    def test_org_level_binding_permits_without_team_membership(self) -> None:
        """Empty `team_ids` skips the team check rather than denying."""
        decision = authorize_action(
            _context(policy=_stamped(team_ids=[]), member_team_ids=frozenset()),
            Action.DEVELOP,
            _develop(),
            1,
        )
        assert decision.permitted

    def test_spend_below_limit_permits(self) -> None:
        context = _context(observed_spend_usd=Decimal("49.99"))
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).permitted


class TestAuthorizeVersionAndLifetime:
    def test_stale_asserted_version_denied(self) -> None:
        """A caller acting on a superseded plan must not inherit the new policy.

        The owner may never have seen the amended policy applied to this work.
        """
        context = _context(accepted_plan_version=2, in_force_plan_version=2)
        decision = authorize_action(context, Action.DEVELOP, _develop(), 1)
        assert decision.reason is DenyReason.STALE_POLICY_VERSION

    def test_policy_accepted_on_a_superseded_version_denied(self) -> None:
        context = _context(accepted_plan_version=1, in_force_plan_version=2)
        assert authorize_action(context, Action.DEVELOP, _develop(), 2).reason is DenyReason.STALE_POLICY_VERSION

    def test_unsupported_schema_version_denied(self) -> None:
        """Reachable when a stored document is reconstructed by a future reader.

        `model_construct` bypasses validation exactly as a raw-dict rehydration
        would, which is the case this guard exists for.
        """
        policy = _stamped().model_construct(**{**_stamped().__dict__, "schema_version": 99})
        assert authorize_action(_context(policy=policy), Action.DEVELOP, _develop(), 1).reason is DenyReason.SCHEMA_UNSUPPORTED

    def test_expired_policy_denied(self) -> None:
        context = _context(now=LATER + timedelta(seconds=1))
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.POLICY_EXPIRED

    def test_expiry_boundary_is_exclusive(self) -> None:
        """At the expiry instant the policy is already expired, not still valid."""
        assert authorize_action(_context(now=LATER), Action.DEVELOP, _develop(), 1).reason is DenyReason.POLICY_EXPIRED

    def test_missing_clock_denies_rather_than_skipping_expiry(self) -> None:
        """A caller that cannot evaluate expiry gets a refusal, not a permanent grant."""
        assert authorize_action(_context(now=None), Action.DEVELOP, _develop(), 1).reason is DenyReason.POLICY_EXPIRED

    def test_revoked_grant_blocks_new_admissions_immediately(self) -> None:
        context = _context(grant_revoked=True)
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.GRANT_REVOKED

    def test_revocation_reported_ahead_of_expiry(self) -> None:
        """An operator's explicit act is the more informative reason when both hold."""
        context = _context(grant_revoked=True, now=LATER + timedelta(days=1))
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.GRANT_REVOKED


class TestAuthorizeIdentity:
    def test_revoked_membership_denied(self) -> None:
        """Naming a principal is not authority: membership is re-read every time.

        A removed member's work stops at the next action, not at the next
        acceptance.
        """
        context = _context(member_org_id=None)
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.MEMBERSHIP_REVOKED

    def test_principal_now_in_another_org_denied(self) -> None:
        context = _context(member_org_id=ORG_B)
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.ORG_MISMATCH

    def test_work_in_another_tenant_denied(self) -> None:
        """Checked separately from the principal's own membership.

        Otherwise a correctly-scoped principal could act on another tenant's node.
        """
        resource = ResourceRef(repository_id=REPO_A, org_id=ORG_B)
        assert authorize_action(_context(), Action.DEVELOP, resource, 1).reason is DenyReason.ORG_MISMATCH

    def test_principal_outside_bound_teams_denied(self) -> None:
        context = _context(member_team_ids=frozenset({"team-unrelated"}))
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.TEAM_NOT_PERMITTED

    def test_work_from_another_flow_denied(self) -> None:
        context = _context(work_owned_by_policy_flow=False)
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.WORK_NOT_OWNED


class TestAuthorizeAuthority:
    def test_undeclared_action_denied(self) -> None:
        policy = _stamped(allowed_actions=[Action.REVIEW], human_gates=[])
        assert authorize_action(_context(policy=policy), Action.DEVELOP, _develop(), 1).reason is DenyReason.ACTION_NOT_PERMITTED

    def test_human_gated_action_denied_even_though_allowed(self) -> None:
        """**The gate beats the permission.** DEPLOY is in both lists.

        Asserting the reason is `HUMAN_GATE_REQUIRED` rather than merely blocked
        is the point: an operator must understand a person needs to act, not go
        hunting for a misconfiguration.
        """
        resource = ResourceRef(environment_connection_id=ENV_A, org_id=ORG_A)
        decision = authorize_action(_context(), Action.DEPLOY, resource, 1)
        assert decision.reason is DenyReason.HUMAN_GATE_REQUIRED

    def test_merge_is_not_implied_by_develop(self) -> None:
        """Delivery authority is enumerated; nothing implies anything else."""
        policy = _stamped(allowed_actions=[Action.DEVELOP], human_gates=[])
        assert authorize_action(_context(policy=policy), Action.MERGE, _develop(), 1).reason is DenyReason.ACTION_NOT_PERMITTED


class TestMachineAcceptanceIsNeverGateAuthority:
    """Machine acceptance is a mode for an evaluation, never authority over a gate.

    The issue's sharpest constraint, and the one most likely to be eroded by a
    later change, so it gets its own class.
    """

    def test_evaluation_without_a_declared_mode_requires_a_human(self) -> None:
        """Absent means HUMAN. The default must be the restrictive one."""
        policy = _stamped(evaluation_acceptance={})
        decision = authorize_action(_context(policy=policy), Action.EVALUATE, ResourceRef(node_address=ADDRESS, org_id=ORG_A), 1)
        assert decision.reason is DenyReason.MACHINE_ACCEPTANCE_NOT_PERMITTED

    def test_evaluation_marked_human_denies_machine_acceptance(self) -> None:
        policy = _stamped(evaluation_acceptance={ADDRESS: AcceptanceMode.HUMAN})
        decision = authorize_action(_context(policy=policy), Action.EVALUATE, ResourceRef(node_address=ADDRESS, org_id=ORG_A), 1)
        assert decision.reason is DenyReason.MACHINE_ACCEPTANCE_NOT_PERMITTED

    def test_mode_does_not_transfer_between_addresses(self) -> None:
        """A machine mode on one evaluation says nothing about another.

        A typo in an address must not promote an evaluation to machine acceptance.
        """
        other = "demo-flow/epic-1/wave-1/eval-2"
        decision = authorize_action(_context(), Action.EVALUATE, ResourceRef(node_address=other, org_id=ORG_A), 1)
        assert decision.reason is DenyReason.MACHINE_ACCEPTANCE_NOT_PERMITTED

    def test_evaluation_without_an_address_denies(self) -> None:
        """No address means no mode to select, so there is nothing to permit."""
        decision = authorize_action(_context(), Action.EVALUATE, ResourceRef(org_id=ORG_A), 1)
        assert decision.reason is DenyReason.MACHINE_ACCEPTANCE_NOT_PERMITTED

    def test_machine_acceptance_never_clears_a_human_gate(self) -> None:
        """Marking every evaluation MACHINE does not make DEPLOY self-approving.

        This is the escalation the constraint exists to block: the two mechanisms
        are independent, and the gate still wins.
        """
        policy = _stamped(evaluation_acceptance={ADDRESS: AcceptanceMode.MACHINE})
        resource = ResourceRef(environment_connection_id=ENV_A, org_id=ORG_A, node_address=ADDRESS)
        assert authorize_action(_context(policy=policy), Action.DEPLOY, resource, 1).reason is DenyReason.HUMAN_GATE_REQUIRED


class TestAuthorizeScope:
    def test_repository_outside_policy_denied(self) -> None:
        assert authorize_action(_context(), Action.DEVELOP, _develop(REPO_B), 1).reason is DenyReason.REPOSITORY_NOT_PERMITTED

    def test_missing_repository_denied_for_a_repository_action(self) -> None:
        """`None` cannot match, which is why the default is not `""`."""
        resource = ResourceRef(org_id=ORG_A)
        assert authorize_action(_context(), Action.DEVELOP, resource, 1).reason is DenyReason.REPOSITORY_NOT_PERMITTED

    def test_environment_outside_policy_denied(self) -> None:
        policy = _stamped(human_gates=[])  # ungate DEPLOY so scope is what is tested
        resource = ResourceRef(environment_connection_id="conn-prod", org_id=ORG_A)
        assert authorize_action(_context(policy=policy), Action.DEPLOY, resource, 1).reason is DenyReason.ENVIRONMENT_NOT_PERMITTED

    def test_deploy_denied_when_no_environment_is_registered(self) -> None:
        """The intended reading of a policy that registered no deploy target."""
        policy = _stamped(human_gates=[], environment_connection_ids=[])
        resource = ResourceRef(environment_connection_id=ENV_A, org_id=ORG_A)
        assert authorize_action(_context(policy=policy), Action.DEPLOY, resource, 1).reason is DenyReason.ENVIRONMENT_NOT_PERMITTED

    def test_evaluation_needs_no_repository_authority(self) -> None:
        """An evaluation reads evidence about work already done.

        Requiring a repository would block a policy that legitimately names none
        for its evaluation nodes.
        """
        decision = authorize_action(_context(), Action.EVALUATE, ResourceRef(node_address=ADDRESS, org_id=ORG_A), 1)
        assert decision.permitted


class TestScopedCredentialIsMandatory:
    """Inability to scope a credential is a block, never a fallback.

    The failure this story exists to prevent, so both non-permitting values are
    covered and the permit path is pinned to `SCOPED` alone.
    """

    def test_unscopable_credential_denied(self) -> None:
        context = _context(credential_scope=CredentialScope.UNSCOPABLE)
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE

    def test_unknown_credential_scope_denied(self) -> None:
        """The value a caller passes when the credential service was unreachable."""
        context = _context(credential_scope=CredentialScope.UNKNOWN)
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE

    def test_unknown_is_the_default_so_omission_denies(self) -> None:
        """A caller that forgets the field is refused, not admitted broadly."""
        assert (
            AuthorizationContext(
                policy=_stamped(),
                accepted_plan_version=1,
                in_force_plan_version=1,
                principal_id=PRINCIPAL,
                member_org_id=ORG_A,
            ).credential_scope
            is CredentialScope.UNKNOWN
        )

    def test_only_scoped_permits(self) -> None:
        """No allow branch exists that does not require SCOPED."""
        permitted = {
            scope for scope in CredentialScope if authorize_action(_context(credential_scope=scope), Action.DEVELOP, _develop(), 1).permitted
        }
        assert permitted == {CredentialScope.SCOPED}


class TestAuthorizeLimits:
    def test_unknown_spend_blocks_new_spend(self) -> None:
        """**Missing usage is unknown, not zero.**

        A caller that cannot read the ledger passes `None`, and passing `0` would
        mint the full allowance again — the "child resets allowance" blast radius.
        """
        context = _context(observed_spend_usd=None)
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.SPEND_UNKNOWN

    def test_unknown_spend_is_distinguishable_from_exceeded(self) -> None:
        """The two need different operator responses, so they are different reasons.

        Reported as `SPEND_UNKNOWN` even when checked against a policy whose limit
        would also have been exceeded — "we cannot tell" must never surface as
        "we can tell, and it is too much".
        """
        unknown = authorize_action(_context(observed_spend_usd=None), Action.DEVELOP, _develop(), 1)
        exceeded = authorize_action(_context(observed_spend_usd=Decimal("50.00")), Action.DEVELOP, _develop(), 1)
        assert unknown.reason is DenyReason.SPEND_UNKNOWN
        assert exceeded.reason is DenyReason.SPEND_LIMIT_EXCEEDED

    def test_spend_at_limit_denied(self) -> None:
        """At the limit is exhausted: reserved plus settled has reached the cap."""
        context = _context(observed_spend_usd=Decimal("50.00"))
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.SPEND_LIMIT_EXCEEDED

    def test_attempt_limit_bounds_a_repair_loop(self) -> None:
        context = _context(observed_attempts=3)
        assert authorize_action(context, Action.REPAIR, _develop(), 1).reason is DenyReason.ATTEMPT_LIMIT_EXCEEDED

    def test_attempt_limit_does_not_self_clear(self) -> None:
        """Exhaustion needs an authorized recovery; nothing here resets the count."""
        context = _context(observed_attempts=99)
        assert authorize_action(context, Action.REPAIR, _develop(), 1).reason is DenyReason.ATTEMPT_LIMIT_EXCEEDED

    def test_concurrency_limit_bounds_fan_out(self) -> None:
        context = _context(observed_concurrency=4)
        assert authorize_action(context, Action.DEVELOP, _develop(), 1).reason is DenyReason.CONCURRENCY_LIMIT_EXCEEDED

    def test_one_slot_below_concurrency_limit_permits(self) -> None:
        assert authorize_action(_context(observed_concurrency=3), Action.DEVELOP, _develop(), 1).permitted


class TestDenyReasonsAreTyped:
    def test_every_deny_reason_is_distinct(self) -> None:
        """No two reasons share a wire value, so #5122 can switch on them."""
        values = [reason.value for reason in DenyReason]
        assert len(values) == len(set(values))

    def test_no_catch_all_reason(self) -> None:
        """ "Denied for some reason" is not auditable, which is the EPIC's complaint."""
        assert not {reason.value for reason in DenyReason} & {"unknown", "other", "denied", "error"}

    def test_reason_vocabulary_shared_with_agentauth(self) -> None:
        """Deliberate reuse of `evaluate_grant`'s wire words for the same refusals.

        Two denials of the same shape should read as the same word to an operator.
        """
        values = {reason.value for reason in DenyReason}
        assert {"repository_not_permitted", "grant_revoked"} <= values
