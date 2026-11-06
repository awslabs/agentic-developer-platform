"""Registration preserves existing apps, credentials and unrelated connections."""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.admin.connections.oauth_apps import require_app_admin
from src.admin.connections.oauth_apps import router as admin_oauth_router
from src.auth import atlassian_mcp_routes
from src.auth.dependencies import get_current_user
from src.auth.middleware import get_current_user_context
from src.shared.database import get_db
from src.shared.identity.oauth_setup import OAuthAppSetup
from src.shared.identity.slack_setup import SLACK_USER_METADATA
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.schemas.auth import TokenContext

WORKLOAD = "adp-tools-dev-oauth-user"
RETURN = "https://dashboard.example.test/api/auth/connections/oauth/return"
CALLBACK = "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/callback/unique"


def missing():
    return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}, "Get")


@pytest.fixture
def setup():
    control, sm = MagicMock(), MagicMock()
    control.get_workload_identity.return_value = {"allowedResourceOauth2ReturnUrls": [RETURN]}
    control.get_oauth2_credential_provider.return_value = {
        "credentialProviderVendor": "AtlassianOauth2",
        "status": "READY",
        "callbackUrl": CALLBACK,
        "clientSecretArn": {"secretArn": "private-arn"},
        "oauth2ProviderConfigOutput": {"clientId": "private-client"},
    }
    sm.get_secret_value.side_effect = missing()
    return OAuthAppSetup(SimpleNamespace(oauth_workload_name=WORKLOAD, oauth_return_url=RETURN, aws_region="us-east-1"), control, sm)


@pytest.fixture
def mcp_setup(setup):
    setup.settings.atlassian_identity_compatibility_verified = True
    setup.settings.atlassian_config_ref = "adp/dev/mcp-config"
    setup.settings.atlassian_contract_revision = "fixture-revision"
    issuer = "https://auth.atlassian.com/example-issuer"
    protected = {"resource": "https://mcp.atlassian.com/v2/mcp", "authorization_servers": [issuer]}
    authorization = {
        "issuer": issuer,
        "authorization_endpoint": "https://auth.atlassian.com/authorize",
        "token_endpoint": "https://auth.atlassian.com/oauth/token",
        "code_challenge_methods_supported": ["S256"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }
    metadata_sha256 = hashlib.sha256(json.dumps([protected, authorization], sort_keys=True).encode()).hexdigest()
    setup.settings.atlassian_metadata_sha256 = metadata_sha256
    plan = {
        "resource": "https://mcp.atlassian.com/v2/mcp",
        "issuer": issuer,
        "authorization_endpoint": "https://auth.atlassian.com/authorize",
        "token_endpoint": "https://auth.atlassian.com/oauth/token",
        "client_id": "deployment-client",
        "app_secret_ref": "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/mcp-app",
        "registration_mode": "retained-deployment-client",
        "return_url": "https://dashboard.example.test/api/auth/connections/atlassian-mcp/return",
        "contract_revision": "fixture-revision",
        "protected_resource_metadata": protected,
        "authorization_server_metadata": authorization,
        "metadata_sha256": metadata_sha256,
    }
    records = {"workload": None, "provider": None}
    legacy_workload = setup.control.get_workload_identity.return_value
    legacy_provider = setup.control.get_oauth2_credential_provider.return_value
    setup.secret_store.get_secret_value.side_effect = None
    setup.secret_store.get_secret_value.return_value = {"SecretString": json.dumps(plan)}
    setup.control.get_workload_identity.side_effect = (
        lambda *, name: records["workload"] if name.endswith("-atlassian-mcp-oauth-user") else legacy_workload
    )
    setup.control.get_oauth2_credential_provider.side_effect = (
        lambda *, name: records["provider"] if name.endswith("-atlassian-mcp-user") else legacy_provider
    )
    setup.control.create_workload_identity.side_effect = lambda **kwargs: records.update(
        workload={"allowedResourceOauth2ReturnUrls": kwargs["allowedResourceOauth2ReturnUrls"]}
    )

    def create_provider(**kwargs):
        config = kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        if "tokenEndpointAuthMethods" in config["oauthDiscovery"]["authorizationServerMetadata"] and "clientAuthenticationMethod" in config:
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "Both authentication method fields cannot be provided together"}},
                "CreateOauth2CredentialProvider",
            )
        records["provider"] = {
            "credentialProviderArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:oauth2credentialprovider/test",
            "credentialProviderVendor": kwargs["credentialProviderVendor"],
            "oauth2ProviderConfigOutput": {
                "customOauth2ProviderConfig": {
                    "oauthDiscovery": config["oauthDiscovery"],
                    "clientId": config["clientId"],
                    "clientAuthenticationMethod": config["clientAuthenticationMethod"],
                }
            },
            "clientSecretSource": "EXTERNAL",
            "clientSecretArn": {"secretArn": plan["app_secret_ref"]},
            "clientSecretJsonKey": "client_secret",
            "callbackUrl": CALLBACK,
            "status": "READY",
        }
        setup.control.list_tags_for_resource.return_value = {"tags": kwargs["tags"]}

    setup.control.create_oauth2_credential_provider.side_effect = create_provider
    return setup, plan, records


@pytest.fixture
def mcp_registration_ledger(mcp_setup, tmp_path):
    setup, plan, records = mcp_setup
    ledger_path = tmp_path / "provider-registrations.json"
    ledger_path.write_text("[]")
    create_provider = setup.control.create_oauth2_credential_provider.side_effect

    def record_registration(**kwargs):
        registrations = json.loads(ledger_path.read_text())
        registrations.append(
            {
                "name": kwargs["name"],
                "vendor": kwargs["credentialProviderVendor"],
                "metadata": kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]["oauthDiscovery"],
            }
        )
        ledger_path.write_text(json.dumps(registrations))
        create_provider(**kwargs)

    setup.control.create_oauth2_credential_provider.side_effect = record_registration
    return setup, plan, records, ledger_path


@pytest.fixture
async def mcp_http_matrix(db_session, mcp_registration_ledger, monkeypatch):
    setup, _, records, ledger_path = mcp_registration_ledger
    for tenant in ("alpha", "beta"):
        org_id = f"org-{tenant}"
        db_session.add(
            Organization(
                id=org_id, name=f"Fixture {tenant}", aws_accounts=[], role_mappings={}, settings={}, github_installation_ids=[], cognito_client_ids=[]
            )
        )
        db_session.add(Department(id=f"dept-{tenant}", org_id=org_id, name=f"Department {tenant}"))
        db_session.add(Team(id=f"team-{tenant}", org_id=org_id, department_id=f"dept-{tenant}", name=f"Team {tenant}"))
        db_session.add(User(id=f"user-{tenant}", org_id=org_id, team_id=f"team-{tenant}", email=f"{tenant}@example.test"))
    await db_session.commit()
    actors = {
        tenant: TokenContext(
            user_id=f"user-{tenant}",
            org_id=f"org-{tenant}",
            team_id=f"team-{tenant}",
            department_id=f"dept-{tenant}",
            account_type="human",
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        for tenant in ("alpha", "beta")
    }
    actors["admin"] = actors["alpha"].model_copy(update={"is_admin": True})
    actor_state = {"current": actors["alpha"]}
    policy_state = {}
    inspect = setup.atlassian_mcp_status
    setup.atlassian_mcp_status = lambda *, policy_result=None: inspect(
        policy_result=policy_result if policy_result is not None else policy_state.get(actor_state["current"].org_id)
    )

    async def controlled_worker(*args, **kwargs):
        setup.prepare_atlassian_mcp(before_effects=lambda: None)
        return "resolved"

    from src.admin.connections import oauth_apps

    monkeypatch.setattr(oauth_apps, "process_setup_once", controlled_worker)
    monkeypatch.setattr(oauth_apps, "get_session_factory", lambda: object())
    setup.secret_store.describe_secret.return_value = {"canary": "fixture-user-token-sentinel"}
    app = FastAPI()
    app.include_router(atlassian_mcp_routes.router)
    app.include_router(admin_oauth_router)

    async def db_override():
        yield db_session

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_current_user] = lambda: actor_state["current"]
    app.dependency_overrides[get_current_user_context] = lambda: actor_state["current"]
    app.dependency_overrides[atlassian_mcp_routes.get_oauth_setup] = lambda: setup
    yield TestClient(app, raise_server_exceptions=False), actors, actor_state, policy_state, records, ledger_path
    records.clear()
    ledger_path.unlink()


def test_atlassian_automatic_setup_retains_single_provider_after_restart(mcp_registration_ledger, record_property):
    setup, plan, records, ledger_path = mcp_registration_ledger
    token_sentinel = "fixture-user-token-must-not-appear"
    setup.secret_store.describe_secret.return_value = {"ARN": plan["app_secret_ref"], "note": token_sentinel}
    original_legacy = setup.control.get_oauth2_credential_provider(name="adp-tools-dev-atlassian-user")
    first = setup.prepare_atlassian_mcp(before_effects=MagicMock())
    restarted = OAuthAppSetup(setup.settings, setup.control, setup.secret_store)
    second = restarted.prepare_atlassian_mcp(before_effects=MagicMock())
    assert first == second
    assert first["return_url"] == "https://dashboard.example.test/api/auth/connections/atlassian-mcp/return"
    assert first["callback_url"] == CALLBACK and first["callback_url"] != first["return_url"]
    registrations = json.loads(ledger_path.read_text())
    assert registrations == [
        {
            "name": "adp-tools-dev-atlassian-mcp-user",
            "vendor": "CustomOauth2",
            "metadata": {
                "authorizationServerMetadata": {
                    "issuer": "https://auth.atlassian.com/example-issuer",
                    "authorizationEndpoint": "https://auth.atlassian.com/authorize",
                    "tokenEndpoint": "https://auth.atlassian.com/oauth/token",
                    "responseTypes": ["code"],
                }
            },
        }
    ]
    assert setup.control.get_oauth2_credential_provider(name="adp-tools-dev-atlassian-user") == original_legacy
    assert token_sentinel not in json.dumps(first)
    assert token_sentinel not in json.dumps(registrations)
    assert setup.secret_store.get_secret_value.call_count == 2
    assert setup.secret_store.create_secret.call_count == 0
    records.clear()
    ledger_path.unlink()
    assert not ledger_path.exists()
    record_property(
        "foundation_observation",
        json.dumps(
            {
                "provider_registrations": len(registrations),
                "secret_creates": setup.secret_store.create_secret.call_count,
                "first": first,
                "second": second,
                "ledger_remaining": ledger_path.exists(),
            }
        ),
    )


@pytest.mark.asyncio
async def test_atlassian_admin_readiness_isolated_role_policy_and_forged_callback(mcp_http_matrix, record_property, monkeypatch):
    client, actors, actor_state, policy_state, _, ledger_path = mcp_http_matrix
    status_path = "/auth/connections/atlassian-mcp/status"
    prepare_path = "/admin/connections/atlassian-mcp/prepare"
    assert client.get(status_path).json()["status"] == "setup_required"
    regular_status = client.post(prepare_path).status_code
    assert regular_status == 403
    actor_state["current"] = actors["admin"]
    assert client.post(prepare_path).json()["status"] == "prepared"
    policy_state["org-alpha"] = "allowed"
    assert client.get(status_path).json()["status"] == "consent_required"
    actor_state["current"] = actors["beta"]
    policy_state["org-beta"] = "blocked"
    blocked = client.get(status_path)
    assert blocked.json() == {
        "status": "policy_blocked",
        "owner": "atlassian_organization_admin",
        "action": "Ask the Atlassian organization administrator to allow this deployment client.",
    }
    blocked_status = client.post(prepare_path).status_code
    assert blocked_status == 403
    # Browser return is navigation only. Inspect the first response: following
    # the redirect would hide it behind this fixture's missing settings page.
    forbidden = MagicMock(side_effect=AssertionError("Browser return accessed a consent/provider dependency"))
    for dependency in (get_db, atlassian_mcp_routes.get_identity_client, atlassian_mcp_routes.get_oauth_setup):
        client.app.dependency_overrides[dependency] = forbidden
    monkeypatch.setattr(atlassian_mcp_routes, "observe_connection", forbidden)
    sentinel = "forged-callback-value-must-not-be-echoed"
    callback_responses = [
        client.get(
            "/auth/connections/atlassian-mcp/return",
            params={"code": sentinel, "state": sentinel, "redirect_uri": "https://example.invalid/" + sentinel},
            follow_redirects=False,
        ),
        client.get(
            "/auth/connections/atlassian-mcp/return",
            params={"error": sentinel, "error_description": sentinel},
            follow_redirects=False,
        ),
        client.post("/auth/connections/atlassian-mcp/return", json={"state": sentinel, "code": sentinel}, follow_redirects=False),
    ]
    callback_statuses = [response.status_code for response in callback_responses]
    callback_locations = [response.headers.get("location") for response in callback_responses]
    callback_payload_reflected = any(sentinel in response.text + str(response.headers) for response in callback_responses)
    assert callback_statuses == [303, 303, 405]
    assert callback_locations == ["/settings/connections?oauth_consent=error", "/settings/connections?oauth_consent=error", None]
    assert callback_payload_reflected is False
    forbidden.assert_not_called()
    assert "fixture-user-token-sentinel" not in blocked.text
    assert "fixture-user-token-sentinel" not in json.dumps(json.loads(ledger_path.read_text()))
    assert len(json.loads(ledger_path.read_text())) == 1
    record_property(
        "foundation_observation",
        json.dumps(
            {
                "regular_status": regular_status,
                "blocked_status": blocked_status,
                "policy": blocked.json(),
                "callback_statuses": callback_statuses,
                "callback_locations": callback_locations,
                "callback_payload_reflected": callback_payload_reflected,
                "callback_effect_calls": forbidden.call_count,
                "provider_registrations": len(json.loads(ledger_path.read_text())),
            }
        ),
    )


def test_atlassian_mcp_prepares_once_and_preserves_legacy(mcp_setup):
    setup, _, _ = mcp_setup
    effects = MagicMock()
    first = setup.prepare_atlassian_mcp(before_effects=effects)
    assert setup.prepare_atlassian_mcp(before_effects=effects) == first
    assert first == {
        "status": "prepared",
        "callback_url": CALLBACK,
        "return_url": "https://dashboard.example.test/api/auth/connections/atlassian-mcp/return",
    }
    assert effects.call_count == 2
    setup.control.create_workload_identity.assert_called_once()
    setup.control.create_oauth2_credential_provider.assert_called_once()
    setup.secret_store.describe_secret.assert_called_with(SecretId=mcp_setup[1]["app_secret_ref"])
    assert setup.control.create_oauth2_credential_provider.call_args.kwargs["name"] == "adp-tools-dev-atlassian-mcp-user"
    setup.secret_store.create_secret.assert_not_called()
    assert "deployment-client" not in json.dumps(first) and "secret" not in json.dumps(first)


@pytest.mark.parametrize("change", ["client_id", "return_url", "issuer", "contract_revision"])
def test_atlassian_mcp_changed_config_never_replaces_registration(mcp_setup, change):
    setup, plan, _ = mcp_setup
    setup.prepare_atlassian_mcp(before_effects=MagicMock())
    plan[change] = {
        "client_id": "replacement",
        "return_url": "https://other.example.test/api/auth/connections/atlassian-mcp/return",
        "issuer": "https://other.example.test",
        "contract_revision": "other",
    }[change]
    setup.secret_store.get_secret_value.return_value = {"SecretString": json.dumps(plan)}
    with pytest.raises(HTTPException) as error:
        setup.prepare_atlassian_mcp(before_effects=MagicMock())
    assert error.value.status_code in {409, 503}
    setup.control.create_oauth2_credential_provider.assert_called_once()
    setup.control.create_workload_identity.assert_called_once()


@pytest.mark.parametrize("mutation", ["root_issuer", "resource_issuer", "metadata", "pinned_digest"])
def test_atlassian_mcp_requires_exact_metadata_binding(mcp_setup, mutation):
    setup, plan, _ = mcp_setup
    if mutation == "root_issuer":
        plan["issuer"] = plan["issuer"].rsplit("/", 1)[0]
    elif mutation == "resource_issuer":
        plan["protected_resource_metadata"]["authorization_servers"] = ["https://auth.atlassian.com/other-issuer"]
    elif mutation == "metadata":
        plan["authorization_server_metadata"]["token_endpoint_auth_methods_supported"] = ["none"]
    else:
        setup.settings.atlassian_metadata_sha256 = "0" * 64
    setup.secret_store.get_secret_value.return_value = {"SecretString": json.dumps(plan)}
    with pytest.raises(HTTPException) as error:
        setup.prepare_atlassian_mcp(before_effects=MagicMock())
    assert error.value.status_code == 503
    setup.control.create_workload_identity.assert_not_called()
    setup.control.create_oauth2_credential_provider.assert_not_called()


def test_unfilled_protected_plan_keeps_atlassian_setup_pending(mcp_setup):
    setup, _, _ = mcp_setup
    setup.secret_store.get_secret_value.side_effect = missing()
    assert setup.atlassian_mcp_status()["status"] == "setup_required"
    setup.control.create_workload_identity.assert_not_called()
    setup.control.create_oauth2_credential_provider.assert_not_called()


def test_atlassian_mcp_unverified_provider_cannot_report_ready(mcp_setup):
    setup, _, records = mcp_setup
    setup.prepare_atlassian_mcp(before_effects=MagicMock())
    records["provider"]["status"] = "CREATE_FAILED"
    with pytest.raises(HTTPException) as error:
        setup.prepare_atlassian_mcp(before_effects=MagicMock())
    assert error.value.status_code == 503
    setup.control.create_oauth2_credential_provider.assert_called_once()


def test_atlassian_admin_prepare_route_preserves_existing_oauth_endpoints():
    paths = {(method, route.path) for route in admin_oauth_router.routes for method in route.methods}
    assert ("POST", "/admin/connections/atlassian-mcp/prepare") in paths
    assert ("POST", "/admin/connections/oauth/{provider}/app") in paths
    assert ("GET", "/admin/connections/oauth/{provider}/app") in paths


@pytest.mark.parametrize(
    "is_admin,account_type,allowed",
    [
        (False, "human", False),
        (True, "service", False),
        (True, "human", True),
    ],
)
def test_atlassian_prepare_requires_human_platform_admin(is_admin, account_type, allowed):
    caller = SimpleNamespace(is_admin=is_admin, account_type=account_type)
    if allowed:
        assert require_app_admin(caller) is caller
    else:
        with pytest.raises(HTTPException) as error:
            require_app_admin(caller)
        assert error.value.status_code == 403


def test_atlassian_policy_refusal_names_the_admin_without_reporting_ready(mcp_setup):
    setup, _, records = mcp_setup
    setup.secret_store.describe_secret.return_value = {"untrusted_note": "do-not-return-sentinel"}
    assert setup.atlassian_mcp_status() == {
        "status": "setup_required",
        "owner": "platform_admin",
        "action": "Ask a platform administrator to complete and verify the deployment setup.",
    }
    setup.control.create_oauth2_credential_provider.assert_not_called()
    setup.prepare_atlassian_mcp(before_effects=MagicMock())
    assert setup.atlassian_mcp_status()["status"] == "consent_required"
    assert setup.atlassian_mcp_status(policy_result="allowed")["status"] == "consent_required"
    refusal = setup.atlassian_mcp_status(policy_result="blocked")
    assert refusal == {
        "status": "policy_blocked",
        "owner": "atlassian_organization_admin",
        "action": "Ask the Atlassian organization administrator to allow this deployment client.",
    }
    assert "do-not-return-sentinel" not in json.dumps(refusal)
    records["provider"]["status"] = "CREATE_FAILED"
    assert setup.atlassian_mcp_status(policy_result="allowed")["status"] != "ready"
    setup.control.create_oauth2_credential_provider.assert_called_once()


def test_atlassian_status_does_not_echo_untrusted_policy_result(mcp_setup):
    setup, _, _ = mcp_setup
    with pytest.raises(HTTPException) as error:
        setup.atlassian_mcp_status(policy_result="unverified-user-input")
    assert error.value.status_code == 503


def test_readiness_returns_metadata_only(setup):
    assert setup.status("atlassian") == {"provider": "atlassian", "service": "jira", "status": "registered"}
    guide = setup.status("atlassian", admin=True)
    assert guide["callback_url"] == CALLBACK
    assert "private" not in json.dumps(guide)
    setup.secret_store.get_secret_value.assert_not_called()


@pytest.mark.parametrize("failure", ["workload", "provider", "vendor", "return", "status"])
def test_incomplete_setup_is_never_registered(setup, failure):
    if failure == "workload":
        setup.control.get_workload_identity.side_effect = missing()
    elif failure == "provider":
        setup.control.get_oauth2_credential_provider.side_effect = missing()
    elif failure == "vendor":
        setup.control.get_oauth2_credential_provider.return_value["credentialProviderVendor"] = "GithubOauth2"
    elif failure == "return":
        setup.control.get_workload_identity.return_value = {"allowedResourceOauth2ReturnUrls": ["https://other.test"]}
    else:
        setup.control.get_oauth2_credential_provider.return_value["status"] = "CREATE_FAILED"
    assert setup.status("atlassian")["status"] != "registered"


def test_unknown_read_error_cannot_be_misread_as_permission_to_create(setup):
    setup.control.get_oauth2_credential_provider.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "Get")
    with pytest.raises(ClientError):
        setup.register("atlassian", "id", "secret", before_effects=MagicMock())
    setup.control.create_oauth2_credential_provider.assert_not_called()
    setup.secret_store.create_secret.assert_not_called()


def test_existing_app_cannot_be_replaced(setup):
    with pytest.raises(HTTPException) as error:
        setup.register("atlassian", "new-id", "new-secret", before_effects=MagicMock())
    assert error.value.status_code == 409
    setup.control.create_oauth2_credential_provider.assert_not_called()
    setup.secret_store.get_secret_value.assert_not_called()


def test_github_replacement_retains_previous_secret_version(setup):
    setup.settings.github_app_id = "12345"
    setup.control.get_oauth2_credential_provider.return_value["credentialProviderVendor"] = "GithubOauth2"
    setup.secret_store.get_secret_value.side_effect = None
    setup.secret_store.get_secret_value.return_value = {"SecretString": json.dumps({"client_id": "legacy", "client_secret": "old"})}
    effects = MagicMock()
    result = setup.register("github", "app-client", "new-secret", replace=True, app_id="12345", before_effects=effects)
    effects.assert_called_once()
    secret_call = setup.secret_store.put_secret_value.call_args.kwargs
    assert secret_call["SecretId"] == "adp/dev/connections/oauth/github"
    assert json.loads(secret_call["SecretString"]) == {"client_id": "app-client", "client_secret": "new-secret", "app_id": "12345"}
    setup.secret_store.create_secret.assert_not_called()
    setup.control.create_oauth2_credential_provider.assert_not_called()
    config = setup.control.update_oauth2_credential_provider.call_args.kwargs["oauth2ProviderConfigInput"]["githubOauth2ProviderConfig"]
    assert config == {
        "clientId": "app-client",
        "clientSecretSource": "EXTERNAL",
        "clientSecretConfig": {"secretId": "adp/dev/connections/oauth/github", "jsonKey": "client_secret"},
    }
    assert "new-secret" not in json.dumps(result)


def test_github_replacement_is_idempotent(setup):
    setup.settings.github_app_id = "12345"
    setup.control.get_oauth2_credential_provider.return_value.update(
        {
            "credentialProviderVendor": "GithubOauth2",
            "oauth2ProviderConfigOutput": {"githubOauth2ProviderConfig": {"clientId": "app-client"}},
        }
    )
    setup.secret_store.get_secret_value.side_effect = None
    setup.secret_store.get_secret_value.return_value = {
        "SecretString": json.dumps({"client_id": "app-client", "client_secret": "new-secret", "app_id": "12345"})
    }
    effects = MagicMock()
    assert setup.register("github", "app-client", "new-secret", replace=True, app_id="12345", before_effects=effects)["status"] == "registered"
    effects.assert_not_called()
    setup.secret_store.put_secret_value.assert_not_called()
    setup.control.update_oauth2_credential_provider.assert_not_called()


def test_github_app_client_can_be_registered_on_a_fresh_deployment(setup):
    setup.settings.github_app_id = "12345"
    app = {**setup.control.get_oauth2_credential_provider.return_value, "credentialProviderVendor": "GithubOauth2"}
    setup.control.get_oauth2_credential_provider.side_effect = [missing(), app]
    setup.register("github", "app-client", "new-secret", replace=True, app_id="12345", before_effects=MagicMock())
    stored = json.loads(setup.secret_store.create_secret.call_args.kwargs["SecretString"])
    assert stored == {"client_id": "app-client", "client_secret": "new-secret", "app_id": "12345"}
    setup.secret_store.put_secret_value.assert_not_called()
    setup.control.create_oauth2_credential_provider.assert_called_once()
    setup.control.update_oauth2_credential_provider.assert_not_called()


@pytest.mark.parametrize(
    "provider,app_id,stored",
    [
        ("atlassian", "12345", None),
        ("github", "67890", None),
        ("github", "12345", "not-json"),
    ],
)
def test_github_replacement_rejects_other_clients_and_invalid_previous_secrets(setup, provider, app_id, stored):
    setup.settings.github_app_id = "12345"
    setup.control.get_oauth2_credential_provider.return_value["credentialProviderVendor"] = (
        "AtlassianOauth2" if provider == "atlassian" else "GithubOauth2"
    )
    if stored is not None:
        setup.secret_store.get_secret_value.side_effect = None
        setup.secret_store.get_secret_value.return_value = {"SecretString": stored}
    with pytest.raises(HTTPException) as error:
        setup.register(provider, "app-client", "new-secret", replace=True, app_id=app_id, before_effects=MagicMock())
    assert error.value.status_code in (400, 409)
    setup.secret_store.put_secret_value.assert_not_called()
    setup.control.update_oauth2_credential_provider.assert_not_called()


@pytest.mark.parametrize("provider,vendor", [("github", "GithubOauth2"), ("atlassian", "AtlassianOauth2"), ("slack", "CustomOauth2")])
def test_each_provider_registers_independently_with_external_secret(setup, provider, vendor):
    app = {**setup.control.get_oauth2_credential_provider.return_value, "credentialProviderVendor": vendor}
    if provider == "slack":
        app["oauth2ProviderConfigOutput"] = {"customOauth2ProviderConfig": {"oauthDiscovery": {"authorizationServerMetadata": SLACK_USER_METADATA}}}
    setup.control.get_oauth2_credential_provider.side_effect = [missing(), app]
    setup.control.get_workload_identity.side_effect = [missing(), {"allowedResourceOauth2ReturnUrls": [RETURN]}]
    effects = MagicMock()
    result = setup.register(provider, "id", "secret", before_effects=effects)
    effects.assert_called_once()
    setup.control.create_workload_identity.assert_called_once_with(name=WORKLOAD, allowedResourceOauth2ReturnUrls=[RETURN])
    args = setup.control.create_oauth2_credential_provider.call_args.kwargs
    assert args["name"] == f"adp-tools-dev-{provider}-user"
    assert args["credentialProviderVendor"] == vendor
    assert args["oauth2ProviderConfigInput"][f"{'custom' if provider == 'slack' else provider}Oauth2ProviderConfig"]["clientSecretConfig"] == {
        "secretId": f"adp/dev/connections/oauth/{provider}",
        "jsonKey": "client_secret",
    }
    assert "secret" not in json.dumps(result)
    assert "clientSecret" not in args["oauth2ProviderConfigInput"][f"{'custom' if provider == 'slack' else provider}Oauth2ProviderConfig"]


@pytest.mark.parametrize("same", [True, False])
def test_retry_preserves_preexisting_secret(setup, same):
    app = setup.control.get_oauth2_credential_provider.return_value
    setup.control.get_oauth2_credential_provider.side_effect = [missing(), app]
    setup.secret_store.get_secret_value.side_effect = None
    setup.secret_store.get_secret_value.return_value = {
        "SecretString": json.dumps({"client_id": "id", "client_secret": "secret" if same else "other"})
    }
    if same:
        assert setup.register("atlassian", "id", "secret", before_effects=MagicMock())["status"] == "registered"
    else:
        with pytest.raises(HTTPException) as error:
            setup.register("atlassian", "id", "secret", before_effects=MagicMock())
        assert error.value.status_code == 409
        setup.control.create_oauth2_credential_provider.assert_not_called()
    setup.secret_store.create_secret.assert_not_called()


@pytest.mark.parametrize("stage,expected", [("creating", "setup_required"), ("credentials_saved", "setup_required"), ("ready", "registered")])
def test_slack_connect_waits_for_automated_callback_readback(setup, stage, expected):
    from src.shared.identity.slack_setup import AUTOMATION

    setup.control.get_oauth2_credential_provider.return_value = {
        "credentialProviderVendor": "CustomOauth2",
        "status": "READY",
        "callbackUrl": CALLBACK,
        "oauth2ProviderConfigOutput": {"customOauth2ProviderConfig": {"oauthDiscovery": {"authorizationServerMetadata": SLACK_USER_METADATA}}},
    }
    setup.secret_store.get_secret_value.side_effect = None
    setup.secret_store.get_secret_value.return_value = {
        "SecretString": json.dumps({"automation": AUTOMATION, "stage": stage, "client_secret": "private-secret"})
    }
    assert setup.status("slack") == {"provider": "slack", "service": "slack", "status": expected}
    admin = setup.status("slack", admin=True)
    assert admin["status"] == "registered" and admin["automation_state"] == stage and admin["callback_url"] == CALLBACK
    assert "private-secret" not in json.dumps(admin)


@pytest.fixture
def dynamic_client_setup(mcp_setup):
    setup, plan, records = mcp_setup
    plan["registration_mode"] = "dynamic-client-registration"
    plan["client_id"] = ""
    plan["authorization_server_metadata"]["registration_endpoint"] = "https://auth.atlassian.com/register"
    plan["metadata_sha256"] = hashlib.sha256(
        json.dumps([plan["protected_resource_metadata"], plan["authorization_server_metadata"]], sort_keys=True).encode()
    ).hexdigest()
    setup.settings.atlassian_metadata_sha256 = plan["metadata_sha256"]
    setup.settings.atlassian_identity_compatibility_verified = True
    store = {"state": "unregistered"}
    requests = []
    events = []

    def read_secret(**kwargs):
        secret_id = kwargs["SecretId"]
        if secret_id == setup.settings.atlassian_config_ref:
            return {"SecretString": json.dumps(plan)}
        assert secret_id == plan["app_secret_ref"]
        return {"SecretString": json.dumps(store)}

    def save_secret(**kwargs):
        assert kwargs["SecretId"] == plan["app_secret_ref"]
        store.clear()
        store.update(json.loads(kwargs["SecretString"]))
        events.append(store["state"])

    def create_provider(**kwargs):
        config = kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        if "tokenEndpointAuthMethods" in config["oauthDiscovery"]["authorizationServerMetadata"] and "clientAuthenticationMethod" in config:
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "Both authentication method fields cannot be provided together"}},
                "CreateOauth2CredentialProvider",
            )
        assert config["clientSecretSource"] == "EXTERNAL"
        assert config["clientSecretConfig"] == {"secretId": plan["app_secret_ref"], "jsonKey": "bootstrap_secret"}
        assert len(store["bootstrap_secret"]) >= 32
        assert "clientSecret" not in config
        assert config["clientId"].startswith("adp-bootstrap-")
        records["provider"] = {
            "credentialProviderArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:oauth2credentialprovider/dynamic",
            "credentialProviderVendor": "CustomOauth2",
            "oauth2ProviderConfigOutput": {
                "customOauth2ProviderConfig": {
                    "oauthDiscovery": config["oauthDiscovery"],
                    "clientId": config["clientId"],
                    "clientAuthenticationMethod": config["clientAuthenticationMethod"],
                }
            },
            "clientSecretSource": "EXTERNAL",
            "callbackUrl": CALLBACK,
            "status": "READY",
        }
        setup.control.list_tags_for_resource.return_value = {"tags": kwargs["tags"]}

    def update_provider(**kwargs):
        config = kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        if "tokenEndpointAuthMethods" in config["oauthDiscovery"]["authorizationServerMetadata"] and "clientAuthenticationMethod" in config:
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "Both authentication method fields cannot be provided together"}},
                "UpdateOauth2CredentialProvider",
            )
        assert config["clientSecretSource"] == "EXTERNAL"
        assert config["clientSecretConfig"] == {"secretId": plan["app_secret_ref"], "jsonKey": "client_secret"}
        records["provider"]["oauth2ProviderConfigOutput"]["customOauth2ProviderConfig"]["clientId"] = config["clientId"]
        records["provider"]["clientSecretSource"] = "EXTERNAL"
        records["provider"]["clientSecretArn"] = {"secretArn": plan["app_secret_ref"]}
        records["provider"]["clientSecretJsonKey"] = "client_secret"

    setup.secret_store.get_secret_value.side_effect = read_secret
    setup.secret_store.put_secret_value.side_effect = save_secret
    setup.control.create_oauth2_credential_provider.side_effect = create_provider
    setup.control.update_oauth2_credential_provider.side_effect = update_provider

    def post(url, *, json, timeout, follow_redirects):
        assert url == "https://auth.atlassian.com/register"
        assert json["redirect_uris"] == [CALLBACK]
        assert json["token_endpoint_auth_method"] == "client_secret_basic"
        assert timeout.connect == 2 and follow_redirects is False
        assert store["state"] == "registration_uncertain"
        assert setup.atlassian_mcp_status(policy_result="allowed")["status"] != "ready"
        requests.append(json)
        response = {
            "client_id": "registered-client",
            "client_secret": "secret-sentinel",
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "client_secret_basic",
        }
        return SimpleNamespace(status_code=201, content=b"valid", json=lambda: response)

    return setup, plan, records, store, requests, events, post


def test_dynamic_client_registers_once_and_retains_real_credentials(dynamic_client_setup):
    setup, plan, records, store, requests, events, post = dynamic_client_setup
    assert setup.prepare_atlassian_mcp(before_effects=None, registration_post=post) == {"status": "setup_required"}
    with pytest.raises(HTTPException) as missing_lease:
        setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_post=post)
    assert missing_lease.value.detail == {"error": "atlassian_registration_requires_lease"}
    assert records["provider"] is None and not requests
    first = setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=post)
    second = setup.prepare_atlassian_mcp(before_effects=None, registration_post=post)
    assert first == second == {"status": "prepared", "callback_url": CALLBACK, "return_url": plan["return_url"]}
    assert events == ["unregistered", "registration_uncertain", "registered"]
    assert len(requests) == 1 and setup.control.create_oauth2_credential_provider.call_count == 1
    assert setup.control.update_oauth2_credential_provider.call_count == 1
    import boto3
    from botocore.validate import validate_parameters

    control_model = boto3.client(
        "bedrock-agentcore-control", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing"
    ).meta.service_model
    for operation, request in (
        ("CreateOauth2CredentialProvider", setup.control.create_oauth2_credential_provider.call_args.kwargs),
        ("UpdateOauth2CredentialProvider", setup.control.update_oauth2_credential_provider.call_args.kwargs),
    ):
        validate_parameters(request, control_model.operation_model(operation).input_shape)
    validate_parameters(
        records["provider"]["clientSecretArn"], control_model.operation_model("GetOauth2CredentialProvider").output_shape.members["clientSecretArn"]
    )
    assert store["client_id"] == "registered-client"
    assert "secret-sentinel" not in json.dumps(first) + json.dumps(second)
    assert setup.atlassian_mcp_status(policy_result="allowed")["status"] == "consent_required"


@pytest.mark.parametrize("legacy_method", ["client_secret_basic", "client_secret_post"])
@pytest.mark.parametrize("dynamic", [False, True])
def test_existing_atlassian_provider_preserves_only_compatible_legacy_metadata(request, legacy_method, dynamic):
    fixture = request.getfixturevalue("dynamic_client_setup" if dynamic else "mcp_setup")
    setup, _, records = fixture[:3]
    options = {"before_effects": lambda: None}
    if dynamic:
        post = fixture[-1]
        options.update(registration_lease=lambda: None, registration_post=post)
    first = setup.prepare_atlassian_mcp(**options)
    config = records["provider"]["oauth2ProviderConfigOutput"]["customOauth2ProviderConfig"]
    config["oauthDiscovery"]["authorizationServerMetadata"]["tokenEndpointAuthMethods"] = [legacy_method]
    if legacy_method == "client_secret_basic":
        assert setup.prepare_atlassian_mcp(before_effects=None) == first
    else:
        with pytest.raises(HTTPException) as error:
            setup.prepare_atlassian_mcp(before_effects=None)
        assert error.value.status_code == 409
    assert setup.control.create_oauth2_credential_provider.call_count == 1


@pytest.mark.parametrize("created", [False, True])
def test_dynamic_client_recovers_identity_failure_before_registration(dynamic_client_setup, created):
    setup, plan, records, store, requests, events, post = dynamic_client_setup
    create = setup.control.create_oauth2_credential_provider.side_effect

    def interrupted(**kwargs):
        if created:
            create(**kwargs)
            raise TimeoutError("Identity creation response lost")
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "CreateOauth2CredentialProvider")

    setup.control.create_oauth2_credential_provider.side_effect = interrupted
    with pytest.raises((ClientError, TimeoutError)):
        setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=post)
    assert store["state"] == "unregistered" and len(store["bootstrap_secret"]) >= 32
    assert not requests and events == ["unregistered"]
    setup.control.create_oauth2_credential_provider.side_effect = create
    with pytest.raises(HTTPException) as missing_lease:
        setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_post=post, recovery_only=True)
    assert missing_lease.value.detail == {"error": "atlassian_registration_requires_lease"}
    assert not requests
    result = setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=post, recovery_only=True)
    assert result == {"status": "prepared", "callback_url": CALLBACK, "return_url": plan["return_url"]}
    assert events == ["unregistered", "registration_uncertain", "registered"] and len(requests) == 1
    assert setup.control.create_oauth2_credential_provider.call_count == (1 if created else 2)
    assert setup.control.create_workload_identity.call_count == 1


@pytest.mark.parametrize("persisted", [False, True])
def test_dynamic_client_recovers_bootstrap_write_without_creating_extra_resources(dynamic_client_setup, persisted):
    setup, _, _, store, requests, _, post = dynamic_client_setup
    save = setup.secret_store.put_secret_value.side_effect

    def interrupted(**kwargs):
        if persisted:
            save(**kwargs)
        raise TimeoutError("bootstrap storage reply lost")

    setup.secret_store.put_secret_value.side_effect = interrupted
    with pytest.raises(TimeoutError):
        setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=post)
    assert store["state"] == "unregistered" and not requests
    assert setup.control.create_oauth2_credential_provider.call_count == 0
    bootstrap = store.get("bootstrap_secret")
    setup.secret_store.put_secret_value.side_effect = save
    result = setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=post, recovery_only=True)
    assert result["status"] == "prepared" and len(requests) == 1
    assert setup.control.create_oauth2_credential_provider.call_count == 1
    setup.secret_store.create_secret.assert_not_called()
    if bootstrap:
        initial_writes = [json.loads(call.kwargs["SecretString"]) for call in setup.secret_store.put_secret_value.call_args_list]
        assert sum(value.get("bootstrap_secret") == bootstrap for value in initial_writes) == 1
    assert "bootstrap_secret" not in store


@pytest.mark.parametrize("reference", [None, "client_secret", {"secretArn": "different"}])
def test_dynamic_client_refuses_unverified_secret_reference(dynamic_client_setup, reference):
    setup, _, records, _, requests, _, post = dynamic_client_setup
    setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=post)
    records["provider"]["clientSecretArn"] = reference
    assert setup.prepare_atlassian_mcp(before_effects=None, registration_post=post) == {"status": "setup_required"}
    assert len(requests) == 1


def test_dynamic_client_refuses_bootstrap_key_as_final_client_secret(dynamic_client_setup):
    setup, _, records, _, requests, _, post = dynamic_client_setup
    setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=post)
    records["provider"]["clientSecretJsonKey"] = "bootstrap_secret"
    assert setup.prepare_atlassian_mcp(before_effects=None, registration_post=post) == {"status": "setup_required"}
    assert len(requests) == 1


@pytest.mark.parametrize("recovery_only", [False, True])
def test_dynamic_client_lost_reply_never_reposts(dynamic_client_setup, recovery_only):
    setup, _, records, store, requests, _, post = dynamic_client_setup

    def lost_reply(*args, **kwargs):
        post(*args, **kwargs)
        raise TimeoutError("registration response lost")

    with pytest.raises(TimeoutError):
        setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=lost_reply)
    assert store["state"] == "registration_uncertain"
    assert records["provider"]["status"] == "READY"
    with pytest.raises(HTTPException) as uncertain:
        setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=post, recovery_only=recovery_only)
    assert uncertain.value.detail == {"error": "atlassian_registration_uncertain"}
    assert len(requests) == 1 and setup.control.update_oauth2_credential_provider.call_count == 0
    assert setup.atlassian_mcp_status(policy_result="allowed")["status"] == "unavailable"


@pytest.mark.parametrize("recovery_only", [False, True])
def test_dynamic_client_retries_update_without_new_registration(dynamic_client_setup, recovery_only):
    setup, plan, _, store, requests, events, post = dynamic_client_setup
    original_update = setup.control.update_oauth2_credential_provider.side_effect
    setup.control.update_oauth2_credential_provider.side_effect = RuntimeError("identity update failed")
    with pytest.raises(RuntimeError, match="identity update failed"):
        setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=post)
    assert store["state"] == "registered" and events == ["unregistered", "registration_uncertain", "registered"]
    setup.control.update_oauth2_credential_provider.side_effect = original_update
    result = setup.prepare_atlassian_mcp(
        before_effects=lambda: None, registration_lease=lambda: None, registration_post=post, recovery_only=recovery_only
    )
    assert result["status"] == "prepared" and result["callback_url"] == CALLBACK
    assert len(requests) == 1 and setup.control.update_oauth2_credential_provider.call_count == 2
    assert "secret-sentinel" not in json.dumps(result) and result["return_url"] == plan["return_url"]


def test_dynamic_client_fences_registration_after_intent(dynamic_client_setup):
    setup, _, _, store, requests, events, post = dynamic_client_setup
    checks = []

    def guard():
        checks.append(True)
        if store["state"] == "registration_uncertain":
            raise RuntimeError("lease lost before registration")

    with pytest.raises(RuntimeError, match="lease lost before registration"):
        setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=guard, registration_post=post)
    assert events == ["unregistered", "registration_uncertain"] and store["state"] == "registration_uncertain"
    assert not requests and setup.control.update_oauth2_credential_provider.call_count == 0
    with pytest.raises(HTTPException) as blocked:
        setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=post)
    assert blocked.value.detail == {"error": "atlassian_registration_uncertain"}
    assert not requests


def test_dynamic_client_requires_compatibility_proof_to_report_ready(dynamic_client_setup):
    setup, _, _, store, requests, _, post = dynamic_client_setup
    setup.settings.atlassian_identity_compatibility_verified = False
    result = setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=post)
    assert result["status"] == "prepared"
    assert store["state"] == "registered" and len(requests) == 1
    assert setup.atlassian_mcp_status(policy_result="allowed")["status"] == "consent_required"
    assert setup.atlassian_mcp_status()["status"] == "consent_required"
    assert setup.atlassian_mcp_status(policy_result="blocked")["status"] == "policy_blocked"
    setup.settings.atlassian_identity_compatibility_verified = True
    assert setup.atlassian_mcp_status(policy_result="allowed")["status"] == "consent_required"
    assert len(requests) == 1


def test_dynamic_client_rejects_callback_substitution_without_replay(dynamic_client_setup):
    setup, _, _, store, requests, _, post = dynamic_client_setup

    def substituted(url, **kwargs):
        response = post(url, **kwargs).json()
        response["redirect_uris"] = ["https://attacker.example.test/callback"]
        return SimpleNamespace(status_code=201, content=b"malformed-callback", json=lambda: response)

    with pytest.raises(HTTPException) as refused:
        setup.prepare_atlassian_mcp(before_effects=lambda: None, registration_lease=lambda: None, registration_post=substituted)
    assert refused.value.detail == {"error": "atlassian_registration_uncertain"}
    assert store["state"] == "registration_uncertain" and len(requests) == 1
    setup.control.update_oauth2_credential_provider.assert_not_called()
    assert setup.atlassian_mcp_status(policy_result="allowed")["status"] == "unavailable"
