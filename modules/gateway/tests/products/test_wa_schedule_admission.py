"""Real installed schedule route, owner delegation, source capture and admission."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import httpx
import pytest
from fastapi import Request
from l5_services.adapters.well_architected.binding import ProfileAdapter, ProfileSelection
from l5_services.events.schedules import receive_schedule_tick
from l5_services.models.event import EventReceipt
from l5_services.models.run import Run
from l5_services.models.schedule import EventObservation
from sqlalchemy import delete, func, select

from src.internal.auth_deps import verify_internal_or_irsa
from src.l5_service_identity import worker_id
from src.shared.models.budget import BudgetConfig
from tests.products.test_wa_backend_authority import installed_wa as installed_wa

PATH = "/internal/v1/l5/well-architected/schedule-admissions"


@pytest.fixture
async def periodic_wa(installed_wa, db_session, app, source_member):  # noqa: F811 - imported pytest fixture
    state = installed_wa
    trigger = {**state.version.settings["triggers"][0], "kind": "schedule", "trigger_id": "weekly-review", "schedule": "* * * * *"}
    state.version.settings = {**state.version.settings, "triggers": [trigger]}
    grant, settings = state.template, state.version.settings
    identity = state.binding.provider_identity
    qualification = {
        **identity,
        "broker_role_arn": identity["role_arn"],
        "trust": "qualified",
        "source_permissions": "qualified",
        "support": "eligible",
    }
    selection = ProfileSelection(
        grant.tenant_id,
        state.product_id,
        state.project_id,
        identity["account_id"],
        identity["region"],
        identity["role_arn"],
        identity["execution_role_arn"],
        ("SECURITY",),
        qualification,
    )
    profile_hash = (
        ProfileAdapter(boto3.client("wellarchitected"), account_id=identity["account_id"], region=identity["region"])
        .adopt(selection, state.binding.native_resource_ref["record_id"])
        .snapshot_hash
    )
    state.binding.native_resource_ref = {**state.binding.native_resource_ref, "record_revision": profile_hash}
    grant.execution_scope = {
        field: deepcopy(getattr(grant, field))
        for field in (
            "tenant_id",
            "product_id",
            "project_id",
            "service_id",
            "credential_id",
            "created_by",
            "binding_generation",
            "visibility",
            "operations",
            "capabilities",
            "sources",
        )
    }
    grant.execution_scope = {
        **grant.execution_scope,
        "configuration_id": "wa-config",
        "configuration_revision": 1,
        "configuration_hash": state.version.hash,
        "region": settings["region"],
        "provider_account_id": identity["account_id"],
        "provider_role_arn": identity["role_arn"],
        "binding_id": state.binding.binding_id,
    }
    await db_session.commit()
    state.actor = source_member.model_copy(
        update={
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "org_id": "platform-example",
            "user_id": "scheduled-repair",
            "agent_registry_id": "l5-well-architected-repair",
            "credential_scopes": ["l5:contribution-repair", "l5:schedule-admission"],
        }
    )

    async def iam(request: Request):
        request.state.token_context = state.actor

    app.dependency_overrides[verify_internal_or_irsa] = iam
    state.tick = datetime.now(UTC).replace(second=0, microsecond=0)
    receipt = await receive_schedule_tick(
        db_session,
        tenant_id=grant.tenant_id,
        product_id=state.product_id,
        project_id=state.project_id,
        service_id="well_architected",
        trigger_id=trigger["trigger_id"],
        scheduled_at=state.tick,
    )
    assert receipt.state == "pending"
    state.body = {field: getattr(receipt, field) for field in ("tenant_id", "product_id", "project_id", "trigger_id", "event_id")}
    state.calls.clear()
    return state


async def test_installed_schedule_admits_once_with_service_actor_and_current_owner_consent(periodic_wa, test_client, db_session):
    state = periodic_wa
    from src.l5_integration import GatewayConfigurationBackend
    from src.l5_wa_schedule import current_selection
    from src.shared.identity.product_scope import service_product_scope

    assert await service_product_scope(db_session, state.actor, state.template.id, state.product_id, state.project_id, "credential:session") is None
    await current_selection(GatewayConfigurationBackend(db_session, state.actor), await db_session.scalar(select(EventReceipt)))
    assert state.native["eligibleForArchitectureGeneration"] is True
    assert state.native["eligibleForScheduledGeneration"] is False
    first = await test_client.post(PATH, json=state.body)
    assert first.status_code == 200, first.text
    second = await test_client.post(PATH, json=state.body)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert await db_session.scalar(select(func.count()).select_from(Run)) == 1
    run = await db_session.get(Run, (state.template.tenant_id, first.json()["run_id"]))
    assert run.admission["actor_type"] == "service" and run.admission["event_kind"] == "schedule"
    assert run.admission["causal_origin"] == "schedule" and run.admission["actor_id"] == state.template.created_by
    assert run.admission["source_snapshots"] and state.actor.account_type == "service" and state.actor.org_id == "platform-example"
    assert len(state.queue) == 1
    observations = (await db_session.scalars(select(EventObservation).where(EventObservation.kind == "admitted"))).all()
    assert len(observations) == 2


@pytest.mark.parametrize(
    "change",
    [
        "legacy",
        "revoked",
        "expired",
        "source",
        "disabled",
        "scope",
        "worker",
        "repair_scope",
        "future",
        "missed",
        "budget",
        "native",
        "partial_limits",
        "classification",
        "budget_scope",
    ],
)
async def test_schedule_rejects_changed_authority_and_never_creates_run(periodic_wa, test_client, db_session, change):
    state = periodic_wa
    if change == "legacy":
        state.template.execution_scope = None
    elif change == "revoked":
        state.template.revoked_at = datetime.now(UTC)
    elif change == "expired":
        state.template.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "source":
        state.source_binding.status = "revoked"
    elif change == "disabled":
        state.version.settings = {**state.version.settings, "enabled": False}
    elif change == "partial_limits":
        state.version.settings = {**state.version.settings, "limits": {"deadline_seconds": 300}}
    elif change == "classification":
        state.version.settings = {**state.version.settings, "classification": "invalid"}
    elif change == "budget_scope":
        limits = state.version.settings["limits"]
        state.version.settings = {
            **state.version.settings,
            "limits": {**limits, "policy_ref": {**limits["policy_ref"], "project_id": "foreign-project"}},
        }
    elif change == "scope":
        state.template.execution_scope = {**state.template.execution_scope, "configuration_hash": "0" * 64}
    elif change == "worker":
        state.actor = state.actor.model_copy(update={"agent_registry_id": worker_id("security")})
    elif change == "repair_scope":
        state.actor = state.actor.model_copy(update={"credential_scopes": ["l5:contribution-repair"]})
    elif change in {"future", "missed"}:
        receipt = await db_session.scalar(select(EventReceipt))
        tick = state.tick + timedelta(minutes=1 if change == "future" else -6)
        instant = tick.isoformat().replace("+00:00", "Z")
        receipt.target = {**receipt.target, "scheduled_at": instant}
        receipt.event_id = state.body["event_id"] = f"schedule:{instant}:revision:1"
    elif change == "budget":
        budget = await db_session.get(BudgetConfig, "wa-budget")
        budget.budget_amount_usd = 0
    else:
        state.native["eligibleForArchitectureGeneration"] = False
        state.native["eligibleForScheduledGeneration"] = True
    await db_session.commit()
    response = await test_client.post(PATH, json=state.body)
    assert response.status_code in {403, 404, 409}, response.text
    if change in {"partial_limits", "classification", "budget_scope"}:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "execution_policy_unavailable"
        assert response.json()["detail"]["gaps"]
    assert await db_session.scalar(select(func.count()).select_from(Run)) == 0
    assert not state.queue
    if change not in {"budget", "native"}:
        assert not state.calls


async def test_schedule_body_cannot_supply_owner_or_delegation(periodic_wa, test_client):
    response = await test_client.post(PATH, json={**periodic_wa.body, "owner_id": "forged", "delegation_id": periodic_wa.template.id})
    assert response.status_code == 422
    assert not periodic_wa.calls


async def test_installed_zero_argument_factory_repair_reaches_gateway_admission(periodic_wa, app, db_session, monkeypatch):
    from l5_services import runtime_support, runtime_well_architected
    from l5_services.workers.relay import repair_once
    from sqlalchemy.ext.asyncio import async_sessionmaker

    state = periodic_wa
    await db_session.execute(delete(EventObservation))
    await db_session.execute(delete(EventReceipt))
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setenv("L5_SERVICE_ID", "well_architected")
    monkeypatch.delenv("L5_WA_PEER_CONTRACTS", raising=False)
    archive = SimpleNamespace(put_archive=AsyncMock(), get_archive=AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://example.test") as transport:
        monkeypatch.setattr(runtime_support, "gateway_transport", lambda: transport)
        monkeypatch.setattr(runtime_support, "sessions", lambda: sessions)
        monkeypatch.setattr(runtime_support, "archive_store", lambda service_id: archive)
        monkeypatch.setattr(runtime_support, "queue_publisher", lambda service_id: state.backend.publish)
        worker = runtime_well_architected.factory()
        worker.clock = lambda: state.tick
        await repair_once(worker)
        await repair_once(worker)
    assert await db_session.scalar(select(func.count()).select_from(Run)) == 1
    receipt = await db_session.scalar(select(EventReceipt).execution_options(populate_existing=True))
    assert receipt.state == "admitted" and receipt.run_id
    assert await db_session.scalar(select(func.count()).select_from(EventObservation).where(EventObservation.kind == "repair_admitted")) == 1
    assert len(state.queue) == 1
