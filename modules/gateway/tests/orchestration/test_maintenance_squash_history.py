"""A real tree-identical squash can retain publication history without fake ancestry."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import tests.orchestration.test_maintenance_adoption as original
from src.orchestration.accepted_node import accepted_node_contract
from src.orchestration.maintenance_adoption import adopt_maintenance
from src.orchestration.maintenance_adoption_resolution import verified_adoption
from src.orchestration.maintenance_squash_history import SquashedRepairHistory
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationAction, OrchestrationDecision
from src.orchestration.review_cycle import CycleBlockedError

engine = original.engine
session = original.session
governed_work_ownership = original.governed_work_ownership
healthy_policy_reservations = original.healthy_policy_reservations
policy_budget_initializers = original.policy_budget_initializers

SQUASH_HEAD, SQUASH_MERGE, SQUASH_PARENT, TREE, EARLIER = (value * 40 for value in "12348")


async def arrange(session, monkeypatch):
    b = await original.arrange(session, monkeypatch)
    b.publication.receipt_ref = f"github/{original.fixtures.REPO}/pull/7"
    b.second_publication = OrchestrationAction(
        org_id=b.node.org_id,
        execution_id=b.execution.id,
        operation_key="published-earlier-head",
        kind="operations_repair_pr",
        status="succeeded",
        attempt=1,
        receipt_ref=f"github/{original.fixtures.REPO}/pull/7",
        detail={"head": EARLIER},
    )
    session.add(b.second_publication)
    await session.flush()
    b.squash = SquashedRepairHistory(
        pull_number=7, reviewed_head=SQUASH_HEAD, merge_revision=SQUASH_MERGE, published_heads=[EARLIER, original.PUBLISHED]
    )
    b.request = b.request.model_copy(update={"squashed_repair_history": [b.squash]})
    b.squash_pr = {
        "number": 7,
        "merged": True,
        "merge_commit_sha": SQUASH_MERGE,
        "head": {"sha": SQUASH_HEAD, "repo": {"id": 12345}},
        "base": {"ref": "main", "repo": {"id": 12345}},
    }
    b.squash_head = {"sha": SQUASH_HEAD, "commit": {"tree": {"sha": TREE}}}
    b.squash_merge = {"sha": SQUASH_MERGE, "parents": [{"sha": SQUASH_PARENT}], "commit": {"tree": {"sha": TREE}}}
    b.squash_check = {"id": 99, "name": "qualified", "head_sha": SQUASH_HEAD, "status": "completed", "conclusion": "success"}
    previous_request, previous_pages = b.provider.request.side_effect, b.provider.pages.side_effect

    async def request(binding, method, path, **kwargs):
        for suffix, value in (("/pulls/7", b.squash_pr), ("/commits/" + SQUASH_HEAD, b.squash_head), ("/commits/" + SQUASH_MERGE, b.squash_merge)):
            if path.endswith(suffix):
                return SimpleNamespace(json=lambda: copy.deepcopy(value))
        return await previous_request(binding, method, path, **kwargs)

    async def pages(binding, path, *args):
        if path.endswith(SQUASH_HEAD + "/check-runs"):
            return [copy.deepcopy(b.squash_check)]
        return await previous_pages(binding, path, *args)

    async def contains(binding, source, actual):
        if (source in {EARLIER, original.PUBLISHED} and actual == original.MERGE) or (source, actual) == b.refused_ancestry:
            raise CycleBlockedError("deployment_newer_release_does_not_contain_merge")

    b.refused_ancestry = None
    b.provider.request.side_effect, b.provider.pages.side_effect, b.provider.contains.side_effect = request, pages, contains
    return b


async def accept(session, b):
    value = await original.preview(session, b)
    return await adopt_maintenance(
        session,
        flow_id=b.node.flow_id,
        actor=b.actor,
        request=b.request.model_copy(update={"expected_snapshot": value["snapshot"]}),
        provider=b.provider,
        accept=True,
    )


async def test_real_squash_proof_preserves_both_publications_and_dispatches_adopted_source(session, monkeypatch, governed_work_ownership):
    b = await arrange(session, monkeypatch)
    failure = copy.deepcopy(b.report.terminal_receipt)
    result = await accept(session, b)
    assert b.node.attempts == 1 and b.node.state == "failed" and b.report.terminal_receipt == failure
    contract = await accepted_node_contract(session, b.node)
    receipt = await verified_adoption(session, b.node, contract)
    proof = receipt["proof"]
    assert proof["included_published_heads"] == sorted([EARLIER, original.PUBLISHED])
    squash = proof["squashed_repair_history"][0]
    assert squash["head_tree"] == squash["merge_tree"] == TREE
    assert squash["publication_ids"] == sorted([b.publication.id, b.second_publication.id])
    assert squash["required_checks"][0]["head_sha"] == SQUASH_HEAD
    assert not await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_repair_merge"))
    assert len(receipt["repair_history"]) == 2
    for published in [EARLIER, original.PUBLISHED]:
        assert any(c.args[1:] == (published, SQUASH_HEAD) for c in b.provider.contains.await_args_list)
        assert not any(c.args[1:] == (published, original.MERGE) for c in b.provider.contains.await_args_list)
    from src.orchestration.controls import ResumeRequest, resume_node
    from src.orchestration.dispatch_pass import run_dispatch_pass

    monkeypatch.setattr("src.orchestration.operations_release_resolution.ReleaseProvider", lambda: b.provider)
    await resume_node(
        b.node.id,
        ResumeRequest(reason="Retry with retained squash provenance."),
        SimpleNamespace(org_id=b.node.org_id, user_id=b.actor.actor_id),
        SimpleNamespace(check_permission=AsyncMock(), get_user_role=AsyncMock(return_value=[SimpleNamespace(value="org_admin")])),
        session,
        None,
    )
    dispatched = await run_dispatch_pass(session, original.fixtures._config())
    assert dispatched.dispatched == 1
    metadata = dispatched.pending[0].envelope["orchestration"]
    assert metadata["deployment_revisions"]["source_revision"] == original.MERGE
    assert metadata["release_binding"]["maintenance_adoption_id"] == result["decision_id"]
    assert b.node.attempts == 2 and b.report.terminal_receipt == failure


@pytest.mark.parametrize(
    "fault,code",
    [
        ("tree", "squash_tree_mismatch"),
        ("missing_tree", "squash_tree_mismatch"),
        ("two_parents", "squash_shape_unverified"),
        ("no_parent", "squash_shape_unverified"),
        ("unmerged", "squash_merge_changed"),
        ("head", "squash_merge_changed"),
        ("merge", "squash_merge_changed"),
        ("repository", "squash_merge_changed"),
        ("branch", "squash_merge_changed"),
        ("publication_receipt", "squash_publication_mismatch"),
        ("unknown_publication", "squash_publication_mismatch"),
        ("duplicate", "squash_history_ambiguous"),
        ("omitted_head", "does_not_contain"),
        ("no_explicit_bridge", "does_not_contain"),
        ("publication_not_ancestor", "does_not_contain"),
        ("parent_not_ancestor", "does_not_contain"),
        ("merge_not_ancestor", "does_not_contain"),
        ("baseline_not_ancestor", "does_not_contain"),
        ("failed_historical_check", "checks_unverified"),
        ("wrong_check_head", "check_head_changed"),
    ],
)
async def test_squash_does_not_bypass_history_or_checks(session, monkeypatch, governed_work_ownership, fault, code):
    b = await arrange(session, monkeypatch)
    if fault == "tree":
        b.squash_merge["commit"]["tree"]["sha"] = "a" * 40
    elif fault == "missing_tree":
        b.squash_head["commit"] = {}
    elif fault in {"two_parents", "no_parent"}:
        b.squash_merge["parents"] = [{"sha": SQUASH_PARENT}, {"sha": SQUASH_HEAD}] if fault == "two_parents" else []
    elif fault == "unmerged":
        b.squash_pr["merged"] = False
    elif fault == "head":
        b.squash_pr["head"]["sha"] = "a" * 40
    elif fault == "merge":
        b.squash_pr["merge_commit_sha"] = "a" * 40
    elif fault == "repository":
        b.squash_pr["head"]["repo"]["id"] = 9876
    elif fault == "branch":
        b.squash_pr["base"]["ref"] = "other"
    elif fault == "publication_receipt":
        b.publication.receipt_ref = "github/acme/widgets/pull/999"
    elif fault == "unknown_publication":
        b.squash.published_heads = ["a" * 40]
    elif fault == "duplicate":
        b.request.squashed_repair_history = [b.squash, b.squash]
    elif fault == "omitted_head":
        b.squash.published_heads = [original.PUBLISHED]
    elif fault == "no_explicit_bridge":
        b.request.squashed_repair_history = []
    elif fault == "publication_not_ancestor":
        b.refused_ancestry = (EARLIER, SQUASH_HEAD)
    elif fault == "parent_not_ancestor":
        b.refused_ancestry = (SQUASH_PARENT, SQUASH_HEAD)
    elif fault == "merge_not_ancestor":
        b.refused_ancestry = (SQUASH_MERGE, original.MERGE)
    elif fault == "baseline_not_ancestor":
        b.refused_ancestry = (original.fixtures.SOURCE, original.MERGE)
    elif fault == "failed_historical_check":
        b.squash_check["conclusion"] = "failure"
    elif fault == "wrong_check_head":
        b.squash_check["head_sha"] = "a" * 40
    await session.flush()
    with pytest.raises(CycleBlockedError, match=code):
        await original.preview(session, b)
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1
    assert not await session.scalar(select(OrchestrationDecision).where(OrchestrationDecision.kind == "maintenance_release_adopted"))


async def test_historical_check_changed_after_preview_does_not_accept(session, monkeypatch, governed_work_ownership):
    b = await arrange(session, monkeypatch)
    value = await original.preview(session, b)
    b.squash_check["id"] += 1
    with pytest.raises(CycleBlockedError, match="preview_changed"):
        await adopt_maintenance(
            session,
            flow_id=b.node.flow_id,
            actor=b.actor,
            provider=b.provider,
            accept=True,
            request=b.request.model_copy(update={"expected_snapshot": value["snapshot"]}),
        )


@pytest.mark.parametrize("fault", ["tree", "publication", "check"])
async def test_readback_rejects_corrupted_squash_proof(session, monkeypatch, governed_work_ownership, fault):
    b = await arrange(session, monkeypatch)
    result = await accept(session, b)
    decision = await session.get(OrchestrationDecision, result["decision_id"])
    data = json.loads(decision.reason)
    item = data["proof"]["squashed_repair_history"][0]
    if fault == "tree":
        item["merge_tree"] = "a" * 40
    elif fault == "publication":
        item["publication_ids"] = []
    else:
        item["required_checks"][0]["conclusion"] = "failure"
    original_get = session.get

    async def corrupted_read(model, identity, **kwargs):
        if model is OrchestrationDecision and identity == decision.id:
            return SimpleNamespace(
                **{name: getattr(decision, name) for name in ("id", "org_id", "flow_id", "node_id", "kind", "actor_kind", "actor_role", "actor_id")},
                reason=json.dumps(data),
            )
        return await original_get(model, identity, **kwargs)

    monkeypatch.setattr(session, "get", corrupted_read)
    with pytest.raises(CycleBlockedError, match="squash_receipt_unverified"):
        await verified_adoption(session, b.node, await accepted_node_contract(session, b.node))


async def test_empty_history_keeps_existing_request_digest(session, monkeypatch, governed_work_ownership):
    b = await original.arrange(session, monkeypatch)
    assert "squashed_repair_history" not in b.request.model_dump(mode="json")
