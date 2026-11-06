"""The amendment impact report: what accepting this would actually do (#5329).

`src/orchestration/amendment_impact.py` answers the question "accept amendment abc123?"
makes of a reviewer. #4529 gave them a gate diff and a base version; neither says what
the amendment does to work already in flight, nor what the engine will run afterwards,
nor whether acceptance will even be permitted.

What is asserted here, and why each matters:

  - **State, not just shape.** A removed node is reported with the state it is in *now*.
    "1 node removed" reads completely differently for a `pending` node and a `passed`
    one, and the diff of two documents contains neither state.
  - **Every predictable refusal, before acceptance.** The blocking diagnostics are
    asserted against what `amend_plan` actually raises — started-node prerequisites,
    definition changes on a reused address, and the state machine's own refusal to
    supersede. A preview that reports "no problems" and is then refused is worse than
    no preview.
  - **All blockers at once.** `amend_plan` stops at the first; this is asserted to
    report the whole list, because a reviewer fixing one per round trip is bisecting by
    hand.
  - **The human-only edge is asked of the state machine.** The same document is
    acceptable for a human and refused for a service actor when it drops `passed` work,
    and that fact is not derivable from any diff — it lives in `LEGAL_TRANSITIONS`.
  - **Derived waves are reported.** Wave membership is stored nowhere, so "Wave 3 did
    not exist until now" appears in no node diff. Asserted including the emptied case.
  - **Read-only.** Asserted by counting rows and states before and after: a preview that
    wrote anything could not be safe to call on every edit.

The session fixture is `test_compile.py`'s, including its two pysqlite hooks. They are
load-bearing (see that file): without them `begin_nested()` becomes the outermost unit of
work, and the read-only assertions here would pass for the wrong reason.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.amend import AmendmentContext, FlowNotFoundError, amend_plan
from src.orchestration.amendment_impact import assess_amendment
from src.orchestration.compile import ApprovalContext, ProposalRejectedError
from src.orchestration.execution_policy import Action, ExecutionPolicy, PolicyLimits
from src.orchestration.models import OrchestrationEdge, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.proposal import LoopProposal, ProposedEdge, ProposedNode
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import ActorKind, NodeState
from src.shared.models.base import Base
from tests.orchestration.legacy_plan import persist_legacy_plan

ORG_A = "org-alpha"
ORG_B = "org-beta"
FLOW = "superplane"
SPEC = "issue-5329-r1"


@pytest.fixture
async def session():
    """In-memory SQLite session with working SAVEPOINTs. See module docstring."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def approval():
    return ApprovalContext(
        org_id=ORG_A,
        actor_id="cognito-sub-planner",
        actor_role="org_admin",
        actor_kind=ActorKind.HUMAN,
        reason="Original plan accepted.",
    )


def at(ref: str, *, epic: str = "epic-1", wave: str = "wave-1") -> str:
    return f"{FLOW}/{epic}/{wave}/{ref}"


def base_plan(**overrides) -> LoopProposal:
    """The plan in force: Wave 1 delivers two stories and evaluates; Wave 2 gates deploy."""
    payload = {
        "flow_slug": FLOW,
        "title": "Superplane delivery",
        "org_id": ORG_A,
        "spec_revision": SPEC,
        "intent_ref": "4191",
        "nodes": [
            ProposedNode(address=at("u1"), kind="story", title="Story U1", issue_ref="5301"),
            ProposedNode(address=at("u2"), kind="story", title="Story U2", issue_ref="5302"),
            ProposedNode(address=at("acceptance"), kind="eval", title="Wave 1 acceptance"),
            ProposedNode(address=at("deploy-gate", wave="wave-2"), kind="gate", title="Deploy gate"),
        ],
        "edges": [
            ProposedEdge(from_address=at("u1"), to_address=at("acceptance")),
            ProposedEdge(from_address=at("u2"), to_address=at("acceptance")),
            ProposedEdge(from_address=at("acceptance"), to_address=at("deploy-gate", wave="wave-2")),
        ],
    }
    payload.update(overrides)
    return LoopProposal(**payload)


def with_cleanup_after_u1() -> LoopProposal:
    """The amendment: cleanup work inserted after U1, additively, feeding the eval."""
    plan = base_plan()
    return base_plan(
        nodes=[
            *plan.nodes,
            ProposedNode(address=at("cleanup"), kind="story", title="Cleanup checker", issue_ref="5410"),
        ],
        edges=[
            *plan.edges,
            ProposedEdge(from_address=at("u1"), to_address=at("cleanup")),
            ProposedEdge(from_address=at("cleanup"), to_address=at("acceptance")),
        ],
    )


def dropping(*refs: str) -> LoopProposal:
    """The plan in force minus the named wave-1 nodes, and minus their edges."""
    dropped = {at(ref) for ref in refs}
    plan = base_plan()
    return base_plan(
        nodes=[node for node in plan.nodes if node.address not in dropped],
        edges=[edge for edge in plan.edges if edge.from_address not in dropped and edge.to_address not in dropped],
    )


async def accepted_flow(session, approval, proposal: LoopProposal | None = None) -> str:
    """Compile a plan into force and return its flow id."""
    result = await persist_legacy_plan(session, proposal or base_plan(), approval)
    await session.commit()
    return result.flow_id


async def set_state(session, flow_id: str, ref: str, state: NodeState, *, attempts: int = 0) -> None:
    """Move one node to `state` directly.

    The state machine is bypassed on purpose: these tests need a node parked in
    `running` or `passed` as a *precondition*, and driving it there through the real
    transitions would test the tick rather than the report.
    """
    await session.execute(
        sa.update(OrchestrationNode)
        .where(OrchestrationNode.flow_id == flow_id, OrchestrationNode.node_ref == ref)
        .values(state=state.value, attempts=attempts)
    )
    await session.commit()


def by_address(impact) -> dict[str, object]:
    return {node.address: node for node in impact.nodes}


def rules(impact) -> set[str]:
    return {violation.rule for violation in impact.blocking}


class TestStructuralDiff:
    """Nodes, edges and derived wave membership, before versus after."""

    async def test_added_nodes_and_edges_are_reported_with_no_state(self, session, approval):
        """An added node has no row, so it has no state. Reported as None, not `pending`.

        `pending` is a real state an existing node can be in, so using it for "does not
        exist yet" would make the two indistinguishable in a review.
        """
        flow_id = await accepted_flow(session, approval)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        cleanup = by_address(impact)[at("cleanup")]
        assert cleanup.change == "added"
        assert cleanup.state is None
        assert cleanup.attempts == 0
        assert {(edge.from_address, edge.to_address) for edge in impact.edges if edge.change == "added"} == {
            (at("u1"), at("cleanup")),
            (at("cleanup"), at("acceptance")),
        }
        assert [edge for edge in impact.edges if edge.change == "removed"] == []

    async def test_a_removed_node_is_reported_with_the_state_and_attempts_it_has_now(self, session, approval):
        """The fact a diff of two documents cannot contain.

        "1 node removed" is a different decision when that node is `running` with two
        attempts spent than when it is untouched, and only the graph rows know which.
        """
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u2", NodeState.RUNNING, attempts=2)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=dropping("u2"))

        removed = by_address(impact)[at("u2")]
        assert removed.change == "removed"
        assert removed.state == NodeState.RUNNING.value
        assert removed.attempts == 2

    async def test_unchanged_edges_are_omitted_rather_than_listed(self, session, approval):
        """An amendment restates the whole graph; listing every restatement buries the changes."""
        flow_id = await accepted_flow(session, approval)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        assert len(impact.edges) == 2, [(edge.change, edge.from_address, edge.to_address) for edge in impact.edges]

    async def test_a_node_kept_verbatim_is_unchanged_and_carries_its_current_state(self, session, approval):
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u1", NodeState.PASSED)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        unchanged = by_address(impact)[at("u1")]
        assert unchanged.change == "unchanged"
        assert unchanged.state == NodeState.PASSED.value

    async def test_an_already_superseded_node_is_not_reported_as_removed(self, session, approval):
        """It was already not in force, so listing it pads the review with retired work.

        `_supersede_absent_nodes` skips it for the same reason, so reporting it would
        also overstate what acceptance is going to do.
        """
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u2", NodeState.SUPERSEDED)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=dropping("u2"))

        assert at("u2") not in by_address(impact)

    async def test_the_report_names_the_base_version_and_hash_it_compared_against(self, session, approval):
        """What a caller binds an approval to. Without it, "preview then accept" has no anchor."""
        flow_id = await accepted_flow(session, approval)
        in_force = await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_A, flow_id=flow_id)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        assert impact.base_plan_version == in_force.version
        assert impact.base_plan_hash == in_force.plan_hash

    async def test_an_existing_node_definition_change_has_reviewable_before_and_after_values(self, session, approval):
        flow_id = await accepted_flow(session, approval)
        renamed = base_plan()
        renamed.nodes[0].title = "Story U1 (clarified)"
        renamed.nodes[0].issue_ref = "9999"

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=renamed)

        modified = by_address(impact)[at("u1")]
        assert modified.change == "modified"
        assert modified.before["title"] == "Story U1"
        assert modified.before["issue_ref"] == "5301"
        assert modified.after["title"] == "Story U1 (clarified)"
        assert modified.after["issue_ref"] == "9999"
        assert impact.is_noop is False
        assert impact.acceptable

    async def test_a_policy_only_authority_change_is_never_reported_as_a_noop(self, session, approval):
        flow_id = await accepted_flow(session, approval)
        proposal = base_plan(
            execution_policy=ExecutionPolicy(
                org_id=ORG_A,
                repository_ids=["repo-1"],
                allowed_actions=[Action.DEVELOP],
                expires_at=datetime.now(UTC) + timedelta(days=1),
                limits=PolicyLimits(
                    max_wall_clock_seconds=3600,
                    max_spend_usd=Decimal("50.00"),
                    max_attempts_per_node=3,
                    max_concurrent_actions=2,
                ),
            )
        )

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert impact.policy_changed is True
        assert impact.policy_before is None
        assert impact.policy_after is not None
        assert impact.policy_after.autonomous_actions == [Action.DEVELOP]
        assert impact.is_noop is False

    async def test_an_existing_issue_owned_by_another_flow_blocks_acceptance(self, session, approval):
        flow_id = await accepted_flow(session, approval)
        proposal = with_cleanup_after_u1()
        proposal.nodes[-1].repository_identity = "aws-e/adp"
        proposal.nodes[-1].provider_repository_id = 99
        session.add(
            OrchestrationWorkClaim(
                org_id=ORG_A,
                provider_repository_id=99,
                issue_number=5410,
                owner_kind="engine_flow",
                owner_ref="another-flow",
                state="held",
            )
        )
        await session.commit()

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        conflict = next(item for item in impact.blocking if item.rule == "active_assignment_conflict")
        assert conflict.where == at("cleanup")
        assert "another-flow" in conflict.message
        assert impact.acceptable is False

    @pytest.mark.parametrize(
        ("replacement_limit", "remove"),
        [
            (Decimal("75.00"), False),
            (Decimal("10.00"), False),
            (None, True),
        ],
        ids=["policy-widened", "policy-narrowed", "policy-removed"],
    )
    async def test_policy_bound_changes_show_complete_before_and_after(self, session, approval, replacement_limit, remove):
        policy = ExecutionPolicy(
            org_id=ORG_A,
            repository_ids=["repo-1"],
            allowed_actions=[Action.DEVELOP],
            expires_at=datetime.now(UTC) + timedelta(days=1),
            limits=PolicyLimits(
                max_wall_clock_seconds=3600,
                max_spend_usd=Decimal("50.00"),
                max_attempts_per_node=3,
                max_concurrent_actions=2,
            ),
        )
        flow_id = await accepted_flow(session, approval, proposal=base_plan(execution_policy=policy))
        replacement = None
        if not remove:
            replacement = policy.model_copy(update={"limits": policy.limits.model_copy(update={"max_spend_usd": replacement_limit})})

        impact = await assess_amendment(
            session,
            org_id=ORG_A,
            flow_id=flow_id,
            proposal=base_plan(execution_policy=replacement),
        )

        assert impact.policy_before is not None
        assert impact.policy_after is None if remove else impact.policy_after is not None
        assert impact.policy_changed is True
        assert impact.is_noop is False


class TestWaveMembership:
    """Waves are derived from addresses and stored nowhere, so they need reporting."""

    async def test_a_wave_no_node_belonged_to_is_reported_as_new(self, session, approval):
        """Nothing in the node diff says "Wave 3 did not exist until now"."""
        flow_id = await accepted_flow(session, approval)
        plan = base_plan()
        proposal = base_plan(
            nodes=[
                *plan.nodes,
                ProposedNode(address=at("cleanup", wave="wave-3"), kind="story", title="Cleanup"),
                ProposedNode(address=at("cleanup-eval", wave="wave-3"), kind="eval", title="Wave 3 acceptance"),
            ],
            edges=[
                *plan.edges,
                ProposedEdge(from_address=at("deploy-gate", wave="wave-2"), to_address=at("cleanup", wave="wave-3")),
                ProposedEdge(from_address=at("cleanup", wave="wave-3"), to_address=at("cleanup-eval", wave="wave-3")),
            ],
        )

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        new_waves = [change for change in impact.wave_changes if change.is_new]
        assert [change.wave for change in new_waves] == ["epic-1/wave-3"]
        assert new_waves[0].added_addresses == sorted([at("cleanup", wave="wave-3"), at("cleanup-eval", wave="wave-3")])

    async def test_a_wave_left_with_no_members_is_reported_as_emptied(self, session, approval):
        """Emptying a wave removes a stage of the plan, which the node diff shows only obliquely."""
        flow_id = await accepted_flow(session, approval)
        plan = base_plan()
        proposal = base_plan(
            nodes=[node for node in plan.nodes if "wave-2" not in node.address],
            edges=[edge for edge in plan.edges if "wave-2" not in edge.to_address],
        )

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        emptied = [change for change in impact.wave_changes if change.is_emptied]
        assert [change.wave for change in emptied] == ["epic-1/wave-2"]

    async def test_a_wave_whose_membership_is_unchanged_is_not_listed(self, session, approval):
        flow_id = await accepted_flow(session, approval)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        assert [change.wave for change in impact.wave_changes] == ["epic-1/wave-1"]


class TestExecutionOrder:
    """What the engine will run, and what it will run at the same time."""

    async def test_concurrently_runnable_waves_share_a_stage(self, session, approval):
        """Grouped by stage because the question is "what runs alongside what".

        Staging comes from `preview.derive_waves`, imported rather than re-derived, so
        the amendment preview cannot disagree with the draft preview about one graph.
        """
        flow_id = await accepted_flow(session, approval)
        plan = base_plan()
        proposal = base_plan(
            nodes=[
                *plan.nodes,
                ProposedNode(address=f"{FLOW}/epic-2/wave-1/spike", kind="story", title="Independent spike"),
                ProposedNode(address=f"{FLOW}/epic-2/wave-1/spike-eval", kind="eval", title="Spike acceptance"),
            ],
            edges=[
                *plan.edges,
                ProposedEdge(from_address=f"{FLOW}/epic-2/wave-1/spike", to_address=f"{FLOW}/epic-2/wave-1/spike-eval"),
            ],
        )

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        stage_zero = next(stage for stage in impact.execution_order if stage.stage == 0)
        assert set(stage_zero.waves) == {"epic-1/wave-1", "epic-2/wave-1"}

    async def test_a_dependent_wave_is_reported_at_a_later_stage(self, session, approval):
        flow_id = await accepted_flow(session, approval)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        assert [(stage.stage, stage.waves) for stage in impact.execution_order] == [
            (0, ["epic-1/wave-1"]),
            (1, ["epic-1/wave-2"]),
        ]

    async def test_waves_with_no_determined_order_are_reported_as_unstaged_and_last(self, session, approval):
        """Mutually dependent waves get `stage=None` rather than an invented number.

        `w1/a -> w2/b` with `w2/c -> w1/d` is acyclic over nodes and mutually dependent
        over waves. Printing a number would assert an order the engine will not honour.
        """
        flow_id = await accepted_flow(session, approval)
        proposal = base_plan(
            nodes=[
                ProposedNode(address=at("u1"), kind="story", title="Story U1", issue_ref="5301"),
                ProposedNode(address=at("acceptance"), kind="eval", title="Wave 1 acceptance"),
                ProposedNode(address=at("u3", wave="wave-2"), kind="story", title="Story U3"),
                ProposedNode(address=at("acceptance-2", wave="wave-2"), kind="eval", title="Wave 2 acceptance"),
            ],
            edges=[
                ProposedEdge(from_address=at("u1"), to_address=at("u3", wave="wave-2")),
                ProposedEdge(from_address=at("u3", wave="wave-2"), to_address=at("acceptance-2", wave="wave-2")),
                ProposedEdge(from_address=at("acceptance-2", wave="wave-2"), to_address=at("acceptance")),
            ],
        )

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert impact.execution_order[-1].stage is None
        assert set(impact.execution_order[-1].waves) == {"epic-1/wave-1", "epic-1/wave-2"}


class TestDownstreamImpact:
    """Whose release condition moves, and what state those nodes are in."""

    async def test_a_node_that_gains_a_prerequisite_is_reported_as_newly_blocked(self, session, approval):
        """The direction that can park work indefinitely behind something unscheduled."""
        flow_id = await accepted_flow(session, approval)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        acceptance = next(item for item in impact.downstream if item.address == at("acceptance"))
        assert acceptance.added_prerequisites == [at("cleanup")]
        assert acceptance.removed_prerequisites == []
        assert acceptance.newly_blocked

    async def test_a_node_whose_prerequisites_do_not_move_is_absent_from_downstream(self, session, approval):
        """Unaffected no matter how much of the graph around it changed.

        The engine releases it on exactly the same condition as before, so listing it
        would report an impact that does not exist.
        """
        flow_id = await accepted_flow(session, approval)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        assert {item.address for item in impact.downstream} == {at("acceptance")}

    async def test_losing_a_prerequisite_is_reported_too(self, session, approval):
        """Removing a prerequisite releases work earlier, which is also a schedule change."""
        flow_id = await accepted_flow(session, approval)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=dropping("u2"))

        acceptance = next(item for item in impact.downstream if item.address == at("acceptance"))
        assert acceptance.removed_prerequisites == [at("u2")]
        assert not acceptance.newly_blocked

    async def test_downstream_carries_the_state_each_affected_node_is_in(self, session, approval):
        """A prerequisite added to a `running` node is a refusal, not a delay — see below."""
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "acceptance", NodeState.RUNNING)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        acceptance = next(item for item in impact.downstream if item.address == at("acceptance"))
        assert acceptance.state == NodeState.RUNNING.value


class TestGateChanges:
    """Gate placement is the human decision points; moving one reads as an ordinary edit."""

    async def test_an_added_gate_is_reported(self, session, approval):
        flow_id = await accepted_flow(session, approval)
        plan = base_plan()
        proposal = base_plan(
            nodes=[*plan.nodes, ProposedNode(address=at("cleanup-gate", wave="wave-2"), kind="gate", title="Cleanup sign-off")],
            edges=[
                *plan.edges,
                ProposedEdge(from_address=at("deploy-gate", wave="wave-2"), to_address=at("cleanup-gate", wave="wave-2")),
            ],
        )

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert impact.gates_added == [at("cleanup-gate", wave="wave-2")]
        assert impact.gates_removed == []

    async def test_a_removed_gate_is_reported(self, session, approval):
        """The consequence that is invisible in a plan-document diff: a lost human decision."""
        flow_id = await accepted_flow(session, approval)
        plan = base_plan()
        proposal = base_plan(
            nodes=[node for node in plan.nodes if node.kind != "gate"],
            edges=[edge for edge in plan.edges if "deploy-gate" not in edge.to_address],
        )

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert impact.gates_removed == [at("deploy-gate", wave="wave-2")]

    async def test_an_already_superseded_gate_is_not_reported_as_removed(self, session, approval):
        """A false gate-removal warning is the kind that gets warnings ignored."""
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "deploy-gate", NodeState.SUPERSEDED)
        plan = base_plan()
        proposal = base_plan(
            nodes=[node for node in plan.nodes if node.kind != "gate"],
            edges=[edge for edge in plan.edges if "deploy-gate" not in edge.to_address],
        )

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert impact.gates_removed == []


class TestBlockingDiagnostics:
    """Every refusal the acceptance path would raise, found before the reviewer approves."""

    async def test_changing_a_started_nodes_prerequisites_blocks_acceptance(self, session, approval):
        """The exact case `amend_plan` refuses, predicted rather than discovered as a 4xx."""
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "acceptance", NodeState.RUNNING)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        assert not impact.acceptable
        assert "amendment_started_node_prerequisites" in rules(impact)

    async def test_the_started_node_blocker_names_both_prerequisite_sets(self, session, approval):
        """An author told only "refused" has to reconstruct why from two documents."""
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "acceptance", NodeState.RUNNING)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        message = next(v.message for v in impact.blocking if v.rule == "amendment_started_node_prerequisites")
        assert at("u1") in message and at("cleanup") in message
        assert NodeState.RUNNING.value in message

    async def test_a_pending_node_may_gain_prerequisites_freely(self, session, approval):
        """The guard is about work already in flight, not about insertion in general.

        If this blocked, every ordinary insertion would be refused and the guard would
        be noise rather than a protection.
        """
        flow_id = await accepted_flow(session, approval)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        assert impact.acceptable, [str(violation) for violation in impact.blocking]

    async def test_a_started_node_may_gain_successors_without_blocking(self, session, approval):
        """Only the *incoming* set is guarded: gaining a successor delays other work, not this node."""
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u1", NodeState.PASSED, attempts=1)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        assert impact.acceptable, [str(violation) for violation in impact.blocking]

    async def test_a_running_node_gaining_successors_is_blocked_only_as_in_flight_work(self, session, approval):
        """The graph change itself is legal; what blocks is stranding the running node's authority."""
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u1", NodeState.RUNNING)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        assert rules(impact) == {"active_nodes_cross_plan_boundary"}
        assert at("u1") in impact.blocking[0].message

    async def test_changing_the_kind_of_a_reused_address_blocks_acceptance(self, session, approval):
        """Kind is execution identity: the row keeps its attempts while what they prove changes."""
        flow_id = await accepted_flow(session, approval)
        proposal = base_plan()
        proposal.nodes[0].kind = "eval"

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert "amendment_definition_change" in rules(impact)
        assert "cannot change kind" in next(v.message for v in impact.blocking if v.rule == "amendment_definition_change")

    async def test_repointing_a_started_nodes_issue_blocks_acceptance(self, session, approval):
        """Re-homing work already attributed to another issue."""
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u1", NodeState.RUNNING)
        proposal = base_plan()
        proposal.nodes[0].issue_ref = "9999"

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert "cannot change issue_ref" in next(v.message for v in impact.blocking if v.rule == "amendment_definition_change")

    async def test_correcting_an_untouched_nodes_issue_is_allowed(self, session, approval):
        """The correction the guard deliberately still permits."""
        flow_id = await accepted_flow(session, approval)
        proposal = base_plan()
        proposal.nodes[0].issue_ref = "5399"

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert impact.acceptable, [str(violation) for violation in impact.blocking]

    async def test_reusing_a_superseded_address_blocks_acceptance(self, session, approval):
        """A spent execution identity. `SUPERSEDED` has no legal outgoing transition."""
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u2", NodeState.SUPERSEDED)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=base_plan())

        assert "cannot reuse superseded node" in next(v.message for v in impact.blocking if v.rule == "amendment_definition_change")

    async def test_document_validation_failures_are_reported_as_blockers(self, session, approval):
        """The authoritative validator's own violations, not a second opinion about them."""
        flow_id = await accepted_flow(session, approval)
        proposal = base_plan(
            nodes=[
                ProposedNode(address=at("u1"), kind="story", title="Story U1", issue_ref="5301"),
                ProposedNode(address=at("smuggled"), kind="wave", title="A container as a node"),
            ],
            edges=[],
        )

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert {"container_as_node", "wave_eval_cardinality"} <= rules(impact)

    async def test_a_cycle_is_reported_as_a_blocker(self, session, approval):
        flow_id = await accepted_flow(session, approval)
        proposal = base_plan(
            nodes=[
                ProposedNode(address=at("u1"), kind="story", title="Story U1"),
                ProposedNode(address=at("u2"), kind="story", title="Story U2"),
                ProposedNode(address=at("acceptance"), kind="eval", title="Wave 1 acceptance"),
            ],
            edges=[
                ProposedEdge(from_address=at("u1"), to_address=at("u2")),
                ProposedEdge(from_address=at("u2"), to_address=at("u1")),
                ProposedEdge(from_address=at("u1"), to_address=at("acceptance")),
            ],
        )

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert not impact.acceptable
        assert any("cycle" in violation.rule for violation in impact.blocking), rules(impact)

    async def test_every_blocker_is_reported_together_not_just_the_first(self, session, approval):
        """`amend_plan` raises on the first; a reviewer fixing one per round trip is bisecting."""
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u1", NodeState.RUNNING)
        await set_state(session, flow_id, "acceptance", NodeState.RUNNING)
        proposal = with_cleanup_after_u1()
        # Two independent problems on two different nodes, plus a document-level one.
        proposal.nodes[1].kind = "eval"
        proposal.nodes.append(ProposedNode(address=at("smuggled"), kind="epic", title="Container as node"))

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert {"amendment_started_node_prerequisites", "amendment_definition_change", "container_as_node"} <= rules(impact)

    async def test_each_blocker_names_the_node_it_is_about(self, session, approval):
        """A document-level "refused" points an author at nothing."""
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "acceptance", NodeState.RUNNING)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())

        started = next(v for v in impact.blocking if v.rule == "amendment_started_node_prerequisites")
        assert started.where == at("acceptance")


class TestSupersedeAuthority:
    """Whether the amendment may discard existing work is asked of the state machine."""

    @pytest.mark.parametrize("state", [NodeState.PASSED, NodeState.FAILED])
    async def test_a_service_actor_cannot_supersede_terminal_work_and_the_preview_says_so(self, session, approval, state):
        """`passed -> superseded` is human-only, which no diff of two documents shows.

        This is the case the report exists for: an agent-proposed amendment that drops
        accepted work is refused by `LEGAL_TRANSITIONS`, and a reviewer reading "1 node
        removed" would have no way to know.
        """
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u2", state, attempts=3)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=dropping("u2"), actor_kind=ActorKind.SERVICE)

        assert not impact.acceptable
        assert "amendment_cannot_supersede" in rules(impact)
        assert next(v for v in impact.blocking if v.rule == "amendment_cannot_supersede").where == at("u2")

    @pytest.mark.parametrize("state", [NodeState.PASSED, NodeState.FAILED])
    async def test_the_same_document_is_acceptable_for_a_human(self, session, approval, state):
        """Which is why `actor_kind` is a parameter and not assumed.

        A preview that hard-coded `HUMAN` would under-report for the agent path; one
        that hard-coded `SERVICE` would block a legitimate human replan.
        """
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u2", state, attempts=3)

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=dropping("u2"), actor_kind=ActorKind.HUMAN)

        assert impact.acceptable, [str(violation) for violation in impact.blocking]

    async def test_dropping_a_pending_node_is_acceptable_for_either_actor(self, session, approval):
        flow_id = await accepted_flow(session, approval)

        for actor_kind in (ActorKind.HUMAN, ActorKind.SERVICE):
            impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=dropping("u2"), actor_kind=actor_kind)
            assert impact.acceptable, (actor_kind, [str(violation) for violation in impact.blocking])


class TestAgreementWithAcceptance:
    """The property that makes the preview worth reading: it predicts the real outcome."""

    async def test_a_document_the_preview_calls_acceptable_is_accepted(self, session, approval):
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u1", NodeState.PASSED)
        proposal = with_cleanup_after_u1()

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)
        assert impact.acceptable, [str(violation) for violation in impact.blocking]

        result = await amend_plan(
            session,
            flow_id,
            proposal,
            AmendmentContext(org_id=ORG_A, actor_id="human-1", actor_role="org_admin", actor_kind=ActorKind.HUMAN, reason="insert cleanup"),
        )

        assert result.plan_version == impact.base_plan_version + 1

    @pytest.mark.parametrize("blocked_state", [NodeState.RUNNING, NodeState.AWAITING_GATE, NodeState.PASSED])
    async def test_a_document_the_preview_blocks_is_refused_by_acceptance(self, session, approval, blocked_state):
        """Asserted across the states the guard covers, so the two cannot agree by luck.

        `AWAITING_GATE` is included deliberately: a node waiting on a human is the case
        where silently adding a prerequisite would strand a person's pending decision.
        """
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "acceptance", blocked_state)
        proposal = with_cleanup_after_u1()

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)
        assert not impact.acceptable

        with pytest.raises(ProposalRejectedError):
            await amend_plan(
                session,
                flow_id,
                proposal,
                AmendmentContext(org_id=ORG_A, actor_id="human-1", actor_role="org_admin", actor_kind=ActorKind.HUMAN),
            )


class TestReadOnly:
    """A preview safe to call on every edit is one that writes nothing."""

    async def test_assessing_writes_no_rows_and_changes_no_state(self, session, approval):
        flow_id = await accepted_flow(session, approval)
        await set_state(session, flow_id, "u1", NodeState.RUNNING, attempts=1)

        async def snapshot():
            nodes = await session.execute(sa.select(OrchestrationNode.id, OrchestrationNode.state, OrchestrationNode.attempts))
            edges = await session.execute(sa.select(sa.func.count()).select_from(OrchestrationEdge))
            plans = await OrchestrationRepository(session).list_plan_versions(org_id=ORG_A, flow_id=flow_id)
            return sorted(nodes.all()), edges.scalar_one(), [plan.version for plan in plans]

        before = await snapshot()
        await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=with_cleanup_after_u1())
        await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=dropping("u2"))

        assert await snapshot() == before

    async def test_assessing_the_same_document_twice_reports_the_same_thing(self, session, approval):
        """Determinism, because an approval is bound to a reviewed revision.

        If two assessments of one document could differ, "I approved what I previewed"
        would not be a claim anyone could check.
        """
        flow_id = await accepted_flow(session, approval)
        proposal = with_cleanup_after_u1()

        first = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)
        second = await assess_amendment(session, org_id=ORG_A, flow_id=flow_id, proposal=proposal)

        assert first == second


class TestTenantIsolation:
    """A preview leaks graph state, so it is scoped exactly as the amendment is."""

    async def test_another_tenants_flow_is_not_found(self, session, approval):
        """404-shaped, never 403: a 403 would confirm the flow id is real.

        Same reasoning as `FlowNotFoundError`'s: a distinguishable answer lets a caller
        enumerate flow ids and learn which exist.
        """
        flow_id = await accepted_flow(session, approval)

        with pytest.raises(FlowNotFoundError):
            await assess_amendment(session, org_id=ORG_B, flow_id=flow_id, proposal=with_cleanup_after_u1())

    async def test_a_missing_flow_is_not_found(self, session):
        with pytest.raises(FlowNotFoundError):
            await assess_amendment(session, org_id=ORG_A, flow_id="no-such-flow", proposal=base_plan())


class TestUnplannedFlow:
    """A flow with no accepted plan is a real case: amending before any compile."""

    async def test_every_proposed_node_is_added_and_no_base_version_is_reported(self, session, approval):
        """Reported as None rather than 0, which would name a version that never existed."""
        repo = OrchestrationRepository(session)
        flow = await repo.create_flow(org_id=ORG_A, slug=FLOW, title="Superplane delivery")
        await session.commit()

        impact = await assess_amendment(session, org_id=ORG_A, flow_id=flow.id, proposal=base_plan())

        assert impact.base_plan_version is None
        assert impact.base_plan_hash is None
        assert {node.change for node in impact.nodes} == {"added"}
        assert impact.gates_added == [at("deploy-gate", wave="wave-2")]
