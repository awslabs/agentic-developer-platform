"""Accepted suite changes and legacy plan hashes retain the human boundary."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.orchestration.amend import AmendmentContext, amend_plan
from src.orchestration.compile import HASH_EXCLUDED_FIELDS, PolicyNotAcceptableError, compile_proposal, plan_hash, require_evaluation_acceptor
from src.orchestration.proposal import validate_proposal
from src.orchestration.state import ActorKind
from tests.orchestration.test_compile import approval, valid_proposal  # noqa: F401
from tests.orchestration.test_evaluation_evidence import evaluation  # noqa: F401
from tests.orchestration.test_pending_amendments_postgres import factory, pg_server, pg_url  # noqa: F401


def with_suite():
    proposal = valid_proposal()
    node = next(n for n in proposal.nodes if n.kind == "eval")
    node.evaluation = {"criteria": [{"criterion_id": "required", "kind": "api", "required": True}]}
    return proposal


def test_legacy_plan_hash_is_identical_before_and_after_e1():
    proposal = valid_proposal()
    old = proposal.model_dump(mode="json", exclude=HASH_EXCLUDED_FIELDS)
    old.pop("execution_policy", None)
    for node in old["nodes"]:
        node.pop("evaluation", None)
    assert plan_hash(proposal) == hashlib.sha256(json.dumps(old, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_explicit_suite_changes_are_not_idempotent_retries():
    proposal = with_suite()
    before = plan_hash(proposal)
    next(n for n in proposal.nodes if n.kind == "eval").evaluation["criteria"][0]["required"] = False
    assert plan_hash(proposal) != before


def test_evaluation_specification_cannot_be_attached_to_story():
    proposal = valid_proposal()
    proposal.nodes[0].evaluation = {}
    assert "evaluation_node_kind" in {v.rule for v in validate_proposal(proposal)}


def test_service_cannot_remove_the_accepted_suite(approval):  # noqa: F811
    previous = SimpleNamespace(plan_document=with_suite().model_dump(mode="json"))
    with pytest.raises(PolicyNotAcceptableError):
        require_evaluation_acceptor(valid_proposal(), replace(approval, actor_kind=ActorKind.SERVICE), previous)


async def test_service_cannot_compile_its_own_evaluation_specification(session, approval):  # noqa: F811
    with pytest.raises(PolicyNotAcceptableError):
        await compile_proposal(session, with_suite(), replace(approval, actor_kind=ActorKind.SERVICE))


async def test_human_amendment_can_change_suite_and_invalidates_prior_hash(session, approval):  # noqa: F811
    proposal = with_suite()
    original = await compile_proposal(session, proposal, approval)
    next(n for n in proposal.nodes if n.kind == "eval").evaluation["criteria"].append({"criterion_id": "second", "kind": "control"})
    actor = AmendmentContext(org_id=approval.org_id, actor_id=approval.actor_id, actor_role=approval.actor_role)
    changed = await amend_plan(session, flow_id=original.flow_id, proposal=proposal, actor=actor)
    assert changed.plan_version == original.plan_version + 1 and changed.plan_hash != original.plan_hash


# Exercise persistence boundaries on real PostgreSQL, including JSON accepted
# documents and transactional refusal of service-authored suite removal.


@pytest.fixture
async def session(factory):  # noqa: F811
    async with factory() as value:
        yield value


async def test_service_amendment_cannot_remove_accepted_suite_or_write_version(session, approval):  # noqa: F811
    from sqlalchemy import func, select

    from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision

    proposal = with_suite()
    original = await compile_proposal(session, proposal, approval)
    await session.commit()
    decisions_before = await session.scalar(select(func.count()).select_from(OrchestrationDecision))
    node = next(n for n in proposal.nodes if n.kind == "eval")
    node.evaluation = None
    actor = AmendmentContext(org_id=approval.org_id, actor_id="service", actor_role="member", actor_kind=ActorKind.SERVICE)
    with pytest.raises(PolicyNotAcceptableError):
        await amend_plan(session, flow_id=original.flow_id, proposal=proposal, actor=actor)
    assert await session.scalar(select(func.count()).select_from(OrchestrationAcceptedPlan)) == 1
    assert await session.scalar(select(func.count()).select_from(OrchestrationDecision)) == decisions_before
    accepted = await session.scalar(select(OrchestrationAcceptedPlan))
    assert accepted.superseded_at is None
    assert any(n.get("evaluation") for n in accepted.plan_document["nodes"])


@pytest.mark.parametrize("mismatch", [None, "policy", "mode", "repository", "connection"])
def test_machine_evaluation_matches_existing_accepted_policy(evaluation, mismatch):  # noqa: F811
    from copy import deepcopy

    from tests.orchestration.test_execution_policy_acceptance import a_policy

    spec = deepcopy(evaluation.expected.specification)
    spec["acceptance_mode"] = "machine"
    proposal = valid_proposal()
    node = next(n for n in proposal.nodes if n.kind == "eval")
    node.evaluation = spec
    policy = a_policy(repository_ids=[spec["runner"]["repository"]], environment_connection_ids=[spec["environment_connection_id"]])
    if mismatch == "repository":
        policy = policy.model_copy(update={"repository_ids": ["other/repository"]})
    elif mismatch == "connection":
        policy = policy.model_copy(update={"environment_connection_ids": ["other"]})
    elif mismatch == "mode":
        policy = policy.model_copy(update={"evaluation_acceptance": {}})
    proposal.execution_policy = None if mismatch == "policy" else policy
    rules = {v.rule for v in validate_proposal(proposal) if v.rule.startswith("evaluation_")}
    assert bool(rules) is (mismatch is not None)


async def test_accepted_suite_survives_real_authoring_base_round_trip(session, tmp_path):
    from src.orchestration.pending_amendments import accept_amendment, in_force_plan, register_amendment_draft
    from src.orchestration.proposal import LoopProposal
    from tests.orchestration.test_authoring_base_input import commissioned, worker
    from tests.orchestration.test_pending_amendments import ORG_A, amender, base_proposal

    proposal = base_proposal()
    suite = next(n for n in with_suite().nodes if n.kind == "eval").evaluation
    next(n for n in proposal.nodes if n.kind == "eval").evaluation = suite
    request, envelope = await commissioned(session, proposal)
    path = worker.materialize_authoring_input(json.loads(json.dumps(envelope)), directory=str(tmp_path))
    from pathlib import Path

    authored = json.loads(Path(path).read_text())
    assert next(n for n in authored["nodes"] if n["kind"] == "eval")["evaluation"] == suite
    next(n for n in authored["nodes"] if n["kind"] == "eval")["title"] = "Clarified evaluation title"
    draft = await register_amendment_draft(
        session, org_id=ORG_A, request=request, author_run_id=request.author_run_id, proposal=LoopProposal.model_validate(authored)
    )
    await session.commit()
    result = await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=request.flow_id)
    await session.commit()
    accepted = await in_force_plan(session, org_id=ORG_A, flow_id=request.flow_id)
    assert result.plan_version == 2
    assert next(n for n in accepted.plan_document["nodes"] if n["kind"] == "eval")["evaluation"] == suite
