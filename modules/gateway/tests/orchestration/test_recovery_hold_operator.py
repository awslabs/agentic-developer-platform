"""The historical adjustment requires explicit owner acknowledgement and audit."""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.orchestration.models import OrchestrationDecision
from tests.orchestration.test_budget_read import display, usage  # noqa: F401
from tests.orchestration.test_list_flows import session  # noqa: F401


@pytest.fixture
def operator():
    path = Path(__file__).resolve().parents[2] / "scripts/resolve-flow-recovery-hold.py"
    spec = importlib.util.spec_from_file_location("hold_operator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_owner_preview_acknowledgement_and_durable_audit_precede_hold_release(display, operator, monkeypatch):  # noqa: F811
    d = display
    recovery = str(uuid4())
    hold = "recovery-" + recovery
    prepared_id = str(uuid4())
    org, flow = d.flow.org_id, d.flow.id
    await usage(d)
    await d.store.reserve(hold, Decimal(100), [d.target])
    await d.store.retain_failed_bound(hold, d.target)
    for kind, identity, reason in [
        ("meter_ceiling_prepared", prepared_id, {"recovery_id": recovery, "unreconciled_exposure_hold_usd": "100"}),
        ("meter_ceiling_restored", str(uuid4()), {"recovery_id": recovery, "intent_id": prepared_id, "unreconciled_hold_usd": "100"}),
    ]:
        d.db.add(
            OrchestrationDecision(
                id=identity,
                org_id=org,
                flow_id=flow,
                kind=kind,
                actor_id="operator",
                actor_role="operator",
                actor_kind="service",
                reason=json.dumps(reason),
            )
        )
    await d.db.commit()
    monkeypatch.setattr(operator, "get_session_factory", lambda: lambda: d.db)
    monkeypatch.setattr(operator, "get_flow_reservations", lambda: d.store)
    monkeypatch.setattr(d.store, "close", AsyncMock())
    monkeypatch.setattr(operator, "load_in_force_policy", AsyncMock(return_value=SimpleNamespace(policy=d.policy, plan_version=1, refusal=None)))
    monkeypatch.setattr(
        operator.boto3, "client", lambda _: SimpleNamespace(get_caller_identity=lambda: {"Account": "123456789012", "Arn": "operator"})
    )
    args = SimpleNamespace(
        account="123456789012",
        org=org,
        flow=flow,
        recovery=recovery,
        reason="Historical incident decision",
        apply=False,
        expected_snapshot=None,
        acknowledge_unverified_history=False,
        approval_reference=None,
    )
    before = await d.client.hgetall(d.target.key())
    preview = await operator.resolve(args)
    assert preview["release_usd"] == "100" and not preview["historical_usage_reconstructed"]
    assert await d.client.hgetall(d.target.key()) == before
    args.apply = True
    args.expected_snapshot = preview["snapshot"]
    with pytest.raises(ValueError, match="explicit owner acknowledgement"):
        await operator.resolve(args)
    assert await d.client.hgetall(d.target.key()) == before
    args.acknowledge_unverified_history = True
    args.approval_reference = "owner-decision-example"
    native_release = operator.release_recovery_hold

    async def audited_release(*arguments, **keywords):
        audit = await d.db.get(OrchestrationDecision, keywords["decision_id"])
        assert audit and json.loads(audit.reason)["approval_reference"] == args.approval_reference
        assert not d.db.new
        return await native_release(*arguments, **keywords)

    monkeypatch.setattr(operator, "release_recovery_hold", audited_release)
    result = await operator.resolve(args)
    assert result["applied"] and result["recorded_spend_unchanged"]
    assert Decimal((await d.client.hget(d.target.key(), hold)).split(":")[0]) == 0
    assert (await d.db.scalar(select(OrchestrationDecision).where(OrchestrationDecision.kind == "meter_hold_released"))) is not None
    again = await operator.resolve(args)
    assert again["already_resolved"] and again["decision_id"] == result["decision_id"]
