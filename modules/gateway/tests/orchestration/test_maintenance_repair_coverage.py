"""A status reread cannot resurrect a repair already proven by owner adoption."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import tests.orchestration.test_maintenance_squash_history as fixtures
from src.agentauth.github_operations import OperationRefusedError
from src.orchestration.accepted_node import accepted_node_contract
from src.orchestration.controls import ResumeRequest, resume_node
from src.orchestration.dispatch_pass import attempt_run_id, run_dispatch_pass
from src.orchestration.models import OrchestrationAction, OrchestrationExecution
from src.orchestration.operations_repair import RepairMergeRequiredError, repair_revisions
from src.orchestration.operations_tasks import read_operations_tasks, record_repair_pr
from src.orchestration.policy_admission import load_in_force_policy

engine = fixtures.engine
session = fixtures.session
governed_work_ownership = fixtures.governed_work_ownership
healthy_policy_reservations = fixtures.healthy_policy_reservations
policy_budget_initializers = fixtures.policy_budget_initializers
REPO = fixtures.original.fixtures.REPO


async def restarted(session, monkeypatch):
    b = await fixtures.arrange(session, monkeypatch)
    for action in (b.publication, b.second_publication):
        action.detail = {**action.detail, "pull_number": 7, "base": fixtures.SQUASH_PARENT}
    await session.flush()
    b.adopted = await fixtures.accept(session, b)
    b.contract = await accepted_node_contract(session, b.node)
    monkeypatch.setattr("src.orchestration.operations_release_resolution.ReleaseProvider", lambda: b.provider)
    await resume_node(
        b.node.id,
        ResumeRequest(reason="Continue unfinished work from the reviewed maintenance checkpoint."),
        SimpleNamespace(org_id=b.node.org_id, user_id=b.actor.actor_id),
        SimpleNamespace(check_permission=AsyncMock(), get_user_role=AsyncMock(return_value=[SimpleNamespace(value="org_admin")])),
        session,
        None,
    )
    dispatched = await run_dispatch_pass(session, fixtures.original.fixtures._config())
    assert dispatched.dispatched == 1
    b.pins = dispatched.pending[0].envelope["orchestration"]["deployment_revisions"]
    b.current = await session.scalar(
        select(OrchestrationExecution).where(OrchestrationExecution.node_id == b.node.id, OrchestrationExecution.cycle == 2)
    )
    b.assignment = SimpleNamespace(
        tenant_id=b.node.org_id,
        node_id=b.node.id,
        invocation_id=attempt_run_id(b.node.id, 2),
        attempt=2,
        accepted_plan_version=2,
        claim_generation=b.current.claim_generation,
        repository=REPO,
    )
    b.kwargs = dict(node_id=b.node.id, org_id=b.node.org_id, plan_version=2, cycle=2, pins=b.pins, contract=b.contract, require_merged=True)
    return b


async def legacy_reread(session, b, *, head=fixtures.SQUASH_HEAD, pull=7, receipt=None, repository=None):
    action = OrchestrationAction(
        org_id=b.node.org_id,
        execution_id=b.current.id,
        operation_key=f"operations-repair-pr:{pull}:{head}:{fixtures.SQUASH_PARENT}",
        kind="operations_repair_pr",
        status="succeeded",
        attempt=1,
        receipt_ref=receipt or f"github/{REPO}/pull/{pull}",
        detail={"pull_number": pull, "head": head, "base": fixtures.SQUASH_PARENT, **({"repository": repository} if repository else {})},
    )
    session.add(action)
    await session.flush()
    return action


@pytest.mark.parametrize("head", [fixtures.SQUASH_HEAD, fixtures.original.PUBLISHED])
async def test_existing_reread_is_covered_without_erasing_history_or_rewinding_pins(session, monkeypatch, governed_work_ownership, head):
    b = await restarted(session, monkeypatch)
    observed = await legacy_reread(session, b, head=head)
    before = copy.deepcopy(observed.detail)
    assert await repair_revisions(session, **b.kwargs) == b.pins
    board = await read_operations_tasks(session, b.assignment)
    assert board["maintenance_adoption"]["pins"] == b.pins
    assert board["maintenance_adoption"]["checkpoint_cycle"] == 1
    assert "Continue unfinished" in board["maintenance_adoption"]["next_action"]
    evidence = board["cycles"][-1]["evidence"][0]
    coverage = evidence["covered_by_adoption"]
    assert coverage["adoption_id"] == b.adopted["decision_id"]
    assert coverage["source_revision"] == fixtures.original.MERGE
    assert coverage["basis"] == ("reviewed_squash" if head == fixtures.SQUASH_HEAD else "included_publication")
    assert b.publication.id in coverage["publication_ids"]
    assert observed.detail == before and await session.get(OrchestrationAction, observed.id) is observed
    assert not await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_repair_merge"))


async def test_new_status_read_returns_actionable_coverage_without_new_publication(session, monkeypatch, governed_work_ownership):
    b = await restarted(session, monkeypatch)
    for _ in range(2):
        result = await record_repair_pr(
            session,
            b.assignment,
            {"pull_number": 7, "head": fixtures.SQUASH_HEAD, "base": fixtures.SQUASH_PARENT, "merged": True, "checks_verified": True},
        )
        assert result["publication_recorded"] is False
        assert result["covered_by_adoption"]["basis"] == "reviewed_squash"
        assert "do not merge or republish" in result["next_action"]
    assert not await session.scalar(
        select(OrchestrationAction).where(OrchestrationAction.execution_id == b.current.id, OrchestrationAction.kind == "operations_repair_pr")
    )
    assert await repair_revisions(session, **b.kwargs) == b.pins


@pytest.mark.parametrize("fault", ["head", "receipt", "foreign_receipt", "repository", "pull_number"])
async def test_changed_observation_identity_remains_an_unmerged_blocker(session, monkeypatch, governed_work_ownership, fault):
    b = await restarted(session, monkeypatch)
    args = {
        "head": {"head": "6" * 40},
        "receipt": {"receipt": f"github/{REPO}/pull/99"},
        "foreign_receipt": {"receipt": "github/foreign/repo/pull/7"},
        "repository": {"repository": "foreign/repo"},
        "pull_number": {"pull": 99},
    }[fault]
    await legacy_reread(session, b, **args)
    with pytest.raises(RepairMergeRequiredError, match="operations_repair_merge_required"):
        await repair_revisions(session, **b.kwargs)
    evidence = (await read_operations_tasks(session, b.assignment))["cycles"][-1]["evidence"][0]
    assert "covered_by_adoption" not in evidence


@pytest.mark.parametrize("pull", [7, 10])
async def test_new_repair_requires_its_own_merge_and_advances_from_adopted_source(session, monkeypatch, governed_work_ownership, pull):
    b = await restarted(session, monkeypatch)
    await legacy_reread(session, b)
    head, merge = "6" * 40, "5" * 40
    result = await record_repair_pr(session, b.assignment, {"pull_number": pull, "head": head, "base": b.pins["source_revision"]})
    assert result["publication_recorded"] is True and "covered_by_adoption" not in result
    with pytest.raises(RepairMergeRequiredError):
        await repair_revisions(session, **b.kwargs)
    policy = (await load_in_force_policy(session, org_id=b.node.org_id, flow_id=b.node.flow_id)).policy
    session.add(
        OrchestrationAction(
            org_id=b.node.org_id,
            execution_id=b.current.id,
            operation_key=f"operations-repair:{pull}:{head}",
            kind="operations_repair_merge",
            status="succeeded",
            attempt=1,
            receipt_ref=f"github/{REPO}/pull/{pull}",
            detail={
                "pull_number": pull,
                "head": head,
                "merge_sha": merge,
                "repository": REPO,
                "default_branch": "main",
                "policy_hash": policy.policy_hash,
                "workflows": ["gateway-deploy.yml"],
                "repair_paths": sorted(policy.repair_paths),
                "before_source_revision": b.pins["source_revision"],
                "before_definition_revision": b.pins["definition_revision"],
                "claim_generation": b.current.claim_generation,
                "run_id": b.assignment.invocation_id,
            },
        )
    )
    await session.flush()
    assert await repair_revisions(session, **b.kwargs) == {**b.pins, "source_revision": merge, "definition_revision": merge}


async def test_changed_checkpoint_is_not_coverage_or_a_new_publication(session, monkeypatch, governed_work_ownership):
    b = await restarted(session, monkeypatch)
    await legacy_reread(session, b)
    b.publication.detail = {**b.publication.detail, "head": "6" * 40}
    await session.flush()
    assert await repair_revisions(session, **b.kwargs) is None
    with pytest.raises(OperationRefusedError, match="checkpoint is unverified"):
        await record_repair_pr(session, b.assignment, {"pull_number": 7, "head": fixtures.SQUASH_HEAD, "base": fixtures.SQUASH_PARENT})
    with pytest.raises(OperationRefusedError, match="checkpoint is unverified"):
        await read_operations_tasks(session, b.assignment)
