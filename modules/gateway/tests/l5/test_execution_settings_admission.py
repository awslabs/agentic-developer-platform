"""Legacy incomplete settings fail before native checks or work delegation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.l5_integration import GatewayConfigurationBackend


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", ["security", "well_architected"])
@pytest.mark.parametrize(
    "settings,gaps",
    [
        (
            {"limits": {"deadline_seconds": 300}},
            [
                "classification",
                "limits.max_provider_attempts",
                "limits.max_polls",
                "limits.concurrency",
                "limits.max_cost",
                "limits.currency",
                "limits.policy_ref",
            ],
        ),
        ({"classification": "internal"}, ["limits"]),
    ],
)
async def test_incomplete_legacy_admission_is_actionable_before_any_effects(service_id, settings, gaps):
    backend = object.__new__(GatewayConfigurationBackend)
    backend.current = AsyncMock(
        return_value=(
            SimpleNamespace(tenant_id="example-tenant", project_id="example-project"),
            SimpleNamespace(settings={"enabled": True, **settings}, state="ready", gaps=[]),
        )
    )
    backend._wa_admission_readback = AsyncMock()
    backend.wa_authority = AsyncMock()
    backend._security_work_target = AsyncMock()
    with pytest.raises(HTTPException) as rejected:
        await backend.admission_authority("example-product", "example-project", service_id, {})
    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "execution_policy_unavailable"
    assert rejected.value.detail["gaps"] == gaps
    assert "saved classification and assessment limits" in rejected.value.detail["message"]
    backend._wa_admission_readback.assert_not_awaited()
    backend.wa_authority.assert_not_awaited()
    backend._security_work_target.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", ["security", "well_architected"])
async def test_direct_work_target_also_guards_before_delegation_or_source_access(service_id):
    backend = object.__new__(GatewayConfigurationBackend)
    backend.actor = SimpleNamespace(account_type="human", user_id="owner")
    backend.source_rights = AsyncMock()
    configuration = SimpleNamespace(tenant_id="tenant", product_id="product", project_id="project", owner_id="owner", handoff_state="linked")
    with pytest.raises(HTTPException) as rejected:
        await backend._security_work_target(configuration, SimpleNamespace(settings={"enabled": True}), {}, service_id=service_id)
    assert rejected.value.status_code == 409
    assert rejected.value.detail["gaps"] == ["classification", "limits"]
    backend.source_rights.assert_not_awaited()
