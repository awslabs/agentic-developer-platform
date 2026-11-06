"""Current Investigation preflight uses real setup bindings without admitting work."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from l5_services.models.binding import Binding
from l5_services.models.run import Run
from sqlalchemy import func, select

from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_l5_devops_resource_verification import (
    activation_settings,
    begin_verification,
)
from tests.products.test_l5_devops_resource_verification import api_setup as api_setup
from tests.products.test_l5_devops_resource_verification import l5_owner as l5_owner
from tests.products.test_l5_devops_resource_verification import resource_setup as resource_setup

__all__ = ["api_setup", "l5_owner", "resource_setup"]


@pytest.fixture
async def active_investigation(test_client, db_session, resource_setup):
    item, setup = resource_setup, resource_setup.setup
    grant, _ = await begin_verification(test_client, db_session, item)
    assert await setup.worker.execute(setup.messages.pop())
    resources = await test_client.get(setup.path + "/bindings/" + item.operation["binding_id"] + "/resources")
    assert resources.status_code == 200, resources.text
    settings = await activation_settings(db_session, setup, item.operation)
    settings["devops"]["accounts"][0]["environments"][0]["resource_refs"] = resources.json()["resource_refs"]
    settings["devops"]["trigger_policies"][0].update(
        resource_refs=resources.json()["resource_refs"], telemetry_refs=resources.json()["telemetry_refs"]
    )
    activated = await test_client.put(
        setup.path + "/configuration",
        json=settings,
        headers={"If-Match": '"' + setup.draft["hash"] + '"', "Idempotency-Key": "devops_readiness_activate"},
    )
    assert activated.status_code == 200, activated.text
    revoked = await test_client.delete(setup.path + "/delegations/" + grant["delegation_id"], headers={"If-Match": '"1"'})
    assert revoked.status_code == 200, revoked.text
    preview = await test_client.get(setup.path + "/delegation-scope", params={"binding_id": item.operation["binding_id"]})
    assert preview.status_code == 200, preview.text
    consent = await test_client.post(
        setup.path + "/delegations",
        json={
            "scope_digest": preview.json()["scope_digest"],
            "binding_id": item.operation["binding_id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        headers={"If-Match": preview.headers["etag"], "Idempotency-Key": "devops_readiness_execution"},
    )
    assert consent.status_code == 200, consent.text
    binding = await db_session.get(Binding, (setup.tenant, item.operation["binding_id"]), populate_existing=True)
    return SimpleNamespace(setup=setup, configuration=activated.json(), consent=consent.json(), binding=binding)


async def test_readiness_returns_exact_identity_without_admitting_work(test_client, db_session, active_investigation, monkeypatch):
    item = active_investigation
    issue = AsyncMock(side_effect=AssertionError("Preflight must not issue work consent"))
    reserve = AsyncMock(side_effect=AssertionError("Preflight must not reserve budget"))
    monkeypatch.setattr("src.l5_integration.issue_l5_work_delegation", issue)
    monkeypatch.setattr("src.l5_integration.GatewayConfigurationBackend._require_devops_budget", reserve)
    before = await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation))
    item.setup.native.reset_mock()
    item.setup.assumed.reset_mock()
    assert item.binding.readiness and all("kind" in row and "capability" not in row for row in item.binding.readiness)
    response = await test_client.post(item.setup.path + "/configuration/check", json={})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ready"] and result["gaps"] == [] and result["provider_contacted"] is False
    for field in ("scope", "configuration_id"):
        assert result[field] == item.configuration[field]
    assert result["configuration_revision"] == item.configuration["revision"]
    assert result["configuration_hash"] == item.configuration["hash"]
    assert result["capability"] == "investigation"
    assert result["binding_id"] == item.binding.binding_id
    assert result["binding_generation"] == item.binding.generation
    assert result["binding_revision"] == item.binding.revision
    assert datetime.fromisoformat(result["valid_until"]) - datetime.fromisoformat(result["checked_at"]) == timedelta(minutes=1)
    assert await db_session.scalar(select(func.count()).select_from(CredentialServiceDelegation)) == before
    assert await db_session.scalar(select(func.count()).select_from(Run)) == 0
    assert item.setup.messages == [] and item.setup.native.mock_calls == []
    item.setup.assumed.assert_not_called()
    issue.assert_not_called()
    reserve.assert_not_called()


async def test_readiness_rechecks_revoked_owner_consent(test_client, active_investigation):
    item = active_investigation
    ready = await test_client.post(item.setup.path + "/configuration/check", json={})
    assert ready.status_code == 200 and ready.json()["ready"] is True
    revoked = await test_client.delete(item.setup.path + "/delegations/" + item.consent["delegation_id"], headers={"If-Match": '"1"'})
    assert revoked.status_code == 200, revoked.text
    response = await test_client.post(item.setup.path + "/configuration/check", json={})
    assert response.status_code == 200, response.text
    assert response.json()["ready"] is False
    assert response.json()["gaps"] == ["delegation_denied"]
    assert response.json()["configuration_hash"] == item.configuration["hash"]
    trigger = item.configuration["settings"]["devops"]["trigger_policies"][0]
    denied = await test_client.post(
        item.setup.path + "/runs",
        json={
            "service_id": "devops",
            "configuration_revision": item.configuration["revision"],
            "binding_id": item.binding.binding_id,
            "capability": "investigation",
            "devops_input": {
                "kind": "investigation",
                "question": "Investigate the selected fixture failure.",
                "resource_refs": trigger["resource_refs"],
                "telemetry_refs": trigger["telemetry_refs"],
            },
        },
        headers={"Idempotency-Key": "devops_readiness_revoked_run"},
    )
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "delegation_denied", denied.text
    assert item.setup.messages == []


async def test_readiness_reports_missing_worker(test_client, active_investigation, monkeypatch):
    monkeypatch.setattr("src.l5_integration.service_publisher", lambda service: None)
    response = await test_client.post(active_investigation.setup.path + "/configuration/check", json={})
    assert response.status_code == 200, response.text
    assert response.json()["ready"] is False
    assert response.json()["gaps"] == ["work_queue_unavailable"]


async def test_readiness_rechecks_binding_generation(test_client, db_session, active_investigation):
    item = active_investigation
    item.binding.generation += 1
    await db_session.commit()
    response = await test_client.post(item.setup.path + "/configuration/check", json={})
    assert response.status_code == 200, response.text
    assert response.json()["ready"] is False
    assert response.json()["gaps"] == ["delegation_denied"]


async def test_unavailable_configured_integration_remains_actionable(db_session, monkeypatch):
    from src.l5_integration import GatewayConfigurationBackend

    selection = {"kind": "cloudwatch", "resource_refs": []}
    version = SimpleNamespace(
        revision=2, state="ready", gaps=[], settings={"capabilities": ["investigation"], "devops": {"integrations": [selection]}}
    )
    observer = AsyncMock(
        return_value=[
            {
                "selection": selection,
                "configuration_revision": 2,
                "unavailable": {"code": "selected_connection_unavailable", "message": "Recheck current provider access."},
            }
        ]
    )
    actor = SimpleNamespace(org_id="tenant-one", user_id="owner-one")
    backend = GatewayConfigurationBackend(db_session, actor, devops_integration_observer=observer)
    backend.current = AsyncMock(return_value=(SimpleNamespace(handoff_state="linked"), version))
    # Isolate aggregation: consent/binding preflight has passed, but the selected
    # integration's real readiness projection still reports unavailable access.
    preflight = AsyncMock(return_value={"ready": True, "gaps": [], "provider_contacted": False})
    monkeypatch.setattr("src.l5_devops_readiness.investigation_readiness", preflight)
    result = await backend.check("product-one", "project-one", "devops")
    assert result["ready"] is False and result["gaps"] == ["integration_not_ready"]
    assert result["integrations"][0]["reason"] == "selected_connection_unavailable"
    assert result["provider_contacted"] is True
    preflight.assert_awaited_once_with(backend, backend.current.return_value[0], version)
    observer.assert_awaited_once_with("tenant-one", "product-one", "project-one", version.settings, 2)
