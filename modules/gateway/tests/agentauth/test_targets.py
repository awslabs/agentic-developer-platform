"""Authoritative target resolution — the unit that decides AC3.

``evaluate_grant`` has the refusals; this resolver supplies the facts they act on.
So the adversarial cases belong here rather than only at the policy layer: a
resolver that reported ``DESCENDANT`` for every run in the tenant would pass every
test in ``test_policy.py`` (they inject a fake resolver) and would hand a
coordinator control over unrelated work.

The two cases worth reading first are ``TestSibling`` and ``TestAncestor``. Those
are the AC3 attacks most likely to look legitimate — a sibling shares a parent, an
ancestor shares the whole subtree — and neither is a special case in the
implementation, so a test is the only thing holding them.
"""

from __future__ import annotations

import pytest

from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.grants import TargetRelationship
from src.agentauth.run_credential import CREDENTIAL_KEY_ENV, mint_credential, verify_credential
from src.agentauth.targets import MAX_LINEAGE_DEPTH, AuthorityTargetResolver

TENANT = "org-tenant-001"
OTHER_TENANT = "org-tenant-002"
ENV = {CREDENTIAL_KEY_ENV: "targets-test-key-not-a-real-secret"}


def record(
    invocation_id: str,
    *,
    tenant_id: str = TENANT,
    attempt: int = 1,
    parent_principal: str | None = None,
    flow_id: str | None = None,
    status: ExecutionStatus = ExecutionStatus.ACTIVE,
) -> ExecutionRecord:
    return ExecutionRecord(
        invocation_id=invocation_id,
        tenant_id=tenant_id,
        current_attempt=attempt,
        status=status,
        current_credential_epoch=1,
        min_acceptable_credential_epoch=1,
        parent_principal=parent_principal,
        flow_id=flow_id,
    )


class FakeExecutions:
    """Stands in for the protected authority table's execution reads.

    Keyed by ``(tenant_id, invocation_id)`` exactly as the real store is, so a
    test cannot accidentally succeed by reading across tenants when the real
    lookup would not.
    """

    def __init__(self, records: list[ExecutionRecord] | None = None, *, fail: bool = False):
        self.records = {(r.tenant_id, r.invocation_id): r for r in (records or [])}
        self.fail = fail
        self.reads: list[tuple[str, str]] = []

    def load_execution(self, *, invocation_id: str, tenant_id: str) -> ExecutionRecord | None:
        self.reads.append((tenant_id, invocation_id))
        if self.fail:
            raise RuntimeError("authority store unavailable")
        return self.records.get((tenant_id, invocation_id))


def caller_credential(invocation_id: str, *, attempt: int = 1, tenant_id: str = TENANT, flow_id: str | None = None):
    """A genuinely minted and verified credential, not a hand-built object.

    Going through mint/verify matters: the resolver compares against
    ``credential.principal``, and a hand-constructed credential could carry a
    principal shape that verification would never produce.
    """
    token = mint_credential(
        invocation_id=invocation_id,
        attempt=attempt,
        tenant_id=tenant_id,
        flow_id=flow_id,
        env=ENV,
    )
    return verify_credential(token, env=ENV)


def resolver(records: list[ExecutionRecord], **kwargs) -> AuthorityTargetResolver:
    return AuthorityTargetResolver(executions=FakeExecutions(records), **kwargs)


class TestSelf:
    """A run reading itself. The path every ordinary run takes."""

    def test_own_run_resolves_as_self(self):
        facts = resolver([record("run-a")]).resolve(run_id="run-a", caller=caller_credential("run-a"))

        assert facts is not None
        assert facts.relationships == frozenset({TargetRelationship.SELF})

    def test_self_does_not_also_claim_descendant_of_itself(self):
        # A record whose parent pointer names itself must not resolve as its own
        # descendant. Self returns early precisely so corrupt lineage cannot make
        # a run its own ancestor and inherit authority over itself.
        facts = resolver([record("run-a", parent_principal="run-a#1")]).resolve(run_id="run-a", caller=caller_credential("run-a"))

        assert facts.relationships == frozenset({TargetRelationship.SELF})


class TestDescendant:
    """The legitimate coordinator path. AC1 — refusals must not break this."""

    def test_direct_child_is_a_descendant(self):
        records = [record("coord"), record("child", parent_principal="coord#1")]

        facts = resolver(records).resolve(run_id="child", caller=caller_credential("coord"))

        assert TargetRelationship.DESCENDANT in facts.relationships

    def test_transitive_grandchild_is_a_descendant(self):
        records = [
            record("coord"),
            record("child", parent_principal="coord#1"),
            record("grandchild", parent_principal="child#1"),
        ]

        facts = resolver(records).resolve(run_id="grandchild", caller=caller_credential("coord"))

        assert TargetRelationship.DESCENDANT in facts.relationships

    def test_descendant_of_a_different_attempt_is_not_inherited(self):
        # Attempt 2 of the coordinator did not dispatch attempt 1's children.
        # Treating them as its own would let a retried pod inherit control over
        # work it never started, so the caller comparison is attempt-exact.
        records = [record("coord", attempt=2), record("child", parent_principal="coord#1")]

        facts = resolver(records).resolve(run_id="child", caller=caller_credential("coord", attempt=2))

        assert TargetRelationship.DESCENDANT not in facts.relationships

    def test_intermediate_record_on_a_newer_attempt_still_links(self):
        # Ancestry is a property of invocations, so a parent that has since moved
        # to attempt 2 is still the same parent. Only the final caller comparison
        # is attempt-exact; intermediate hops are looked up by invocation.
        records = [
            record("coord"),
            record("child", attempt=2, parent_principal="coord#1"),
            record("grandchild", parent_principal="child#1"),
        ]

        facts = resolver(records).resolve(run_id="grandchild", caller=caller_credential("coord"))

        assert TargetRelationship.DESCENDANT in facts.relationships


class TestSibling:
    """AC3: a sibling is refused. Shares a parent, but not the caller."""

    def test_sibling_is_not_a_descendant(self):
        records = [
            record("parent"),
            record("sibling-a", parent_principal="parent#1"),
            record("sibling-b", parent_principal="parent#1"),
        ]

        facts = resolver(records).resolve(run_id="sibling-b", caller=caller_credential("sibling-a"))

        assert facts is not None
        assert facts.relationships == frozenset()

    def test_niece_is_not_a_descendant(self):
        # One level deeper: the target is the sibling's child. The walk up from it
        # passes through the sibling and the shared parent, never the caller.
        records = [
            record("parent"),
            record("sibling-a", parent_principal="parent#1"),
            record("sibling-b", parent_principal="parent#1"),
            record("niece", parent_principal="sibling-b#1"),
        ]

        facts = resolver(records).resolve(run_id="niece", caller=caller_credential("sibling-a"))

        assert facts.relationships == frozenset()


class TestAncestor:
    """AC3: an ancestor is refused. Control does not flow upward."""

    def test_parent_is_not_a_descendant_of_its_child(self):
        records = [record("coord"), record("child", parent_principal="coord#1")]

        facts = resolver(records).resolve(run_id="coord", caller=caller_credential("child"))

        assert facts is not None
        assert facts.relationships == frozenset()

    def test_grandparent_is_not_a_descendant(self):
        records = [
            record("root"),
            record("middle", parent_principal="root#1"),
            record("leaf", parent_principal="middle#1"),
        ]

        facts = resolver(records).resolve(run_id="root", caller=caller_credential("leaf"))

        assert facts.relationships == frozenset()


class TestCrossTenant:
    """AC3: cross-tenant targets are not found, not found-and-refused."""

    def test_target_in_another_tenant_is_not_found(self):
        records = [record("mine"), record("theirs", tenant_id=OTHER_TENANT)]

        facts = resolver(records).resolve(run_id="theirs", caller=caller_credential("mine"))

        # None rather than empty relationships: a caller able to distinguish
        # "exists but forbidden" from "does not exist" can enumerate another
        # tenant's run IDs.
        assert facts is None

    def test_lookup_is_scoped_to_the_caller_tenant(self):
        executions = FakeExecutions([record("theirs", tenant_id=OTHER_TENANT)])
        target_resolver = AuthorityTargetResolver(executions=executions)

        target_resolver.resolve(run_id="theirs", caller=caller_credential("mine"))

        assert executions.reads == [(TENANT, "theirs")]

    def test_a_store_returning_a_foreign_tenant_is_refused(self):
        # Defence in depth against a store bug: the lookup is tenant-keyed, so
        # this should be unreachable. If it happens anyway, isolation must hold.
        class WrongTenantStore:
            def load_execution(self, *, invocation_id, tenant_id):
                return record(invocation_id, tenant_id=OTHER_TENANT)

        facts = AuthorityTargetResolver(executions=WrongTenantStore()).resolve(run_id="run-x", caller=caller_credential("mine"))

        assert facts is None


class TestFlowNode:
    """A shared flow is a fact. It is not authority — the grant decides that."""

    def test_same_flow_reports_flow_node(self):
        records = [
            record("coord", flow_id="flow-1"),
            record("node", flow_id="flow-1"),
        ]

        facts = resolver(records).resolve(run_id="node", caller=caller_credential("coord"))

        assert TargetRelationship.FLOW_NODE in facts.relationships

    def test_different_flow_reports_nothing(self):
        records = [
            record("coord", flow_id="flow-1"),
            record("node", flow_id="flow-2"),
        ]

        facts = resolver(records).resolve(run_id="node", caller=caller_credential("coord"))

        assert facts.relationships == frozenset()

    def test_shared_tenant_without_a_flow_grants_no_relationship(self):
        # AC3's headline: "a common tenant or human root alone grants no control
        # authority." Two unrelated runs in one tenant, neither in a flow.
        records = [record("coord"), record("unrelated")]

        facts = resolver(records).resolve(run_id="unrelated", caller=caller_credential("coord"))

        assert facts.relationships == frozenset()

    def test_flow_comes_from_the_store_not_the_credential(self):
        # The credential's flow was true when minted; a long-lived credential
        # outlives changes to it. The store's value is the current one, so a
        # credential claiming a flow its record does not have establishes nothing.
        records = [record("coord"), record("node", flow_id="flow-1")]

        facts = resolver(records).resolve(run_id="node", caller=caller_credential("coord", flow_id="flow-1"))

        assert facts.relationships == frozenset()

    def test_caller_without_a_protected_record_shares_no_flow(self):
        facts = resolver([record("node", flow_id="flow-1")]).resolve(run_id="node", caller=caller_credential("ghost"))

        assert facts.relationships == frozenset()


class TestCorruptLineage:
    """Corrupt lineage must refuse, and must not spin."""

    def test_cycle_is_refused_and_terminates(self):
        records = [
            record("a", parent_principal="b#1"),
            record("b", parent_principal="a#1"),
        ]

        facts = resolver(records).resolve(run_id="a", caller=caller_credential("coord"))

        assert facts.relationships == frozenset()

    def test_chain_longer_than_the_bound_is_refused(self):
        # A chain deeper than dispatch can create is corrupt data. "I ran out of
        # budget" must not become "the relationship holds", so the caller sitting
        # at the far end of an over-long chain is still refused.
        depth = MAX_LINEAGE_DEPTH + 3
        records = [record("coord")]
        previous = "coord#1"
        for index in range(depth):
            name = f"link-{index}"
            records.append(record(name, parent_principal=previous))
            previous = f"{name}#1"

        facts = resolver(records).resolve(run_id=f"link-{depth - 1}", caller=caller_credential("coord"))

        assert TargetRelationship.DESCENDANT not in facts.relationships

    def test_missing_intermediate_record_fails_closed(self):
        # The caller may genuinely be further up, but an unprovable relationship
        # is not a relationship. Skipping the gap would make a deleted record an
        # authority bypass.
        records = [
            record("coord"),
            record("orphan", parent_principal="vanished#1"),
        ]

        facts = resolver(records).resolve(run_id="orphan", caller=caller_credential("coord"))

        assert facts.relationships == frozenset()

    @pytest.mark.parametrize("pointer", ["", "no-attempt", "#1", "run#notanumber", "run#"])
    def test_malformed_parent_pointer_is_refused(self, pointer):
        records = [record("coord"), record("child", parent_principal=pointer)]

        facts = resolver(records).resolve(run_id="child", caller=caller_credential("coord"))

        assert TargetRelationship.DESCENDANT not in facts.relationships

    def test_store_failure_mid_walk_refuses(self):
        class FailingOnParent:
            def load_execution(self, *, invocation_id, tenant_id):
                if invocation_id == "child":
                    return record("child", parent_principal="middle#1")
                raise RuntimeError("authority store unavailable")

        facts = AuthorityTargetResolver(executions=FailingOnParent()).resolve(run_id="child", caller=caller_credential("coord"))

        assert facts.relationships == frozenset()

    def test_store_failure_on_the_target_read_is_not_found(self):
        target_resolver = AuthorityTargetResolver(executions=FakeExecutions(fail=True))

        assert target_resolver.resolve(run_id="child", caller=caller_credential("coord")) is None


class TestReportedFacts:
    """Fields the policy and envelope binding consume."""

    def test_terminal_status_is_reported(self):
        records = [record("coord"), record("done", parent_principal="coord#1", status=ExecutionStatus.COMPLETED)]

        facts = resolver(records).resolve(run_id="done", caller=caller_credential("coord"))

        assert facts.is_terminal is True

    def test_pending_is_not_terminal(self):
        # PENDING is not actionable, but reporting it as terminal would tell a
        # coordinator its child had finished before it started.
        records = [record("coord"), record("new", parent_principal="coord#1", status=ExecutionStatus.PENDING)]

        facts = resolver(records).resolve(run_id="new", caller=caller_credential("coord"))

        assert facts.is_terminal is False

    def test_generation_defaults_to_zero_without_a_reader(self):
        facts = resolver([record("run-a")]).resolve(run_id="run-a", caller=caller_credential("run-a"))

        assert facts.generation == 0

    def test_generation_comes_from_the_injected_reader(self):
        class Reader:
            def read_generation(self, *, run_id, tenant_id):
                return 7

        target_resolver = AuthorityTargetResolver(executions=FakeExecutions([record("run-a")]), generation_reader=Reader())

        assert target_resolver.resolve(run_id="run-a", caller=caller_credential("run-a")).generation == 7

    @pytest.mark.parametrize("bogus", [-1, None, "3", True])
    def test_nonsense_generation_reports_zero(self, bogus):
        # 0 matches no registered generation, so a nonsense value refuses at the
        # listener rather than binding an envelope to an arbitrary number.
        class Reader:
            def read_generation(self, *, run_id, tenant_id):
                return bogus

        target_resolver = AuthorityTargetResolver(executions=FakeExecutions([record("run-a")]), generation_reader=Reader())

        assert target_resolver.resolve(run_id="run-a", caller=caller_credential("run-a")).generation == 0

    def test_reader_failure_reports_zero_rather_than_raising(self):
        class Reader:
            def read_generation(self, *, run_id, tenant_id):
                raise RuntimeError("unreadable")

        target_resolver = AuthorityTargetResolver(executions=FakeExecutions([record("run-a")]), generation_reader=Reader())

        assert target_resolver.resolve(run_id="run-a", caller=caller_credential("run-a")).generation == 0

    def test_run_id_and_flow_are_taken_from_the_store(self):
        facts = resolver([record("run-a", flow_id="flow-9")]).resolve(run_id="run-a", caller=caller_credential("run-a"))

        assert (facts.run_id, facts.flow_id, facts.tenant_id) == ("run-a", "flow-9", TENANT)


class TestMissingInputs:
    def test_empty_run_id_is_not_found(self):
        assert resolver([record("run-a")]).resolve(run_id="", caller=caller_credential("run-a")) is None

    def test_unknown_run_is_not_found(self):
        assert resolver([record("run-a")]).resolve(run_id="nope", caller=caller_credential("run-a")) is None
