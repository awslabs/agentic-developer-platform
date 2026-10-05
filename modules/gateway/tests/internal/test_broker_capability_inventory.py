"""Actual IAM registry and broker decisions for every user credential operation."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from src.agentauth.grants import AuthorityReference, DelegatedGrant
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.credential_authorization import require_broker_capability
from src.shared.config import Settings

OPERATIONS = [
    ("GET", "/internal/v1/user-credentials", "credential:list"),
    ("POST", "/internal/v1/proxy-request", "credential:proxy"),
    ("POST", "/internal/v1/credential-assume-role", "credential:assume-role"),
    ("POST", "/internal/v1/worker-task-credentials", "credential:task-session"),
    ("POST", "/internal/v1/credential-raw-read", "credential:raw-read"),
    ("POST", "/internal/v1/credential-materialize", "credential:materialize"),
]


@pytest.fixture
def boundary(monkeypatch):
    settings = Settings(trust_apigw_headers=True, apigw_provenance_secret="offline-edge", webhook_events_table="offline-events")
    for module in ["src.internal.auth_deps", "src.auth.middleware", "src.shared.config"]:
        monkeypatch.setattr(module + ".get_settings", lambda: settings)
    registry = MagicMock()
    entry = {
        "agent_id": "worker",
        "agent_name": "worker",
        "org_id": "__platform__",
        "team_id": "__agents__",
        "scope": "internal",
        "credential_scopes": [],
    }
    registry.get_agent_by_role_arn.return_value = entry
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: registry)
    grant = DelegatedGrant(
        grant_id="grant",
        tenant_id="tenant",
        principal="owned-run#1",
        authority=AuthorityReference("github_event", "event", "owner", "tenant"),
        allowed_actions=frozenset(),
        flow_id="flow",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    event = MagicMock()
    event.get_item.return_value = {"Item": {"authorized_user_id": {"S": "owner"}}}
    runtime = SimpleNamespace(
        authenticate=MagicMock(
            return_value=(None, SimpleNamespace(invocation_id="owned-run", tenant_id="tenant", principal="owned-run#1"), None, grant)
        ),
        validate_flow=AsyncMock(),
        store=SimpleNamespace(_read=MagicMock(return_value={"arrived_at": {"S": "origin-time"}}), client=event),
    )
    monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)

    class Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: Session)
    effect = MagicMock()
    app = FastAPI()

    async def endpoint(_: None = Depends(verify_internal_or_irsa)):
        effect()
        return {"authorized": True}

    for method, path, _ in OPERATIONS:
        app.add_api_route(path, endpoint, methods=[method])
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, entry=entry, registry=registry, runtime=runtime, event=event, effect=effect)


def call(boundary, method, path, **changes):
    body = {"invocation_id": "owned-run", "user_id": "owner", "credential_scopes": [scope for _, _, scope in OPERATIONS], **changes}
    headers = {
        "X-Caller-Identity": "arn:aws:sts::123456789012:assumed-role/worker/session",
        "X-Adp-Edge-Provenance": "offline-edge",
        "X-Adp-Run-Credential": "fixture-proof",
        "X-Adp-Workload-Token": "fixture-pod",
        "X-Agent-Scopes": " ".join(scope for _, _, scope in OPERATIONS),
    }
    return boundary.client.request(method, path, headers=headers, **({"params": body} if method == "GET" else {"json": body}))


@pytest.mark.parametrize("method,path,scope", OPERATIONS)
@pytest.mark.parametrize("grant_kind", ["missing", "wrong", "exact"])
def test_registry_permission_required_for_valid_owned_run(boundary, method, path, scope, grant_kind):
    boundary.entry["credential_scopes"] = [scope] if grant_kind == "exact" else ["credential:unrelated"] if grant_kind == "wrong" else []
    response = call(boundary, method, path)
    assert response.status_code == (200 if grant_kind == "exact" else 403), response.text
    boundary.registry.get_agent_by_role_arn.assert_called_once_with("arn:aws:iam::123456789012:role/worker")
    if grant_kind == "exact":
        boundary.effect.assert_called_once()
        assert boundary.event.get_item.call_args.kwargs["Key"] == {"event_id": {"S": "owned-run"}, "arrived_at": {"S": "origin-time"}}
    else:
        boundary.effect.assert_not_called()
        assert response.json()["detail"]["error"] == "insufficient_scope"


@pytest.mark.parametrize("method,path,scope", OPERATIONS)
@pytest.mark.parametrize("mutation", ["run", "user", "owner", "tenant", "missing-authority"])
def test_capability_cannot_replace_run_owner_tenant(boundary, method, path, scope, mutation):
    from dataclasses import replace

    from src.agentauth.bootstrap import BootstrapRefusedError

    boundary.entry["credential_scopes"] = [scope]
    changes = {}
    if mutation in {"run", "user"}:
        changes["invocation_id" if mutation == "run" else "user_id"] = "foreign"
    elif mutation == "owner":
        boundary.event.get_item.return_value = {"Item": {"authorized_user_id": {"S": "foreign"}}}
    elif mutation == "tenant":
        context = boundary.runtime.authenticate.return_value
        boundary.runtime.authenticate.return_value = (
            *context[:3],
            replace(context[3], authority=AuthorityReference("github_event", "event", "owner", "foreign")),
        )
    else:
        boundary.runtime.authenticate.side_effect = BootstrapRefusedError("missing proof")
    assert call(boundary, method, path, **changes).status_code == 404
    boundary.effect.assert_not_called()


def test_unmapped_broker_cannot_inherit_permission():
    request = Request({"type": "http", "method": "POST", "path": "/internal/v1/new-credential-delivery", "headers": [], "query_string": b""})
    request.state.token_context = SimpleNamespace(user_id="worker", credential_scopes=[scope for _, _, scope in OPERATIONS])
    with pytest.raises(HTTPException) as error:
        require_broker_capability(request)
    assert error.value.status_code == 403


def test_all_user_credential_routes_require_explicit_inventory():
    from src.internal.assume_role_routes import router as assume
    from src.internal.credential_authorization import BROKER_CAPABILITIES
    from src.internal.credential_routes import router as credentials
    from src.internal.task_credentials import router as tasks

    paths = {route.path for router in (assume, credentials, tasks) for route in router.routes}
    assert paths == set(BROKER_CAPABILITIES)


def test_removed_owner_flag_cannot_restore_shadow_binding(monkeypatch):
    monkeypatch.setenv("BG_ENFORCE_CREDENTIAL_BINDING", "false")
    assert "enforce_credential_binding" not in Settings.model_fields
    from src.internal.credential_binding import resolve_credential_binding

    with pytest.raises(HTTPException):
        resolve_credential_binding(invocation_id="claimed-run", body_user_id="claimed-user", settings=Settings())
