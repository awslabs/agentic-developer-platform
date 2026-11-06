"""PostgreSQL shared-client serialization and guarded partial-SDK recovery."""

import asyncio
import copy
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from app.config import settings
from app.models.organization import Organization
from app.routers import orgs
from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests import test_installation_postgres as postgres_fixtures
from tests import test_sso_tenant_mapping as sso_fixtures
from tests.test_sso_ownership_postgres import migrate

installation_postgres_url = postgres_fixtures.installation_postgres_url
isolated_database = postgres_fixtures.isolated_database
approved_origins = sso_fixtures.approved_origins
cognito = sso_fixtures.cognito
pytestmark = postgres_fixtures.pytestmark


def assert_guarded_downgrade(url, schema):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "054_unstarted_bootstrap"],
        check=False,
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, DATABASE_URL=url, SUPERPLANE_DB_SCHEMA=schema),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "preserve SSO provider bindings" in result.stderr


@pytest.fixture
async def postgres_sso_api(isolated_database, approved_origins, cognito, monkeypatch):
    _, engine, url, _, schema, _ = isolated_database
    first_id, second_id, _ = approved_origins
    migrate(url, schema, "head")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO organizations (id, name, billing_plan) VALUES "
                "(:first, 'first', 'free'), (:second, 'second', 'free')"
            ),
            {"first": first_id, "second": second_id},
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "cognito_user_pool_id", "offline-pool")
    monkeypatch.setattr(settings, "cognito_app_client_id", "offline-client")
    _, client = cognito
    providers = {}

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

    def get_org(request: Request) -> UUID:
        return UUID(request.headers["x-test-org"])

    app = FastAPI()
    app.include_router(orgs.router)
    app.dependency_overrides[orgs.get_current_org] = get_org
    app.dependency_overrides[orgs.get_session] = get_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        yield http, sessions, client, first_id, second_id, url, schema


async def test_two_orgs_wait_on_real_shared_client_lock_and_retain_settings(
    postgres_sso_api, monkeypatch
):
    http, sessions, client, first_id, second_id, _, _ = postgres_sso_api
    initial = copy.deepcopy(client.client_state)
    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    second_attempted = asyncio.Event()
    original_lock = orgs._lock_shared_sso_client
    lock_calls = 0

    async def controlled_lock(db):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 1:
            await original_lock(db)
            first_locked.set()
            await asyncio.wait_for(release_first.wait(), 10)
        else:
            if lock_calls == 2:
                second_attempted.set()
            await original_lock(db)

    monkeypatch.setattr(orgs, "_lock_shared_sso_client", controlled_lock)

    def patch(org_id, origin):
        return http.patch(
            "/orgs/current/sso",
            json=sso_fixtures.request_body(url=f"{origin}/metadata"),
            headers={"x-test-org": str(org_id)},
        )

    first = asyncio.create_task(patch(first_id, "https://idp.example.test"))
    second = None
    try:
        await asyncio.wait_for(first_locked.wait(), 10)
        second = asyncio.create_task(patch(second_id, "https://other.example.test"))
        await asyncio.wait_for(second_attempted.wait(), 10)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.2)
    finally:
        release_first.set()
    assert second is not None
    responses = await asyncio.wait_for(asyncio.gather(first, second), 30)
    assert [response.status_code for response in responses] == [200, 200], [
        response.text for response in responses
    ]
    assert lock_calls == 4
    names = {f"org-{first_id}", f"org-{second_id}"}
    assert set(client.client_state["SupportedIdentityProviders"]) == {"COGNITO", *names}
    assert len(client.client_state["SupportedIdentityProviders"]) == 3
    assert client.create_identity_provider.call_count == 2
    assert client.update_user_pool_client.call_count == 2
    for key, value in initial.items():
        if key != "SupportedIdentityProviders":
            assert client.client_state[key] == value
    async with sessions() as session:
        first_org = await session.get(Organization, first_id)
        second_org = await session.get(Organization, second_id)
        assert first_org.sso_enabled and second_org.sso_enabled
        assert {
            first_org.sso_cognito_provider_name,
            second_org.sso_cognito_provider_name,
        } == names


@pytest.mark.parametrize("failure", ["applied_then_raised", "unverified_write"])
async def test_partial_sdk_write_keeps_binding_guarded_and_retries_safely(
    postgres_sso_api, failure
):
    http, sessions, client, first_id, _, url, schema = postgres_sso_api
    initial = copy.deepcopy(client.client_state)
    provider_name = f"org-{first_id}"

    if failure == "applied_then_raised":

        def partial_update(**kwargs):
            client.reconcile_update(**kwargs)
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailableException"}},
                "UpdateUserPoolClient",
            )

        client.update_user_pool_client.side_effect = partial_update
    else:
        client.update_user_pool_client.side_effect = lambda **kwargs: None

    body = sso_fixtures.request_body()
    headers = {"x-test-org": str(first_id)}
    failed = await http.patch("/orgs/current/sso", json=body, headers=headers)
    assert failed.status_code == 502, failed.text
    async with sessions() as session:
        pending = await session.get(Organization, first_id)
        assert pending.sso_cognito_provider_name == provider_name
        assert pending.sso_enabled is False
    assert client.create_identity_provider.call_count == 1
    assert (provider_name in client.client_state["SupportedIdentityProviders"]) == (
        failure == "applied_then_raised"
    )

    assert_guarded_downgrade(url, schema)
    async with sessions() as session:
        revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
        assert revision == "058_interrupted_planning"
        assert (
            await session.get(Organization, first_id)
        ).sso_cognito_provider_name == provider_name
    migrate(url, schema, "head")

    client.update_user_pool_client.side_effect = client.reconcile_update
    retried = await http.patch("/orgs/current/sso", json=body, headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["cognito_idp_identifier"] == provider_name
    assert client.create_identity_provider.call_count == 1
    assert client.update_identity_provider.call_count == 1
    assert client.client_state["SupportedIdentityProviders"] == [
        "COGNITO",
        provider_name,
    ]
    for key, value in initial.items():
        if key != "SupportedIdentityProviders":
            assert client.client_state[key] == value
    async with sessions() as session:
        recovered = await session.get(Organization, first_id)
        assert recovered.sso_enabled is True
        assert recovered.sso_cognito_provider_name == provider_name
