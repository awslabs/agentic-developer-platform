"""Only a live chat capability and an explicit installation reader reach diagnostics."""

from unittest.mock import MagicMock

import pytest

from src.activity.service import WorkActivityPage
from src.shared.models.vault import ChannelTenantMap
from tests.agentauth.test_chat_data_routes import (
    admit,
    exchange,
)
from tests.agentauth.test_chat_data_routes import (
    client as client_fixture,
)
from tests.agentauth.test_chat_data_routes import (
    runtime as runtime_fixture,
)
from tests.agentauth.test_chat_data_routes import (
    store as store_fixture,
)
from tests.agentauth.test_chat_data_routes import (
    sts as sts_fixture,
)

client = client_fixture
runtime = runtime_fixture
sts = sts_fixture
store = store_fixture


@pytest.mark.parametrize("kind", ["status", "failure"])
async def test_personal_work_access_does_not_grant_installation_access(client, runtime, db_session_factory, monkeypatch, kind):
    service = MagicMock()
    service.query_work_by_user.return_value = WorkActivityPage(items=[], direct_cursor=None, descendant_cursor=None)
    monkeypatch.setattr("src.activity.routes.ActivityService", lambda: service)
    monkeypatch.delenv("ADP_TASK_API_READ_ENABLED", raising=False)
    assert (await admit(client, runtime)).status_code == 200
    capability = (await exchange(client)).json()["capability"]
    headers = {"Authorization": f"Bearer {capability}"}
    window = {"run_id": "run-a", "from": "2026-10-01T00:00:00Z", "to": "2026-10-04T00:00:00Z", "timezone": "UTC"}

    work = await client.post("/v1/chat/data/activity/work", headers=headers, json=window)
    assert work.status_code == 200, work.text
    assert work.headers["cache-control"] == "no-store"
    assert work.json()["runs"] == []
    assert service.query_work_by_user.call_args.args == ("human",)
    assert service.query_work_by_user.call_args.kwargs["tenant_id"] == "tenant"

    route = f"/v1/chat/data/installation/{kind}"
    payload = {"run_id": "run-a", "installation_id": 1234}
    denied = await client.post(route, headers=headers, json=payload)
    assert denied.status_code == 404
    assert denied.json() == {"detail": {"error": "chat_scope_refused"}}

    async with db_session_factory() as db:
        db.add(
            ChannelTenantMap(
                provider="github",
                provider_scope_id="account-a",
                org_id="tenant",
                installation_id="1234",
                installed_by_user_id="human",
            )
        )
        await db.commit()

    allowed = await client.post(route, headers=headers, json=payload)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["reason"] == "diagnostic_record_unavailable"
    assert allowed.headers["cache-control"] == "no-store"
    assert service.query_work_by_user.call_count == 1


@pytest.mark.parametrize("kind", ["status", "failure"])
async def test_bounded_diagnostics_require_canonical_installation_reader(client, runtime, db_session_factory, kind):
    route = f"/v1/chat/data/installation/{kind}"
    assert (await admit(client, runtime)).status_code == 200
    capability = (await exchange(client)).json()["capability"]
    headers = {"Authorization": f"Bearer {capability}"}
    payload = {"run_id": "run-a", "installation_id": 1234}

    denied = await client.post(route, headers=headers, json=payload)
    assert denied.status_code == 404
    assert denied.json() == {"detail": {"error": "chat_scope_refused"}}

    async with db_session_factory() as db:
        db.add(
            ChannelTenantMap(
                provider="github",
                provider_scope_id="account-a",
                org_id="tenant",
                installation_id="1234",
                installed_by_user_id="human",
                install_metadata={
                    "secret": "canary-secret-123",
                    "state_url": "s3://private-state",
                    "installed_record": {"schema_version": 2, "desired_release": "v999"},
                },
            )
        )
        db.add(
            ChannelTenantMap(
                provider="github",
                provider_scope_id="account-b",
                org_id="tenant",
                installation_id="5678",
                installed_by_user_id="other",
            )
        )
        await db.commit()

    allowed = await client.post(route, headers=headers, json=payload)
    assert allowed.status_code == 200, allowed.text
    expected = {
        "status": "unavailable",
        "installation_id": 1234,
        "reason": "diagnostic_record_unavailable",
    }
    if kind == "status":
        expected.update(
            {
                "capabilities": {"installed_record": {"status": "unavailable", "reason": "installed_record_provider_unavailable"}},
                "desired_release": {"status": "unavailable", "reason": "installed_record_provider_unavailable"},
                "last_verified_release": {"status": "unavailable", "reason": "installed_record_provider_unavailable"},
                "optional_modules": {"status": "unknown", "reason": "installed_record_provider_unavailable"},
            }
        )
    assert allowed.json() == expected
    assert allowed.headers["cache-control"] == "no-store"
    assert "canary-secret-123" not in allowed.text
    assert "private-state" not in allowed.text
    assert "v999" not in allowed.text

    for supplied in (
        {**payload, "installation_id": 5678},
        {**payload, "installation_id": 9876},
    ):
        refused = await client.post(route, headers=headers, json=supplied)
        assert refused.status_code == 404
        assert "canary-secret-123" not in refused.text


@pytest.mark.parametrize("kind", ["status", "failure"])
async def test_diagnostic_request_rejects_arbitrary_state_and_aws_reads(client, runtime, kind):
    route = f"/v1/chat/data/installation/{kind}"
    assert (await admit(client, runtime)).status_code == 200
    capability = (await exchange(client)).json()["capability"]
    payload = {"run_id": "run-a", "installation_id": 1234}
    assert (await client.post(route, json=payload)).status_code == 401
    for field in ("state_url", "aws_action", "log_group", "account_id", "environment", "component", "credential_ref"):
        rejected = await client.post(route, headers={"Authorization": f"Bearer {capability}"}, json={**payload, field: "canary-secret-123"})
        assert rejected.status_code == 422
        assert "canary-secret-123" not in rejected.text
