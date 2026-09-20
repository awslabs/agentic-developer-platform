"""NON-LIVE Q2 tests: real PostgreSQL scope and native reservation Lua."""

import ast
import importlib.util
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from sqlalchemy import select

from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationFlow, OrchestrationNode
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401
from tests.orchestration.test_execution_policy import _stamped
from tests.orchestration.test_execution_runner_postgres import pg_engine, pg_session_factory  # noqa: F401

source = Path(__file__).resolve().parents[4] / "tests/e2e/orchestration/scenarios/control_process.py"
spec = importlib.util.spec_from_file_location("q2_control_contract", source)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


@pytest.fixture
async def control_scope(pg_session_factory, monkeypatch):  # noqa: F811
    policy = _stamped()
    async with pg_session_factory() as session:
        flow = OrchestrationFlow(org_id=policy.org_id, slug="q-control0123456789-allowance", title="Non-live control contract")
        session.add(flow)
        await session.flush()
        for i in range(3):
            session.add(
                OrchestrationNode(
                    org_id=flow.org_id,
                    flow_id=flow.id,
                    epic_ref="controls",
                    wave_ref="probe",
                    node_ref=f"gate-{i}",
                    kind="gate",
                    state="pending",
                    title="Non-live",
                )
            )
        session.add(
            OrchestrationAcceptedPlan(
                org_id=flow.org_id,
                flow_id=flow.id,
                version=1,
                plan_document={"spec_revision": "a" * 64, "execution_policy": policy.model_dump(mode="json")},
                plan_hash="b" * 64,
            )
        )
        await session.commit()
    from src.shared import database

    monkeypatch.setattr(database, "get_session_factory", lambda: pg_session_factory)
    request = dict(
        mode="budget",
        org_id=flow.org_id,
        flow_id=flow.id,
        qualification_id="q-control0123456789",
        definition_hash="a" * 64,
        plan_version=1,
        plan_hash="b" * 64,
    )
    return request, pg_session_factory


@pytest.mark.parametrize(
    "field,value",
    [("org_id", "foreign"), ("flow_id", "foreign"), ("qualification_id", "q-foreign0123456789"), ("plan_hash", "c" * 64), ("plan_version", 2)],
)
async def test_refuse_foreign_or_changed_pg_scope(control_scope, field, value):
    request, factory = control_scope
    request[field] = value
    async with factory() as session:
        with pytest.raises(ValueError):
            await native.read_scope(session, request)


async def test_refuse_dispatchable_node(control_scope):
    request, factory = control_scope
    async with factory() as session:
        node = await session.scalar(select(OrchestrationNode).limit(1))
        node.kind = "story"
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="dispatchable"):
            await native.read_scope(session, request)


async def test_withdrawn_policy_is_native_stale_denial(control_scope):
    request, factory = control_scope
    request["mode"] = "policy"
    async with factory() as session:
        flow = await session.get(OrchestrationFlow, request["flow_id"])
        flow.slug = "q-control0123456789-revocation"
        await session.commit()
    before = await native.execute(request)
    assert before["policy"] is not None and before["refusal"] is None
    async with factory() as session:
        old = await session.scalar(select(OrchestrationAcceptedPlan))
        old.superseded_at = datetime.now(UTC)
        session.add(
            OrchestrationAcceptedPlan(
                org_id=old.org_id, flow_id=old.flow_id, version=2, plan_document={"spec_revision": "a" * 64}, plan_hash="c" * 64
            )
        )
        await session.commit()
    request.update(plan_version=2, plan_hash="c" * 64)
    after = await native.execute(request)
    assert after["refusal"]["permitted"] is False
    assert after["refusal"]["reason"].value == "stale_policy_version"
    assert after["previous_policy_versions"] == [1] and after["executions"] == []


async def test_real_reservation_lua_and_owned_cancellation(control_scope, monkeypatch):
    request, factory = control_scope
    from src.budget.config import budget_config
    from src.budget.reservations import ReservationStore
    from src.orchestration import flow_budget, policy_admission

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ReservationStore(redis_url=None, ttl_seconds=3600, client=redis)
    monkeypatch.setattr(flow_budget, "get_flow_reservations", lambda: store)
    monkeypatch.setattr(policy_admission, "get_cost_by_address", AsyncMock(return_value=[]))
    monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)
    async with factory() as session:
        _, _, _, inputs = await native.read_scope(session, request)
    monkeypatch.setattr(budget_config, "budget_run_cap_usd", inputs.policy.limits.max_spend_usd / 2)
    try:
        result = await native.execute(request)
        assert result["fanout"]["admitted"] is False and result["repair"]["admitted"] is False
        assert not result["fanout"]["degraded"] and not result["repair"]["degraded"]
        assert Decimal(result["reserved_usd"]) == Decimal(result["limit"])
        assert result["cancellation"]["remaining_usd"] == "0"
    finally:
        await redis.aclose()


@pytest.mark.parametrize("available,expected", [(1, "deployment_gateway_revision_mismatch"), (0, "deployment_gateway_rollout_incomplete")])
def test_capture_reaches_native_negative_verifier(monkeypatch, capsys, available, expected):
    tree = ast.parse(source.with_name("runtime_faults.py").read_text())
    source_text = next(
        ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "SOURCE" for t in n.targets)
    )
    namespace = "q-control0123456789"
    request = {
        "namespace": namespace,
        "deployment_path": f"/apis/apps/v1/namespaces/{namespace}/deployments/bedrockgateway",
        "digest": "sha256:" + "b" * 64,
        "source_revision": "b" * 40,
        "account": "111122223333",
        "region": "us-east-1",
        "deployment": {
            "metadata": {"generation": 1},
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "bedrockgateway", "image": "111122223333.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:" + "a" * 64}
                        ]
                    }
                },
            },
            "status": {"updatedReplicas": 1, "availableReplicas": available, "observedGeneration": 1},
        },
    }
    monkeypatch.setattr(sys, "argv", ["native-simulation", json.dumps(request)])
    exec(compile(source_text, "q2-native-capture", "exec"), {})
    value = json.loads(capsys.readouterr().out.split("ADP_Q2_RESULT:")[1])
    assert value == {"status": "BLOCKED", "reason": expected}
