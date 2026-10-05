"""Default transport and canonical-state gates, without provider credential reads."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.credential_binding import BindingResult, resolve_credential_binding
from src.internal.run_identity import verified_run_identity
from src.shared.config import Settings


def request(path, body=None, headers=()):
    raw = json.dumps(body or {}).encode()

    async def receive():
        return {"type": "http.request", "body": raw}

    return Request({"type": "http", "method": "POST", "path": path, "headers": list(headers), "query_string": b""}, receive)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "credential-raw-read",
        "credential-materialize",
        "credential-assume-role",
        "github-installation-token",
        "proxy-request",
        "user-credentials",
        "provenance",
    ],
)
async def test_shared_key_cannot_select_another_run_even_with_flags_off(monkeypatch, path):
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "false")
    with pytest.raises(HTTPException) as exc:
        await verify_internal_or_irsa(request("/internal/v1/" + path), x_internal_api_key="unused-fixture-key")
    assert exc.value.status_code == 403


@pytest.mark.parametrize("enforce", [False, True])
def test_body_selected_lookup_never_substitutes_for_verified_binding(monkeypatch, enforce):
    monkeypatch.setenv("BG_ENFORCE_CREDENTIAL_BINDING", str(enforce).lower())
    monkeypatch.setenv("ENFORCE_CREDENTIAL_BINDING", str(enforce).lower())
    lookup = MagicMock(side_effect=AssertionError("must not query caller-selected event"))
    monkeypatch.setattr("src.internal.credential_binding._get_dynamodb_table", lookup)
    with pytest.raises(HTTPException):
        resolve_credential_binding(invocation_id="other-run", body_user_id="user", settings=Settings())
    lookup.assert_not_called()


@pytest.mark.parametrize("field,value", [("invocation_id", "other"), ("resolved_user_id", "other"), ("tenant_id", None), ("from_registry", False)])
def test_verified_binding_mismatch_denies(field, value):
    row = dict(resolved_user_id="user", from_registry=True, drift_detected=False, body_user_id="user", invocation_id="run", tenant_id="tenant")
    row[field] = value
    with pytest.raises(HTTPException):
        resolve_credential_binding(invocation_id="run", body_user_id="user", settings=Settings(), verified_binding=BindingResult(**row))


def test_valid_verified_binding_is_used_without_second_lookup():
    binding = BindingResult("user", True, False, "user", "run", "tenant")
    assert resolve_credential_binding(invocation_id="run", body_user_id="user", settings=Settings(), verified_binding=binding) is binding


@pytest.mark.asyncio
async def test_provenance_identity_uses_exact_authenticated_origin(monkeypatch):
    from src.agentauth import routes
    from src.shared import config

    caller = SimpleNamespace(invocation_id="owned-run", tenant_id="tenant")
    grant = SimpleNamespace(tenant_id="tenant", authority=SimpleNamespace(human_id="human", org_id="tenant"))
    client = MagicMock()
    client.get_item.return_value = {
        "Item": {
            "tenant_id": {"S": "tenant"},
            "actor_user_id": {"S": "actor"},
            "correlation_id": {"S": "owned-chain"},
            "root_human_id": {"S": "human"},
            "is_human_rooted": {"BOOL": True},
        }
    }
    runtime = SimpleNamespace(
        authenticate=MagicMock(return_value=(None, caller, None, grant)),
        validate_flow=AsyncMock(),
        store=SimpleNamespace(_read=MagicMock(return_value={"arrived_at": {"S": "server-time"}}), client=client),
    )
    monkeypatch.setattr(routes, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(config, "get_settings", lambda: Settings(webhook_events_table="fixture-events"))
    value = await verified_run_identity(request("/internal/v1/provenance", {"invocation_id": "foreign", "correlation_id": "foreign"}))
    assert value.invocation_id == "owned-run" and value.correlation_id == "owned-chain"
    assert client.get_item.call_args.kwargs["Key"] == {"event_id": {"S": "owned-run"}, "arrived_at": {"S": "server-time"}}
    assert client.get_item.call_args.kwargs["ConsistentRead"] is True
    client.get_item.return_value["Item"]["tenant_id"] = {"S": "foreign"}
    with pytest.raises(HTTPException):
        await verified_run_identity(request("/internal/v1/provenance"))
