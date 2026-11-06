"""The OIDC bridge shares the existing version-bound vault/STS delivery path."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from src.internal import assume_role_routes as assume
from src.internal.sts_assume_service import AssumeRoleResult
from src.orchestration import deployment_credentials as route
from src.shared.models.audit import AuditLog
from tests.internal.test_assume_role_routes import (
    _ROLE_SECRET_JSON,
    _seed_aws_role_credential,
    db,  # noqa: F401
    engine,  # noqa: F401
)


@pytest.mark.parametrize("refusal", [None, "changed_role", "wrong_owner", "revoked_during_sts", "secret_version"])
async def test_job_vault_delivery_revalidates_before_return(db, monkeypatch, refusal):  # noqa: F811
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "999999999999")
    credential = await _seed_aws_role_credential(db)
    saved = {
        "principal_user_id": "user-alice",
        "credential_id": credential.id,
        "credential_role_arn": credential.scopes["role_arn"],
        "inputs": {"customer_aws_label": "prod"},
    }
    if refusal == "changed_role":
        saved["credential_role_arn"] = "arn:aws:iam::123456789012:role/other"
    if refusal == "wrong_owner":
        saved["principal_user_id"] = "another-human"
    state = {"revoked": False}

    async def authorize(*_):
        if state["revoked"]:
            raise HTTPException(403, "revoked")
        return SimpleNamespace(id="action-id", org_id="org-test"), saved

    @asynccontextmanager
    async def factory():
        yield db

    broker = SimpleNamespace(factory=factory, authorize=AsyncMock(side_effect=authorize))
    sm = MagicMock()
    sm.current_version_id.return_value = "different-version" if refusal == "secret_version" else "version-1"
    sm.get_secret_at_version.return_value = (_ROLE_SECRET_JSON, "version-1")

    def sts(**kwargs):
        assert kwargs["role_arn"] == credential.scopes["role_arn"]
        assert kwargs["user_id"] == "user-alice"
        assert kwargs["external_id"] == "adp-dev-hosted-agent"
        if refusal == "revoked_during_sts":
            state["revoked"] = True
        return AssumeRoleResult(
            profile_name="adp-prod",
            access_key_id="private-access",
            secret_access_key="private-secret",
            session_token="private-session",
            expiration="2099-01-01T00:00:00Z",
            region="us-west-2",
            assumed_role_arn="arn:aws:sts::123456789012:assumed-role/ADPDeployAgent/deploy",
        )

    monkeypatch.setattr(assume, "assume_role", sts)
    monkeypatch.setattr(route, "github_claims", AsyncMock(return_value={"run_id": "42"}))
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route.service] = lambda: broker
    app.dependency_overrides[route.get_secrets_manager] = lambda: sm
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        response = await client.post(
            "/orchestration/deployment-credentials/assume",
            headers={"Authorization": "Bearer signed"},
            json={
                "correlation": "c" * 64,
                "source_revision": "a" * 40,
                "inputs": {},
            },
        )
    if refusal:
        assert response.status_code in {403, 404, 409}, response.text
        assert "private-" not in response.text
    else:
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["access_key_id"] == "private-access"
        audit = await db.scalar(select(AuditLog).where(AuditLog.event_type == "vault_aws_role_assumed"))
        assert audit.details["deployment_action_id"] == "action-id"
        assert broker.authorize.await_count == 5
