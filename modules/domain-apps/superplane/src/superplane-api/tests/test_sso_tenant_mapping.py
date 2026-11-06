"""SSO claim mapping and operator-approved issuer boundaries (#7841, S-06)."""

import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.config import settings
from app.routers import orgs
from app.schemas.org import SSOConfigRequest
from app.services import sso
from botocore.exceptions import ClientError
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError


@pytest.fixture
def approved_origins(tmp_path, monkeypatch):
    org_id, other_org_id = uuid.uuid4(), uuid.uuid4()
    path = tmp_path / "sso-origins.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenants": {
                    str(org_id): ["https://idp.example.test"],
                    str(other_org_id): ["https://other.example.test"],
                },
            }
        )
    )
    monkeypatch.setattr(settings, "superplane_sso_approved_origins_file", str(path))
    return org_id, other_org_id, path


@pytest.fixture
def cognito(monkeypatch):
    sdk = MagicMock()
    client = MagicMock()
    sdk.return_value = client
    client_state = {
        "SupportedIdentityProviders": ["COGNITO"],
        "WriteAttributes": ["email", "name"],
        "ReadAttributes": ["email", "custom:org_id"],
        "ExplicitAuthFlows": ["ALLOW_ADMIN_USER_PASSWORD_AUTH"],
        "EnableTokenRevocation": True,
        "RefreshTokenRotation": {"Feature": "ENABLED", "RetryGracePeriodSeconds": 60},
        "TokenValidityUnits": {"RefreshToken": "minutes"},
        "RefreshTokenValidity": 1440,
        "AccessTokenValidity": 15,
        "IdTokenValidity": 15,
        "AllowedOAuthFlows": ["code"],
        "AllowedOAuthScopes": ["openid", "email"],
        "AllowedOAuthFlowsUserPoolClient": True,
        "CallbackURLs": ["https://app.example.test/callback"],
        "LogoutURLs": ["https://app.example.test/logout"],
        "PreventUserExistenceErrors": "ENABLED",
    }
    client.describe_user_pool_client.side_effect = lambda **kwargs: {
        "UserPoolClient": copy.deepcopy(client_state)
    }

    def update_client(**kwargs):
        client_state.update(
            {
                key: value
                for key, value in kwargs.items()
                if key not in ("ClientId", "UserPoolId")
            }
        )

    client.client_state = client_state
    client.reconcile_update = update_client
    client.update_user_pool_client.side_effect = update_client
    monkeypatch.setattr(sso.boto3, "client", sdk)
    return sdk, client


@pytest.fixture
def sso_api(approved_origins, monkeypatch):
    org_id, _, _ = approved_origins
    org = SimpleNamespace(
        id=org_id,
        sso_provider=None,
        sso_provider_type=None,
        sso_cognito_provider_name=None,
        sso_enabled=False,
    )
    db = AsyncMock()
    db.bind.dialect.name = "postgresql"
    monkeypatch.setattr(settings, "cognito_user_pool_id", "offline-pool")
    monkeypatch.setattr(settings, "cognito_app_client_id", "offline-client")
    monkeypatch.setattr(orgs, "_get_org", AsyncMock(return_value=org))
    app = FastAPI()
    app.include_router(orgs.router)
    app.dependency_overrides[orgs.get_current_org] = lambda: org_id
    app.dependency_overrides[orgs.get_session] = lambda: db
    return app, org_id, org, db


def request_body(provider_type="SAML", url="https://idp.example.test/metadata"):
    return {
        "sso_provider": "okta",
        "sso_provider_type": provider_type,
        "metadata_url": url,
    }


@pytest.mark.parametrize(
    "provider_type,expected",
    [
        (
            "SAML",
            {
                "email": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
            },
        ),
        ("OIDC", {"email": "email"}),
    ],
)
@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize(
    "url",
    ["https://idp.example.test/metadata", "https://idp.example.test:443/metadata"],
)
@pytest.mark.asyncio
async def test_approved_sso_config_updates_or_creates_email_only(
    sso_api, cognito, provider_type, expected, existing, url
):
    app, org_id, org, db = sso_api
    sdk, client = cognito
    provider_name = f"org-{org_id}"
    provider = {
        "IdentityProvider": {
            "ProviderName": provider_name,
            "ProviderType": provider_type,
            "IdpIdentifiers": [str(org_id)],
        }
    }
    client.describe_identity_provider.return_value = provider
    if existing:
        org.sso_provider = "okta"
        org.sso_provider_type = provider_type
        org.sso_cognito_provider_name = provider_name
    else:
        client.describe_identity_provider.side_effect = [
            ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}},
                "DescribeIdentityProvider",
            ),
            ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}},
                "DescribeIdentityProvider",
            ),
            provider,
        ]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        response = await http.patch(
            "/orgs/current/sso", json=request_body(provider_type, url=url)
        )
    assert response.status_code == 200, response.text
    assert sdk.call_count == 2
    sdk_call = (
        client.update_identity_provider.call_args
        if existing
        else client.create_identity_provider.call_args
    )
    assert sdk_call.kwargs["AttributeMapping"] == expected
    assert (
        sdk_call.kwargs["ProviderDetails"][
            "MetadataURL" if provider_type == "SAML" else "iss"
        ]
        == url
    )
    if not existing:
        assert sdk_call.kwargs["ProviderType"] == provider_type
    assert org.sso_metadata_url == url
    assert org.sso_provider_type == provider_type
    assert db.commit.await_count == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://idp.example.test/metadata",
        "https://user:pass@idp.example.test/metadata",
        "https://idp.example.test:8443/metadata",
        "https://127.0.0.1/metadata",
        "https://[::1]/metadata",
        "https://idp.example.test./metadata",
        "https://IDP.example.test/metadata",
        "https://idp.example.test/metadata#fragment",
        "https://idp.example.test/metadata?query=1",
        "https://idp.example.test/metadata?",
        "https://evil.example.test/metadata",
        "https://other.example.test/metadata",
    ],
)
@pytest.mark.asyncio
async def test_unsafe_metadata_rejected_before_sdk(sso_api, cognito, url):
    app, _, _, db = sso_api
    sdk, _ = cognito
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        response = await http.patch("/orgs/current/sso", json=request_body(url=url))
    assert response.status_code == 422, response.text
    sdk.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"sso_provider_type": "OTHER"},
        {"sso_provider": "other"},
        {"approved_origins": ["https://evil.example.test"]},
        {"org_id": "requester-supplied-org"},
    ],
)
@pytest.mark.asyncio
async def test_request_cannot_change_provider_type_or_approved_origins(
    sso_api, cognito, change
):
    app, _, _, db = sso_api
    sdk, _ = cognito
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        response = await http.patch(
            "/orgs/current/sso", json={**request_body(), **change}
        )
    assert response.status_code == 422, response.text
    sdk.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "policy", [None, "{}", '{"version":1,"tenants":{}}', "not-json"]
)
@pytest.mark.asyncio
async def test_missing_or_unusable_policy_fails_closed(
    sso_api, cognito, approved_origins, monkeypatch, policy
):
    app, _, _, db = sso_api
    sdk, _ = cognito
    _, _, path = approved_origins
    if policy is None:
        monkeypatch.setattr(settings, "superplane_sso_approved_origins_file", "")
    else:
        path.write_text(policy)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        response = await http.patch("/orgs/current/sso", json=request_body())
    assert response.status_code == 503, response.text
    sdk.assert_not_called()
    db.commit.assert_not_awaited()


def test_schema_rejects_unsafe_url_and_external_allowlist_without_endpoint():
    for invalid in (
        request_body(url="ftp://idp.example.test/metadata"),
        {**request_body(), "approved_origins": ["https://idp.example.test"]},
    ):
        with pytest.raises(ValidationError):
            SSOConfigRequest.model_validate(invalid)


@pytest.mark.parametrize("provider_type", ["SAML", "OIDC"])
def test_direct_service_does_not_bypass_origin_policy(
    approved_origins, cognito, provider_type
):
    org_id, _, _ = approved_origins
    _, client = cognito
    service = sso.SSOService()
    with pytest.raises(sso.SSOOriginNotApproved):
        if provider_type == "SAML":
            service.create_or_update_saml_provider(
                org_id, "https://other.example.test/metadata"
            )
        else:
            service.create_or_update_oidc_provider(
                org_id, "https://other.example.test/metadata"
            )
    client.update_identity_provider.assert_not_called()
    client.create_identity_provider.assert_not_called()


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
@pytest.mark.asyncio
async def test_unreconciled_legacy_provider_refuses_every_method(
    sso_api, cognito, method
):
    app, org_id, org, db = sso_api
    org.sso_provider = "okta"
    org.sso_provider_type = "SAML"
    org.sso_provider_name = "Corporate sign-in"
    org.sso_enabled = True
    sdk, _ = cognito
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        if method == "get":
            response = await http.get("/orgs/current/sso")
        elif method == "patch":
            response = await http.patch("/orgs/current/sso", json=request_body())
        else:
            response = await http.request(
                "DELETE", "/orgs/current/sso", json={"confirm": True}
            )
    assert response.status_code == 409
    assert response.json() == {"detail": "SSO provider configuration conflict"}
    sdk.assert_not_called()
    db.commit.assert_not_awaited()
    assert org.sso_provider_name == "Corporate sign-in"
    assert org.sso_enabled is True
    orgs._get_org.assert_awaited_once_with(org_id, db, for_update=True)


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
@pytest.mark.parametrize("bad_proof", ["foreign_owner", "wrong_type"])
@pytest.mark.asyncio
async def test_foreign_or_wrong_type_provider_never_reaches_mutation(
    sso_api, cognito, approved_origins, method, bad_proof
):
    app, org_id, org, db = sso_api
    _, foreign_id, _ = approved_origins
    provider_name = f"org-{foreign_id}"
    org.sso_provider = "okta"
    org.sso_provider_type = "SAML"
    org.sso_provider_name = "Corporate sign-in"
    org.sso_cognito_provider_name = provider_name
    org.sso_enabled = True
    sdk, client = cognito
    client.describe_identity_provider.return_value = {
        "IdentityProvider": {
            "ProviderName": provider_name,
            "ProviderType": "OIDC" if bad_proof == "wrong_type" else "SAML",
            "IdpIdentifiers": [str(foreign_id)]
            if bad_proof == "foreign_owner"
            else [str(org_id)],
            "ProviderDetails": {"secret": "must-not-be-disclosed"},
        }
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        if method == "get":
            response = await http.get("/orgs/current/sso")
        elif method == "patch":
            response = await http.patch("/orgs/current/sso", json=request_body())
        else:
            response = await http.request(
                "DELETE", "/orgs/current/sso", json={"confirm": True}
            )
    assert response.status_code == 409
    assert response.json() == {"detail": "SSO provider configuration conflict"}
    assert "must-not-be-disclosed" not in response.text
    sdk.assert_called_once()
    client.update_identity_provider.assert_not_called()
    client.create_identity_provider.assert_not_called()
    client.delete_identity_provider.assert_not_called()
    client.update_user_pool_client.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciled_legacy_provider_keeps_exact_handle_and_display_name(
    sso_api, cognito
):
    app, org_id, org, db = sso_api
    org.sso_provider = "okta"
    org.sso_provider_type = "SAML"
    org.sso_provider_name = "Corporate sign-in"
    org.sso_metadata_url = "https://idp.example.test/metadata"
    org.sso_cognito_provider_name = f"org-{str(org_id)[:8]}"
    org.sso_enabled = True
    _, client = cognito
    client.describe_identity_provider.return_value = {
        "IdentityProvider": {
            "ProviderName": org.sso_cognito_provider_name,
            "ProviderType": "SAML",
            "IdpIdentifiers": [],
        }
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        read = await http.get("/orgs/current/sso")
        configured = await http.patch("/orgs/current/sso", json=request_body())
        disabled = await http.request(
            "DELETE", "/orgs/current/sso", json={"confirm": True}
        )
    assert read.status_code == configured.status_code == disabled.status_code == 200
    assert read.json()["cognito_idp_identifier"] == f"org-{str(org_id)[:8]}"
    assert read.json()["sso_provider_name"] == "Corporate sign-in"
    assert configured.json()["cognito_idp_identifier"] == f"org-{str(org_id)[:8]}"
    assert (
        client.update_identity_provider.call_args.kwargs["ProviderName"]
        == f"org-{str(org_id)[:8]}"
    )
    assert (
        client.delete_identity_provider.call_args.kwargs["ProviderName"]
        == f"org-{str(org_id)[:8]}"
    )
    assert org.sso_cognito_provider_name is None
    assert db.commit.await_count == 3


@pytest.mark.asyncio
async def test_unbound_canonical_name_is_never_silently_adopted(sso_api, cognito):
    app, org_id, _, db = sso_api
    _, client = cognito
    client.describe_identity_provider.return_value = {
        "IdentityProvider": {
            "ProviderName": f"org-{org_id}",
            "ProviderType": "SAML",
            "IdpIdentifiers": [str(org_id)],
        }
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        response = await http.patch("/orgs/current/sso", json=request_body())
    assert response.status_code == 409
    client.create_identity_provider.assert_not_called()
    client.update_identity_provider.assert_not_called()
    db.commit.assert_not_awaited()


def test_full_uuid_provider_names_do_not_collide(cognito):
    first = uuid.UUID("12345678-0000-4000-8000-000000000001")
    second = uuid.UUID("12345678-0000-4000-8000-000000000002")
    assert sso.SSOService()._provider_name(first) == f"org-{first}"
    assert sso.SSOService()._provider_name(second) == f"org-{second}"
    assert sso.SSOService()._provider_name(first) != sso.SSOService()._provider_name(
        second
    )


@pytest.mark.asyncio
async def test_sso_org_lookup_uses_row_lock_for_read_and_mutation():
    from sqlalchemy.dialects import postgresql

    org_id = uuid.uuid4()
    db = AsyncMock()
    db.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(id=org_id)
    await orgs._get_org(org_id, db, for_update=True)
    query = db.execute.call_args.args[0]
    sql = str(query.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql
    assert "organizations.id" in sql


@pytest.mark.parametrize("method", ["get", "delete"])
@pytest.mark.asyncio
async def test_provider_client_unavailable_does_not_disclose_or_clear_binding(
    sso_api, cognito, method
):
    app, org_id, org, db = sso_api
    org.sso_provider = "okta"
    org.sso_provider_type = "SAML"
    org.sso_cognito_provider_name = f"org-{org_id}"
    org.sso_metadata_url = "https://idp.example.test/metadata"
    org.sso_enabled = True
    sdk, _ = cognito
    sdk.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailableException"}}, "CreateClient"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        if method == "get":
            response = await http.get("/orgs/current/sso")
        else:
            response = await http.request(
                "DELETE", "/orgs/current/sso", json={"confirm": True}
            )
    assert response.status_code == 502
    assert response.json() == {"detail": "SSO provider unavailable"}
    assert "idp.example.test" not in response.text
    assert org.sso_cognito_provider_name == f"org-{org_id}"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_inconsistent_enabled_state_does_not_create_provider(sso_api, cognito):
    app, _, org, db = sso_api
    org.sso_enabled = True
    sdk, _ = cognito
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        response = await http.patch("/orgs/current/sso", json=request_body())
    assert response.status_code == 409
    sdk.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_client_preserves_security_settings_and_other_providers(
    sso_api, cognito
):
    app, org_id, org, db = sso_api
    _, client = cognito
    provider_name = f"org-{org_id}"
    client.client_state.update(
        {
            "SupportedIdentityProviders": ["COGNITO", "org-another-tenant"],
            "WriteAttributes": ["email", "name", "custom:org_id", "custom:role"],
            "ClientSecret": "must-not-be-forwarded",
            "CreationDate": "must-not-be-forwarded",
        }
    )
    provider = {
        "IdentityProvider": {
            "ProviderName": provider_name,
            "ProviderType": "SAML",
            "IdpIdentifiers": [str(org_id)],
        }
    }
    missing = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeIdentityProvider"
    )
    client.describe_identity_provider.side_effect = [missing, missing, provider]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        response = await http.patch("/orgs/current/sso", json=request_body())
    assert response.status_code == 200, response.text
    sdk_call = client.update_user_pool_client.call_args.kwargs
    assert sdk_call["SupportedIdentityProviders"] == [
        "COGNITO",
        "org-another-tenant",
        provider_name,
    ]
    assert sdk_call["WriteAttributes"] == ["email", "name"]
    assert sdk_call["ReadAttributes"] == ["email", "custom:org_id"]
    for key in (
        "ExplicitAuthFlows",
        "EnableTokenRevocation",
        "RefreshTokenRotation",
        "TokenValidityUnits",
        "AccessTokenValidity",
        "RefreshTokenValidity",
        "IdTokenValidity",
        "AllowedOAuthFlows",
        "AllowedOAuthScopes",
        "AllowedOAuthFlowsUserPoolClient",
        "CallbackURLs",
        "LogoutURLs",
        "PreventUserExistenceErrors",
    ):
        assert sdk_call[key] == client.client_state[key]
    assert "ClientSecret" not in sdk_call
    assert "CreationDate" not in sdk_call
    assert org.sso_enabled is True
    assert db.commit.await_count == 2
    assert (
        len(
            [
                call
                for call in db.execute.call_args_list
                if "pg_advisory_xact_lock" in str(call.args[0])
            ]
        )
        == 2
    )


@pytest.mark.asyncio
async def test_partial_client_failure_retries_same_owned_provider_without_second_create(
    sso_api, cognito
):
    app, org_id, org, db = sso_api
    _, client = cognito
    provider_name = f"org-{org_id}"
    provider = {
        "IdentityProvider": {
            "ProviderName": provider_name,
            "ProviderType": "SAML",
            "IdpIdentifiers": [str(org_id)],
        }
    }
    created = False

    def describe_provider(**kwargs):
        if not created:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException"}},
                "DescribeIdentityProvider",
            )
        return provider

    def create_provider(**kwargs):
        nonlocal created
        created = True

    client.describe_identity_provider.side_effect = describe_provider
    client.create_identity_provider.side_effect = create_provider
    client.update_user_pool_client.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailableException"}}, "UpdateUserPoolClient"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        failed = await http.patch("/orgs/current/sso", json=request_body())
        assert failed.status_code == 502
        assert org.sso_cognito_provider_name == provider_name
        assert org.sso_enabled is False
        assert db.commit.await_count == 1
        client.update_user_pool_client.side_effect = client.reconcile_update
        retried = await http.patch("/orgs/current/sso", json=request_body())
    assert retried.status_code == 200, retried.text
    client.create_identity_provider.assert_called_once()
    client.update_identity_provider.assert_called_once()
    assert org.sso_enabled is True
    assert db.commit.await_count == 3


@pytest.mark.asyncio
async def test_disable_unpublishes_before_provider_delete_and_retries_failed_delete(
    sso_api, cognito
):
    app, org_id, org, db = sso_api
    _, client = cognito
    provider_name = f"org-{org_id}"
    org.sso_provider = "okta"
    org.sso_provider_type = "SAML"
    org.sso_cognito_provider_name = provider_name
    org.sso_enabled = True
    client.client_state["SupportedIdentityProviders"].append(provider_name)
    client.describe_identity_provider.return_value = {
        "IdentityProvider": {
            "ProviderName": provider_name,
            "ProviderType": "SAML",
            "IdpIdentifiers": [str(org_id)],
        }
    }
    client.delete_identity_provider.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailableException"}}, "DeleteIdentityProvider"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        failed = await http.request(
            "DELETE", "/orgs/current/sso", json={"confirm": True}
        )
        assert failed.status_code == 502
        assert org.sso_enabled is True
        assert provider_name not in client.client_state["SupportedIdentityProviders"]
        db.commit.assert_not_awaited()
        client.delete_identity_provider.side_effect = None
        retried = await http.request(
            "DELETE", "/orgs/current/sso", json={"confirm": True}
        )
    assert retried.status_code == 200, retried.text
    assert org.sso_enabled is False
    assert org.sso_cognito_provider_name is None
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_advisory_lock_scope_is_shared_across_organizations(sso_api):
    _, org_id, _, db = sso_api
    await orgs._lock_shared_sso_client(db)
    await orgs._lock_shared_sso_client(db)
    scopes = [call.args[1]["scope"] for call in db.execute.call_args_list]
    assert scopes == ["sso_client/offline-pool/offline-client"] * 2
    assert all(
        "pg_advisory_xact_lock" in str(call.args[0])
        for call in db.execute.call_args_list
    )
    assert str(org_id) not in scopes[0]


@pytest.mark.asyncio
async def test_changed_binding_between_transactions_refuses_before_sdk_write(
    sso_api, cognito
):
    app, org_id, org, db = sso_api
    _, client = cognito
    foreign = SimpleNamespace(
        sso_provider="okta",
        sso_provider_type="SAML",
        sso_cognito_provider_name="org-foreign",
        sso_provider_name="okta",
        sso_metadata_url="https://idp.example.test/metadata",
        sso_enabled=False,
    )
    orgs._get_org.side_effect = [org, foreign]
    missing = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeIdentityProvider"
    )
    client.describe_identity_provider.side_effect = missing
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        response = await http.patch("/orgs/current/sso", json=request_body())
    assert response.status_code == 409
    assert response.json() == {"detail": "SSO provider configuration conflict"}
    client.create_identity_provider.assert_not_called()
    client.update_identity_provider.assert_not_called()
    client.update_user_pool_client.assert_not_called()
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("failure", ["unconfirmed_update", "missing_write_policy"])
@pytest.mark.asyncio
async def test_client_snapshot_must_verify_before_enabling(sso_api, cognito, failure):
    app, org_id, org, db = sso_api
    _, client = cognito
    name = f"org-{org_id}"
    provider = {
        "IdentityProvider": {
            "ProviderName": name,
            "ProviderType": "SAML",
            "IdpIdentifiers": [str(org_id)],
        }
    }
    missing = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeIdentityProvider"
    )
    client.describe_identity_provider.side_effect = [missing, missing, provider]
    if failure == "missing_write_policy":
        client.client_state.pop("WriteAttributes")
    else:
        client.update_user_pool_client.side_effect = lambda **kwargs: None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://offline.test"
    ) as http:
        response = await http.patch("/orgs/current/sso", json=request_body())
    assert response.status_code == 502
    assert org.sso_enabled is False
    assert org.sso_cognito_provider_name == name
    db.commit.assert_awaited_once()
    if failure == "missing_write_policy":
        client.update_user_pool_client.assert_not_called()
