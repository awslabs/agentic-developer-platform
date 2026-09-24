"""A completed bootstrap must not break clients listing actionable plans."""

from contextlib import asynccontextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.routers.onboarding import lifecycle_proposals
from app.services import lifecycle_proposals as service


@pytest.mark.parametrize("complete", [False, True])
async def test_listing_keeps_only_approval_proposals_and_preserves_result_evidence(
    monkeypatch, complete
):
    org_id, workspace_id = uuid4(), uuid4()
    artifact = {
        "artifact_id": "a" * 64,
        "workspace_id": str(workspace_id),
        "source_operation_id": "original-operation",
        "source_job_id": "original-job",
        "source_attempt_id": "original-attempt",
        "request_revision": "b" * 64,
        "account_id": "123456789012",
        "target_json": json.dumps({"account_id": "123456789012"}),
        "parameters_json": json.dumps(
            {
                "lifecycle_phase": "bootstrap-workspace"
                if complete
                else "prepare-infrastructure",
                "lifecycle_artifact_id": "c" * 64,
                "lifecycle_request": "{}",
                "lifecycle_inputs": "{}",
            }
        ),
        "artifact_metadata_json": json.dumps(
            {
                "next_phase": "complete" if complete else "apply-infrastructure",
                "plan_file_sha256": "d" * 64,
                "plan_json_sha256": "e" * 64,
            }
        ),
    }
    retained = deepcopy(artifact)
    connection = SimpleNamespace(
        fetch=AsyncMock(return_value=[{"artifact_id": artifact["artifact_id"]}])
    )

    @asynccontextmanager
    async def connect():
        yield connection

    composition = SimpleNamespace(operation_connect=connect)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(trust_composition=composition))
    )
    db = object()
    scope = AsyncMock(
        return_value=(
            SimpleNamespace(provisioning_operation_id="original-operation"),
            object(),
        )
    )
    verified = AsyncMock(return_value=artifact)
    monkeypatch.setattr(service, "workspace_scope", scope)
    monkeypatch.setattr(service, "verified_proposal", verified)

    result = await lifecycle_proposals(workspace_id, request, org_id, db)

    scope.assert_awaited_once_with(db, org_id, workspace_id)
    verified.assert_awaited_once_with(
        composition, org_id, workspace_id, artifact["artifact_id"]
    )
    assert result["workspace_id"] == str(workspace_id)
    assert artifact == retained
    if complete:
        assert result["proposals"] == []
    else:
        assert len(result["proposals"]) == 1
        assert result["proposals"][0]["status"] == "awaiting_plan_approval"
        assert result["proposals"][0]["artifact_id"] == artifact["artifact_id"]
