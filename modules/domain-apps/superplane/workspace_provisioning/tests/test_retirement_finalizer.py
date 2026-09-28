"""Retirement verification never converts an unknown provider read to completion."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.execution import CallOutcome
from harness_jobs.identity import OperationRefused
from harness_jobs.inventory import (
    AllocationResource,
    ResourceObservation,
    ResourcePresence,
)

from workspace_provisioning.retirement_finalizer import RetirementFinalizer

from .test_retirement_plan import inventory


@pytest.fixture
def finalizer():
    record = inventory()
    lease = SimpleNamespace(
        operation_id="original-op",
        org_id=record.org_id,
        workspace_id=record.workspace_id,
    )
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=lease),
        request=SimpleNamespace(
            action="teardown",
            parameters={
                "allocation_id": "original",
                "original_allocation_id": "original",
            },
        ),
    )
    resolve = AsyncMock(return_value=(operation, record, None))
    pool = SimpleNamespace(acquire=lambda: None)
    finalize = RetirementFinalizer(
        execution_pool=pool,
        domain_pool=pool,
        resolve=resolve,
        observations=None,
        authenticate=AsyncMock(),
        token_for=lambda op: "verified-opaque-authority",
    )
    retained = AllocationResource(
        "vol-1", "aws", "vol-1", "volume", frozenset({"original-create"})
    )
    finalize.discover = AsyncMock(return_value={"vol-1": retained})
    return finalize, operation, record


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "presence",
    [ResourcePresence.PRESENT, ResourcePresence.UNKNOWN, ResourcePresence.ABSENT],
)
async def test_last_step_requires_fresh_absence_but_does_not_release_allocation(
    finalizer, presence
):
    finalize, operation, record = finalizer
    finalize.observe = AsyncMock(
        return_value=ResourceObservation(
            presence,
            "vol-1",
            provider_state="available"
            if presence is ResourcePresence.PRESENT
            else None,
            detail="provider answer"
            if presence is not ResourcePresence.UNKNOWN
            else "provider unavailable",
        )
    )
    finalize.authority.assess_cleanup = AsyncMock()
    result = await finalize.verify_step(operation, record, AsyncMock())
    assert result[0] is (
        CallOutcome.SUCCEEDED
        if presence is ResourcePresence.ABSENT
        else CallOutcome.UNKNOWN
    )
    # Final release belongs to the separate maintained after-step inventory
    # assessment. The provider step itself cannot return somebody's reservation.
    finalize.authority.assess_cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_new_allocation_or_foreign_ownership_cannot_finish_original_retirement(
    finalizer,
):
    finalize, operation, record = finalizer
    operation.request.parameters["allocation_id"] = "new"
    with pytest.raises(OperationRefused):
        await finalize.verify_step(operation, record, AsyncMock())
    operation.request.parameters["allocation_id"] = "original"
    finalize.resolve.return_value = (
        operation,
        replace(record, org_id="other-tenant"),
        None,
    )
    with pytest.raises(OperationRefused):
        await finalize.verify_step(operation, record, AsyncMock())
    finalize.discover.assert_not_called()
