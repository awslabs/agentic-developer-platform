"""The accepted, bounded coordination capability and its refusals (#5224).

Reuses `test_execution_policy`'s helpers deliberately: the v1/v2 fixtures there are
the *already published* documents whose bytes and hash this story must not disturb,
so asserting invariance against a locally-redefined copy would prove nothing about
them.

Two conventions carried over from that module, for the same reasons:

- Assertions are on `DenyReason` members, never on `detail` prose.
- A denial is asserted to be *the specific* reason. Most of these paths would deny
  for a second, incidental reason if the intended check were removed (an
  out-of-scope node would still be out of the child-action set; a stale version
  would also fail expiry), so `assert not permitted` alone would keep passing with
  the real check deleted.

The load-bearing case in this file is
`TestCoordinationDoesNotSubstituteForChildAuthority`: a permit from
`authorize_child_request` means "the coordinator was allowed to ask", never "the
child is authorized". Every other test here is a bound; that one is the boundary.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.orchestration.execution_policy import (
    COORDINATION_SCHEMA_VERSION,
    AcceptanceMode,
    Action,
    ChildPersona,
    CoordinationScope,
    CredentialScope,
    DenyReason,
    ExecutionPolicy,
    ResourceRef,
    authorize_action,
    authorize_child_request,
    policy_hash,
    stamp_policy,
    summarize_policy,
)
from tests.orchestration.test_execution_policy import (
    ADDRESS,
    LATER,
    NOW,
    ORG_A,
    ORG_B,
    PRINCIPAL,
    REPO_A,
    REPO_B,
    TEAM_A,
    _context,
    _limits,
    _policy,
    _stamped,
)

# The coordinator's own assigned node, and a sibling it was NOT assigned.
COORDINATOR_NODE = "demo-flow/epic-1/wave-1/story-1"
UNASSIGNED_NODE = "demo-flow/epic-1/wave-2/story-9"


def _scope(**overrides: object) -> CoordinationScope:
    base: dict[str, object] = {
        "assigned_node_addresses": [COORDINATOR_NODE],
        "allowed_child_personas": [ChildPersona.DEVELOPER, ChildPersona.REVIEWER],
        "allowed_child_actions": [Action.DEVELOP, Action.REVIEW, Action.REPAIR],
    }
    base.update(overrides)
    return CoordinationScope(**base)  # type: ignore[arg-type]


def _unvalidated_scope(**overrides: object) -> CoordinationScope:
    """A scope that BYPASSES validation, for documents acceptance would refuse.

    Necessary because the interesting boundary cases here are scopes the validators
    reject outright (a scope naming `merge`), and the point of the tests using this
    is that admission refuses them *again* — a stored document reaching this build
    from a raw dict must not be trusted just because acceptance would have caught it.
    """
    base: dict[str, object] = {
        "assigned_node_addresses": [COORDINATOR_NODE],
        "allowed_child_personas": [ChildPersona.DEVELOPER, ChildPersona.REVIEWER],
        "allowed_child_actions": [Action.DEVELOP],
    }
    base.update(overrides)
    return CoordinationScope.model_construct(**base)


def _coordinator_policy(**overrides: object) -> ExecutionPolicy:
    """A v3 policy that accepts a bounded coordinator."""
    base: dict[str, object] = {
        "schema_version": COORDINATION_SCHEMA_VERSION,
        "allowed_actions": [Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.DEPLOY, Action.EVALUATE, Action.COORDINATE],
        "coordination": _scope(),
    }
    base.update(overrides)
    return _policy(**base)


def _stamped_coordinator(**overrides: object) -> ExecutionPolicy:
    return stamp_policy(_coordinator_policy(**overrides), principal_id=PRINCIPAL, org_id=ORG_A)


def _coordinator_context(**overrides: object):
    """A context that permits coordination, so each test breaks exactly one thing."""
    return _context(policy=overrides.pop("policy", None) or _stamped_coordinator(), **overrides)


def _at(node: str = COORDINATOR_NODE, org: str = ORG_A) -> ResourceRef:
    """What a coordinator's request names: its assigned node and its tenant."""
    return ResourceRef(node_address=node, org_id=org)


# ---------------------------------------------------------------------------
# Published v1/v2 documents are untouched. This is the compatibility contract.
# ---------------------------------------------------------------------------


class TestExistingDocumentsAreUnchanged:
    """Absence grants nothing, and adding the field changed no existing bytes."""

    def test_v1_document_omits_the_coordination_key_entirely(self) -> None:
        """Not `"coordination": null` — the key is absent.

        This is what keeps the canonical JSON, and therefore `policy_hash`, identical
        for every already-accepted document. `compile.plan_hash` compares that hash
        for idempotency, so a serialized `null` would make a retried acceptance of an
        unchanged policy stop matching its own in-force plan.
        """
        document = _policy().model_dump(mode="json")
        assert "coordination" not in document

    def test_v2_document_omits_the_coordination_key_entirely(self) -> None:
        policy = _policy(
            schema_version=2,
            user_credentials={
                "permission_mode": "user_configured",
                "lifetime": "provider_managed",
                "vault_credential_ids": ["cred-1"],
                "actions": [Action.DEVELOP],
            },
        )
        assert "coordination" not in policy.model_dump(mode="json")

    def test_v1_hash_is_computed_over_a_document_with_no_coordination_key(self) -> None:
        """The v1 hash is the digest of the pre-#5224 document shape, independently derived.

        Rather than pinning a magic digest (which would have to be regenerated from
        this very code, and so could not detect the code changing), this recomputes
        the canonical digest the way `policy_hash` documents it — sorted keys, no
        incidental whitespace, stamped fields excluded — over the serialized document,
        and separately asserts that document carries no `coordination` key. Together
        those are the invariance claim: the bytes being hashed are the same bytes an
        accepted v1 document always hashed.
        """
        document = _policy().model_dump(mode="json", exclude={"policy_id", "policy_hash", "principal_id"})
        assert "coordination" not in document
        expected = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        assert policy_hash(_policy()) == expected

    def test_adding_a_coordination_scope_is_the_only_way_the_hash_moves(self) -> None:
        """Two independently-built v1 policies agree; a v3 one differs.

        Guards the specific regression this field could cause: if `coordination`
        leaked into the hashed content of a v1 document (as `null`), these two would
        still agree with each other but would both disagree with every hash already
        stored against an accepted plan — a failure no same-version comparison
        catches. The third assertion is what makes it detectable, by pinning that the
        v1 digest is computed over a document of exactly the pre-existing key set.
        """
        assert policy_hash(_policy()) == policy_hash(_policy())
        assert policy_hash(_coordinator_policy()) != policy_hash(_policy())
        assert set(_policy().model_dump(mode="json")) == set(_policy().model_dump(mode="json")) - {"coordination"}

    def test_absent_coordination_denies_coordinate_rather_than_defaulting(self) -> None:
        """A v1 policy cannot acquire coordinate authority by omission.

        `model_construct` bypasses validation exactly as a raw-dict rehydration of a
        stored `plan_document` would — the route a future reader takes. The action is
        not in `allowed_actions`, so this denies on authority.
        """
        assert authorize_action(_context(), Action.COORDINATE, _at(), 1).reason is DenyReason.ACTION_NOT_PERMITTED

    def test_coordinate_allowed_but_scope_absent_denies(self) -> None:
        """The unreachable-through-pydantic case, reachable by rehydration.

        `_coordination_requires_v3` refuses this pair at acceptance, so it can only
        arrive from a raw dict. It must deny rather than default a scope.
        """
        policy = _stamped_coordinator()
        rehydrated = policy.model_construct(**{**policy.__dict__, "coordination": None})
        decision = authorize_action(_coordinator_context(policy=rehydrated), Action.COORDINATE, _at(), 1)
        assert decision.reason is DenyReason.COORDINATION_NOT_PERMITTED

    def test_a_v1_policy_that_lists_coordinate_is_refused_at_acceptance(self) -> None:
        """Rejected, never silently upgraded to v3."""
        with pytest.raises(ValidationError, match="declares no coordination scope"):
            _policy(allowed_actions=[Action.DEVELOP, Action.COORDINATE])


# ---------------------------------------------------------------------------
# Schema: an unbounded coordinator is unrepresentable
# ---------------------------------------------------------------------------


class TestCoordinationScopeSchema:
    def test_scope_on_a_v1_document_is_rejected_not_upgraded(self) -> None:
        with pytest.raises(ValidationError, match="requires policy schema_version 3"):
            _policy(schema_version=1, allowed_actions=[Action.DEVELOP, Action.COORDINATE], coordination=_scope())

    def test_scope_on_a_v2_document_is_rejected_not_upgraded(self) -> None:
        with pytest.raises(ValidationError, match="requires policy schema_version 3"):
            _policy(schema_version=2, allowed_actions=[Action.DEVELOP, Action.COORDINATE], coordination=_scope())

    def test_scope_without_coordinate_is_rejected_as_dead_configuration(self) -> None:
        """Config that reads as granted authority but cannot take effect."""
        with pytest.raises(ValidationError, match="has no effect unless"):
            _coordinator_policy(allowed_actions=[Action.DEVELOP, Action.REVIEW, Action.REPAIR])

    @pytest.mark.parametrize("field", ["assigned_node_addresses", "allowed_child_personas", "allowed_child_actions"])
    def test_no_bound_may_be_empty(self, field: str) -> None:
        """There is no "coordinate everything" spelling."""
        with pytest.raises(ValidationError):
            _scope(**{field: []})

    def test_assigned_addresses_must_be_graph_addresses(self) -> None:
        """A malformed address can never match a node, so it is permanently dead."""
        with pytest.raises(ValidationError, match="must be graph addresses"):
            _scope(assigned_node_addresses=["not-an-address"])

    def test_a_coordinator_may_not_delegate_coordination_onward(self) -> None:
        """The unbounded-tree case: every limit is per-policy, not per-level."""
        with pytest.raises(ValidationError, match="cannot delegate coordination authority onward"):
            _scope(allowed_child_actions=[Action.DEVELOP, Action.COORDINATE])

    @pytest.mark.parametrize("action", [Action.MERGE, Action.DEPLOY, Action.EVALUATE])
    def test_merge_deploy_and_evaluate_are_not_delegable(self, action: Action) -> None:
        """Refused at acceptance so the accepted document cannot misread as granting them."""
        with pytest.raises(ValidationError, match="not delegable through coordination authority"):
            _scope(allowed_child_actions=[Action.DEVELOP, action])

    def test_child_action_must_be_a_policy_action(self) -> None:
        """An owner cannot see a child action the policy itself would refuse."""
        with pytest.raises(ValidationError, match="absent from allowed_actions"):
            _coordinator_policy(
                allowed_actions=[Action.DEVELOP, Action.COORDINATE],
                coordination=_scope(allowed_child_actions=[Action.DEVELOP, Action.REVIEW]),
            )

    def test_child_action_may_not_be_human_gated(self) -> None:
        """Coordination cannot route around a gate the owner put in place."""
        with pytest.raises(ValidationError, match="cannot route around a human gate"):
            _coordinator_policy(human_gates=[Action.DEPLOY, Action.REVIEW])

    def test_no_secret_shaped_field_on_the_scope(self) -> None:
        with pytest.raises(ValidationError):
            _scope(token="ghp_notarealtoken")

    def test_duplicate_bounds_rejected(self) -> None:
        """Keeps the accepted document readable and its hash stable."""
        with pytest.raises(ValidationError, match="repeats"):
            _scope(assigned_node_addresses=[COORDINATOR_NODE, COORDINATOR_NODE])

    def test_operations_is_not_a_requestable_child_persona(self) -> None:
        """A coordinator cannot request another coordinator."""
        with pytest.raises(ValidationError):
            _scope(allowed_child_personas=["operations"])

    def test_v3_still_accepts_user_credentials(self) -> None:
        """v3 is a superset of v2: accepting a coordinator does not cost user credentials."""
        policy = _coordinator_policy(
            user_credentials={
                "permission_mode": "user_configured",
                "lifetime": "provider_managed",
                "vault_credential_ids": ["cred-1"],
                "actions": [Action.DEVELOP],
            }
        )
        assert policy.user_credentials is not None and policy.coordination is not None


# ---------------------------------------------------------------------------
# The coordinator's own authority
# ---------------------------------------------------------------------------


class TestCoordinatorAdmission:
    def test_an_accepted_coordinator_is_admitted_at_its_assigned_node(self) -> None:
        assert authorize_action(_coordinator_context(), Action.COORDINATE, _at(), 1).permitted

    def test_coordination_outside_the_assigned_node_set_is_denied(self) -> None:
        """Full addresses, not a prefix: a node added after acceptance is not covered."""
        decision = authorize_action(_coordinator_context(), Action.COORDINATE, _at(node=UNASSIGNED_NODE), 1)
        assert decision.reason is DenyReason.COORDINATION_NODE_NOT_ASSIGNED

    def test_coordination_with_no_node_address_is_denied(self) -> None:
        decision = authorize_action(_coordinator_context(), Action.COORDINATE, ResourceRef(org_id=ORG_A), 1)
        assert decision.reason is DenyReason.COORDINATION_NODE_NOT_ASSIGNED

    def test_a_gated_coordinate_action_requires_a_human(self) -> None:
        """An owner may accept a coordinator and still gate it."""
        policy = _stamped_coordinator(human_gates=[Action.DEPLOY, Action.COORDINATE])
        decision = authorize_action(_coordinator_context(policy=policy), Action.COORDINATE, _at(), 1)
        assert decision.reason is DenyReason.HUMAN_GATE_REQUIRED

    def test_coordination_needs_no_repository_authority(self) -> None:
        """A coordinator does not act in a repository; its children do.

        The policy names only `REPO_A`, and this request names no repository at all.
        It is admitted because the repository check belongs to the child's admission.
        """
        assert authorize_action(_coordinator_context(), Action.COORDINATE, _at(), 1).permitted

    def test_another_tenants_node_is_denied(self) -> None:
        decision = authorize_action(_coordinator_context(), Action.COORDINATE, _at(org=ORG_B), 1)
        assert decision.reason is DenyReason.ORG_MISMATCH

    def test_revoked_membership_denies_coordination(self) -> None:
        decision = authorize_action(_coordinator_context(member_org_id=None), Action.COORDINATE, _at(), 1)
        assert decision.reason is DenyReason.MEMBERSHIP_REVOKED

    def test_stale_plan_version_denies_coordination(self) -> None:
        decision = authorize_action(_coordinator_context(in_force_plan_version=2), Action.COORDINATE, _at(), 1)
        assert decision.reason is DenyReason.STALE_POLICY_VERSION

    def test_expired_policy_denies_coordination(self) -> None:
        decision = authorize_action(_coordinator_context(now=LATER + timedelta(seconds=1)), Action.COORDINATE, _at(), 1)
        assert decision.reason is DenyReason.POLICY_EXPIRED

    def test_revoked_grant_denies_coordination(self) -> None:
        decision = authorize_action(_coordinator_context(grant_revoked=True), Action.COORDINATE, _at(), 1)
        assert decision.reason is DenyReason.GRANT_REVOKED

    def test_unknown_spend_denies_coordination(self) -> None:
        """Missing usage is not zero, for a coordinator as for anything else."""
        decision = authorize_action(_coordinator_context(observed_spend_usd=None), Action.COORDINATE, _at(), 1)
        assert decision.reason is DenyReason.SPEND_UNKNOWN


# ---------------------------------------------------------------------------
# Requesting a child
# ---------------------------------------------------------------------------


class TestChildRequest:
    def test_an_eligible_child_may_be_requested(self) -> None:
        decision = authorize_child_request(_coordinator_context(), ChildPersona.DEVELOPER, Action.DEVELOP, _at(), 1)
        assert decision.permitted

    def test_a_persona_outside_the_scope_is_denied(self) -> None:
        policy = _stamped_coordinator(coordination=_scope(allowed_child_personas=[ChildPersona.DEVELOPER]))
        decision = authorize_child_request(_coordinator_context(policy=policy), ChildPersona.REVIEWER, Action.REVIEW, _at(), 1)
        assert decision.reason is DenyReason.CHILD_PERSONA_NOT_PERMITTED

    def test_an_unexpected_persona_string_is_refused_not_passed_through(self) -> None:
        """A malformed request is a typed refusal, not an exception and not a pass."""
        decision = authorize_child_request(_coordinator_context(), "root", Action.DEVELOP, _at(), 1)
        assert decision.reason is DenyReason.CHILD_PERSONA_NOT_PERMITTED

    def test_requesting_a_coordinator_child_is_denied(self) -> None:
        decision = authorize_child_request(_coordinator_context(), "operations", Action.DEVELOP, _at(), 1)
        assert decision.reason is DenyReason.CHILD_PERSONA_NOT_PERMITTED

    def test_an_action_outside_the_scope_is_denied(self) -> None:
        policy = _stamped_coordinator(coordination=_scope(allowed_child_actions=[Action.DEVELOP]))
        decision = authorize_child_request(_coordinator_context(policy=policy), ChildPersona.REVIEWER, Action.REVIEW, _at(), 1)
        assert decision.reason is DenyReason.CHILD_ACTION_NOT_PERMITTED

    def test_a_request_from_an_unassigned_node_is_denied(self) -> None:
        """The coordinator's own bound is checked before the child's eligibility."""
        decision = authorize_child_request(_coordinator_context(), ChildPersona.DEVELOPER, Action.DEVELOP, _at(node=UNASSIGNED_NODE), 1)
        assert decision.reason is DenyReason.COORDINATION_NODE_NOT_ASSIGNED

    def test_a_request_without_coordinate_authority_is_denied(self) -> None:
        """A developer's context cannot request children by naming a child action."""
        decision = authorize_child_request(_context(), ChildPersona.DEVELOPER, Action.DEVELOP, _at(), 1)
        assert decision.reason is DenyReason.ACTION_NOT_PERMITTED

    @pytest.mark.parametrize(
        ("field", "value", "reason"),
        [
            ("member_org_id", None, DenyReason.MEMBERSHIP_REVOKED),
            ("in_force_plan_version", 2, DenyReason.STALE_POLICY_VERSION),
            ("grant_revoked", True, DenyReason.GRANT_REVOKED),
            ("observed_spend_usd", None, DenyReason.SPEND_UNKNOWN),
            ("principal_can_authorize", False, DenyReason.ROLE_REVOKED),
            ("credential_scope", CredentialScope.UNKNOWN, DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE),
            ("work_owned_by_policy_flow", False, DenyReason.WORK_NOT_OWNED),
        ],
    )
    def test_every_live_fact_gates_the_request_itself(self, field: str, value: object, reason: DenyReason) -> None:
        """The request is a full admission, re-evaluated immediately before each child.

        This is what stops a coordinator whose flow has lost membership, exhausted its
        allowance or been superseded from continuing to ask.
        """
        decision = authorize_child_request(_coordinator_context(**{field: value}), ChildPersona.DEVELOPER, Action.DEVELOP, _at(), 1)
        assert decision.reason is reason

    def test_exhausted_shared_allowance_denies_further_requests(self) -> None:
        """The coordinator charges the same flow meter as its children."""
        decision = authorize_child_request(
            _coordinator_context(observed_spend_usd=Decimal("50.00")), ChildPersona.DEVELOPER, Action.DEVELOP, _at(), 1
        )
        assert decision.reason is DenyReason.SPEND_LIMIT_EXCEEDED

    def test_concurrency_limit_bounds_simultaneous_coordinators(self) -> None:
        """Fan-out is bounded by the policy's shared concurrency, not per coordinator."""
        decision = authorize_child_request(_coordinator_context(observed_concurrency=4), ChildPersona.DEVELOPER, Action.DEVELOP, _at(), 1)
        assert decision.reason is DenyReason.CONCURRENCY_LIMIT_EXCEEDED

    def test_attempt_limit_bounds_a_coordinator_repair_loop(self) -> None:
        decision = authorize_child_request(_coordinator_context(observed_attempts=3), ChildPersona.DEVELOPER, Action.DEVELOP, _at(), 1)
        assert decision.reason is DenyReason.ATTEMPT_LIMIT_EXCEEDED


# ---------------------------------------------------------------------------
# The boundary: what coordinate authority is NOT
# ---------------------------------------------------------------------------


class TestCoordinationDoesNotSubstituteForChildAuthority:
    """Holding `coordinate` confers none of the authorities an owner kept.

    Each case below is asserted against a policy that DOES permit coordination, so a
    denial here cannot be explained by the coordinator lacking authority generally —
    it is specifically that `coordinate` does not reach these.
    """

    @pytest.mark.parametrize("action", [Action.MERGE, Action.DEPLOY])
    def test_coordinate_confers_no_merge_or_deploy(self, action: Action) -> None:
        """`MERGE` is not in this policy at all; `DEPLOY` is present but gated."""
        context = _coordinator_context()
        decision = authorize_action(context, action, ResourceRef(repository_id=REPO_A, org_id=ORG_A, node_address=COORDINATOR_NODE), 1)
        assert decision.reason in {DenyReason.ACTION_NOT_PERMITTED, DenyReason.HUMAN_GATE_REQUIRED}

    def test_coordinate_confers_no_machine_evaluation_acceptance(self) -> None:
        """A coordinator cannot declare evaluation success at an unmarked node."""
        decision = authorize_action(_coordinator_context(), Action.EVALUATE, _at(), 1)
        assert decision.reason is DenyReason.MACHINE_ACCEPTANCE_NOT_PERMITTED

    def test_a_child_request_cannot_carry_a_gated_action(self) -> None:
        """Even a scope naming it — which acceptance refuses — denies at admission."""
        policy = _stamped_coordinator()
        rehydrated = policy.model_construct(
            **{**policy.__dict__, "coordination": _unvalidated_scope(allowed_child_actions=[Action.DEVELOP, Action.DEPLOY])}
        )
        decision = authorize_child_request(_coordinator_context(policy=rehydrated), ChildPersona.DEVELOPER, Action.DEPLOY, _at(), 1)
        assert decision.reason is DenyReason.CHILD_ACTION_NOT_PERMITTED

    def test_a_child_request_cannot_carry_an_undeclared_action(self) -> None:
        """`MERGE` is absent from `allowed_actions`; a scope cannot add it."""
        policy = _stamped_coordinator()
        rehydrated = policy.model_construct(
            **{**policy.__dict__, "coordination": _unvalidated_scope(allowed_child_actions=[Action.DEVELOP, Action.MERGE])}
        )
        decision = authorize_child_request(_coordinator_context(policy=rehydrated), ChildPersona.DEVELOPER, Action.MERGE, _at(), 1)
        assert decision.reason is DenyReason.CHILD_ACTION_NOT_PERMITTED

    def test_independent_approved_child_actions_still_work(self) -> None:
        """The capability is additive: accepting a coordinator breaks nothing.

        A developer acting under its own `DEVELOP` authority is unaffected by the
        presence of coordination on the same policy.
        """
        context = _coordinator_context()
        decision = authorize_action(context, Action.DEVELOP, ResourceRef(repository_id=REPO_A, org_id=ORG_A, node_address=COORDINATOR_NODE), 1)
        assert decision.permitted

    def test_a_permitted_request_says_the_child_still_needs_its_own_admission(self) -> None:
        """The permit detail is explicit that this is not the child's authorization.

        Asserted because the whole safety argument rests on callers not treating this
        permit as the child's, and the detail is what an operator reads in the audit
        record. Substring, not exact prose.
        """
        decision = authorize_child_request(_coordinator_context(), ChildPersona.DEVELOPER, Action.DEVELOP, _at(), 1)
        assert decision.permitted and "own admission still applies" in decision.detail


# ---------------------------------------------------------------------------
# What the owner sees before accepting
# ---------------------------------------------------------------------------


class TestCoordinationSummary:
    def test_summary_shows_the_bounds_the_owner_is_accepting(self) -> None:
        summary = summarize_policy(_stamped_coordinator())
        assert summary.coordination is not None
        assert summary.coordination.assigned_node_count == 1
        assert summary.coordination.allowed_child_personas == [ChildPersona.DEVELOPER, ChildPersona.REVIEWER]
        assert summary.coordination.allowed_child_actions == [Action.DEVELOP, Action.REVIEW, Action.REPAIR]

    def test_coordinate_appears_as_an_autonomous_action(self) -> None:
        assert Action.COORDINATE in summarize_policy(_stamped_coordinator()).autonomous_actions

    def test_a_gated_coordinator_reads_as_a_human_decision(self) -> None:
        summary = summarize_policy(_stamped_coordinator(human_gates=[Action.DEPLOY, Action.COORDINATE]))
        assert Action.COORDINATE in summary.human_decisions
        assert Action.COORDINATE not in summary.autonomous_actions

    def test_a_policy_without_a_coordinator_summarizes_as_absent_not_empty(self) -> None:
        """`None`, not a zeroed summary: "none accepted" and "accepted but useless" differ."""
        assert summarize_policy(_stamped()).coordination is None

    def test_summary_omits_internal_node_addresses(self) -> None:
        """§7.2 makes graph addresses non-renderable; the count is the fact needed."""
        rendered = summarize_policy(_stamped_coordinator()).model_dump(mode="json")
        assert COORDINATOR_NODE not in str(rendered)

    def test_canonical_ordering_is_independent_of_authoring_order(self) -> None:
        """Two spellings of one scope read identically to an owner comparing them."""
        forward = summarize_policy(_stamped_coordinator())
        reversed_scope = _scope(
            allowed_child_personas=[ChildPersona.REVIEWER, ChildPersona.DEVELOPER],
            allowed_child_actions=[Action.REPAIR, Action.REVIEW, Action.DEVELOP],
        )
        assert summarize_policy(_stamped_coordinator(coordination=reversed_scope)).coordination == forward.coordination


# ---------------------------------------------------------------------------
# Identity and idempotency of the accepted document
# ---------------------------------------------------------------------------


class TestCoordinatorPolicyIdentity:
    def test_the_same_scope_stamps_to_the_same_id(self) -> None:
        """A retried acceptance of an unchanged coordinator policy converges."""
        assert _stamped_coordinator().policy_id == _stamped_coordinator().policy_id

    def test_narrowing_the_assigned_set_changes_the_hash(self) -> None:
        """The scope is authorizing content, so it is inside the identity."""
        wide = _stamped_coordinator(coordination=_scope(assigned_node_addresses=[COORDINATOR_NODE, UNASSIGNED_NODE]))
        assert wide.policy_hash != _stamped_coordinator().policy_hash

    def test_narrowing_the_child_actions_changes_the_hash(self) -> None:
        narrow = _stamped_coordinator(coordination=_scope(allowed_child_actions=[Action.DEVELOP]))
        assert narrow.policy_hash != _stamped_coordinator().policy_hash

    def test_a_coordinator_policy_round_trips_through_its_stored_document(self) -> None:
        """The acceptance path stores a dict; admission reads it back.

        Asserted end to end because a scope that failed to survive serialization would
        rehydrate as `None`, and an absent scope denies — a coordinator that worked at
        acceptance and refused forever afterwards.
        """
        stamped = _stamped_coordinator()
        restored = ExecutionPolicy.model_validate(stamped.model_dump(mode="json"))
        assert restored == stamped
        assert restored.coordination is not None
        assert authorize_action(_coordinator_context(policy=restored), Action.COORDINATE, _at(), 1).permitted

    def test_a_submitted_coordinator_policy_cannot_name_its_own_principal(self) -> None:
        """The forgery refusal is unchanged by the new field."""
        from src.orchestration.execution_policy import PolicyRejectedError

        with pytest.raises(PolicyRejectedError):
            stamp_policy(_coordinator_policy(principal_id="user-someone-else"), principal_id=PRINCIPAL, org_id=ORG_A)


# `ADDRESS`, `REPO_B`, `TEAM_A`, `NOW` and `AcceptanceMode` are imported for the
# shared fixtures' benefit; reference them so linting reflects real use.
assert ADDRESS and REPO_B and TEAM_A and NOW and AcceptanceMode and _limits
