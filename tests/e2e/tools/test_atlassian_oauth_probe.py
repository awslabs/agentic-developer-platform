"""Generated non-live Identity SDK fixtures, not authenticated acceptance."""

import pytest

from tests.e2e.tools.atlassian_oauth_probe import IdentityConfig, IdentityLifecycle, ProbeBlocked, READ_SCOPES, CONSENT_SCOPES, IDENTITY_SCOPES, RESOURCE


TEST_ISSUER = "https://auth.atlassian.com/example-issuer"


def configured_identity(scopes=CONSENT_SCOPES):
    return IdentityConfig.from_metadata(
        "probe-user", "probe-atlassian-mcp", "https://adp.example.test/return",
        {"resource": RESOURCE, "authorization_servers": [TEST_ISSUER], "scopes_supported": list(CONSENT_SCOPES) + ["write:jira:agent-interface"]},
        {"issuer": TEST_ISSUER, "code_challenge_methods_supported": ["S256"], "grant_types_supported": ["authorization_code", "refresh_token"]},
        scopes,
    )


class IdentityFixture:
    def __init__(self, role):
        self.role = role
        self.calls = []
        self.session_status = "IN_PROGRESS"

    def get_workload_access_token_for_user_id(self, **kwargs):
        self.calls.append(("workload", kwargs))
        return {"workloadAccessToken": f"{self.role}-workload"}

    def get_resource_oauth2_token(self, **kwargs):
        self.calls.append(("resource", kwargs))
        if kwargs.get("forceAuthentication"):
            return {"sessionStatus": self.session_status, "sessionUri": "identity-session", "authorizationUrl": "https://id.example.test/consent"}
        return {"sessionStatus": self.session_status if self.session_status == "FAILED" else "COMPLETE", "accessToken": "ephemeral-sentinel"}

    def complete_resource_token_auth(self, **kwargs):
        self.calls.append(("complete", kwargs))


def test_identity_lifecycle_uses_distinct_roles_and_transient_token():
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    config = configured_identity()
    lifecycle = IdentityLifecycle(config, core, adapter)
    session = lifecycle.start("owner-a", "random-bound-state")
    assert session["session_uri"] == "identity-session"
    assert core.calls[1] == ("resource", {
        "workloadIdentityToken": "core-workload", "resourceCredentialProviderName": "probe-atlassian-mcp",
        "scopes": list(CONSENT_SCOPES), "resources": [RESOURCE], "oauth2Flow": "USER_FEDERATION", "forceAuthentication": True,
        "resourceOauth2ReturnUrl": config.return_url, "customState": "random-bound-state",
    })
    lifecycle.complete("owner-a", session["session_uri"])
    assert core.calls[2] == ("complete", {"userIdentifier": {"userId": "owner-a"}, "sessionUri": "identity-session"})
    assert lifecycle.with_access_token("owner-a", lambda token: {"observed": token == "ephemeral-sentinel"}) == {"observed": True}
    assert adapter.calls[1][1]["workloadIdentityToken"] == "adapter-workload"
    assert "forceAuthentication" not in adapter.calls[1][1]
    assert not hasattr(lifecycle, "access_token")


@pytest.mark.parametrize("scopes", [(), ("read:jira-work", "read:me", "offline_access"), READ_SCOPES + ("read:me",)])
def test_reject_legacy_or_incomplete_scope_sets(scopes):
    with pytest.raises(ValueError):
        IdentityConfig("probe-user", "probe-atlassian-mcp", "https://adp.example.test/return", scopes)


def test_incomplete_identity_session_and_unproven_revocation_fail_closed():
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    lifecycle = IdentityLifecycle(configured_identity(), core, adapter)
    core.session_status = "FAILED"
    with pytest.raises(ProbeBlocked):
        lifecycle.start("owner-a", "state")
    adapter.session_status = "FAILED"
    with pytest.raises(ProbeBlocked):
        lifecycle.with_access_token("owner-a", lambda token: None)
    with pytest.raises(ProbeBlocked, match="No supported per-user"):
        lifecycle.revoke("owner-a")


def test_probe_rejects_shared_authority_or_insecure_return_url():
    with pytest.raises(ValueError):
        IdentityConfig("probe-user", "probe-mcp", "http://adp.example.test/return")
    client = IdentityFixture("same-role")
    with pytest.raises(ValueError):
        IdentityLifecycle(IdentityConfig("probe-user", "probe-mcp", "https://adp.example.test/return"), client, client)


def test_v2_metadata_accepts_identity_site_and_managed_refresh_scopes():
    config = configured_identity()
    assert set(IDENTITY_SCOPES) <= set(config.scopes)
    assert config.resource == RESOURCE and config.issuer == TEST_ISSUER
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    lifecycle = IdentityLifecycle(config, core, adapter)
    lifecycle.start("owner-a", "state")
    assert set(IDENTITY_SCOPES) <= set(core.calls[1][1]["scopes"])
    assert "read:jira-work" not in core.calls[1][1]["scopes"]
    lifecycle.with_access_token("owner-a", lambda token: token == "ephemeral-sentinel")
    assert set(IDENTITY_SCOPES) <= set(adapter.calls[1][1]["scopes"])


@pytest.mark.parametrize("scopes", [
    ("read:jira-work", "read:me", "offline_access"),
    READ_SCOPES + ("read:me", "read:account"),
    CONSENT_SCOPES + ("read:jira-work",),
    CONSENT_SCOPES + ("delete:jira:agent-interface",),
])
def test_legacy_only_missing_refresh_or_extra_scope_is_not_v2(scopes):
    with pytest.raises(ValueError):
        configured_identity(scopes)


def test_issuer_resource_or_pkce_drift_blocks_v2_metadata():
    resource = {"resource": RESOURCE, "authorization_servers": [TEST_ISSUER], "scopes_supported": list(CONSENT_SCOPES)}
    issuer = {"issuer": TEST_ISSUER, "code_challenge_methods_supported": ["S256"], "grant_types_supported": ["authorization_code", "refresh_token"]}
    for resource_drift, issuer_drift in [
        ({**resource, "resource": "https://api.atlassian.com/legacy"}, issuer),
        (resource, {**issuer, "issuer": "https://auth.atlassian.com/other-issuer"}),
        (resource, {**issuer, "code_challenge_methods_supported": ["plain"]}),
        (resource, {**issuer, "grant_types_supported": ["authorization_code"]}),
    ]:
        with pytest.raises(ValueError):
            IdentityConfig.from_metadata("probe", "mcp", "https://adp.example.test/return", resource_drift, issuer_drift)
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    with pytest.raises(ProbeBlocked, match="must be pinned"):
        IdentityLifecycle(IdentityConfig("probe", "mcp", "https://adp.example.test/return"), core, adapter).start("owner-a", "state")
    assert not core.calls


def test_sdk_model_accepts_exact_mcp_resource_on_both_identity_requests():
    from botocore.session import get_session
    from botocore.validate import ParamValidator

    shape = get_session().get_service_model("bedrock-agentcore").operation_model("GetResourceOauth2Token").input_shape
    assert {"resources", "customState", "customParameters"} <= set(shape.members)
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    lifecycle = IdentityLifecycle(configured_identity(), core, adapter)
    lifecycle.start("owner-a", "bound-state")
    lifecycle.with_access_token("owner-a", lambda token: token == "ephemeral-sentinel")
    for client in (core, adapter):
        params = client.calls[1][1]
        assert params["resources"] == [RESOURCE]
        assert not ParamValidator().validate(params, shape).has_errors()
    assert core.calls[1][1]["customState"] == "bound-state"
    assert "customState" not in adapter.calls[1][1]
