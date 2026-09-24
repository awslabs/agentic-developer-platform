"""An authenticated request cannot turn missing cleanup access into authority."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from app.routers.retirement import (
    RetirementAdmission,
    RetirementPreview,
    retirement_admission,
)
from app.services import provisioning, retirement
from tests.test_deployment_replay_postgres import runtime as runtime
from tests.test_installation_postgres import (
    installation_postgres_url as installation_postgres_url,
    isolated_database as isolated_database,
    pytestmark as postgres_required,
)

pytestmark = postgres_required


async def test_missing_staged_cleanup_recipe_never_admits_or_changes_workspace(
    runtime,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(retirement, "async_session_factory", runtime.factory)
    facade = SimpleNamespace(open_operation=AsyncMock())
    monkeypatch.setattr(provisioning, "_facade", facade)
    async with runtime.factory() as db:
        db.add(
            WorkspaceGrantRecord(
                org_id=runtime.org_id,
                workspace_id=runtime.workspace_id,
                principal="test-user",
                principal_type="human",
                permissions="workspace:provision",
            )
        )
        await db.commit()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(trust_composition=SimpleNamespace()))
    )
    body = RetirementAdmission(
        operation_id=uuid.uuid4(), plan_revision="a" * 64, approval_id=str(uuid.uuid4())
    )
    async with runtime.factory() as db:
        with pytest.raises(HTTPException) as refusal:
            await retirement_admission(
                runtime.workspace_id, body, request, runtime.org_id, db
            )
        assert refusal.value.status_code == 503
    facade.open_operation.assert_not_called()
    async with runtime.factory() as db:
        workspace = await db.get(Workspace, runtime.workspace_id)
        assert workspace.status == "Active"
        assert workspace.teardown_operation_id is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("org_id", "another-org"),
        ("execution_steps", "[]"),
        ("allocation_id", "another-allocation"),
        ("grant_policy", {"Action": "*"}),
    ],
)
def test_retirement_preview_rejects_caller_supplied_ownership_or_authority(
    field, value
):
    with pytest.raises(ValidationError):
        RetirementPreview.model_validate(
            {"operation_id": str(uuid.uuid4()), field: value}
        )
