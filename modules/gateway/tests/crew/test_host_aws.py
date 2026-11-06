"""Exercise real connection selection and STS delivery under a verified host identity."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.aws_connection_authority import connection_binding
from src.crew import host_aws
from src.crew.model_token import mint_model_token
from src.internal import assume_role_routes
from src.internal.sts_assume_service import AssumeRoleResult
from src.proxy.routes import get_token_context, set_agent_run_id_from_header
from src.shared.models.audit import AuditLog
from src.shared.models.onboarding import TenantMembership
from src.shared.models.vault import UserCredential
from tests.crew.test_host_github import session  # noqa: F401
from tests.crew.test_host_tools import active, host_context, registry  # noqa: F401

ROLE = "arn:aws:iam::123456789012:role/OwnerRole"


async def connect(db, label="development", **changes):
    row = UserCredential(
        id="connection-" + label,
        org_id="tenant-a",
        user_id="owner-a",
        service="aws",
        label=label,
        credential_type="aws_role",
        secret_arn="synthetic-secret",
        agent_consent=True,
        aws_external_id="synthetic-trust",
        aws_verification_attempt="attempt",
        aws_verified_version_id="version",
        aws_verified_at=datetime.now(UTC),
        scopes={"account_id": "123456789012", "role_arn": ROLE, "status": "verified", "source": "imported_role"},
    )
    for key, value in changes.items():
        setattr(row, key, value)
    db.add(row)
    await db.flush()
    row.aws_verified_binding = connection_binding(row)
    await db.commit()
    return row


@pytest.fixture
async def api(session, active, monkeypatch):  # noqa: F811
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr(host_aws, "get_session_factory", lambda: factory)
    session.add(TenantMembership(user_id="owner-a", tenant_id="tenant-a", is_active=True))
    await session.commit()
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "000000000001")
    sm = MagicMock()
    sm.current_version_id.return_value = "version"
    sm.get_secret_at_version.return_value = (
        json.dumps({"account_id": "123456789012", "role_arn": ROLE, "external_id": "synthetic-trust"}),
        "version",
    )
    issue = MagicMock(
        return_value=AssumeRoleResult(
            "synthetic-access",
            "synthetic-secret",
            "synthetic-session",
            (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            "us-east-1",
            "adp-aws-development",
        )
    )
    monkeypatch.setattr(assume_role_routes, "assume_role", issue)
    app = FastAPI()
    app.include_router(host_aws.router)
    identity = SimpleNamespace(context=host_context())
    app.dependency_overrides[get_token_context] = lambda: identity.context
    app.dependency_overrides[set_agent_run_id_from_header] = lambda: active.run_id
    app.dependency_overrides[host_aws.get_secrets_manager] = lambda: sm
    token = await mint_model_token(active.owner, active.target, now=active.now)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://host", headers={"X-Adp-Crew-Model-Token": token}) as client:
        yield SimpleNamespace(client=client, identity=identity, issue=issue, sm=sm)


async def post(api, **body):
    return await api.client.post("/crew/aws-credentials", json={"operation": "assume", **body})


async def test_automatic_default_reuses_broker_and_records_owner_host_without_secrets(api, session, active):  # noqa: F811
    await connect(session)
    response = await post(api)
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"Version", "AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration"}
    assert response.headers["cache-control"] == "no-store"
    assert api.issue.call_args.kwargs["user_id"] == "owner-a"
    assert api.issue.call_args.kwargs["agent_id"] == "dev-box"
    assert api.issue.call_args.kwargs["task_id"] == active.run_id
    assert api.issue.call_args.kwargs["session_duration_seconds"] == 900
    audit = (await session.scalars(select(AuditLog))).one()
    assert audit.details["box_id"] == "box-a" and audit.details["authorized_user_id"] == "owner-a"
    assert "synthetic-session" not in str(audit.details)


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"agent_consent": False}, 403),
        ({"aws_verified_at": None}, 409),
        ({"expires_at": datetime.now(UTC) - timedelta(seconds=1)}, 409),
        ({"user_id": None, "team_id": "team"}, 404),
        ({"user_id": None}, 404),
    ],
)
async def test_denied_or_wider_connections_never_issue(api, session, changes, status):  # noqa: F811
    await connect(session, **changes)
    assert (await post(api)).status_code == status
    api.issue.assert_not_called()


async def test_multiple_connections_require_selection_and_never_fall_back(api, session):  # noqa: F811
    await connect(session)
    await connect(session, "production")
    assert (await post(api)).status_code == 409
    assert (await post(api, label="missing")).status_code == 404
    assert (await post(api, connection_id="foreign")).status_code == 404
    api.issue.assert_not_called()
    assert (await post(api, label="development")).status_code == 200
    assert (await post(api, connection_id="connection-production")).status_code == 200


async def test_list_is_metadata_only_and_refresh_rechecks_consent(api, session):  # noqa: F811
    row = await connect(session)
    listing = await post(api, operation="list")
    assert listing.json() == {"connections": [{"id": row.id, "label": row.label, "account_id": "123456789012"}]}
    assert "synthetic" not in listing.text
    assert (await post(api)).status_code == 200
    row.agent_consent = False
    await session.commit()
    assert (await post(api)).status_code == 403
    assert (await post(api, operation="list")).json() == {"connections": []}
    assert api.issue.call_count == 1


async def test_host_cannot_choose_another_user_or_use_a_stale_identity(api, session, active):  # noqa: F811
    await connect(session)
    assert (await post(api, user_id="someone-else")).status_code == 422
    api.identity.context = host_context("i-0fedcba9876543210")
    assert (await post(api)).status_code == 403
    api.identity.context = host_context()
    active.resolver.fenced = True
    assert (await post(api)).status_code == 409
    api.issue.assert_not_called()


async def test_fence_during_sts_does_not_deliver_credentials(api, session, active):  # noqa: F811
    await connect(session)

    def fenced(**_):
        active.resolver.fenced = True
        return api.issue.return_value

    api.issue.side_effect = fenced
    response = await post(api)
    assert response.status_code == 409
    assert "synthetic" not in response.text


async def test_revoked_membership_blocks_refresh(api, session):  # noqa: F811
    await connect(session)
    member = (await session.scalars(select(TenantMembership))).one()
    member.revoked_at = datetime.now(UTC)
    await session.commit()
    assert (await post(api)).status_code == 403
    api.issue.assert_not_called()


async def test_missing_model_token_cannot_use_instance_identity_alone(api, session):  # noqa: F811
    await connect(session)
    api.client.headers.pop("X-Adp-Crew-Model-Token")
    response = await post(api)
    assert response.status_code == 401
    api.issue.assert_not_called()


async def test_rotated_secret_requires_reverification_before_sts(api, session):  # noqa: F811
    await connect(session)
    api.sm.current_version_id.return_value = "rotated"
    response = await post(api)
    assert response.status_code == 409
    api.issue.assert_not_called()
