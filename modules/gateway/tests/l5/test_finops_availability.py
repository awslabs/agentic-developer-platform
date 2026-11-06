"""FinOps inquiry availability is distinct from Product eligibility and setup."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from l5_services.api.router import create_router

from src import l5_integration


@pytest.fixture
def selected_backend(monkeypatch):
    publish = AsyncMock(side_effect=AssertionError("Availability must not dispatch work"))
    authority = AsyncMock(side_effect=AssertionError("Availability must not contact the native executor"))
    actor = SimpleNamespace(org_id="example-tenant", user_id="example-owner")
    backend = l5_integration.GatewayConfigurationBackend(
        SimpleNamespace(bind=None),
        actor,
        publish=publish,
        finops_authority=authority,
    )
    backend.profile = AsyncMock(
        return_value={
            "tenant_id": actor.org_id,
            "project_id": "example-project",
            "configuration": {"selected_capabilities": [{"capability": "finops", "ownership_mode": "managed"}]},
        }
    )
    backend.profile_inputs = AsyncMock(
        return_value=(
            None,
            None,
            {
                "coverage": "complete",
                "next_cursor": None,
                "items": [],
                "regions": [],
            },
        )
    )
    backend.current = AsyncMock(
        return_value=(
            SimpleNamespace(handoff_state="linked"),
            SimpleNamespace(state="needs_input", gaps=[]),
        )
    )
    # Isolate deployment availability from independently tested binding/consent readiness.
    readiness = AsyncMock(return_value={"ready": True, "gaps": [], "provider_contacted": False})
    monkeypatch.setattr(l5_integration, "project_readiness", readiness)
    app = FastAPI()
    app.include_router(create_router(lambda: backend))
    return SimpleNamespace(backend=backend, app=app, publish=publish, authority=authority, readiness=readiness)


@pytest.mark.parametrize("flag,enabled", [(None, False), ("false", False), ("TRUE", False), ("true", True)])
async def test_options_report_existing_admission_switch_without_claiming_qualification(selected_backend, monkeypatch, flag, enabled):
    if flag is None:
        monkeypatch.delenv("L5_FINOPS_ADMISSION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", flag)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=selected_backend.app), base_url="http://test") as client:
        response = await client.get("/v1/products/example-product/projects/example-project/services/finops/configuration/options")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    value = response.json()
    assert value["admissions_enabled"] is enabled
    assert value["capabilities"] == ["cost_inquiry"]
    assert value["readiness"]["status"] == "blocked"
    assert value["provider_contacted"] is False
    selected_backend.backend.current.assert_not_awaited()
    selected_backend.readiness.assert_not_awaited()
    selected_backend.publish.assert_not_awaited()
    selected_backend.authority.assert_not_awaited()


@pytest.mark.parametrize(
    "disabled,missing_publish,missing_authority,project_gaps,expected",
    [
        (False, False, False, [], []),
        (True, False, False, [], ["finops_admissions_disabled"]),
        (False, True, False, [], ["work_queue_unavailable"]),
        (False, False, True, [], ["finops_authority_unavailable"]),
        (False, False, False, ["native_unqualified"], ["native_unqualified"]),
        (
            True,
            True,
            True,
            ["execution_delegation"],
            [
                "execution_delegation",
                "finops_admissions_disabled",
                "work_queue_unavailable",
                "finops_authority_unavailable",
            ],
        ),
    ],
)
async def test_check_distinguishes_inquiry_availability_from_native_readiness(
    selected_backend,
    monkeypatch,
    disabled,
    missing_publish,
    missing_authority,
    project_gaps,
    expected,
):
    monkeypatch.setenv("L5_FINOPS_ADMISSION_ENABLED", "false" if disabled else "true")
    if missing_publish:
        selected_backend.backend.publish = None
    if missing_authority:
        selected_backend.backend.finops_authority = None
    selected_backend.readiness.return_value = {"ready": not project_gaps, "gaps": project_gaps, "provider_contacted": False}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=selected_backend.app), base_url="http://test") as client:
        response = await client.post("/v1/products/example-product/projects/example-project/services/finops/configuration/check", json={})
    assert response.status_code == 200
    assert response.json() == {"ready": not expected, "gaps": expected, "provider_contacted": False}
    selected_backend.publish.assert_not_awaited()
    selected_backend.authority.assert_not_awaited()
