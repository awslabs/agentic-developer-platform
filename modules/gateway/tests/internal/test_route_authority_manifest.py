"""Mounted internal verifier inventory and vault helper authority boundary."""

from __future__ import annotations

import asyncio
import inspect
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from src.app import UNIT_MODULES, create_app
from src.auth.vault_delivery import DeliveryRefusedError, revocation_state
from src.auth.vault_evidence import read_credential_evidence
from src.crew.checkpoint_routes import helper_identity, restore_identity
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.route_authority_manifest import INLINE_VERIFIERS, classify_internal_route
from src.internal.tool_adapter_authority import router as tool_adapter_router
from src.internal.vault_evidence_routes import EvidenceBody
from tests.internal.test_vault_evidence_routes import engine, metadata_client, session_factory, sm  # noqa: F401


def test_adapter_observation_authorize_has_registered_iam_verifier():
    route = next(route for route in tool_adapter_router.routes if route.path.endswith("/authorize"))
    assert classify_internal_route(route).verifier == "registered_iam"


@pytest.mark.parametrize("indexing_enabled", [False, True])
def test_mounted_internal_routes_have_named_verifiers(monkeypatch, indexing_enabled):
    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root / "modules/agent-context"))
    monkeypatch.syspath_prepend(str(root / "modules/L5-services/src"))
    monkeypatch.setenv("AGENT_CONTEXT_ENABLED", "true" if indexing_enabled else "false")
    available = []
    for module_path in UNIT_MODULES:
        try:
            available.append(import_module(module_path))
        except ImportError as error:
            assert module_path == "src.pool.routes" and error.name == module_path
    app = create_app()
    mounted = [route for route in app.routes if isinstance(route, APIRoute)]
    internal = [route for route in mounted if route.path.startswith("/internal/")]
    assert len(internal) >= 138
    actual = {(route.path, frozenset(route.methods), route.endpoint) for route in mounted}
    for module in available:
        if isinstance(getattr(module, "router", None), APIRouter):
            for route in module.router.routes:
                if isinstance(route, APIRoute) and route.path.startswith("/internal/"):
                    assert (route.path, frozenset(route.methods), route.endpoint) in actual, route.path
    if indexing_enabled:
        from agent_context.api.indexing_router import router as indexing_router

        assert all((route.path, frozenset(route.methods), route.endpoint) in actual for route in indexing_router.routes)
    else:
        assert not any(route.path.startswith("/admin/indexing") for route in mounted)
    covered = {(route.path, frozenset(route.methods)): classify_internal_route(route) for route in internal}
    assert len(covered) == len(internal)
    assert set(INLINE_VERIFIERS) <= {route.path for route in internal}
    assert covered[("/internal/v1/credential-evidence", frozenset({"POST"}))].tenant_boundary == "verified_vault_metadata"
    assert covered[("/internal/v1/credential-revocation-state", frozenset({"POST"}))].tenant_boundary == "verified_vault_metadata"


def test_new_internal_routes_and_metadata_helpers_cannot_rely_on_request_tenant():
    app = FastAPI()

    @app.post("/internal/v1/new-tenant-metadata")
    async def unguarded_metadata(body: EvidenceBody, _: None = Depends(verify_internal_or_irsa)):
        return {"org_id": body.org_id}

    @app.post("/internal/v1/new-unclassified")
    async def unclassified():
        return {"allowed": True}

    async def fake_transport():
        return None

    fake_transport.__name__ = "verify_internal_or_irsa"

    @app.get("/internal/v1/forged-verifier")
    async def forged_verifier(_: None = Depends(fake_transport)):
        return {"allowed": True}

    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/internal/"):
            with pytest.raises(AssertionError, match="authority missing|without named verifier"):
                classify_internal_route(route)

    session = AsyncMock()
    secrets = MagicMock()
    evidence = asyncio.run(
        read_credential_evidence(
            session,
            secrets,
            authority=None,
            credential_id="credential",
            service=None,
            label=None,
            report_digest=None,
        )
    )
    assert evidence is None
    with pytest.raises(DeliveryRefusedError):
        asyncio.run(revocation_state(session, authority=None, credential_id="credential"))
    session.execute.assert_not_awaited()
    session.scalar.assert_not_awaited()
    secrets.current_version_id.assert_not_called()


def test_mounted_metadata_route_denies_before_reader_without_verified_tenant(metadata_client):  # noqa: F811
    client, state = metadata_client
    state["tenant"] = None
    with patch("src.internal.vault_evidence_routes.read_credential_evidence", new_callable=AsyncMock) as reader:
        response = client.post(
            "/internal/v1/credential-evidence",
            json={
                "org_id": "org-acme",
                "workspace_id": "ws-1",
                "credential_id": "cred-1",
                "principal": "service:metadata-reader",
            },
        )
    assert response.status_code == 403
    assert response.json() == {"detail": {"error": "denied", "message": "credential evidence could not be established"}}
    reader.assert_not_awaited()


@pytest.mark.parametrize("path,handler", sorted(INLINE_VERIFIERS.items()))
def test_named_inline_route_refuses_absent_independent_proof(path, handler, monkeypatch):
    module_name, _, endpoint_name = handler[0].rpartition(".")
    module = import_module(module_name)
    endpoint = getattr(module, endpoint_name)
    body = MagicMock()
    body.model_dump.return_value = {"run_id": "offline-run", "invocation_id": "offline-run"}
    body.status = "indexing"
    body.asset_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    body.callback_grant = "invalid-grant"
    request = SimpleNamespace(headers={}, body=AsyncMock(return_value=b"{}"))
    workloads = (
        SimpleNamespace(verify=MagicMock(side_effect=module.WorkloadRefusedError("refused"))) if path.endswith("chat/model-decision") else None
    )
    runtime = SimpleNamespace(
        env={
            "ADP_TASK_API_RECOVERY_ENABLED": "true",
            "ADP_TASK_DISPATCH_PRODUCER_ROLES": "arn:aws:iam::111122223333:role/offline-producer",
            "ADP_TASK_RECOVERY_PRODUCER_ROLES": "arn:aws:iam::111122223333:role/offline-producer",
        },
        workloads=workloads,
    )
    store = MagicMock()
    kwargs = {
        name: body
        if name == "body"
        else request
        if name == "request"
        else runtime
        if name == "runtime"
        else "offline-run"
        if name == "run_id"
        else (MagicMock(), MagicMock())
        if name == "services"
        else store
        if name == "store"
        else MagicMock()
        for name in inspect.signature(endpoint).parameters
    }
    if path == "/internal/v1/l5/contribution-authority":

        async def stream():
            yield b"{}"

        request.stream = stream
        reader = AsyncMock()
        monkeypatch.setattr(module, "authorize_assertion", reader)
        refused = asyncio.run(endpoint(**kwargs))
        assert refused.status_code == 200
        assert refused.body == b'{"authorized":false}'
        reader.assert_not_awaited()
        return
    if path in {"/internal/v1/tools/knowledge/mcp", "/internal/v1/tools/github/mcp", "/internal/v1/tools/jira/mcp", "/internal/v1/tools/slack/mcp"}:
        request.headers = Headers({})
        with pytest.raises(HTTPException) as error:
            asyncio.run(endpoint(**kwargs))
        assert error.value.status_code == 403
        request.body.assert_not_awaited()
        assert not store.mock_calls
        return
    if path.endswith("/model-policy-keys"):
        monkeypatch.delenv("AGENT_CONTROL_ENVELOPE_KEY_ID", raising=False)
        with pytest.raises(HTTPException) as error:
            asyncio.run(endpoint(**kwargs))
        assert error.value.status_code == 503
        assert not store.mock_calls
        return
    if path.endswith("/tasks/admit"):
        monkeypatch.setenv("ADP_TASK_API_ADMISSION_ENABLED", "true")
        monkeypatch.setenv("ADP_TASK_ADMISSION_PRODUCER_ROLES", "arn:aws:iam::111122223333:role/offline-producer")
        request.scope = {"type": "http", "method": "POST", "path": path, "headers": []}
        refused = asyncio.run(endpoint(**kwargs))
        assert refused.status_code == 403
        request.body.assert_not_awaited()
        assert not store.mock_calls
        return
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint(**kwargs))
    assert error.value.status_code == (404 if path.endswith("chat/model-decision") else 403), path
    assert not store.mock_calls
    if path.endswith("chat/model-decision"):
        runtime.workloads.verify.assert_called_once_with("")


@pytest.mark.parametrize(("namespace", "operation"), [("knowledge", "search"), ("github", "get_user"), ("jira", "get_issue"), ("slack", "get_user")])
def test_mounted_private_mediator_requires_transport_before_reading_body(monkeypatch, namespace, operation):
    monkeypatch.setenv("ADP_TOOLS_KNOWLEDGE_EDGE_PROVENANCE", "fixture-private-proof")
    monkeypatch.setenv(f"ADP_TOOLS_{namespace.upper()}_MEDIATOR_ENABLED", "true")
    packet = {
        "jsonrpc": "2.0",
        "id": "request-one",
        "method": "tools/call",
        "params": {"name": operation, "arguments": {"query": "foreign-sentinel"}},
    }
    with (
        patch(f"src.internal.tools_{namespace}.bounded_body", new_callable=AsyncMock) as body,
        patch("src.agentauth.routes.get_agent_runtime") as runtime,
    ):
        client = TestClient(create_app())
        for headers in ({}, {"x-caller-identity": "arn:aws:sts::123456789012:assumed-role/fixture-tools/forged"}):
            response = client.post(f"/internal/v1/tools/{namespace}/mcp", json=packet, headers=headers)
            assert response.status_code == 403 and "foreign-sentinel" not in response.text
    body.assert_not_awaited()
    runtime.assert_not_called()


@pytest.mark.parametrize("identity_guard", [helper_identity, restore_identity])
def test_checkpoint_helper_without_bearer_refuses_before_workload_lookup(identity_guard):
    verifier = SimpleNamespace(verify=MagicMock())
    request = SimpleNamespace(headers={})
    with pytest.raises(HTTPException) as error:
        asyncio.run(identity_guard(request, verifier=verifier))
    assert error.value.status_code == 401
    assert error.value.detail == {"error": "helper_identity_invalid"}
    verifier.verify.assert_not_called()


def test_metadata_sibling_routes_refuse_missing_tenant_before_vault_helpers(metadata_client):  # noqa: F811
    client, state = metadata_client
    state["tenant"] = None
    common = {"org_id": "org-acme", "workspace_id": "ws-1", "credential_id": "cred-1"}
    evidence = {**common, "principal": "service:metadata-reader"}
    revocation = {**common, "operation_id": "op-1", "job_id": "job-1", "attempt_id": "att-1"}
    with (
        patch("src.internal.vault_evidence_routes.read_credential_evidence", new_callable=AsyncMock) as evidence_reader,
        patch("src.internal.vault_evidence_routes.revocation_state", new_callable=AsyncMock) as revocation_reader,
    ):
        evidence_response = client.post("/internal/v1/credential-evidence", json=evidence)
        revocation_response = client.post("/internal/v1/credential-revocation-state", json=revocation)
    assert evidence_response.status_code == revocation_response.status_code == 403
    assert evidence_response.json() == {"detail": {"error": "denied", "message": "credential evidence could not be established"}}
    assert revocation_response.json() == {"detail": {"error": "denied", "message": "credential delivery refused"}}
    evidence_reader.assert_not_awaited()
    revocation_reader.assert_not_awaited()
