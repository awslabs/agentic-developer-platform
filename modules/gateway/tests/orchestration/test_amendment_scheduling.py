"""Non-live amendment adoption through the normal ownership and scheduler paths."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from sqlalchemy import select

from src.orchestration.amendment_impact import assess_amendment
from src.orchestration.compile import ProposalRejectedError
from src.orchestration.dispatch_pass import publish_pending, run_dispatch_pass
from src.orchestration.execution_policy import AcceptanceMode, ExecutionPolicy, PolicyLimits
from src.orchestration.flow_budget import get_flow_reservations
from src.orchestration.flow_meter import meter_target, prepare_flow_meter, read_flow_meter
from src.orchestration.models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationAmendmentIssueBinding,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.pending_amendments import (
    accept_amendment,
    publish_for_acceptance,
    register_amendment_draft,
    revise_amendment,
)
from src.orchestration.proposal import ProposedEdge, ProposedNode
from src.orchestration.repository_evaluation_contract import harness_digest
from src.orchestration.run_store import EngineRunStore
from src.orchestration.tick import run_tick
from src.orchestration.work_admission import admit
from src.orchestration.work_claims import ClaimOwner, OwnerKind
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.usage import UsageLog
from tests.orchestration import amendment_evidence
from tests.orchestration import test_dispatch_pass as dispatch_fixture
from tests.orchestration.legacy_plan import persist_legacy_plan
from tests.orchestration.test_pending_amendments import address, amender, approval, base_proposal, open_request
from tests.orchestration.test_policy_admission import _make_usage

engine = dispatch_fixture.engine
session_factory = dispatch_fixture.session_factory
session = dispatch_fixture.session
run_store = dispatch_fixture.run_store
work_claims_enabled = dispatch_fixture.work_claims_enabled
policy_bound_dispatch = dispatch_fixture.policy_bound_dispatch
protected_engine = dispatch_fixture.protected_engine
provider_evidence = amendment_evidence.provider_evidence

ORG = dispatch_fixture.ORG_A
REPO = dispatch_fixture.REPO
CHECKER = address("cleanup-checker", wave="cleanup")
VALIDATION = address("cleanup-validation", wave="cleanup")
INPUTS = address("authorized-inputs", wave="prerequisites")
ACCEPTANCE = address("deploy-gate", wave="wave-2")


def cleanup_proposal(base):
    proposal = base.model_copy(deep=True)
    proposal.execution_policy.evaluation_acceptance[VALIDATION] = AcceptanceMode.MACHINE
    proposal.nodes.extend(
        [
            ProposedNode(
                address=CHECKER,
                kind="story",
                title="Implement cleanup checker",
                issue_ref="6301",
                repository_identity=REPO,
                provider_repository_id=12345,
                criteria=["cleanup-checker"],
                evidence_expectations=["verified-merge"],
            ),
            ProposedNode(address=INPUTS, kind="gate", title="Authorize the cleanup target and inputs"),
            ProposedNode(
                address=VALIDATION,
                kind="eval",
                title="Observe cleanup validation evidence",
                issue_ref="6302",
                repository_identity=REPO,
                provider_repository_id=12345,
                criteria=["cleanup-validation"],
                required_inputs=[dict(name="authorized-cleanup-inputs", source=f"node:{INPUTS}")],
                evidence_expectations=["trusted-evaluation"],
                evaluation=dict(
                    evidence_schema="repository-evaluation/v1",
                    runner=dict(adapter="engine-repository-evidence-v1", repository=REPO, repository_id=12345, harness_sha256=harness_digest()),
                    predecessors=[dict(address=CHECKER, required_checks=[dict(name="Unit tests", app_id=15368)])],
                    workflows=[
                        dict(
                            criterion_id="cleanup-execution",
                            path=".github/workflows/cleanup-validation.yml",
                            source=dict(predecessor=CHECKER),
                            definition=dict(predecessor=CHECKER),
                            required_jobs=["Validate cleanup"],
                            artifacts=[
                                dict(
                                    name="cleanup-result",
                                    path="result.json",
                                    predicates=[dict(criterion_id="cleanup-validation", pointer="/passed", operation="equals", expected=True)],
                                )
                            ],
                        )
                    ],
                ),
            ),
        ]
    )
    proposal.edges.extend(
        ProposedEdge(from_address=parent, to_address=child)
        for parent, child in [(address("story-a"), CHECKER), (CHECKER, VALIDATION), (VALIDATION, ACCEPTANCE)]
    )
    return type(base).model_validate(proposal.model_dump(mode="json"))


@pytest.mark.parametrize(
    "conflicting_issue", [None, 6301, 6302], ids=["dispatch-and-restart", "checker-already-claimed", "validation-already-claimed"]
)
async def test_adopted_checker_and_read_only_validation_survive_acceptance_and_scheduler_restart(
    session, session_factory, work_claims_enabled, policy_bound_dispatch, protected_engine, conflicting_issue, provider_evidence, monkeypatch
):
    await dispatch_fixture._make_org(session)
    for user_id in (approval().actor_id, amender().actor_id):
        session.add(User(id=user_id, org_id=ORG, team_id="team-test", email=f"{user_id}@example.com", cognito_sub=user_id))
        session.add(TenantMembership(user_id=user_id, tenant_id=ORG, role="org_admin"))
    base = base_proposal(
        execution_policy=ExecutionPolicy(
            org_id=ORG,
            repository_ids=[REPO],
            allowed_actions=["develop", "review", "repair", "merge", "evaluate"],
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            limits=PolicyLimits(max_wall_clock_seconds=86400, max_spend_usd=Decimal("100"), max_attempts_per_node=10, max_concurrent_actions=5),
        )
    )
    base.nodes[0].issue_ref = "6101"
    compiled = await persist_legacy_plan(session, base, approval())
    flow_id = compiled.flow_id
    (await session.get(OrchestrationFlow, flow_id)).execution_paused = False
    original = {node.node_ref: node for node in await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == flow_id))}
    assert await prepare_flow_meter(org_id=ORG, flow_id=flow_id, policy=base.execution_policy, nodes=list(original.values()))
    target = meter_target(org_id=ORG, flow_id=flow_id, policy=base.execution_policy)
    store = get_flow_reservations()
    assert (await store.reserve("completed-work", Decimal("5"), [target])).admitted
    await store.reconcile("completed-work", Decimal("5"), [target])
    for node_ref in ("story-a", "eval"):
        original[node_ref].state = "passed"
        original[node_ref].attempts = 3
        await _make_usage(session, original[node_ref], flow_slug=base.flow_slug, cost_usd="2.50")
    history = OrchestrationDecision(
        org_id=ORG,
        flow_id=flow_id,
        node_id=original["story-a"].id,
        kind=DecisionKind.RESULT_OBSERVED.value,
        actor_id="system:results",
        actor_kind="service",
        actor_role="engine",
        from_state="running",
        to_state="passed",
        reason='{"evidence_url":"https://github.com/example/project/pull/101","fixture":"previously verified merge"}',
    )
    session.add(history)
    await session.flush()
    original_nodes = {ref: (node.id, node.state, node.attempts, node.issue_ref) for ref, node in original.items()}
    history_id, history_reason = history.id, history.reason
    old_plan = (await session.scalars(select(OrchestrationAcceptedPlan))).one()
    old_hash, old_document = old_plan.plan_hash, old_plan.plan_document
    request = await open_request(session, flow_id, text="Adopt the checker and cleanup validation follow-up issues")
    proposed = cleanup_proposal(base)
    draft = await register_amendment_draft(session, org_id=ORG, request=request, author_run_id=request.author_run_id, proposal=proposed)
    proposed.edges.append(ProposedEdge(from_address=INPUTS, to_address=VALIDATION))
    revision = await revise_amendment(
        session, org_id=ORG, draft_id=draft.draft_id, proposal=proposed, revised_by=approval().actor_id, flow_id=flow_id
    )
    preview = await assess_amendment(session, org_id=ORG, flow_id=flow_id, proposal=proposed)
    assert not preview.blocking
    assert preview.issue_publications == []
    assert revision.proposal_hash != draft.proposal_hash
    assert {(edge.from_address, edge.to_address) for edge in preview.edges if edge.change == "added"} == {
        (address("story-a"), CHECKER),
        (CHECKER, VALIDATION),
        (INPUTS, VALIDATION),
        (VALIDATION, ACCEPTANCE),
    }
    assert len(list(await session.scalars(select(OrchestrationNode)))) == len(original)
    assert (await run_dispatch_pass(session, dispatch_fixture._config())).dispatched == 0
    assert list(await session.scalars(select(OrchestrationWorkClaim))) == []
    accept_args = dict(
        draft_id=revision.draft_id,
        actor=amender(),
        flow_id=flow_id,
        expected_proposal_hash=revision.proposal_hash,
        expected_base_plan_version=request.base_plan_version,
        expected_base_plan_hash=request.base_plan_hash,
    )
    publisher = SimpleNamespace(create_issue=AsyncMock(side_effect=AssertionError("Adoption must not publish another issue")))
    if conflicting_issue is not None:
        await admit(
            session,
            org_id=ORG,
            repository_id=12345,
            issue=conflicting_issue,
            owner=ClaimOwner(OwnerKind.DIRECT_DISPATCH, "existing-assignment"),
            invocation_id="existing-run",
        )
        assert await publish_for_acceptance(session, **accept_args, publisher=publisher) == []
        with pytest.raises(ProposalRejectedError, match="active_assignment_conflict") as refused:
            await accept_amendment(session, **accept_args)
        assert "existing-assignment" in str(refused.value)
        assert len(list(await session.scalars(select(OrchestrationNode)))) == len(original)
        assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1
        claim = (await session.scalars(select(OrchestrationWorkClaim))).one()
        assert claim.owner_ref == "existing-assignment" and claim.active_run_id == "existing-run"
        assert list(await session.scalars(select(OrchestrationExecution))) == []
        assert list(await session.scalars(select(OrchestrationAmendmentIssueBinding))) == []
        publisher.create_issue.assert_not_awaited()
        return
    assert await publish_for_acceptance(session, **accept_args, publisher=publisher) == []
    accepted = await accept_amendment(session, **accept_args)
    await session.commit()
    await session.close()

    async with session_factory() as restarted:
        replay = await accept_amendment(restarted, **accept_args)
        assert replay.replayed and replay.plan_version == accepted.plan_version == 2
        assert replay.decision_id == accepted.decision_id
        nodes = {node.node_ref: node for node in await restarted.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == flow_id))}
        for ref, snapshot in original_nodes.items():
            node = nodes[ref]
            assert (node.id, node.state, node.attempts, node.issue_ref) == snapshot
        assert (await restarted.get(OrchestrationDecision, history_id)).reason == history_reason
        assert sorted(await restarted.scalars(select(UsageLog.cost_usd))) == [Decimal("2.50"), Decimal("2.50")]
        plans = list(await restarted.scalars(select(OrchestrationAcceptedPlan).order_by(OrchestrationAcceptedPlan.version)))
        assert len(plans) == 2
        assert (plans[0].plan_hash, plans[0].plan_document) == (old_hash, old_document)
        assert plans[1].plan_document["nodes"] == proposed.model_dump(mode="json")["nodes"]
        assert plans[1].plan_document["execution_policy"]["limits"] == old_document["execution_policy"]["limits"]
        assert (await read_flow_meter(org_id=ORG, flow_id=flow_id, policy=base.execution_policy)).total_usd == Decimal("5")
        assert list(await restarted.scalars(select(OrchestrationAmendmentIssueBinding))) == []
        assert nodes["cleanup-validation"].kind == "eval"
        assert nodes["cleanup-validation"].issue_ref == "6302"
        await run_tick(restarted)
        report = await run_dispatch_pass(restarted, dispatch_fixture._config())
        assert report.dispatched == 1 and len(report.pending) == 1
        envelope = report.pending[0].envelope
        assert envelope["source_ref"]["issue"] == 6301
        assert envelope["source_ref"]["repo"] == REPO
        assert envelope["source_ref"]["provider_repository_id"] == 12345
        assert envelope["work_claim_required"] and envelope["handoff_required"]
        claims = list(await restarted.scalars(select(OrchestrationWorkClaim)))
        executions = list(await restarted.scalars(select(OrchestrationExecution)))
        assert len(claims) == len(executions) == 1
        assert claims[0].owner_ref == flow_id and claims[0].active_run_id == envelope["message_id"]
        assert executions[0].node_id == nodes["cleanup-checker"].id
        assert executions[0].accepted_plan_version == 2
        assert (executions[0].claim_id, executions[0].claim_generation) == (claims[0].id, claims[0].generation)
        await restarted.commit()
        queue = dispatch_fixture.FakeSQS()
        publish_pending(report, dispatch_fixture._config(), client=queue)
        assert len(queue.calls) == 1
        assert report.publish_failed == 0
        reporting_store = EngineRunStore(boto3.resource("dynamodb", region_name="us-east-1").Table("events"))
        reporting_row = reporting_store.get(envelope["message_id"], envelope["arrived_at"])
        assert reporting_row["engine_node_id"] == nodes["cleanup-checker"].id
        assert reporting_row["engine_attempt"] == 1

    async with session_factory() as recovered:
        await run_tick(recovered)
        replay_report = await run_dispatch_pass(recovered, dispatch_fixture._config())
        assert replay_report.dispatched == 0 and replay_report.publish_failed == 0
        nodes = {node.node_ref: node for node in await recovered.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == flow_id))}
        assert (nodes["cleanup-checker"].state, nodes["cleanup-checker"].attempts) == ("running", 1)
        assert (nodes["cleanup-validation"].state, nodes["cleanup-validation"].attempts) == ("pending", 0)
        assert nodes["authorized-inputs"].state == "awaiting_gate"
        assert nodes["deploy-gate"].state == "pending"
        assert len(list(await recovered.scalars(select(OrchestrationWorkClaim)))) == 1
        assert len(list(await recovered.scalars(select(OrchestrationExecution)))) == 1
        assert len(list(await recovered.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "node_dispatched")))) == 1
        original_edges = {
            (original_nodes["story-a"][0], original_nodes["eval"][0]),
            (original_nodes["eval"][0], original_nodes["deploy-gate"][0]),
        }
        actual_edges = {(edge.from_node_id, edge.to_node_id) for edge in await recovered.scalars(select(OrchestrationEdge))}
        assert original_edges <= actual_edges
    publisher.create_issue.assert_not_awaited()
    await amendment_evidence.verify_validation_progression(
        session_factory,
        flow_id=flow_id,
        actor=amender(),
        plan_hash=plans[1].plan_hash,
        config=dispatch_fixture._config(),
        data=provider_evidence,
        monkeypatch=monkeypatch,
    )
    async with session_factory() as completed:
        for ref in ("story-a", "eval"):
            node = await completed.get(OrchestrationNode, original_nodes[ref][0])
            assert (node.id, node.state, node.attempts, node.issue_ref) == original_nodes[ref]
        assert (await completed.get(OrchestrationDecision, history_id)).reason == history_reason
        assert len(list(await completed.scalars(select(OrchestrationAcceptedPlan)))) == 2
        assert len(list(await completed.scalars(select(OrchestrationWorkClaim)))) == 1
