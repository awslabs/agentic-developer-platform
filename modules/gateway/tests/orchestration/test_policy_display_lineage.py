"""Policy display must retain authority semantics with bounded history reads."""

import copy
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision
from src.orchestration.plan_lineage import PolicyDisplayLineage
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.shared_budget import CONTRACT, accepted_document_hash
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401
from tests.orchestration.legacy_plan import persist_legacy_plan
from tests.orchestration.test_amend import approval, valid_proposal  # noqa: F401
from tests.orchestration.test_list_flows import statement_recorder
from tests.orchestration.test_policy_admission import _policy
from tests.orchestration.test_shared_amendment_postgres import session  # noqa: F401


@pytest.mark.parametrize("hops", [1, 10, 30])
@pytest.mark.parametrize("corruption", [None, "foreign_receipt", "missing_plan", "bad_hash"])
async def test_policy_history_reads_are_bounded_and_match_authority(session, approval, hops, corruption, monkeypatch):  # noqa: F811
    compiled = await persist_legacy_plan(session, valid_proposal(execution_policy=_policy()), approval)
    base = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == compiled.flow_id))
    policy = base.plan_document["execution_policy"]
    receipt = OrchestrationDecision(
        org_id=approval.org_id,
        flow_id=compiled.flow_id,
        kind="budget_increased",
        actor_id=approval.actor_id,
        actor_role="platform_admin",
        actor_kind="human",
        reason=json.dumps(
            {
                "contract": CONTRACT,
                "flow_id": base.flow_id,
                "plan_version": 1,
                "plan_hash": base.plan_hash,
                "original_policy_hash": policy["policy_hash"],
                "principal_id": policy["principal_id"],
                "limits": {"max_spend_usd": "100", "max_run_spend_usd": "100", "max_chain_spend_usd": "100"},
            }
        ),
    )
    session.add(receipt)
    current = base
    plans, decisions = [], []
    for version in range(2, hops + 2):
        current.superseded_at = datetime.now(UTC)
        decision = OrchestrationDecision(
            org_id="foreign-tenant" if corruption == "foreign_receipt" and version == 2 else approval.org_id,
            flow_id=base.flow_id,
            kind="plan_amended",
            actor_id=approval.actor_id,
            actor_role=approval.actor_role,
            actor_kind="human",
            reason=f"Owned fixture [amendment: v{version - 1} -> v{version}, unchanged authority]",
        )
        session.add(decision)
        await session.flush()
        document = copy.deepcopy(base.plan_document)
        document["nodes"][0]["title"] = f"Display title {version}"
        current = OrchestrationAcceptedPlan(
            org_id=approval.org_id,
            flow_id=base.flow_id,
            version=version,
            plan_document=document,
            plan_hash=accepted_document_hash(document),
            accepted_by_decision_id=decision.id,
        )
        session.add(current)
        plans.append(current)
        decisions.append(decision)
    await session.commit()
    if corruption == "missing_plan":
        await session.delete(base)
    elif corruption == "bad_hash":
        base.plan_hash = "0" * 64
    await session.commit()
    authoritative = await load_in_force_policy(session, org_id=approval.org_id, flow_id=compiled.flow_id)
    import src.orchestration.shared_budget as shared_budget

    hash_calls = []
    original_hash = shared_budget.accepted_document_hash

    def counted_hash(document):
        hash_calls.append(id(document))
        return original_hash(document)

    monkeypatch.setattr(shared_budget, "accepted_document_hash", counted_hash)
    statements, stop = statement_recorder(session)
    try:
        displayed = await load_in_force_policy(
            session,
            org_id=approval.org_id,
            flow_id=compiled.flow_id,
            display_lineage=PolicyDisplayLineage(),
        )
    finally:
        stop()
    assert len(statements) <= 10, statements
    assert len(hash_calls) <= hops + 2, "Each historical plan must be hashed at most once per display snapshot, plus the receipt check"
    assert displayed.refusal == authoritative.refusal
    assert displayed.policy.model_dump(mode="json") == authoritative.policy.model_dump(mode="json")
    assert displayed.policy._shared_budget_decision_id == authoritative.policy._shared_budget_decision_id
    assert displayed.policy._shared_budget_decision_id == (receipt.id if corruption is None else None)
    foreign = await load_in_force_policy(session, org_id="foreign-tenant", flow_id=compiled.flow_id, display_lineage=PolicyDisplayLineage())
    assert foreign.policy is None and foreign.plan_version == 0
