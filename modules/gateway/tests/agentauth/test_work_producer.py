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
