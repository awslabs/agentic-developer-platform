"""Native retirement refuses changed current authority before provider composition."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest

from workspace_provisioning.retirement_composer import (
    current_retirement,
    retirement_delivery_session,
)
from workspace_provisioning.runtime_config import LifecycleRefused


def operation():
    request = OperationRequest(
        action="teardown",
        idempotency_key="retire",
        parameters={"lifecycle_phase": "retire-workspace"},
    )
    return SimpleNamespace(
        request=request,
        job_id="job",
        plan_digest=payload_digest(request),
        request_payload=encode_payload(request),
        reservation_state="confirmed",
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                operation_id="operation",
                org_id="org",
                workspace_id="workspace",
                holder="worker",
                attempt_id="attempt",
                fence_token=1,
            )
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        None,
        "request_payload",
        "plan_digest",
        "reservation_state",
        "job_id",
        "fence_token",
        "holder",
    ],
)
async def test_current_native_authority_is_revalidated_before_provider_delivery(
    changed,
):
    original = operation()
    current = deepcopy(original)
    if changed in {"fence_token", "holder"}:
        setattr(current.grant.lease, changed, "changed")
    elif changed:
        setattr(current, changed, "changed")
    authority = SimpleNamespace(
        resolve=AsyncMock(return_value=current), preflight=AsyncMock()
    )
    if changed:
        with pytest.raises(LifecycleRefused, match="authority changed"):
            await current_retirement(original, SimpleNamespace(authority=authority))
        authority.preflight.assert_not_called()
    else:
        assert (
            await current_retirement(original, SimpleNamespace(authority=authority))
            is current
        )
        authority.preflight.assert_awaited_once_with(current)


@pytest.mark.asyncio
async def test_native_delivery_uses_current_grant_and_rejects_foreign_account():
    original = operation()
    authority = SimpleNamespace(
        resolve=AsyncMock(return_value=original),
        preflight=AsyncMock(),
        provider_session=AsyncMock(
            return_value=SimpleNamespace(
                _superplane_role_arn="arn:aws:iam::222222222222:role/foreign"
            )
        ),
    )
    with pytest.raises(LifecycleRefused, match="another AWS account"):
        await retirement_delivery_session(
            original,
            SimpleNamespace(authority=authority, brokered_provider=True),
            "111111111111",
            "us-east-1",
        )
    _, kwargs = authority.provider_session.call_args
    assert callable(kwargs["verify"])
    authority.resolve.return_value = deepcopy(original)
    authority.resolve.return_value.grant.lease.fence_token = 9
    with pytest.raises(LifecycleRefused, match="authority changed"):
        await kwargs["verify"]()
