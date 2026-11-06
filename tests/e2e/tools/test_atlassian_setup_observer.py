"""Controlled independent reads; no AWS role or provider account is implied."""

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from tests.e2e.tools.atlassian_oauth_probe import CONSENT_SCOPES
from tests.e2e.tools.atlassian_setup_observer import IndependentSetupObserver, _aws
from tests.e2e.tools.test_atlassian_setup_live import configuration, private


def test_selected_aws_identity_is_checked_before_each_read(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE", raising=False)
    _, config = configuration(tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        result = (
            {
                "Account": config["account"],
                "Arn": "arn:aws:sts::123456789012:assumed-role/test-adapter/lease",
            }
            if "get-caller-identity" in command
            else {"accessToken": "ephemeral"}
        )
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(result), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", run)
    result = _aws(
        config,
        "adapter",
        "bedrock-agentcore",
        "get-resource-oauth2-token",
        input_value={
            "workloadIdentityToken": "ephemeral",
            "scopes": list(CONSENT_SCOPES),
        },
    )
    assert result == {"accessToken": "ephemeral"}
    assert calls[0][0][:7] == [
        "adp-cred",
        "assume",
        "--service",
        "aws",
        "--label",
        "test-adapter",
        "--exec",
    ]
    assert "ephemeral" not in str(calls[1][0])
    assert json.loads(calls[1][1]["input"])["workloadIdentityToken"] == "ephemeral"
    config["adapter_role_arn"] = "arn:aws:iam::123456789012:role/other-role"
    with pytest.raises(ValueError, match="exact role"):
        _aws(config, "adapter", "bedrock-agentcore", "get-resource-oauth2-token")
    assert len(calls) == 3


def test_oidc_observer_uses_isolated_profile_not_deployment_credentials(
    tmp_path, monkeypatch
):
    _, config = configuration(tmp_path)
    config["core_label"] = "oidc-core"
    config["core_role_arn"] = (
        "arn:aws:iam::123456789012:role/adp-dev-atlassian-mcp-observer-core"
    )
    aws_config = tmp_path / "setup" / "aws-config"
    private(aws_config, "[profile oidc-core]\n")
    private(aws_config.with_name("aws-credentials"), "")
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE", str(aws_config))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "unrelated-deployment-key")
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/unrelated/pod-token")
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", "https://unrelated.example.test")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "Account": config["account"],
                    "Arn": (
                        "arn:aws:sts::123456789012:assumed-role/"
                        "adp-dev-atlassian-mcp-observer-core/setup-123"
                    ),
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)
    _aws(config, "core", "sts", "get-caller-identity")
    assert len(calls) == 2
    assert all(command[:3] == ["aws", "--profile", "oidc-core"] for command, _ in calls)
    assert all("AWS_ACCESS_KEY_ID" not in kwargs["env"] for _, kwargs in calls)
    assert all(
        "AWS_WEB_IDENTITY_TOKEN_FILE" not in kwargs["env"] for _, kwargs in calls
    )
    assert all("AWS_ENDPOINT_URL_STS" not in kwargs["env"] for _, kwargs in calls)
    assert all(
        kwargs["env"]["AWS_CONFIG_FILE"] == str(aws_config) for _, kwargs in calls
    )
    monkeypatch.delenv("ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(ValueError, match="credential profile"):
        _aws(config, "core", "sts", "get-caller-identity")


@pytest.mark.parametrize("legacy_metadata", [False, True])
def test_dcr_provider_is_read_back_from_registered_client_and_pinned_plan(
    tmp_path, monkeypatch, legacy_metadata
):
    _, config = configuration(tmp_path)
    callback = "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/callback/owned"
    plan = {
        "registration_mode": "dynamic-client-registration",
        "client_id": "",
        "metadata_sha256": config["metadata_sha256"],
        "app_secret_ref": config["client_secret_arn"],
        "issuer": "https://auth.atlassian.com/oauth2",
        "authorization_endpoint": "https://auth.atlassian.com/oauth2/authorize",
        "token_endpoint": "https://auth.atlassian.com/oauth2/token",
        "return_url": config["gateway_url"]
        + "/api/auth/connections/atlassian-mcp/return",
    }
    stored = {
        "state": "registered",
        "client_id": "registered-client",
        "client_secret": "not-logged",
        "callback_sha256": hashlib.sha256(callback.encode()).hexdigest(),
    }
    provider_arn = (
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:oauth2-provider/owned"
    )
    overrides = {}

    def aws(_, role, service, operation, *, arguments=(), **kwargs):
        assert role == "core"
        if operation == "get-secret-value":
            return {
                "SecretString": json.dumps(
                    plan if arguments[-1] == config["plan_secret_arn"] else stored
                )
            }
        if operation == "get-oauth2-credential-provider":
            return {
                "status": "READY",
                "credentialProviderArn": provider_arn,
                "callbackUrl": callback,
                "clientSecretSource": "EXTERNAL",
                "clientSecretArn": {"secretArn": config["client_secret_arn"]},
                "clientSecretJsonKey": "client_secret",
                "oauth2ProviderConfigOutput": {
                    "customOauth2ProviderConfig": {
                        "clientId": "registered-client",
                        "clientAuthenticationMethod": "CLIENT_SECRET_BASIC",
                        "oauthDiscovery": {
                            "authorizationServerMetadata": {
                                "issuer": plan["issuer"],
                                "authorizationEndpoint": plan["authorization_endpoint"],
                                "tokenEndpoint": plan["token_endpoint"],
                                "responseTypes": ["code"],
                                **({"tokenEndpointAuthMethods": ["client_secret_basic"]} if legacy_metadata else {}),
                            }
                        },
                    }
                },
                **overrides,
            }
        if operation == "get-workload-identity":
            return {"allowedResourceOauth2ReturnUrls": [plan["return_url"]]}
        if operation == "list-tags-for-resource":
            return {
                "tags": {
                    "adp-config-digest": hashlib.sha256(
                        json.dumps(plan, sort_keys=True).encode()
                    ).hexdigest()
                }
            }
        pytest.fail("unexpected read")

    monkeypatch.setattr("tests.e2e.tools.atlassian_setup_observer._aws", aws)
    observer = IndependentSetupObserver(config)
    assert observer._provider()[0] == plan
    for field, value in (("clientSecretArn", config["client_secret_arn"]), ("clientSecretJsonKey", "bootstrap_secret")):
        overrides[field] = value
        with pytest.raises(ValueError, match="readback differs"):
            observer._provider()
        overrides.clear()
    stored["client_id"] = "different-client"
    with pytest.raises(ValueError, match="readback differs"):
        observer._provider()


def test_live_observer_requires_gateway_binding_and_direct_provider_identity(
    tmp_path, monkeypatch
):
    _, config = configuration(tmp_path)
    observer = IndependentSetupObserver(config)
    plan = {"metadata_sha256": config["metadata_sha256"]}
    provider = {
        "credentialProviderArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:oauth2-provider/owned"
    }
    monkeypatch.setattr(observer, "_provider", lambda: (plan, provider))
    binding = {
        "id": "owned-connection",
        "revision": "revision-one",
        "selected_site_id": "site-one",
        "sites": ["site-one"],
    }
    private(
        config["attempt_file"],
        json.dumps(
            {
                "run_id": "run-one",
                "owner_id": "person-one",
                "fixture_kind": "fresh",
                "phase": "selected",
                "connection_id": "owned-connection",
                "revision": "revision-one",
            }
        ),
    )
    monkeypatch.setattr(
        observer, "_gateway_status", lambda: {"status": "ready", "connection": binding}
    )
    monkeypatch.setattr(observer, "_access_token", lambda _: "ephemeral-provider-token")
    monkeypatch.setattr(
        "tests.e2e.tools.atlassian_setup_client.observe_client_configuration",
        lambda config, revision: {
            "action": "mcp_configuration",
            "actor": "machine",
            "status": "observed",
            "source": "client",
            "evidence_sha256": "d" * 64,
            "run_id": config["run_id"],
            "owner_id": config["owner_id"],
        },
    )
    monkeypatch.setattr(
        "tool_adapters.atlassian_observe.collect_observation",
        lambda token, settings: (
            "provider-user-one",
            (SimpleNamespace(site_id="site-one"),),
            tuple(set(CONSENT_SCOPES) - {"offline_access"}),
        ),
    )
    events = observer.read_actions(
        run_id="run-one", owner_id="person-one", fixture_kind="fresh"
    )
    assert len(events) == 6 and all(
        event["status"] == "observed" and len(event["evidence_sha256"]) == 64
        for event in events
    )
    assert {event["source"] for event in events} == {
        "gateway",
        "identity",
        "provider",
        "client",
    }
    assert "policy_approval" not in {event["action"] for event in events}
    config["policy_required"] = True
    assert "policy_approval" not in {
        event["action"]
        for event in observer.read_actions(
            run_id="run-one", owner_id="person-one", fixture_kind="fresh"
        )
    }
    binding["selected_site_id"] = "foreign-site"
    with pytest.raises(ValueError, match="Gateway consent"):
        observer.read_actions(
            run_id="run-one", owner_id="person-one", fixture_kind="fresh"
        )
    binding["selected_site_id"] = "site-one"
    monkeypatch.setattr(
        "tool_adapters.atlassian_observe.collect_observation",
        lambda token, settings: (
            "foreign-user",
            (SimpleNamespace(site_id="site-one"),),
            tuple(CONSENT_SCOPES),
        ),
    )
    with pytest.raises(ValueError, match="subject or site"):
        observer.read_actions(
            run_id="run-one", owner_id="person-one", fixture_kind="fresh"
        )


def test_cleanup_requires_both_missing_identity_grant_and_provider_rejection(
    tmp_path, monkeypatch
):
    _, config = configuration(tmp_path)
    observer = IndependentSetupObserver(config)
    monkeypatch.setattr(observer, "_gateway_status", lambda: {"status": "ready"})
    assert (
        observer.cleaned(run_id="run-one", owner_id="person-one", fixture_kind="fresh")
        is False
    )
    observer.token = "previous-provider-token"
    monkeypatch.setattr(
        IndependentSetupObserver, "_gateway_status", lambda self: {"status": "ready"}
    )
    assert (
        IndependentSetupObserver(config).cleaned(
            run_id="run-one", owner_id="person-one", fixture_kind="fresh"
        )
        is False
    )
    monkeypatch.setattr(observer, "_access_token", lambda _: "still-active")
    assert (
        observer.cleaned(run_id="run-one", owner_id="person-one", fixture_kind="fresh")
        is False
    )

    def absent(_):
        raise ValueError("Identity grant absent")

    monkeypatch.setattr(observer, "_access_token", absent)
    from tool_adapters.atlassian_observe import MCPTransport, ProviderResponseError

    def refused(*_args, **_kwargs):
        raise ProviderResponseError("provider_unauthorized")

    monkeypatch.setattr(MCPTransport, "send", refused)
    assert (
        observer.cleaned(run_id="run-one", owner_id="person-one", fixture_kind="fresh")
        is True
    )
    monkeypatch.setattr(
        observer,
        "_gateway_status",
        lambda: {"connection": {"revision": "still-active"}},
    )
    assert (
        observer.cleaned(run_id="run-one", owner_id="person-one", fixture_kind="fresh")
        is False
    )
