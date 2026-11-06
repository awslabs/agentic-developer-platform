"""Synthetic JWT-session and Identity callbacks: no live consent claim."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from tests.e2e.tools import atlassian_listener
from tests.e2e.tools.atlassian_contract import load_contract
from tests.e2e.tools.atlassian_oauth_probe import IdentityLifecycle, ProbeBlocked, proof_capability_contract
from tests.e2e.tools.atlassian_probe_ledger import ProbeLedger
from tests.e2e.tools.test_atlassian_capability_procedure import inputs
from tests.e2e.tools.test_atlassian_catalogue import Pages
from tests.e2e.tools.test_atlassian_oauth_probe import (
    IdentityFixture,
    configured_identity,
)


@pytest.fixture
def listener(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "test-secret-key-do-not-use-in-production")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-do-not-use-in-production")
    from src.auth.middleware import get_current_user_context
    from src.shared.database import get_db
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    config = replace(configured_identity(), return_url="https://adp.example.test/auth/probes/atlassian/return")
    ledger = ProbeLedger(tmp_path / "attempt.json", IdentityLifecycle(config, core, adapter))

    async def authorized_user(_db, subject, tenant, **_kwargs):
        if subject == "foreign" or tenant == "invalid":
            return None
        return SimpleNamespace(id=subject, user_kind="human", is_shadow=False)

    async def session(request: Request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not token:
            raise HTTPException(401, detail={"error": "missing_token"})
        values = {
            "owner": ("owner", "tenant-a", "human", "jwt"),
            "other": ("other", "tenant-a", "human", "jwt"),
            "tenant": ("owner", "tenant-b", "human", "jwt"),
            "service": ("owner", "tenant-a", "service", "jwt"),
            "iam": ("owner", "tenant-a", "human", "iam"),
            "invalid": ("foreign", "invalid", "human", "jwt"),
        }
        if token not in values:
            raise HTTPException(401, detail={"error": "invalid_token"})
        user, tenant, kind, source = values[token]
        return SimpleNamespace(user_id=user, org_id=tenant, account_type=kind,
                               auth_source=source, cognito_username="")

    from src.shared.identity import workspaces
    monkeypatch.setattr(workspaces, "workspace_user", authorized_user)
    app = atlassian_listener.create_probe_app(ledger)
    app.dependency_overrides[get_current_user_context] = session
    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app) as client:
        yield client, ledger, core, adapter


def signed(client, path, token="owner", *, method="post", **kwargs):
    return getattr(client, method)(path, headers={"Authorization": "Bearer " + token}, **kwargs)


def test_return_uses_authenticated_owner_tenant_and_server_session(listener):
    client, ledger, core, adapter = listener
    started = signed(client, "/auth/probes/atlassian/start")
    assert started.status_code == 200
    state = started.json()["state"]
    assert ledger.status("owner", tenant="tenant-a") == "consent_pending"
    assert json.loads(ledger.path.read_text())["tenant"] == "tenant-a"
    assert state not in ledger.path.read_text()
    assert client.get("/auth/probes/atlassian/return", params={"state": state}).status_code == 401
    assert signed(client, "/auth/probes/atlassian/return", "other", method="get", params={"state": state}).status_code == 403
    assert signed(client, "/auth/probes/atlassian/return", "tenant", method="get", params={"state": state}).status_code == 403
    assert signed(client, "/auth/probes/atlassian/return", method="get", params={"state": "wrong"}).status_code == 403
    assert core.calls[-1][0] == "resource"
    finished = signed(client, "/auth/probes/atlassian/return", method="get", params={"state": state})
    assert finished.status_code == 200 and finished.json() == {"status": "identity_completion_requested"}
    assert core.calls[-1] == ("complete", {"userIdentifier": {"userId": "owner"}, "sessionUri": "identity-session"})
    assert signed(client, "/auth/probes/atlassian/return", json={"state": state}).status_code == 403
    assert ledger.status("owner", tenant="tenant-a") == "completed"
    with pytest.raises(ProbeBlocked, match="authorize"):
        ledger.observe("owner", lambda token: token, tenant="tenant-b")
    assert not adapter.calls


def test_catalogue_after_authenticated_consent_preserves_tenant(listener):
    client, ledger, _, adapter = listener
    started = signed(client, "/auth/probes/atlassian/start")
    assert started.status_code == 200
    finished = signed(client, "/auth/probes/atlassian/return", json={"state": started.json()["state"]})
    assert finished.status_code == 200
    assert ledger.status("owner", tenant="tenant-a") == "completed"
    contract = load_contract()
    pages = Pages(contract)
    params = {**inputs(contract, pages), "owner": "owner"}

    with pytest.raises(ProbeBlocked, match="authorize"):
        proof_capability_contract(ledger, **params)
    with pytest.raises(ProbeBlocked, match="authorize"):
        proof_capability_contract(ledger, **params, tenant="tenant-b")
    assert not adapter.calls and not pages.calls

    report = proof_capability_contract(ledger, **params, tenant="tenant-a")
    assert report["controlled_status"] == "pass" and report["live_status"] == "unproven"
    assert report["tool_count"] == 8 and len(report["schema_sha256"]) == 8
    assert [call[0] for call in adapter.calls] == ["workload", "resource"]
    assert [call[0] for call in pages.calls] == ["initialize", "notifications/initialized", "tools/list", "tools/list"]
    assert all(call[2] == "ephemeral-sentinel" for call in pages.calls)
    assert "ephemeral-sentinel" not in str(report) and "ephemeral-sentinel" not in ledger.path.read_text()


def test_post_return_denies_forged_owner_tenant_session_and_bad_auth(listener):
    client, ledger, core, _ = listener
    state = signed(client, "/auth/probes/atlassian/start").json()["state"]
    route = "/auth/probes/atlassian/return"
    for token in ("other", "tenant", "invalid", "service", "iam"):
        assert signed(client, route, token, json={"state": state}).status_code == 403
    assert client.post(route, json={"state": state}).status_code == 401
    assert signed(client, route, "unknown", json={"state": state}).status_code == 401
    assert signed(client, route, json={"state": state, "session_uri": "forged"}).status_code == 422
    assert signed(client, route, json={"state": state, "owner": "other", "tenant": "tenant-a"}).status_code == 422
    assert signed(client, route, method="get", params={"state": state, "code": "do-not-exchange"}).status_code == 403
    assert core.calls[-1][0] == "resource"
    assert signed(client, route, json={"state": state}).status_code == 200
    assert ledger.status("owner", tenant="tenant-a") == "completed"
    with pytest.raises(ProbeBlocked, match="authorize"):
        ledger.observe("owner", lambda token: token, tenant="tenant-b")


def test_lost_sdk_completion_reply_does_not_replay_or_record_token(listener):
    client, ledger, core, _ = listener
    state = signed(client, "/auth/probes/atlassian/start").json()["state"]
    attempts = 0

    def lost(**_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("token-sentinel-must-not-escape")

    core.complete_resource_token_auth = lost
    route = "/auth/probes/atlassian/return"
    failed = signed(client, route, json={"state": state})
    assert failed.status_code == 502 and "token-sentinel" not in failed.text
    assert ledger.status("owner", tenant="tenant-a") == "completing"
    assert signed(client, route, json={"state": state}).status_code == 403
    assert attempts == 1 and "token-sentinel" not in ledger.path.read_text()


def test_listener_refuses_return_url_without_its_authenticated_endpoint(tmp_path):
    core, adapter = IdentityFixture("core"), IdentityFixture("adapter")
    ledger = ProbeLedger(tmp_path / "attempt.json", IdentityLifecycle(configured_identity(), core, adapter))
    with pytest.raises(ValueError):
        atlassian_listener.create_probe_app(ledger)


def test_listener_routes_require_adp_jwt_dependency(listener):
    from src.auth.middleware import get_current_user_context

    client, _, _, _ = listener
    routes = [route for route in client.app.routes if route.path.startswith("/auth/probes/atlassian")]
    assert {route.path for route in routes} == {
        "/auth/probes/atlassian/start", "/auth/probes/atlassian/return",
    }
    for route in routes:
        dependencies = list(route.dependant.dependencies)
        assert dependencies
        assert all(get_current_user_context in [child.call for child in dep.dependencies]
                   for dep in dependencies if dep.call.__name__ == "principal")
