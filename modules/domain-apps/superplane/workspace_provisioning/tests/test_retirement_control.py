"""A control admission cannot be inferred from a caller's retirement fields."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from harness_jobs.identity import OperationRefused

from workspace_provisioning.retirement_control import resolve_managed_control

from .test_retirement_plan import inventory


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["action", "adopted", "tenant", "allocation", "receipt"])
async def test_control_resolution_refuses_unapproved_scope_before_artifact_read(changed):
    owned = inventory()
    parameters = {
        "original_allocation_id": "paid-allocation",
        "control_allocation_id": "control-allocation",
        "retirement_access_artifact_id": "a" * 64,
        "retirement_request_id": "reviewed-retirement",
    }
    action = "teardown"
    org_id = owned.org_id
    if changed == "action":
        action = "provision"
    elif changed == "adopted":
        owned = replace(owned, cluster_ownership="adopted")
    elif changed == "tenant":
        org_id = "unrelated-org"
    elif changed == "allocation":
        parameters.pop("control_allocation_id")
    elif changed == "receipt":
        parameters.pop("retirement_access_artifact_id")
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(org_id=org_id, workspace_id=owned.workspace_id)
        ),
        request=SimpleNamespace(action=action, parameters=parameters),
    )
    domain_connect = Mock()
    with pytest.raises(OperationRefused, match="approved scope"):
        await resolve_managed_control(
            operation,
            owned,
            SimpleNamespace(domain_connect=domain_connect),
        )
    domain_connect.assert_not_called()
