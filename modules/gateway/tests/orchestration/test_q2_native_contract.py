"""Real-PG contract checks for Q2's isolated process. NON-LIVE harness tests."""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from src.orchestration.execution_runner import _identity
from src.orchestration.execution_state import ActionIntent, Observation, ObservedOutcome
from src.orchestration.execution_store import prepare_action, record_observation
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationAction, OrchestrationDecision, OrchestrationFlow, OrchestrationWorkClaim
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401
from tests.orchestration.test_execution_runner_postgres import execution, pg_engine, pg_session_factory  # noqa: F401

source = Path(__file__).resolve().parents[4] / "tests/e2e/orchestration/scenarios/native_process.py"
spec = importlib.util.spec_from_file_location("q2_native_contract", source)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


@pytest.fixture
async def scoped(pg_engine, pg_session_factory, execution, monkeypatch):  # noqa: F811
    async with pg_engine.begin() as connection:
        await connection.run_sync(OrchestrationDecision.__table__.create)
    async with pg_session_factory() as session:
        flow = await session.get(OrchestrationFlow, execution.flow_id)
        flow.slug = "q-contract-0123456789"
        plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == execution.flow_id))
        plan.plan_document = {"spec_revision": "a" * 64, "execution_policy": {"policy_id": "offline-policy", "policy_hash": "c" * 64}}
        await session.commit()
    from src.shared import database

    monkeypatch.setattr(database, "get_session_factory", lambda: pg_session_factory)
    request = dict(
        mode="read",
        org_id=execution.org_id,
        flow_id=execution.flow_id,
        execution_id=execution.id,
        qualification_id="q-contract-0123456789",
        definition_hash="a" * 64,
        plan_hash="b" * 64,
        plan_version=1,
    )
    return pg_session_factory, execution, request


async def test_native_read_uses_real_tenant_plan_and_claim(scoped):
    factory, record, request = scoped
    actual, actions, state = await native.scoped_state(factory, request)
    assert actual.id == record.id and actions == []
    assert state["flow_id"] == record.flow_id
    assert state["policy_id"] == "offline-policy"
    assert state["claim_generation"] == 1 and state["mutating_owner_count"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("org_id", "foreign"),
        ("flow_id", "foreign"),
        ("execution_id", "foreign"),
        ("qualification_id", "q-another-0123456789"),
        ("definition_hash", "d" * 64),
        ("plan_hash", "e" * 64),
        ("plan_version", 2),
    ],
)
async def test_native_refuses_foreign_or_stale_scope(scoped, field, value):
    factory, _, request = scoped
    request[field] = value
    with pytest.raises(ValueError):
        await native.scoped_state(factory, request)


async def test_native_refuses_changed_claim_generation(scoped):
    factory, record, request = scoped
    async with factory() as session:
        claim = await session.get(OrchestrationWorkClaim, record.claim_id)
        claim.generation += 1
        await session.commit()
    with pytest.raises(ValueError, match="claim"):
        await native.scoped_state(factory, request)


async def test_native_never_activates_disabled_runner(scoped, monkeypatch):
    _, _, request = scoped
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "false")
    request["mode"] = "once"
    with pytest.raises(ValueError, match="disabled"):
        await native.execute(request)


@pytest.mark.parametrize("mode", ["duplicate-events", "out-of-order"])
async def test_native_replays_actual_stored_observations_without_new_actions(scoped, monkeypatch, mode):
    factory, record, request = scoped
    async with factory() as session:
        for index in range(2):
            key = f"offline-operation-{index}"
            await prepare_action(session, identity=_identity(record), intent=ActionIntent(key, "offline-simulation"))
            await record_observation(
                session, identity=_identity(record), observation=Observation(key, ObservedOutcome.SUCCEEDED, receipt_ref=f"offline-receipt-{index}")
            )
        await session.commit()
    monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
    request["mode"] = mode
    observed = await native.execute(request)
    assert len(observed["injection"]["delivery_ids"]) == 2
    assert observed["before"]["effect_ids"] == observed["after"]["effect_ids"]
    async with factory() as session:
        assert len(list((await session.scalars(select(OrchestrationAction))).all())) == 2
