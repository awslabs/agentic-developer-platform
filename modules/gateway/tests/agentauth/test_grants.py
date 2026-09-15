"""Shared tenancy and human-rootedness confer no control authority (#5028 AC3, AC7).

The refusals below are the ones the design calls out by name: sibling, ancestor,
cross-flow and cross-tenant targets, and the specific claim that a common tenant
or human root is enough. The allowed cases matter just as much — a policy that
only refuses would break the AI-DLC flow it exists to protect (AC1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.agentauth.grants import (
    AgentAction,
    AuthorityReference,
    AuthorizationDecision,
    DelegatedGrant,
    GrantRefusedError,
    TargetFacts,
    TargetRelationship,
    evaluate_grant,
)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
TENANT = "org-tenant-001"

AUTHORITY = AuthorityReference(
    kind="gate_decision",
    reference_id="decision-abc",
    human_id="human-operator-1",
    org_id=TENANT,
)


def coordinator_grant(**overrides) -> DelegatedGrant:
    """A realistic Operations coordinator grant: dispatch + monitor its flow."""
    kwargs = {
        "grant_id": "grant-coordinator-1",
        "tenant_id": TENANT,
        "principal": "inv-coordinator#1",
        "authority": AUTHORITY,
        "allowed_actions": frozenset({AgentAction.DISPATCH, AgentAction.MONITOR}),
        "target_relationships": frozenset({TargetRelationship.FLOW_NODE, TargetRelationship.DESCENDANT}),
        "flow_id": "flow-42",
        "max_dispatch_concurrency": 3,
        "max_chain_depth": 4,
        "delegable_actions": frozenset({AgentAction.MONITOR}),
    }
    kwargs.update(overrides)
    return DelegatedGrant(**kwargs)


def target(**overrides) -> TargetFacts:
    kwargs = {
        "run_id": "run-developer-7",
        "tenant_id": TENANT,
        "flow_id": "flow-42",
        "relationships": frozenset({TargetRelationship.FLOW_NODE}),
        "generation": 3,
    }
    kwargs.update(overrides)
    return TargetFacts(**kwargs)


@pytest.mark.parametrize("repo,allowed", [("org/approved", True), ("org/another", False), (None, False)])
def test_repository_scope_is_checked_even_for_explicit_target(repo, allowed):
    grant = coordinator_grant(repo_scope=frozenset({"org/approved"}), target_run_ids=frozenset({"run-developer-7"}))
    result = decide(grant, AgentAction.MONITOR, target(repo=repo))
    assert result.allowed is allowed
    if not allowed:
        assert result.reason == "repository_not_permitted"


def decide(grant, action, tgt, caller_tenant=TENANT, caller_principal=None) -> AuthorizationDecision:
    """Decide one request.

    ``caller_principal`` defaults to the grant's own principal so the existing
    cases read as "this grant's holder is calling". It is an explicit parameter
    because the authenticated caller and the grant are separate inputs to the
    real policy — passing the grant's principal is a test convenience, not how
    production resolves it.
    """
    if caller_principal is None:
        caller_principal = grant.principal if grant is not None else "inv-anonymous#1"
    return evaluate_grant(
        grant=grant,
        action=action,
        target=tgt,
        caller_tenant_id=caller_tenant,
        caller_principal=caller_principal,
        now=NOW,
    )


class TestAuthorizedCoordinatorFlow:
    """AC1: the legitimate flow must keep working, without human confirmation."""

    def test_coordinator_may_monitor_a_flow_node_it_coordinates(self):
        d = decide(coordinator_grant(), AgentAction.MONITOR, target())
        assert d.allowed
        assert d.reason == "flow_node"

    def test_coordinator_may_dispatch_within_its_flow(self):
        assert decide(coordinator_grant(), AgentAction.DISPATCH, target()).allowed

    def test_a_run_may_monitor_itself_without_any_grant(self):
        """The common case must not need a provisioned grant."""
        d = decide(
            None,
            AgentAction.MONITOR,
            target(relationships=frozenset({TargetRelationship.SELF})),
        )
        assert d.allowed
        assert d.reason == "self_monitor"

    def test_an_explicitly_granted_control_target_is_authorized(self):
        grant = coordinator_grant(
            allowed_actions=frozenset({AgentAction.MONITOR, AgentAction.ABORT}),
            target_run_ids=frozenset({"run-developer-7"}),
        )
        d = decide(grant, AgentAction.ABORT, target())
        assert d.allowed
        assert d.reason == "explicit_target"

    def test_the_decision_names_the_human_authority_without_making_the_caller_human(self):
        """AC7: truthful service-actor attribution."""
        d = decide(coordinator_grant(), AgentAction.MONITOR, target())
        assert d.principal == "inv-coordinator#1"
        assert d.human_authority_id == "human-operator-1"
        assert d.authority_kind == "gate_decision"


class TestUnauthorizedTargets:
    """AC3: relationship must be granted, not merely shared."""

    def test_cross_tenant_is_refused(self):
        d = decide(coordinator_grant(), AgentAction.MONITOR, target(tenant_id="org-other"))
        assert not d.allowed
        assert d.reason == "cross_tenant"

    def test_a_common_tenant_alone_grants_nothing(self):
        """Same tenant, no relationship the grant permits."""
        d = decide(
            coordinator_grant(),
            AgentAction.MONITOR,
            target(relationships=frozenset()),
        )
        assert not d.allowed
        assert d.reason == "target_not_permitted"

    def test_a_sibling_run_is_refused(self):
        """A sibling is neither a descendant nor a node this caller coordinates."""
        d = decide(
            coordinator_grant(),
            AgentAction.MONITOR,
            target(run_id="run-sibling", relationships=frozenset()),
        )
        assert not d.allowed
        assert d.reason == "target_not_permitted"

    def test_an_ancestor_run_is_refused(self):
        d = decide(
            coordinator_grant(),
            AgentAction.ABORT,
            target(run_id="run-parent", relationships=frozenset()),
        )
        assert not d.allowed

    def test_cross_flow_is_refused_even_when_a_relationship_matches(self):
        """A flow-scoped grant must not reach outside its flow."""
        d = decide(coordinator_grant(), AgentAction.MONITOR, target(flow_id="flow-99"))
        assert not d.allowed
        assert d.reason == "cross_flow"

    def test_a_relationship_the_resolver_found_but_the_grant_omits_is_refused(self):
        """The resolver reports facts; the grant confers authority."""
        grant = coordinator_grant(target_relationships=frozenset({TargetRelationship.DESCENDANT}))
        d = decide(
            grant,
            AgentAction.MONITOR,
            target(relationships=frozenset({TargetRelationship.FLOW_NODE})),
        )
        assert not d.allowed
        assert d.reason == "target_not_permitted"

    def test_an_empty_caller_tenant_is_refused_rather_than_matching_anything(self):
        d = decide(coordinator_grant(), AgentAction.MONITOR, target(), caller_tenant="")
        assert not d.allowed
        assert d.reason == "cross_tenant"


class TestActionScope:
    def test_monitor_authority_does_not_imply_any_control_verb(self):
        """The single most important non-escalation: reading is not controlling."""
        for action in (AgentAction.PAUSE, AgentAction.RESUME, AgentAction.STEER, AgentAction.ABORT):
            d = decide(coordinator_grant(), action, target())
            assert not d.allowed, action
            assert d.reason == "action_not_granted"

    def test_one_granted_control_verb_does_not_imply_another(self):
        grant = coordinator_grant(allowed_actions=frozenset({AgentAction.MONITOR, AgentAction.PAUSE}))
        assert decide(grant, AgentAction.PAUSE, target()).allowed
        assert not decide(grant, AgentAction.ABORT, target()).allowed

    def test_self_relationship_does_not_grant_self_control(self):
        """A compromised worker must not abort or steer itself into new behaviour."""
        d = decide(
            None,
            AgentAction.STEER,
            target(relationships=frozenset({TargetRelationship.SELF})),
        )
        assert not d.allowed
        assert d.reason == "no_grant"


class TestRevocationAndExpiry:
    def test_a_revoked_grant_authorizes_nothing(self):
        d = decide(coordinator_grant(revoked=True), AgentAction.DISPATCH, target())
        assert not d.allowed
        assert d.reason == "grant_revoked"

    def test_an_expired_grant_authorizes_nothing(self):
        grant = coordinator_grant(expires_at=NOW - timedelta(seconds=1))
        d = decide(grant, AgentAction.DISPATCH, target())
        assert not d.allowed
        assert d.reason == "grant_expired"

    def test_the_decision_records_the_epoch_for_queued_action_revalidation(self):
        """AC6: a queued action revalidates against the current epoch."""
        d = decide(coordinator_grant(revocation_epoch=7), AgentAction.MONITOR, target())
        assert d.revocation_epoch == 7


class TestPrivilegeExpansion:
    """AC7: an agent cannot widen what it was delegated."""

    def test_delegable_actions_wider_than_the_grant_are_rejected_at_construction(self):
        with pytest.raises(GrantRefusedError, match="exceed the grant"):
            coordinator_grant(
                allowed_actions=frozenset({AgentAction.MONITOR}),
                delegable_actions=frozenset({AgentAction.MONITOR, AgentAction.ABORT}),
            )

    def test_a_child_requesting_more_than_the_parent_holds_gets_the_subset(self):
        grant = coordinator_grant()
        child = grant.child_grant_actions(frozenset({AgentAction.MONITOR, AgentAction.ABORT, AgentAction.DISPATCH}))
        assert child == frozenset({AgentAction.MONITOR})

    def test_a_grant_with_no_delegable_actions_produces_no_child_authority(self):
        grant = coordinator_grant(delegable_actions=frozenset())
        assert grant.child_grant_actions(frozenset({AgentAction.MONITOR})) == frozenset()


class TestAuthorityReference:
    def test_an_incomplete_authority_reference_cannot_be_constructed(self):
        """No grant may exist without a real human authorization event behind it."""
        for missing in ("kind", "reference_id", "human_id", "org_id"):
            kwargs = {
                "kind": "gate_decision",
                "reference_id": "d-1",
                "human_id": "h-1",
                "org_id": TENANT,
            }
            kwargs[missing] = ""
            with pytest.raises(GrantRefusedError):
                AuthorityReference(**kwargs)


class TestAuditRecords:
    """AC7: both outcomes auditable, neither leaking secrets."""

    def test_refusals_are_recorded_with_caller_target_action_and_outcome(self):
        d = decide(coordinator_grant(), AgentAction.ABORT, target())
        fields = d.to_log_fields()

        assert fields["allowed"] is False
        assert fields["action"] == "abort"
        assert fields["target_run_id"] == "run-developer-7"
        assert fields["reason"] == "action_not_granted"
        assert fields["authority_reference_id"] == "decision-abc"

    def test_log_fields_carry_no_credential_or_instruction_material(self):
        d = decide(coordinator_grant(), AgentAction.MONITOR, target())
        serialized = str(d.to_log_fields())

        for forbidden in ("token", "secret", "instruction", "signature", "envelope"):
            assert forbidden not in serialized.lower()
