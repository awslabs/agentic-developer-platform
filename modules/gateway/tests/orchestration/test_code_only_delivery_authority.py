"""Code-only delivery is decided by deployment authority, not by an action whitelist (#7457)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration.execution_policy import Action
from src.orchestration.merge_controller import code_only_delivery
from src.orchestration.policy_admission import AdmissionInputs
from tests.orchestration.test_execution_policy import _policy

CODE = [Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.MERGE]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"allowed_actions": CODE}, True),
        # `operate` and `evaluate` are not deployment permissions: a policy that
        # grants them but no deploy authority still delivers code only, and the
        # repository's own pipelines own what happens after the merge.
        ({"allowed_actions": [*CODE, Action.OPERATE]}, True),
        ({"allowed_actions": [*CODE, Action.EVALUATE, Action.OPERATE]}, True),
        ({"allowed_actions": [*CODE, Action.DEPLOY]}, False),
        ({"allowed_actions": [*CODE, Action.DEPLOY], "human_gates": [Action.DEPLOY]}, False),
        ({"allowed_actions": CODE, "environment_connection_ids": ["env-a"]}, False),
        ({"allowed_actions": [Action.DEVELOP, Action.REVIEW, Action.REPAIR]}, False),
    ],
)
async def test_delivery_ends_at_merge_without_deploy_authority(monkeypatch, overrides, expected):
    policy = _policy(**{"environment_connection_ids": [], "human_gates": [], **overrides})
    monkeypatch.setattr("src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(policy, 3)))
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))  # no execution_continuation marker
    context = SimpleNamespace(identity=SimpleNamespace(accepted_plan_version=3))
    node = SimpleNamespace(org_id=policy.org_id, flow_id="flow", id="node")
    assert await code_only_delivery(session, context, node) is expected


@pytest.mark.parametrize("preserved", [True, False])
async def test_an_amendment_that_preserved_the_node_keeps_its_merge_contract(monkeypatch, preserved):
    """The plan in force moved on (v3 -> v5) after this execution was admitted (#7535).

    Strict version equality blocked every merge-ready story of a flow after any
    ordinary amendment with code_delivery_contract_changed; lineage decides instead.
    """
    from src.orchestration.review_cycle import CycleBlockedError

    policy = _policy(environment_connection_ids=[], human_gates=[], allowed_actions=CODE)
    monkeypatch.setattr("src.orchestration.policy_admission.load_in_force_policy", AsyncMock(return_value=AdmissionInputs(policy, 5)))
    matches = AsyncMock(return_value=preserved)
    monkeypatch.setattr("src.orchestration.plan_lineage.execution_plan_matches", matches)
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    context = SimpleNamespace(identity=SimpleNamespace(accepted_plan_version=3))
    node = SimpleNamespace(org_id=policy.org_id, flow_id="flow", id="node")
    if preserved:
        assert await code_only_delivery(session, context, node) is True
    else:
        with pytest.raises(CycleBlockedError, match="code_delivery_contract_changed"):
            await code_only_delivery(session, context, node)
    matches.assert_awaited_once_with(session, org_id=policy.org_id, flow_id="flow", node_id="node", version=3, current_version=5)
