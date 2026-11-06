"""Installed Transform repair reaches common admission with current scoped consent."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Request
from l5_services.events.schedules import receive_schedule_tick
from l5_services.models import ConfigurationVersion, EventReceipt, Run
from l5_services.models.schedule import EventObservation, ScheduleState
from sqlalchemy import delete, func, select

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_transform_policy import run_policy
from src.products.models import ProductSourceBinding
from src.shared.models.budget import BudgetConfig
from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_transform_backend import transform_gateway as gateway_fixture

transform_gateway = gateway_fixture

PATH = "/internal/v1/l5/transform/schedule-admissions"


@pytest.fixture
async def periodic_transform(transform_gateway, db_session, app, source_member):
    state = transform_gateway
    parent = state.template
    version = await db_session.get(ConfigurationVersion, (parent.tenant_id, "config-a", 1))
    settings = deepcopy(version.settings)
    definition = {**settings["transform"]["definition_ref"], "record_type": "transform_definition"}
    settings["transform"]["definition_ref"] = definition
    state.binding.native_resource_ref = definition
    settings["triggers"] = [
        {
            **settings["triggers"][0],
            "trigger_id": "recurring-transform",
            "kind": "schedule",
            "schedule": "* * * * *",
            "goal": state.body["transform_input"]["goal"],
            "source_policy": "pinned",
        }
    ]
    version.settings = settings
    parent.visibility = settings["visibility"]
    scope = {
        field: deepcopy(getattr(parent, field))
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
    parent.execution_scope = {
        **scope,
        "configuration_id": "config-a",
        "configuration_revision": 1,
        "configuration_hash": version.hash,
        "region": settings["transform"]["region"],
        "provider_account_id": state.binding.provider_identity["account_id"],
        "provider_role_arn": state.binding.provider_identity["role_arn"],
        "binding_id": state.binding.binding_id,
    }
    state.version = version
    state.actor = source_member.model_copy(
        update={
            "account_type": "service",
            "auth_source": "iam",
            "scope": "internal",
            "org_id": "platform-example",
            "user_id": "scheduled-repair",
            "agent_registry_id": "l5-transform-repair",
            "credential_scopes": ["l5:contribution-repair", "l5:schedule-admission"],
        }
    )

    async def iam(request: Request):
        request.state.token_context = state.actor

    app.dependency_overrides[verify_internal_or_irsa] = iam
    await db_session.commit()
    state.tick = datetime.now(UTC).replace(second=0, microsecond=0)
    receipt = await receive_schedule_tick(
        db_session,
        tenant_id=parent.tenant_id,
        product_id=parent.product_id,
        project_id=parent.project_id,
        service_id="transform",
        trigger_id=settings["triggers"][0]["trigger_id"],
        scheduled_at=state.tick,
        now=state.tick,
    )
    assert receipt.state == "pending"
    state.body = {field: getattr(receipt, field) for field in ("tenant_id", "product_id", "project_id", "trigger_id", "event_id")}
    return state


async def test_schedule_admits_once_and_keeps_service_attribution_and_exact_broker_scope(periodic_transform, test_client, db_session):
    state = periodic_transform
    from src.shared.identity.product_scope import service_product_scope

    assert (
        await service_product_scope(
            db_session, state.actor, state.template.id, state.template.product_id, state.template.project_id, "credential:session"
        )
        is None
    )
    first = await test_client.post(PATH, json=state.body)
    assert first.status_code == 200, first.text
    replay = await test_client.post(PATH, json=state.body)
    assert replay.status_code == 200 and replay.json() == first.json(), replay.text
    assert await db_session.scalar(select(func.count()).select_from(Run)) == 1
    run = await db_session.get(Run, (state.template.tenant_id, first.json()["run_id"]))
    assert run.admission["actor_type"] == "service" and run.admission["event_kind"] == "schedule"
    assert run.admission["causal_origin"] == "schedule" and run.admission["actor_id"] == state.template.created_by
    assert run.admission["source_snapshots"] and state.actor.org_id == "platform-example"
    assert run.input["transform_input"]["goal"] == state.version.settings["triggers"][0]["goal"]
    assert len(state.captures) == 1 and state.publish.await_count == 1
    child = await db_session.get(CredentialServiceDelegation, run.admission["delegation_id"])
    policy = json.loads(run_policy(state.version.settings, state.binding, run, child.expires_at))
    assert policy["Statement"][0]["Action"] == ["transform-custom:GetTransformationPackageUrl", "transform-custom:ExecuteTransformation"]
    assert policy["Statement"][0]["Resource"] == "arn:aws:transform-custom:us-east-1:123456789012:package/definition-a"
    assert policy["Statement"][1]["Action"] == ["transform-custom:ConverseStream"]
    assert policy["Statement"][1]["Resource"] == "*"
    assert all(item["Condition"]["StringEquals"] == {"aws:RequestedRegion": "us-east-1"} for item in policy["Statement"])
    assert child.sources == state.template.sources and child.execution_scope == state.template.execution_scope


async def mutate(state, db_session, change):
    if change == "legacy":
        state.template.execution_scope = None
    elif change == "revoked":
        state.template.revoked_at = datetime.now(UTC)
    elif change == "expired":
        state.template.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "source":
        binding = await db_session.scalar(
            select(ProductSourceBinding).where(ProductSourceBinding.native_id == state.template.sources[1]["record_id"])
        )
        binding.status = "revoked"
    elif change == "disabled":
        state.version.settings = {**state.version.settings, "enabled": False}
    elif change == "scope":
        state.template.execution_scope = {**state.template.execution_scope, "configuration_hash": "0" * 64}
    elif change == "binding":
        state.binding.generation += 1
    elif change == "provider":
        state.binding.provider_identity = {**state.binding.provider_identity, "account_id": "999999999999"}
    elif change == "definition":
        state.binding.native_resource_ref = {**state.binding.native_resource_ref, "record_revision": "other"}
    elif change == "qualification":
        state.records[0]["transform_qualification"]["executor_qualification"] = "unqualified"
        state.pin_catalog()
    elif change == "worker":
        state.actor = state.actor.model_copy(update={"agent_registry_id": "l5-well-architected-worker"})
    elif change == "repair_scope":
        state.actor = state.actor.model_copy(update={"credential_scopes": ["l5:contribution-repair"]})
    elif change == "human":
        state.actor = state.actor.model_copy(update={"account_type": "human"})
    elif change in {"future", "missed"}:
        receipt = await db_session.scalar(select(EventReceipt))
        tick = state.tick + timedelta(minutes=1 if change == "future" else -6)
        instant = tick.isoformat().replace("+00:00", "Z")
        receipt.target = {**receipt.target, "scheduled_at": instant}
        receipt.event_id = state.body["event_id"] = f"schedule:{instant}:revision:1"
    elif change == "cron":
        receipt = await db_session.scalar(select(EventReceipt))
        receipt.target = {**receipt.target, "schedule": f"{(state.tick.minute + 1) % 60} * * * *"}
    elif change == "budget":
        budget = await db_session.get(BudgetConfig, "budget-a")
        budget.budget_amount_usd = 0
    elif change == "branch":
        state.version.settings = {**state.version.settings, "triggers": [{**state.version.settings["triggers"][0], "source_policy": "branch"}]}
    else:
        raise AssertionError(change)
    await db_session.commit()


@pytest.mark.parametrize(
    "change",
    [
        "legacy",
        "revoked",
        "expired",
        "source",
        "disabled",
        "scope",
        "binding",
        "provider",
        "definition",
        "qualification",
        "worker",
        "repair_scope",
        "human",
        "future",
        "missed",
        "cron",
        "budget",
        "branch",
    ],
)
async def test_schedule_denies_drift_before_capture_or_dispatch(periodic_transform, test_client, db_session, change):
    state = periodic_transform
    await mutate(state, db_session, change)
    response = await test_client.post(PATH, json=state.body)
    assert response.status_code in {403, 404, 409}, response.text
    assert await db_session.scalar(select(func.count()).select_from(Run)) == 0
    assert not state.captures and not state.publish.await_count
    if change == "branch":
        assert response.json()["detail"]["code"] == "branch_schedule_consent_required"


@pytest.mark.parametrize("change", ["revoked", "source", "binding", "provider"])
async def test_admitted_replay_rechecks_current_authority(periodic_transform, test_client, db_session, change):
    state = periodic_transform
    first = await test_client.post(PATH, json=state.body)
    assert first.status_code == 200, first.text
    await mutate(state, db_session, change)
    denied = await test_client.post(PATH, json=state.body)
    assert denied.status_code in {403, 404, 409}, denied.text
    assert await db_session.scalar(select(func.count()).select_from(Run)) == 1
    assert len(state.captures) == 1 and state.publish.await_count == 1


async def test_body_cannot_supply_owner_source_or_delegation(periodic_transform, test_client):
    response = await test_client.post(PATH, json={**periodic_transform.body, "owner_id": "forged", "delegation_id": "forged", "sources": []})
    assert response.status_code == 422
    assert not periodic_transform.captures


async def test_installed_factory_repair_produces_occurrence_through_gateway(periodic_transform, db_session, app, monkeypatch):
    await exercise_installed_factory(periodic_transform, db_session, app, monkeypatch)


async def exercise_installed_factory(state, db_session, app, monkeypatch):
    from l5_services import runtime_support, runtime_transform
    from l5_services.workers.relay import repair_once
    from sqlalchemy.ext.asyncio import async_sessionmaker

    await db_session.execute(delete(EventObservation))
    await db_session.execute(delete(EventReceipt))
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setenv("L5_SERVICE_ID", "transform")
    for key, value in {
        "L5_TRANSFORM_CLUSTER_ENDPOINT": "https://example.test",
        "L5_TRANSFORM_CLUSTER_NAME": "example",
        "L5_TRANSFORM_CLUSTER_CA": "ZXhhbXBsZQ==",
        "L5_TRANSFORM_NODE_NAME": "node-a",
        "L5_TRANSFORM_NODE_UID": "00000000-0000-0000-0000-000000000001",
        "L5_TRANSFORM_PROVIDER_IMAGE": "example@sha256:" + "b" * 64,
        "L5_TRANSFORM_PROVIDER_NAMESPACE": "providers",
        "L5_TRANSFORM_CHECK_IMAGE": "example@sha256:" + "c" * 64,
        "L5_TRANSFORM_CHECK_NAMESPACE": "checks",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("L5_TRANSFORM_PROVIDER_ENDPOINT_REF", raising=False)
    monkeypatch.delenv("L5_TRANSFORM_PEER_CONTRACTS", raising=False)
    archive = SimpleNamespace(put_archive=AsyncMock(), get_archive=AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://example.test") as transport:
        monkeypatch.setattr(runtime_support, "gateway_transport", lambda: transport)
        monkeypatch.setattr(runtime_support, "sessions", lambda: sessions)
        monkeypatch.setattr(runtime_support, "archive_store", lambda service_id: archive)
        monkeypatch.setattr(runtime_support, "queue_publisher", lambda service_id: state.publish)
        cluster = SimpleNamespace(request=AsyncMock(side_effect=AssertionError("schedule must not access Kubernetes")))
        monkeypatch.setattr(runtime_transform, "TransformKubernetesAPI", lambda **kwargs: cluster)
        worker = runtime_transform.factory()
        worker.clock = lambda: state.tick
        await repair_once(worker)
        await repair_once(worker)
    assert await db_session.scalar(select(func.count()).select_from(Run)) == 1
    receipt = await db_session.scalar(select(EventReceipt).execution_options(populate_existing=True))
    assert receipt.state == "admitted" and receipt.run_id
    assert await db_session.scalar(select(func.count()).select_from(ScheduleState)) == 1
    assert len(state.captures) == 1 and state.publish.await_count == 1


@pytest.mark.parametrize("change", ["revoked", "source", "binding", "provider"])
async def test_qualification_readback_rechecks_authority_before_source_capture(periodic_transform, test_client, db_session, monkeypatch, change):
    from src import l5_transform_schedule

    read = l5_transform_schedule.read_qualification

    async def changed_during_read(*args):
        record = await read(*args)
        await mutate(periodic_transform, db_session, change)
        return record

    monkeypatch.setattr(l5_transform_schedule, "read_qualification", changed_during_read)
    denied = await test_client.post(PATH, json=periodic_transform.body)
    assert denied.status_code in {403, 404, 409}, denied.text
    assert not periodic_transform.captures and not periodic_transform.publish.await_count
    assert await db_session.scalar(select(func.count()).select_from(Run)) == 0
