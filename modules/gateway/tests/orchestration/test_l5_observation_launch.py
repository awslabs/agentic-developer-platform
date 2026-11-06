"""Launch accounting survives failures without invoking a real provider."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from src import l5_observation_launch as launch_module
from src.agentauth.human_control import HumanControlSession
from src.l5_integration import GatewayConfigurationBackend
from src.orchestration import l5_observations as observations
from src.orchestration.l5_observation_contract import LAUNCH, RecordObservation
from src.orchestration.l5_observation_receipts import launch_id
from src.orchestration.models import OrchestrationDecision
from src.shared.schemas.auth import TokenContext
from tests.orchestration.test_l5_observation_scope import product_case, scope_case  # noqa: F401
from tests.orchestration.test_policy_admission import APPROVER, ORG_A, TEAM_A


@pytest.fixture
async def launch_case(scope_case, db_session_factory, monkeypatch):  # noqa: F811
    c = scope_case
    expiry = datetime.now(UTC) + timedelta(hours=1)
    c.actor = TokenContext(
        user_id=APPROVER,
        org_id=ORG_A,
        team_id=TEAM_A,
        department_id="department",
        account_type="human",
        is_admin=True,
        token_use="access",
        expires_at=expiry,
    )
    c.human = HumanControlSession(user_id=APPROVER, tenant_id=ORG_A, expires_at=expiry)
    c.request = c.request.model_copy(update={"subject": "native_ui"})
    c.challenge = await observations.prepare(c.db, flow_id=c.flow.id, human=c.human, request=c.request)
    c.flow_id = c.flow.id
    await c.db.commit()
    c.http = Request({"type": "http", "headers": [(b"x-adp-observation-id", c.challenge["challenge_id"].encode())]})
    c.backend = GatewayConfigurationBackend(c.db, c.actor)
    c.factory = db_session_factory
    monkeypatch.setattr(launch_module, "get_session_factory", lambda: db_session_factory)
    return c


async def invoke(c):
    return await c.backend.launch_browser(c.request.product_id, c.request.project_id, c.request.binding_id, 1, c.http)


async def events(c):
    async with c.factory() as db:
        rows = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == LAUNCH)))
        return {json.loads(row.reason)["phase"]: (row.id, json.loads(row.reason)) for row in rows}


async def test_denial_is_durable_has_zero_provider_calls_and_cannot_reissue(launch_case, monkeypatch):
    c = launch_case
    provider = AsyncMock(side_effect=HTTPException(403, detail={"code": "delegation_denied"}))
    monkeypatch.setattr(c.backend, "_launch_browser", provider)
    with pytest.raises(HTTPException) as caught:
        await invoke(c)
    await c.db.rollback()
    saved = await events(c)
    assert set(saved) == {"prepared", "denied"}
    receipt_id, receipt = saved["denied"]
    assert caught.value.headers["X-ADP-Observation-Receipt"] == receipt_id
    assert receipt["provider_session_calls"] == 0 and receipt["provider_invocation_started"] is False
    assert receipt["error_code"] == "delegation_denied"
    with pytest.raises(HTTPException) as repeated:
        await invoke(c)
    assert repeated.value.detail["code"] == "l5_observation_launch_already_attempted"
    assert provider.await_count == 1


@pytest.mark.parametrize("succeed", [False, True])
async def test_provider_boundary_is_committed_before_call_and_never_claims_zero(launch_case, monkeypatch, succeed):
    c = launch_case

    async def simulated_provider(*args):
        observer = args[-1]
        await observer.before_provider()
        assert set(await events(c)) == {"prepared", "provider_started"}
        if not succeed:
            raise RuntimeError("lost provider response")
        return {"url": "https://example.invalid/credential-must-never-be-retained"}

    monkeypatch.setattr(c.backend, "_launch_browser", simulated_provider)
    if succeed:
        result = await invoke(c)
        assert result["observation_receipt_id"] == launch_id(c.challenge["challenge_id"], "issued")
    else:
        with pytest.raises(RuntimeError, match="lost provider response"):
            await invoke(c)
    await c.db.rollback()
    saved = await events(c)
    phase = "issued" if succeed else "unknown"
    assert saved[phase][1]["provider_session_calls"] is None
    assert saved[phase][1]["provider_invocation_started"] is True
    assert "credential-must-never-be-retained" not in json.dumps(saved)
    readback = await observations.read(c.db, flow_id=c.flow_id, challenge_id=c.challenge["challenge_id"], human=c.human)
    assert readback["launch_attempt"][-1]["phase"] == phase
    assert readback["launch_attempt"][-1]["provider_session_calls"] is None
    if succeed:
        record = await observations.record(
            c.db,
            flow_id=c.flow_id,
            challenge_id=c.challenge["challenge_id"],
            human=c.human,
            body=RecordObservation(
                snapshot_sha256=c.challenge["snapshot_sha256"],
                observation="displayed",
                displayed_native_hostname="fixture-space.aidevops.global.app.aws",
                launch_receipt_id=saved[phase][0],
            ),
        )
        assert record["release_check_completed"] is False


async def test_changed_scope_prevents_provider_invocation(launch_case, monkeypatch):
    c = launch_case
    calls = []

    async def simulated_provider(*args):
        async with c.factory() as db:
            from l5_services.models.binding import Binding

            binding = await db.get(Binding, (ORG_A, "binding"))
            binding.generation += 1
            await db.commit()
        await args[-1].before_provider()
        calls.append("provider")

    monkeypatch.setattr(c.backend, "_launch_browser", simulated_provider)
    with pytest.raises(HTTPException) as changed:
        await invoke(c)
    assert changed.value.detail["code"] == "l5_observation_snapshot_changed"
    assert calls == []
    assert "provider_started" not in await events(c)
