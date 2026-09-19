"""Own-run service identities cannot be selected by the coding worker."""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.agentauth.execution import ExecutionRecord, ExecutionStateError, ExecutionStatus
from src.agentauth.grants import AuthorityReference, DelegatedGrant
from src.agentauth.routes import get_agent_runtime, require_agent_transport
from src.agentauth.run_services import MARKER_KEY_ENV, router
from src.agentauth.store import AuthorityStoreError

KEY = "gateway-only-marker-key-for-this-test"
RECORD = ExecutionRecord(
    invocation_id="run-one",
    tenant_id="tenant-one",
    current_attempt=1,
    status=ExecutionStatus.ACTIVE,
    current_credential_epoch=1,
    min_acceptable_credential_epoch=1,
    workload_binding="pod-one",
    flow_id="flow-one",
)
GRANT = DelegatedGrant(
    grant_id="grant-one",
    tenant_id="tenant-one",
    principal=RECORD.principal,
    authority=AuthorityReference(kind="github_event", reference_id="decision-one", human_id="human-one", org_id="tenant-one"),
    allowed_actions=frozenset(),
    max_chain_depth=4,
    flow_id="flow-one",
)
HEADERS = {"X-Adp-Run-Credential": "own-run-proof", "X-Adp-Workload-Token": "own-pod-proof"}


@pytest.fixture
async def service():
    context = (SimpleNamespace(uid="pod-one"), "verified-caller", RECORD, GRANT)
    runtime = SimpleNamespace(
        env={MARKER_KEY_ENV: KEY},
        authenticate=Mock(return_value=context),
        validate_flow=AsyncMock(),
        store=SimpleNamespace(_read=Mock(return_value={"chain_depth": {"N": "2"}})),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    app.dependency_overrides[require_agent_transport] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield client, runtime


async def test_marker_is_signed_only_for_protected_identity(service):
    client, runtime = service
    response = await client.post("/internal/v1/agent/self/marker", json={}, headers=HEADERS)
    assert response.status_code == 200
    fields = response.json()
    assert fields == {
        "correlation_id": "flow-one",
        "root_human_id": "human-one",
        "is_human_rooted": "true",
        "invocation_id": "run-one",
        "chain_depth": "2",
        "signature": base64.urlsafe_b64encode(hmac.new(KEY.encode(), b"flow-one:human-one:true:run-one:2", hashlib.sha256).digest())
        .rstrip(b"=")
        .decode(),
    }
    assert KEY not in response.text
    assert response.headers["cache-control"] == "no-store"
    runtime.store._read.assert_called_once_with("TENANT#tenant-one", "EXEC#run-one")
    assert all(call.args == ("own-run-proof", "own-pod-proof") for call in runtime.authenticate.call_args_list)


@pytest.mark.parametrize(
    "field", ["root_human_id", "tenant_id", "invocation_id", "correlation_id", "chain_depth", "signing_input", "key", "signature"]
)
async def test_worker_cannot_request_another_identity_or_signing_input(service, field):
    client, runtime = service
    response = await client.post("/internal/v1/agent/self/marker", json={field: "other-user"}, headers=HEADERS)
    assert response.status_code == 422
    runtime.store._read.assert_not_called()


@pytest.mark.parametrize("missing", list(HEADERS))
async def test_both_proofs_required(service, missing):
    client, runtime = service
    response = await client.post("/internal/v1/agent/self/marker", json={}, headers={k: v for k, v in HEADERS.items() if k != missing})
    assert response.status_code == 404
    runtime.authenticate.assert_not_called()


async def test_expiry_during_flow_read_refuses_before_signing(service):
    client, runtime = service
    original = runtime.authenticate.return_value
    runtime.authenticate.side_effect = [original, ExecutionStateError("expired")]
    response = await client.post("/internal/v1/agent/self/marker", json={}, headers=HEADERS)
    assert response.status_code == 404
    runtime.store._read.assert_not_called()


async def test_changed_grant_after_protected_read_cannot_sign(service):
    client, runtime = service
    original = runtime.authenticate.return_value
    narrowed = (*original[:3], replace(GRANT, revocation_epoch=2))
    runtime.authenticate.side_effect = [original, original, narrowed, narrowed]
    response = await client.post("/internal/v1/agent/self/marker", json={}, headers=HEADERS)
    assert response.status_code == 404
    assert "signature" not in response.json()


@pytest.mark.parametrize("raw_depth", [None, "", "-1", "101", "2.0", "secret"])
async def test_missing_or_invalid_protected_depth_is_not_inferred(service, raw_depth):
    client, runtime = service
    runtime.store._read.return_value = {"chain_depth": {"N": raw_depth}}
    response = await client.post("/internal/v1/agent/self/marker", json={}, headers=HEADERS)
    assert response.status_code == 503


async def test_store_failure_refuses_without_key_disclosure(service):
    client, runtime = service
    runtime.store._read.side_effect = AuthorityStoreError("internal sensitive detail")
    response = await client.post("/internal/v1/agent/self/marker", json={}, headers=HEADERS)
    assert response.status_code == 503
    assert "sensitive" not in response.text and KEY not in response.text


async def test_missing_service_key_is_unavailable(service):
    client, runtime = service
    runtime.env.clear()
    response = await client.post("/internal/v1/agent/self/marker", json={}, headers=HEADERS)
    assert response.status_code == 503
