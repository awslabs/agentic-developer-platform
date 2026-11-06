from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest

from app.adapters import bootstrap_observer as observer
from app.config import settings
from app.services.operation_approvals import ApprovalDenied


def setup(monkeypatch):
    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", False)
    request = OperationRequest(
        action="provision",
        idempotency_key="original",
        parameters={"lifecycle_phase": "bootstrap-workspace"},
    )
    row = {k: k for k in observer.FIELDS}
    row.update(
        mode="recovery",
        state="running",
        version=6,
        cleanup_required=False,
        cancel_requested_at=None,
        request_payload=encode_payload(request),
        plan_digest=payload_digest(request),
    )
    lease = {
        "fence_token": 11,
        "closed_at": None,
        "holder": "original-holder",
        "expires_at": datetime.now(UTC) - timedelta(seconds=30),
        "runtime_deadline": datetime.now(UTC) - timedelta(seconds=1),
    }
    connection = SimpleNamespace(
        fetchrow=AsyncMock(
            side_effect=lambda query, *args: (
                lease
                if query.startswith("SELECT * FROM harness_operation_leases")
                else row
            )
        )
    )

    @asynccontextmanager
    async def connect():
        yield connection

    async def post(route, body):
        return {
            **body,
            "version": 1,
            "domain_org_id": row["org_id"],
            "adp_org_id": "adp",
            "status": "pending",
            "invocation_id": "native-run",
            "principal": "native-run#1",
            "not_after": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }

    dispatcher = SimpleNamespace(
        enabled=False,
        connect=connect,
        _domain_registration=AsyncMock(return_value={"adp_org_id": "adp"}),
        _policy=AsyncMock(return_value=SimpleNamespace(adp_org_id="adp")),
        _budget_current=AsyncMock(return_value=True),
        _registration=AsyncMock(return_value=True),
        transport=SimpleNamespace(post=AsyncMock(side_effect=post)),
    )
    return dispatcher, row, lease


async def test_paused_original_bootstrap_publishes_only_bounded_recovery(monkeypatch):
    dispatcher, row, _ = setup(monkeypatch)
    result = await observer.publish(
        dispatcher, row, expected_version=6, expected_fence=11
    )
    route, body = dispatcher.transport.post.await_args.args
    assert route == "/dispatch" and body["mode"] == "recovery"
    assert body["expected_version"] == 6 and body["expected_fence"] == 11
    assert result["observation_only"] is True and result["selector"] == {
        k: row[k] for k in observer.FIELDS
    }
    assert (
        dispatcher.enabled is False
        and settings.superplane_operation_dispatch_enabled is False
    )


@pytest.mark.parametrize(
    "change",
    [
        "live",
        "closed",
        "fence",
        "version",
        "foreign",
        "cancel",
        "execute",
        "enabled",
        "budget",
    ],
)
async def test_invalid_source_cannot_publish_or_change_budget(monkeypatch, change):
    dispatcher, row, lease = setup(monkeypatch)
    original = deepcopy(row)
    if change == "live":
        lease.update(
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
            runtime_deadline=datetime.now(UTC) + timedelta(minutes=1),
        )
    if change == "closed":
        lease["closed_at"] = datetime.now(UTC)
    if change == "fence":
        lease["fence_token"] = 12
    if change == "version":
        row["version"] = 7
    if change == "foreign":
        row["workspace_id"] = "another-workspace"
    if change == "cancel":
        row["cancel_requested_at"] = datetime.now(UTC)
    if change == "execute":
        row["mode"] = "execution"
    if change == "enabled":
        dispatcher.enabled = True
    if change == "budget":
        dispatcher._budget_current.return_value = False
    with pytest.raises(ApprovalDenied):
        await observer.publish(
            dispatcher, original, expected_version=6, expected_fence=11
        )
    dispatcher.transport.post.assert_not_awaited()
