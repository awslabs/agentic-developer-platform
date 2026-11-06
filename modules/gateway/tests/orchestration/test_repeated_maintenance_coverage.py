"""Later checkpoints retain only the squash aliases proven by their ancestors."""

import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import tests.orchestration.test_maintenance_repair_coverage as coverage
import tests.orchestration.test_maintenance_squash_history as squash
import tests.orchestration.test_repeated_maintenance_adoption as repeated
from src.orchestration.accepted_node import accepted_node_contract
from src.orchestration.maintenance_adoption_resolution import verified_adoption
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution
from src.orchestration.operations_repair import RepairMergeRequiredError, repair_revisions
from src.orchestration.operations_tasks import read_operations_tasks, record_repair_pr
from src.orchestration.review_cycle import CycleBlockedError

engine = squash.engine
session = squash.session
governed_work_ownership = squash.governed_work_ownership
healthy_policy_reservations = squash.healthy_policy_reservations
policy_budget_initializers = squash.policy_budget_initializers


async def arrange(session, monkeypatch, *, restart=True):
    b = await squash.arrange(session, monkeypatch)
    for action in (b.publication, b.second_publication):
        action.detail = {**action.detail, "pull_number": 7, "base": squash.SQUASH_PARENT}
    await session.flush()
    b.first = await repeated.adopt(session, b)
    b.original = await session.get(OrchestrationDecision, b.first["decision_id"])
    b.original_reason = b.original.reason
    repeated.next_review(b, b.first)
    b.request = b.request.model_copy(update={"squashed_repair_history": []})
    second = await repeated.adopt(session, b)
    repeated.next_review(b, second, source="5", head="4")
    b.adopted = await repeated.adopt(session, b)
    b.contract = await accepted_node_contract(session, b.node)
    b.receipt = await session.get(OrchestrationDecision, b.adopted["decision_id"])
    if restart:
        envelope = await repeated.resume(session, b, monkeypatch)
        b.pins = envelope["orchestration"]["deployment_revisions"]
        b.current = await session.scalar(
            select(OrchestrationExecution).where(OrchestrationExecution.node_id == b.node.id, OrchestrationExecution.cycle == 2)
        )
        b.assignment = SimpleNamespace(
            tenant_id=b.node.org_id,
            node_id=b.node.id,
            invocation_id=envelope["message_id"],
            attempt=2,
            accepted_plan_version=b.adopted["plan_version"],
            claim_generation=b.current.claim_generation,
            repository=coverage.REPO,
        )
        b.kwargs = dict(
            node_id=b.node.id,
            org_id=b.node.org_id,
            plan_version=b.adopted["plan_version"],
            cycle=2,
            pins=b.pins,
            contract=b.contract,
            require_merged=True,
        )
    return b


async def test_old_squash_head_stays_covered_by_latest_checkpoint_in_status_and_board(session, monkeypatch, governed_work_ownership):
    b = await arrange(session, monkeypatch)
    original_proof = json.loads(b.original_reason)["proof"]["squashed_repair_history"]
    latest = await verified_adoption(session, b.node, b.contract)
    assert latest["inherited_squash_proofs"] == original_proof
    assert len(latest["inherited_squash_proofs"]) == 1
    assert "squashed_repair_history" not in latest["proof"]
    assert squash.SQUASH_HEAD not in latest["proof"]["included_published_heads"]
    # The verifier returns the exact stored receipt; derived readback fields
    # must never change the digest that already fenced this dispatch.
    assert latest == json.loads(b.receipt.reason)
    for _ in range(2):
        status = await record_repair_pr(
            session,
            b.assignment,
            {"pull_number": 7, "head": squash.SQUASH_HEAD, "base": squash.SQUASH_PARENT, "merged": True, "checks_verified": True},
        )
        assert status["publication_recorded"] is False
        assert status["covered_by_adoption"]["adoption_id"] == b.adopted["decision_id"]
        assert status["covered_by_adoption"]["source_revision"] == "5" * 40
        assert status["covered_by_adoption"]["basis"] == "reviewed_squash"
    assert not await session.scalar(
        select(OrchestrationAction).where(OrchestrationAction.execution_id == b.current.id, OrchestrationAction.kind == "operations_repair_pr")
    )
    # Retained observations from an older client are classified without being
    # deleted or turning into a fresh, unmerged repair obligation.
    observed = await coverage.legacy_reread(session, b)
    before = copy.deepcopy(observed.detail)
    assert await repair_revisions(session, **b.kwargs) == b.pins
    board = await read_operations_tasks(session, b.assignment)
    evidence = board["cycles"][-1]["evidence"][0]
    assert evidence["covered_by_adoption"]["adoption_id"] == b.adopted["decision_id"]
    assert evidence["covered_by_adoption"]["source_revision"] == "5" * 40
    assert b.original.reason == b.original_reason and observed.detail == before
    assert not await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_repair_merge"))


@pytest.mark.parametrize("fault", ["head", "pull_number", "repository"])
async def test_inherited_alias_never_covers_another_repair_identity(session, monkeypatch, governed_work_ownership, fault):
    b = await arrange(session, monkeypatch)
    arguments = {
        "head": {"head": "6" * 40},
        "pull_number": {"pull": 99},
        "repository": {"repository": "foreign/repository", "receipt": "github/foreign/repository/pull/7"},
    }[fault]
    await coverage.legacy_reread(session, b, **arguments)
    with pytest.raises(RepairMergeRequiredError, match="operations_repair_merge_required"):
        await repair_revisions(session, **b.kwargs)
    evidence = (await read_operations_tasks(session, b.assignment))["cycles"][-1]["evidence"][0]
    assert "covered_by_adoption" not in evidence


@pytest.mark.parametrize("fault", ["head", "publication", "duplicate", "omitted", "ancestor", "dangling_root"])
async def test_inherited_proof_must_equal_verified_parent_derivation(session, monkeypatch, governed_work_ownership, fault):
    b = await arrange(session, monkeypatch, restart=False)
    receipt = b.original if fault in {"ancestor", "dangling_root"} else b.receipt
    data = json.loads(receipt.reason)
    if fault == "head":
        data["inherited_squash_proofs"][0]["reviewed_head"] = "6" * 40
    elif fault == "publication":
        data["inherited_squash_proofs"][0]["publication_ids"] = ["unrelated-publication"]
    elif fault == "duplicate":
        data["inherited_squash_proofs"] *= 2
    elif fault == "omitted":
        del data["inherited_squash_proofs"]
    elif fault == "ancestor":
        data["proof"]["squashed_repair_history"][0]["head_tree"] = "6" * 40
    else:
        data["inherited_squash_proofs"] = copy.deepcopy(data["proof"]["squashed_repair_history"])
    original_get = session.get

    async def corrupt_read(model, identity, **kwargs):
        if model is OrchestrationDecision and identity == receipt.id:
            return SimpleNamespace(
                **{key: getattr(receipt, key) for key in ("id", "org_id", "flow_id", "node_id", "kind", "actor_kind", "actor_role", "actor_id")},
                reason=json.dumps(data),
            )
        return await original_get(model, identity, **kwargs)

    monkeypatch.setattr(session, "get", corrupt_read)
    if fault == "dangling_root":
        # Check the original boundary directly so a descendant digest mismatch
        # cannot accidentally be the only protection against a dangling alias.
        from src.orchestration.maintenance_adoption_resolution import _before_contract

        latest = json.loads(b.receipt.reason)
        parent = await original_get(OrchestrationDecision, latest["before_binding"]["maintenance_adoption_id"])
        current = _before_contract(_before_contract(b.contract, latest), json.loads(parent.reason))
    else:
        current = b.contract
    with pytest.raises(CycleBlockedError, match="maintenance_(receipt_chain_changed|squash_receipt_unverified)"):
        await verified_adoption(session, b.node, current)
