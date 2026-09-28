"""A shared worker identity cannot act as the trusted ingress producer."""

import base64
import json

import httpx
import pytest
from fastapi import HTTPException

from src.agentauth.work_routes import verify_producer

ROLE = "arn:aws:iam::123456789012:role/webhook"


def proof(invocation="run-1", *, signed=True):
    headers = {
        "authorization": "AWS4-HMAC-SHA256 Credential=example, SignedHeaders=host;x-amz-date"
        + (";x-adp-work-invocation" if signed else "")
        + ", Signature=example",
        "x-amz-date": "20260915T120000Z",
        "content-type": "application/x-www-form-urlencoded",
        "x-adp-work-invocation": invocation,
    }
    return base64.b64encode(json.dumps(headers).encode()).decode()


@pytest.fixture
def sts(monkeypatch):
    monkeypatch.setenv("ADP_WORK_CLAIM_PRODUCER_ROLES", ROLE)
    state = {"role": "webhook", "status": 200, "requests": []}

    def handler(request):
        state["requests"].append(request)
        assert request.url.host == "sts.us-east-1.amazonaws.com"
        assert request.content == b"Action=GetCallerIdentity&Version=2011-06-15"
        return httpx.Response(
            state["status"],
            text=(
                '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
                f"<GetCallerIdentityResult><Arn>arn:aws:sts::123456789012:assumed-role/{state['role']}/session</Arn>"
                "</GetCallerIdentityResult></GetCallerIdentityResponse>"
            ),
        )

    original = httpx.AsyncClient
    monkeypatch.setattr("src.agentauth.work_routes.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    return state


async def test_producer_proof_is_verified_by_sts(sts):
    await verify_producer(proof(), "run-1")
    assert len(sts["requests"]) == 1


async def test_worker_cannot_use_valid_aws_credentials_to_admit_others(sts):
    sts["role"] = "worker"
    with pytest.raises(HTTPException) as failure:
        await verify_producer(proof(), "run-1")
    assert failure.value.status_code == 403


@pytest.mark.parametrize("token", [proof("another-run"), proof(signed=False), "not-base64"])
async def test_proof_cannot_be_rebound_to_another_invocation(sts, token):
    with pytest.raises(HTTPException):
        await verify_producer(token, "run-1")
    assert sts["requests"] == []


async def test_invalid_signature_is_refused(sts):
    sts["status"] = 403
    with pytest.raises(HTTPException):
        await verify_producer(proof(), "run-1")


async def test_missing_allowlist_never_accepts_producer(sts, monkeypatch):
    monkeypatch.delenv("ADP_WORK_CLAIM_PRODUCER_ROLES")
    with pytest.raises(HTTPException):
        await verify_producer(proof(), "run-1")
    assert sts["requests"] == []


@pytest.fixture
def admission_http(sts, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.agentauth import work_routes

    runtime = SimpleNamespace(store=object())
    admission = AsyncMock(return_value={"disposition": "admitted", "invocation_id": "run-1"})
    monkeypatch.setattr(work_routes, "admit_pending", admission)
    app = FastAPI()
    app.include_router(work_routes.router)
    app.dependency_overrides[work_routes.get_agent_runtime] = lambda: runtime
    with TestClient(app) as client:
        yield client, admission, runtime


def test_http_producer_admits_only_protected_invocation(admission_http):
    client, admission, runtime = admission_http
    response = client.post("/internal/v1/agent/work/admit", json={"invocation_id": "run-1"}, headers={"X-Adp-Producer-Proof": proof()})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    admission.assert_awaited_once_with(runtime.store, "run-1")


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Api-Key": "shared-key"}, {"X-Adp-Role-Arn": ROLE}])
def test_http_shared_key_or_role_header_never_authenticates(admission_http, headers):
    client, admission, _ = admission_http
    assert client.post("/internal/v1/agent/work/admit", json={"invocation_id": "run-1"}, headers=headers).status_code == 403
    admission.assert_not_awaited()


@pytest.mark.parametrize("field", ["org_id", "owner_ref", "issue_number", "generation", "force_handover"])
def test_http_producer_cannot_choose_ownership_fields(admission_http, field):
    client, admission, _ = admission_http
    response = client.post(
        "/internal/v1/agent/work/admit", json={"invocation_id": "run-1", field: "untrusted"}, headers={"X-Adp-Producer-Proof": proof()}
    )
    assert response.status_code == 422
    admission.assert_not_awaited()


def test_http_conflict_has_stable_refusal(admission_http):
    from src.orchestration.work_claims import WorkClaimError

    client, admission, _ = admission_http
    admission.side_effect = WorkClaimError("held_by_other_owner", "private details")
    response = client.post("/internal/v1/agent/work/admit", json={"invocation_id": "run-1"}, headers={"X-Adp-Producer-Proof": proof()})
    assert response.status_code == 409
    assert response.json() == {"detail": "work ownership refused"}
