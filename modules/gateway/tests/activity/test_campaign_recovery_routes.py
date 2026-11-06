from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.activity import campaign_recovery_routes as routes
from src.agentauth import human_control
from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.exit_retention import ExitRetentionError
from src.agentauth.workload import WorkloadRefusedError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext
from tests.agentauth.test_campaign_execution import native as native_fixture
from tests.agentauth.test_campaign_execution import retirement

native = native_fixture


async def request(native, monkeypatch, *, changes=None, membership=None, arguments=None, action="retirement", invocation="root"):
    app = FastAPI()
    app.include_router(routes.router)
    current = TokenContext(
        user_id="human", org_id="tenant", team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(minutes=5)
    ).model_copy(update=changes or {})
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_db] = lambda: Mock()
    app.dependency_overrides[routes.get_campaign_recovery] = lambda: native.service
    monkeypatch.setattr(routes, "get_agent_runtime", lambda: native.runtime)

    @asynccontextmanager
    async def fresh_db():
        yield Mock()

    monkeypatch.setattr(routes, "get_session_factory", lambda: fresh_db)

    async def canonical(_db, identifier, **kwargs):
        return "canonical-human" if identifier == "human" else identifier

    monkeypatch.setattr(human_control, "resolve_canonical_user_id", canonical)
    monkeypatch.setattr(routes, "resolve_canonical_user_id", canonical)
    monkeypatch.setattr(human_control, "require_live_human_membership", AsyncMock(side_effect=membership))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if arguments is None:
            return await client.get(f"/activity/invocations/{invocation}/campaign-binding")
        return await client.request(
            "POST" if action == "stop" else "PUT",
            f"/activity/invocations/{invocation}/campaign-{action}/{arguments['operation_id']}",
            json={"expected_version": arguments["expected_version"]},
        )


def register(native):
    for invocation in ("root", "child"):
        native.client.put_item(
            TableName=native.store.table,
            Item={
                "pk": {"S": "TENANT#tenant"},
                "sk": {"S": f"REG#{invocation}#1"},
                "invocation_id": {"S": invocation},
                "tenant_id": {"S": "tenant"},
                "attempt": {"N": "1"},
                "generation": {"N": "7"},
                "workload_binding": {"S": f"pod-{invocation}"},
                "address": {"S": "127.0.0.1"},
                "port": {"N": "8770"},
                "registered_at": {"S": "fixture-registration"},
            },
        )
    native.runtime.workloads.has_exited = Mock(return_value=False)
    native.runtime.workloads.verify_bound = Mock()
    native.runtime.workloads.observation_scope = {"namespace": "agents", "service_account": "worker"}
    native.runtime.workloads.campaign_recovery = Mock()
    native.runtime.workloads.campaign_recovery.retained.return_value = False


async def test_owner_captures_and_retires_with_an_idempotent_authority_only_receipt(native, monkeypatch):
    captured = await request(native, monkeypatch)
    assert captured.status_code == 200
    arguments = retirement(native)
    assert captured.json()["version"] == arguments["expected_version"]
    response = await request(native, monkeypatch, arguments=arguments)
    assert response.status_code == 200
    assert response.json()["status"] == "authority_revoked"
    assert response.headers["cache-control"] == "no-store"
    replay = await request(native, monkeypatch, arguments=arguments)
    assert replay.json() == response.json()


@pytest.mark.parametrize("action", ["retirement", "stop", "enrollment"])
@pytest.mark.parametrize(
    "changes,membership",
    [
        ({"user_id": "another-human", "is_admin": True}, None),
        ({"org_id": "another-tenant"}, None),
        ({"account_type": "service"}, None),
        ({"auth_source": "iam"}, None),
        ({"expires_at": datetime.now(UTC) - timedelta(seconds=1)}, None),
        ({}, BootstrapRefusedError("membership revoked")),
        ({}, [None, BootstrapRefusedError("revoked before commit")]),
    ],
)
async def test_nonowner_nonhuman_and_revoked_membership_never_mutate(native, monkeypatch, changes, membership, action):
    register(native)
    arguments = retirement(native)
    response = await request(native, monkeypatch, arguments=arguments, changes=changes, membership=membership, action=action)
    assert response.status_code == 409
    assert native.store._read("TENANT#tenant", "EXEC#root")["status"] == {"S": "active"}
    assert native.store._read("TENANT#tenant", f"CAMPAIGN_RETIRE#{arguments['operation_id']}") is None
    native.runtime.workloads.campaign_recovery.request_stop.assert_not_called()


async def test_root_revocation_fences_agents_but_does_not_disable_independent_descendant_cleanup(native, monkeypatch):
    register(native)
    arguments = {invocation: retirement(native, invocation) for invocation in ("root", "child")}
    for invocation in arguments:
        assert (
            human_control._protected_human_owner(native.store, tenant_id="tenant", run_id=invocation, generation=7, now=datetime.now(UTC)) == "human"
        )
    response = await request(native, monkeypatch, arguments=arguments["root"], action="stop")
    assert response.status_code == 202
    assert response.json()["status"] == "stop_requested"
    for invocation, error in (("root", "target execution is unavailable"), ("child", "parent delegation refused")):
        with pytest.raises(BootstrapRefusedError, match=error):
            human_control._protected_human_owner(native.store, tenant_id="tenant", run_id=invocation, generation=7, now=datetime.now(UTC))
    with pytest.raises(BootstrapRefusedError, match="parent delegation refused"):
        native.runtime.authenticate(native.credentials["child"], "child")
    native.runtime.authenticate(native.credentials["unrelated"], "unrelated")
    child = await request(native, monkeypatch, arguments=arguments["child"], action="stop", invocation="child")
    assert child.status_code == 202
    native.runtime.workloads.campaign_recovery.request_stop.assert_called_with(
        name="worker-child", uid="pod-child", invocation_id="child", tenant_id="tenant", operation_id=arguments["child"]["operation_id"]
    )
    assert native.store._read("TENANT#tenant", f"CAMPAIGN_EXIT#{arguments['root']['operation_id']}") is None


async def test_root_completion_and_retirement_fence_descendants_without_rewriting_terminal_status(native, monkeypatch):
    register(native)
    arguments = retirement(native)
    child_arguments = retirement(native, "child")
    native.runtime.workloads.has_exited.return_value = True
    execution = native.store._read("TENANT#tenant", "EXEC#root")
    execution["status"] = {"S": "completed"}
    native.client.put_item(TableName=native.store.table, Item=execution)
    response = await request(native, monkeypatch, arguments=arguments, action="stop")
    assert response.status_code == 200
    assert response.json()["status"] == "workload_exited"
    assert native.store._read("TENANT#tenant", "EXEC#root")["status"] == {"S": "completed"}
    with pytest.raises(BootstrapRefusedError, match="parent delegation refused"):
        native.runtime.authenticate(native.credentials["child"], "child")
    native.runtime.workloads.has_exited.return_value = False
    child = await request(native, monkeypatch, arguments=child_arguments, action="stop", invocation="child")
    assert child.status_code == 202


@pytest.mark.parametrize(
    "row,field,value",
    [
        ("REG#root#1", "generation", {"N": "8"}),
        ("REG#root#1", "workload_binding", {"S": "replacement"}),
        ("EXEC#root", "current_attempt", {"N": "2"}),
        ("EXEC#root", "current_credential_epoch", {"N": "2"}),
    ],
)
async def test_replacement_is_preserved_even_after_lost_stop_reply(native, monkeypatch, row, field, value):
    register(native)
    arguments = retirement(native)
    native.runtime.workloads.campaign_recovery.request_stop.side_effect = ExitRetentionError("lost reply")
    first = await request(native, monkeypatch, arguments=arguments, action="stop")
    assert first.status_code == 503
    changed = native.store._read("TENANT#tenant", row)
    changed[field] = value
    native.client.put_item(TableName=native.store.table, Item=changed)
    native.runtime.workloads.campaign_recovery.reset_mock()
    replay = await request(native, monkeypatch, arguments=arguments, action="stop")
    assert replay.status_code == 409
    native.runtime.workloads.campaign_recovery.request_stop.assert_not_called()
    assert native.store._read("TENANT#tenant", row) == changed


async def test_lost_stop_reply_can_resume_after_revocation_and_observe_exact_exit(native, monkeypatch):
    register(native)
    arguments = retirement(native)
    recovery = native.runtime.workloads.campaign_recovery
    recovery.request_stop.side_effect = ExitRetentionError("lost reply")
    assert (await request(native, monkeypatch, arguments=arguments, action="stop")).status_code == 503
    recovery.retained.return_value = True
    recovery.request_stop.side_effect = None
    native.runtime.workloads.verify_bound.side_effect = AssertionError("retained terminating workload is not newly admitted")
    assert (await request(native, monkeypatch, arguments=arguments, action="stop")).status_code == 202
    native.runtime.workloads.has_exited.return_value = True
    exited = await request(native, monkeypatch, arguments=arguments, action="stop")
    assert exited.status_code == 200
    assert exited.json()["status"] == "workload_exited"
    recovery.release.assert_called_once()


async def test_lost_exit_reply_reconciles_after_pod_disappears(native, monkeypatch):
    register(native)
    arguments = retirement(native)
    native.runtime.workloads.has_exited.return_value = True
    transact = native.client.transact_write_items

    def lost(**kwargs):
        transact(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://example.invalid")

    monkeypatch.setattr(native.client, "transact_write_items", lost)
    response = await request(native, monkeypatch, arguments=arguments, action="stop")
    assert response.status_code == 503
    monkeypatch.setattr(native.client, "transact_write_items", transact)
    native.runtime.workloads.has_exited.side_effect = AssertionError("do not re-observe a durably recorded exit")
    native.runtime.workloads.verify_bound.side_effect = WorkloadRefusedError("pod no longer exists")
    retry = await request(native, monkeypatch, arguments=arguments, action="stop")
    assert retry.status_code == 200
    assert retry.json()["status"] == "workload_exited"


async def test_missing_or_replaced_pod_does_not_establish_exit(native, monkeypatch):
    register(native)
    arguments = retirement(native)
    native.runtime.workloads.verify_bound.side_effect = WorkloadRefusedError("missing or replaced workload")
    response = await request(native, monkeypatch, arguments=arguments, action="stop")
    assert response.status_code == 409
    assert native.store._read("TENANT#tenant", f"CAMPAIGN_EXIT#{arguments['operation_id']}") is None
    native.runtime.workloads.campaign_recovery.request_stop.assert_not_called()
