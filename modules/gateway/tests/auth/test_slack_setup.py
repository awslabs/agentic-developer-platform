"""Manifest automation survives interrupted setup without duplicate apps or secrets in output."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from src.shared.identity.slack_setup import AUTOMATION, SLACK_USER_METADATA, SlackSetupRejected, automate_slack, slack_manifest

CALLBACK = "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/callback/fixture"


@pytest.fixture
def setup():
    state = {"secret": None, "provider": None, "manifest": None}
    effects = Mock()

    def read(method, **kwargs):
        if method == control.get_oauth2_credential_provider:
            return state["provider"]
        if method == control.get_workload_identity:
            return {"allowedResourceOauth2ReturnUrls": ["https://dashboard.example.test/api/auth/connections/oauth/return"]}
        return {"SecretString": json.dumps(state["secret"])} if state["secret"] is not None else None

    def save(**kwargs):
        state["secret"] = json.loads(kwargs["SecretString"])

    control = SimpleNamespace(get_oauth2_credential_provider=Mock(), get_workload_identity=Mock())
    store = SimpleNamespace(get_secret_value=Mock(), create_secret=Mock(side_effect=save), put_secret_value=Mock(side_effect=save))
    status = {"provider": "slack", "service": "slack", "status": "registered", "callback_url": CALLBACK}

    def register(provider, client_id, secret, *, before_effects):
        assert (provider, client_id, secret) == ("slack", "client-id", "private-client-secret")
        assert state["secret"]["app_id"] == "A00000001"
        before_effects()
        state["provider"] = {
            "credentialProviderVendor": "CustomOauth2",
            "oauth2ProviderConfigOutput": {
                "customOauth2ProviderConfig": {"clientId": client_id, "oauthDiscovery": {"authorizationServerMetadata": SLACK_USER_METADATA}}
            },
        }
        return status

    service = SimpleNamespace(
        settings=SimpleNamespace(
            oauth_workload_name="adp-tools-dev-oauth-user", oauth_return_url="https://dashboard.example.test/api/auth/connections/oauth/return"
        ),
        control=control,
        secret_store=store,
        _get=read,
        provider_name=lambda p: "adp-tools-dev-slack-user",
        secret_name=lambda p: "adp/dev/connections/oauth/slack",
        register=Mock(side_effect=register),
        status=Mock(return_value=status),
    )

    def call(operation, **kwargs):
        if operation == "create":
            assert state["secret"] == {"automation": AUTOMATION, "stage": "creating"}
            assert effects.call_count > 0
            state["manifest"] = json.loads(kwargs["manifest"])
            # Simulate an owner setting that must survive the callback update.
            state["manifest"]["settings"]["org_deploy_enabled"] = True
            return {"ok": True, "app_id": "A00000001", "credentials": {"client_id": "client-id", "client_secret": "private-client-secret"}}
        assert kwargs["app_id"] == "A00000001"
        if operation == "export":
            return {"ok": True, "manifest": copy.deepcopy(state["manifest"])}
        state["manifest"] = json.loads(kwargs["manifest"])
        return {"ok": True, "app_id": "A00000001"}

    client = SimpleNamespace(call=Mock(side_effect=call))
    return service, client, state, effects


def test_automates_registration_callback_and_readback_without_storing_config_token(setup):
    service, client, state, effects = setup
    result = automate_slack(service, "private-config-token", before_effects=effects, client=client)
    assert result["callback_configured"] is True and result["automation_state"] == "ready"
    assert [call.args[0] for call in client.call.call_args_list] == ["create", "export", "update", "export"]
    assert state["manifest"]["oauth_config"]["redirect_urls"] == [CALLBACK]
    assert state["manifest"]["settings"]["org_deploy_enabled"] is True
    assert "private-config-token" not in str(state)
    assert "private-" not in json.dumps(result)
    # Retrying after a lost successful response checks the same app, without creation.
    result = automate_slack(service, "replacement-token", before_effects=effects, client=client)
    assert result["callback_configured"] is True
    assert [call.args[0] for call in client.call.call_args_list].count("create") == 1
    service.register.assert_called_once()


def test_uncertain_create_is_not_retried(setup):
    service, client, state, effects = setup
    client.call.side_effect = HTTPException(503, "upstream unavailable")
    with pytest.raises(HTTPException):
        automate_slack(service, "token", before_effects=effects, client=client)
    assert state["secret"]["stage"] == "creating"
    with pytest.raises(HTTPException) as refused:
        automate_slack(service, "token", before_effects=effects, client=client)
    assert refused.value.detail["error"] == "slack_setup_reconciliation_required"
    client.call.assert_called_once()


def test_explicit_vendor_rejection_allows_a_new_token_without_operator_reconciliation(setup):
    service, client, state, effects = setup
    original = client.call.side_effect
    client.call.side_effect = SlackSetupRejected(400, "invalid_auth")
    with pytest.raises(SlackSetupRejected):
        automate_slack(service, "expired-token", before_effects=effects, client=client)
    assert state["secret"]["stage"] == "create_rejected"
    client.call.side_effect = original
    assert automate_slack(service, "new-token", before_effects=effects, client=client)["callback_configured"] is True
    service.secret_store.create_secret.assert_called_once()


def test_callback_failure_can_resume_same_registered_app(setup):
    service, client, state, effects = setup
    original = client.call.side_effect

    def interrupted(operation, **kwargs):
        if operation == "update":
            raise HTTPException(503, "interrupted")
        return original(operation, **kwargs)

    client.call.side_effect = interrupted
    with pytest.raises(HTTPException):
        automate_slack(service, "token", before_effects=effects, client=client)
    assert state["secret"]["stage"] == "credentials_saved" and state["provider"] is not None
    client.call.side_effect = original
    assert automate_slack(service, "token", before_effects=effects, client=client)["callback_configured"] is True
    service.register.assert_called_once()
    assert [call.args[0] for call in client.call.call_args_list].count("create") == 1


@pytest.mark.parametrize("record", [{"client_id": "existing", "client_secret": "existing"}, {"automation": "different-app", "stage": "ready"}])
def test_never_replaces_existing_app_or_secret(setup, record):
    service, client, state, effects = setup
    state["secret"] = record
    with pytest.raises(HTTPException) as refused:
        automate_slack(service, "token", before_effects=effects, client=client)
    assert refused.value.status_code == 409 and state["secret"] == record
    client.call.assert_not_called()
    effects.assert_not_called()


def test_manifest_requests_user_permissions_without_a_bot_or_events():
    manifest = slack_manifest()
    assert set(manifest["oauth_config"]["scopes"]) == {"user"}
    assert "features" not in manifest and "event_subscriptions" not in manifest["settings"]
    assert SLACK_USER_METADATA["tokenEndpoint"] == "https://slack.com/api/oauth.v2.user.access"


@pytest.mark.parametrize("reply", ["redirect", "too_large", "private_error", "invalid_token"])
def test_manifest_transport_is_bounded_and_never_echoes_provider_body(monkeypatch, reply):
    import httpx

    from src.shared.identity import slack_setup

    original = httpx.Client
    calls = []

    def respond(request):
        calls.append(request)
        assert str(request.url) == "https://slack.com/api/apps.manifest.create"
        assert request.headers["Authorization"] == "Bearer private-config-token"
        if reply == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.example.test/private"})
        if reply == "too_large":
            return httpx.Response(200, content=b"x" * 65537)
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth" if reply == "invalid_token" else "private-provider-error"})

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(slack_setup.httpx, "Client", client)
    with pytest.raises(HTTPException) as refused:
        slack_setup.SlackManifestClient("private-config-token").call("create", manifest=json.dumps(slack_manifest()))
    assert len(calls) == 1 and "private-" not in str(refused.value.detail)
    assert isinstance(refused.value, SlackSetupRejected) is (reply == "invalid_token")
