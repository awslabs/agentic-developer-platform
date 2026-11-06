"""Migrated SSO ownership and retry across real PostgreSQL request sessions."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from app.config import settings
from app.models.organization import Organization
from app.routers import orgs
from botocore.exceptions import ClientError
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests import test_installation_postgres as postgres_fixtures
from tests import test_sso_tenant_mapping as sso_fixtures

installation_postgres_url = postgres_fixtures.installation_postgres_url
isolated_database = postgres_fixtures.isolated_database
approved_origins = sso_fixtures.approved_origins
cognito = sso_fixtures.cognito
pytestmark = postgres_fixtures.pytestmark


def migrate(url, schema, revision):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        check=False,
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, DATABASE_URL=url, SUPERPLANE_DB_SCHEMA=schema),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


async def test_migrated_legacy_binding_and_cross_request_provider_retry(
    isolated_database, approved_origins, cognito, monkeypatch
):
    _, engine, url, _, schema, _ = isolated_database
    legacy_id, fresh_id, _ = approved_origins
    legacy_handle = "reconciled-federation"
    migrate(url, schema, "054_unstarted_bootstrap")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO organizations (id, name, billing_plan, sso_provider, "
                "sso_provider_type, sso_enabled) VALUES "
                "(:legacy, 'legacy', 'free', 'okta', 'SAML', true), "
                "(:fresh, 'fresh', 'free', NULL, NULL, false)"
            ),
            {"legacy": legacy_id, "fresh": fresh_id},
        )
    migrate(url, schema, "head")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        legacy = await session.get(Organization, legacy_id)
        assert legacy.sso_cognito_provider_name is None
        assert legacy.sso_enabled is True
        fresh = await session.get(Organization, fresh_id)
        assert fresh.sso_cognito_provider_name is None

    monkeypatch.setattr(settings, "cognito_user_pool_id", "offline-pool")
    monkeypatch.setattr(settings, "cognito_app_client_id", "offline-client")
    _, client = cognito
    providers = {
        legacy_handle: {
            "ProviderName": legacy_handle,
            "ProviderType": "SAML",
            "IdpIdentifiers": [str(legacy_id)],
        }
    }

    def describe_provider(*, ProviderName, **kwargs):
        if ProviderName not in providers:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}},
                "DescribeIdentityProvider",
            )
        return {"IdentityProvider": providers[ProviderName]}

    def create_provider(*, ProviderName, ProviderType, IdpIdentifiers, **kwargs):
        providers[ProviderName] = {
            "ProviderName": ProviderName,
            "ProviderType": ProviderType,
            "IdpIdentifiers": IdpIdentifiers,
        }

    client.describe_identity_provider.side_effect = describe_provider
    client.create_identity_provider.side_effect = create_provider

    async def get_session():
        async with sessions() as session:
            yield session

    current_org = legacy_id
    app = FastAPI()
    app.include_router(orgs.router)
    app.dependency_overrides[orgs.get_current_org] = lambda: current_org
    app.dependency_overrides[orgs.get_session] = get_session
    body = sso_fixtures.request_body()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        assert (await http.get("/orgs/current/sso")).status_code == 409
        assert (await http.patch("/orgs/current/sso", json=body)).status_code == 409
        client.create_identity_provider.assert_not_called()

        async with sessions.begin() as session:
            legacy = await session.get(Organization, legacy_id)
            legacy.sso_cognito_provider_name = legacy_handle
        response = await http.get("/orgs/current/sso")
        assert response.status_code == 200, response.text
        assert response.json()["cognito_idp_identifier"] == legacy_handle

        current_org = fresh_id
        body = sso_fixtures.request_body(url="https://other.example.test/metadata")
        client.update_user_pool_client.side_effect = ClientError(
            {"Error": {"Code": "ServiceUnavailableException"}},
            "UpdateUserPoolClient",
        )
        failed = await http.patch("/orgs/current/sso", json=body)
        assert failed.status_code == 502
        async with sessions() as session:
            pending = await session.get(Organization, fresh_id)
            assert pending.sso_cognito_provider_name == f"org-{fresh_id}"
            assert pending.sso_enabled is False

        client.update_user_pool_client.side_effect = client.reconcile_update
        retried = await http.patch("/orgs/current/sso", json=body)
        assert retried.status_code == 200, retried.text
        assert retried.json()["cognito_idp_identifier"] == f"org-{fresh_id}"
        client.create_identity_provider.assert_called_once()
        client.update_identity_provider.assert_called_once()

    async with sessions() as session:
        owners = (
            await session.execute(
                select(Organization).where(Organization.id.in_([legacy_id, fresh_id]))
            )
        ).scalars().all()
        assert {owner.id: owner.sso_cognito_provider_name for owner in owners} == {
            legacy_id: legacy_handle,
            fresh_id: f"org-{fresh_id}",
        }
        assert next(owner for owner in owners if owner.id == fresh_id).sso_enabled

    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            duplicate = await session.get(Organization, fresh_id)
            duplicate.sso_cognito_provider_name = legacy_handle
