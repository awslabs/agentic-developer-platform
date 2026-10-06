"""Lifecycle delivery checks real registration before using the protected transport."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters import operation_dispatch
from app.adapters.operation_dispatch import OperationDispatcher
from app.config import settings


class RegisteredConnection:
    def __init__(self, row, registration):
        self.row = row
        self.registration = registration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def fetchrow(self, query, *_):
        if query.startswith("SELECT * FROM workspace_lifecycle_control_operations"):
            return self.registration
        if "JOIN workspaces" in query:
            return self.row
        raise AssertionError("unexpected operation read")


@pytest.mark.asyncio
async def test_registered_control_rechecks_binding_before_protected_dispatch(
    monkeypatch,
):
    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", True)
    monkeypatch.setattr(settings, "superplane_paid_worker_mode", "native-lifecycle")
    monkeypatch.setattr(
        settings, "superplane_paid_worker_binding_file", "/private/binding"
    )
    monkeypatch.setattr(
        settings, "superplane_operation_gateway_url", "https://gateway.example"
    )
    monkeypatch.setattr(
        operation_dispatch,
        "decode_payload",
        lambda _: SimpleNamespace(
            parameters={"lifecycle_phase": "prepare-retirement-access"}
        ),
    )
    monkeypatch.setattr(operation_dispatch, "payload_digest", lambda _: "digest")
    monkeypatch.setattr(
        "app.operation_activation.expected_lifecycle_binding",
        lambda: {"queue_arn": "queue"},
    )
    registration = {
        "operation_id": "op",
        "org_id": "org",
        "workspace_id": "workspace",
        "source_bootstrap_operation_id": "bootstrap",
        "request_id": "request",
        "phase": "prepare-retirement-access",
        "plan_digest": "digest",
    }
    monkeypatch.setattr(
        "workspace_provisioning.control_registry.registration_values",
        AsyncMock(return_value=dict(registration)),
    )
    identity = {
        "operation_id": "op",
        "job_id": "job",
        "attempt_id": "attempt",
        "org_id": "org",
        "workspace_id": "workspace",
        "action": "provision",
        "request_payload": "payload",
    }
    row = {
        **identity,
        "plan_digest": "digest",
        "adp_org_id": "tenant",
        "state": "pending",
    }
    connection = RegisteredConnection(row, registration)
    binding = {
        "version": 1,
        "installed": True,
        "checked_at": datetime.now(UTC).isoformat(),
        "domain": "superplane",
        "org_id": "org",
        "adp_org_id": "tenant",
        "queue_arn": "queue",
    }
    receipt = {
        "version": 1,
        "domain": "superplane",
        "mode": "execution",
        "domain_org_id": "org",
        "adp_org_id": "tenant",
        "status": "pending",
        "invocation_id": "invocation",
        "principal": "invocation#1",
        "not_after": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        **{
            key: identity[key]
            for key in (
                "operation_id",
                "job_id",
                "attempt_id",
                "org_id",
                "workspace_id",
            )
        },
    }
    transport = SimpleNamespace(post=AsyncMock(side_effect=[binding, receipt]))
    dispatcher = OperationDispatcher(
        lambda: connection,
        transport,
        policy_for=lambda _: SimpleNamespace(adp_org_id="tenant"),
    )
    envelope = SimpleNamespace(**identity)
    assert await dispatcher.deliver(envelope) is True
    assert [call.args[0] for call in transport.post.await_args_list] == [
        "/binding-proof",
        "/dispatch",
    ]

    transport.post.reset_mock()
    transport.post.side_effect = None
    transport.post.return_value = {**binding, "installed": False}
    assert await dispatcher.deliver(envelope) is False
    transport.post.assert_awaited_once()

    transport.post.reset_mock()
    connection.registration = None
    assert await dispatcher.deliver(envelope) is False
    transport.post.assert_not_called()

    connection.registration = {**registration, "plan_digest": "wrong"}
    assert await dispatcher.deliver(envelope) is False
    transport.post.assert_not_called()


def test_outbox_and_recovery_require_durable_control_registration():
    for statement in (operation_dispatch._REGISTERED, operation_dispatch._RECOVERABLE):
        assert "workspace_lifecycle_control_operations" in statement
        assert (
            "control.source_bootstrap_operation_id=w.provisioning_operation_id"
            in statement
        )


@pytest.mark.asyncio
async def test_runtime_without_approved_phase_never_reaches_dispatch(monkeypatch):
    monkeypatch.setattr(
        operation_dispatch,
        "decode_payload",
        lambda _: SimpleNamespace(parameters={"runtime_config_sha256": "a" * 64}),
    )
    dispatcher = OperationDispatcher(lambda: None, SimpleNamespace(post=AsyncMock()))
    assert await dispatcher._lifecycle_ready({"request_payload": "payload"}) is False
    dispatcher.transport.post.assert_not_called()


@pytest.mark.asyncio
async def test_controller_only_mode_cannot_dispatch_registered_lifecycle(monkeypatch):
    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", True)
    monkeypatch.setattr(settings, "superplane_paid_worker_mode", "native-controller")
    monkeypatch.setattr(
        operation_dispatch,
        "decode_payload",
        lambda _: SimpleNamespace(
            parameters={"lifecycle_phase": "bootstrap-workspace"}
        ),
    )
    dispatcher = OperationDispatcher(lambda: None, SimpleNamespace(post=AsyncMock()))
    assert await dispatcher._lifecycle_ready({"request_payload": "payload"}) is False
    dispatcher.transport.post.assert_not_called()
