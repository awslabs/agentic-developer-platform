"""A staged process cannot admit or dispatch even through direct service calls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.adapters.harness_operation_facade import HarnessOperationFacade
from app.adapters.operation_dispatch import OperationDispatcher
from app.composition import Composition
from app.config import Settings, settings
from app.services.provisioning import ProvisioningUnavailable


@pytest.mark.parametrize("value", ["yes", "1", "TRUE", 1, None, [], {}])
def test_dispatch_setting_is_exact_boolean(value):
    with pytest.raises(ValidationError):
        Settings(superplane_operation_dispatch_enabled=value)


@pytest.mark.parametrize(
    "value,expected", [(True, True), (False, False), ("true", True), ("false", False)]
)
def test_dispatch_setting_accepts_boolean_only(value, expected):
    assert (
        Settings(
            superplane_operation_dispatch_enabled=value
        ).superplane_operation_dispatch_enabled
        is expected
    )


@pytest.mark.asyncio
async def test_disabled_facade_does_not_enter_shared_admission(monkeypatch):
    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", False)
    service = SimpleNamespace(open_operation=AsyncMock())
    facade = HarnessOperationFacade(service)
    with pytest.raises(
        ProvisioningUnavailable, match="disabled for adapter verification"
    ):
        await facade.open_operation(
            action="provision",
            workspace_id="workspace",
            org_id="org",
            permission="workspace:provision",
            parameters={},
        )
    service.open_operation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "constructor_enabled,setting_enabled", [(False, True), (True, False)]
)
async def test_all_dispatch_effects_are_fenced_without_connecting(
    monkeypatch, constructor_enabled, setting_enabled
):
    monkeypatch.setattr(
        settings, "superplane_operation_dispatch_enabled", setting_enabled
    )
    connect = Mock(side_effect=AssertionError("disabled dispatcher touched the DB"))
    transport = SimpleNamespace(post=AsyncMock())
    dispatcher = OperationDispatcher(connect, transport, enabled=constructor_enabled)
    dispatcher.start()
    assert dispatcher._task is None
    assert await dispatcher.drain_once() == ()
    assert await dispatcher.recover_once() == ()
    assert await dispatcher.deliver(None) is False
    assert await dispatcher._dispatch(None, None) is False
    connect.assert_not_called()
    transport.post.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_dispatcher_keeps_non_mutating_readiness(monkeypatch):
    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", False)
    transport = SimpleNamespace(
        post=AsyncMock(
            return_value={
                "version": 1,
                "ready": True,
                "domain": "superplane",
                "org_id": "org",
                "domain_org_id": "org",
                "adp_org_id": "adp",
            }
        )
    )
    dispatcher = OperationDispatcher(
        Mock(),
        transport,
        enabled=False,
        policy_for=lambda _: SimpleNamespace(adp_org_id="adp"),
    )
    assert await dispatcher.ready("org") is True
    transport.post.assert_awaited_once_with(
        "/producer-readiness", {"domain": "superplane", "org_id": "org"}
    )


def test_composition_does_not_start_a_substituted_dispatcher(monkeypatch):
    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", False)
    dispatcher = Mock()
    composition = Composition(
        dispatcher=dispatcher, _connections=SimpleNamespace(opened=True)
    )
    composition.start_dispatcher()
    dispatcher.start.assert_not_called()


@pytest.mark.asyncio
async def test_domain_admission_entrypoints_refuse_before_any_domain_write(monkeypatch):
    from app.routers.workspaces import create_workspace, delete_workspace
    from app.services import (
        controller_deployments,
        deployment_operations,
        lifecycle_proposals,
        retirement_access,
    )
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", False)
    # None for request/body/DB is intentional: touching any input or DB before
    # the guard is a failure. This catches provisional quota and intent writes.
    calls = [
        deployment_operations.create(None, None, None, None, None),
        deployment_operations.delete(None, None, None, None, None, None),
        controller_deployments.admit_controller_deployment(
            None,
            None,
            org_id=None,
            workspace_id=None,
            preview=None,
            approval_id=None,
            revision=None,
        ),
        lifecycle_proposals.continue_lifecycle(
            None, None, None, None, None, None, None
        ),
        retirement_access.admit_access(None, None, None, None, None, None, None),
    ]
    for call in calls:
        with pytest.raises(ProvisioningUnavailable):
            await call
    for call in (
        create_workspace(None, None, None),
        delete_workspace(None, None, None),
    ):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 503


@pytest.mark.parametrize("value", ["workspace-lifecycle", "native", "", None, True])
def test_paid_worker_mode_is_closed(value):
    with pytest.raises(ValidationError):
        Settings(superplane_paid_worker_mode=value)


@pytest.mark.asyncio
async def test_native_mode_refuses_lifecycle_before_domain_reads_or_writes(monkeypatch):
    from app.routers.workspaces import create_workspace, delete_workspace
    from app.services import lifecycle_proposals, retirement_access, provisioning
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", True)
    monkeypatch.setattr(settings, "superplane_paid_worker_mode", "native-controller")
    for call in (
        lifecycle_proposals.continue_lifecycle(
            None, None, None, None, None, None, None
        ),
        retirement_access.admit_access(None, None, None, None, None, None, None),
        provisioning._start(
            operation_id="op",
            action="provision",
            workspace_id="workspace",
            org_id="org",
            parameters={"runtime_config_sha256": "a" * 64},
        ),
    ):
        with pytest.raises(
            ProvisioningUnavailable, match="workspace lifecycle admission"
        ):
            await call
    for call in (
        create_workspace(None, None, None),
        delete_workspace(None, None, None),
    ):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_native_mode_refuses_lifecycle_in_shared_facade_before_reservation(
    monkeypatch,
):
    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", True)
    monkeypatch.setattr(settings, "superplane_paid_worker_mode", "native-controller")
    service = SimpleNamespace(open_operation=AsyncMock())
    with pytest.raises(ProvisioningUnavailable, match="workspace lifecycle admission"):
        await HarnessOperationFacade(service).open_operation(
            action="provision",
            workspace_id="workspace",
            org_id="org",
            permission="workspace:provision",
            parameters={"runtime_config_sha256": "a" * 64},
        )
    service.open_operation.assert_not_called()


@pytest.mark.parametrize("action", ["provision", "cleanup"])
@pytest.mark.asyncio
async def test_native_mode_reaches_existing_admission_for_native_actions(
    monkeypatch, action
):
    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", True)
    monkeypatch.setattr(settings, "superplane_paid_worker_mode", "native-controller")
    # Stop exactly at the real shared-admission boundary, without a substitute success.
    service = SimpleNamespace(
        open_operation=AsyncMock(side_effect=RuntimeError("reached shared admission"))
    )
    with pytest.raises(ProvisioningUnavailable, match="could not establish an outcome"):
        await HarnessOperationFacade(service).open_operation(
            action=action,
            workspace_id="workspace",
            org_id="org",
            permission="workspace:provision",
            parameters={"deployment_id": "native"},
        )
    service.open_operation.assert_awaited_once()
