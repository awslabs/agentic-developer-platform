"""Optional native history preserves original recovery and authorization semantics."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException
import pytest

from app.routers import onboarding
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest


@pytest.mark.parametrize("history", ["verified", "unavailable", "denied"])
async def test_optional_history_cannot_upgrade_original_operation_or_bypass_authority(
    monkeypatch, history
):
    org_id, workspace_id = uuid4(), uuid4()
    request = OperationRequest(
        action="provision",
        idempotency_key=str(uuid4()),
        parameters={"lifecycle_request": "{}"},
    )
    operation_id, current_id = str(uuid4()), str(uuid4())
    row = dict(
        operation_id=operation_id,
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        idempotency_key=request.idempotency_key,
        request_payload=encode_payload(request),
        plan_digest=payload_digest(request),
        state="pending",
        detail="original still pending",
    )

    @asynccontextmanager
    async def connect():
        yield SimpleNamespace(fetch=AsyncMock(return_value=[row]))

    composition = SimpleNamespace(operation_connect=connect, domain_connect=connect)
    incoming = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(trust_composition=composition))
    )
    workspace = SimpleNamespace(id=workspace_id, provisioning_operation_id=current_id)
    db = SimpleNamespace(scalar=AsyncMock(return_value=workspace))
    authority = AsyncMock(return_value=None if history == "denied" else object())
    monkeypatch.setattr(onboarding.GrantBackedAuthority, "resolve", authority)
    proof = {"test": "optional history cannot overwrite original pending state"}
    lineage = AsyncMock(
        return_value=proof,
        side_effect=RuntimeError("private database details")
        if history == "unavailable"
        else None,
    )
    monkeypatch.setattr(
        "workspace_provisioning.lineage.verified_native_lineage", lineage
    )
    if history == "denied":
        with pytest.raises(HTTPException) as refused:
            await onboarding.operation_response(
                incoming, db, org_id, request.idempotency_key, by_request=True
            )
        assert refused.value.status_code == 403
        lineage.assert_not_awaited()
        db.scalar.assert_not_awaited()
        return
    result = await onboarding.operation_response(
        incoming, db, org_id, request.idempotency_key, by_request=True
    )
    assert result["state"] == "pending"
    assert result["reason"] == "original still pending"
    assert result["request_id"] == request.idempotency_key
    assert result["provisioning_operation_id"] == operation_id
    assert result["workspace_id"] == str(workspace_id)
    assert result["retryable"] is False
    assert result["lifecycle_lineage"] == (None if history == "unavailable" else proof)
    assert "private database details" not in str(result)
    lineage.assert_awaited_once_with(
        connect,
        connect,
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        root_operation_id=operation_id,
        current_operation_id=current_id,
    )
