"""A stale or changed execution grant cannot reach cleanup access effects."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.identity import encode_payload, payload_digest

from workspace_provisioning.retirement_access_context import current_access_operation
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_retirement_access_authority import access_case as access_case


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [None, "attempt", "fence", "holder", "digest", "reservation", "payload"]
)
async def test_current_control_identity_and_provider_preflight_are_required(
    access_case,  # noqa: F811
    tmp_path,
    change,
):
    operation, fixture_context, _ = access_case
    lease = operation.grant.lease
    lease.operation_id, lease.attempt_id, lease.holder, lease.fence_token = (
        "control",
        "attempt",
        "worker",
        4,
    )
    operation.job_id = "control-job"
    operation.plan_digest = payload_digest(operation.request)
    operation.request_payload = encode_payload(operation.request)
    operation.reservation_state = "confirmed"
    current = SimpleNamespace(**vars(operation))
    current.grant = SimpleNamespace(lease=SimpleNamespace(**vars(lease)))
    if change == "attempt":
        current.grant.lease.attempt_id = "new-attempt"
    elif change == "fence":
        current.grant.lease.fence_token = 5
    elif change == "holder":
        current.grant.lease.holder = "other-worker"
    elif change == "digest":
        current.plan_digest = "f" * 64
    elif change == "reservation":
        current.reservation_state = "released"
    elif change == "payload":
        current.request_payload = "{}"
    path = tmp_path / "deployment-policy.json"
    path.write_text(
        json.dumps({"version": 1, "tenants": {lease.org_id: fixture_context.policy}})
    )
    context = SimpleNamespace(
        policy_file=str(path),
        authority=SimpleNamespace(
            resolve=AsyncMock(return_value=current), preflight=AsyncMock()
        ),
    )
    if change is None:
        assert await current_access_operation(operation, context) is current
        context.authority.preflight.assert_awaited_once_with(current)
    else:
        with pytest.raises(LifecycleRefused):
            await current_access_operation(operation, context)
        context.authority.preflight.assert_not_called()
