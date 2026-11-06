"""The live evaluator signs the exact HTTPX request, including its query."""

from datetime import UTC, datetime

import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from tests.e2e.conftest import IAMSignedClient


@pytest.mark.parametrize("streaming", [False, True])
async def test_signature_matches_transmitted_stage_path_query_and_body(monkeypatch, streaming):
    import botocore.auth

    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda **kwargs: datetime(2026, 1, 1, tzinfo=UTC))
    credentials = Credentials("test-access-key", "test-secret-key", "test-session-token")
    observed = []

    async def handle(request):
        assert str(request.url) == "https://api.example.invalid/dev/agent/v1/messages?limit=1"
        assert request.content == b"{}"
        headers = dict(request.headers)
        actual = headers.pop("authorization")
        signed = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        SigV4Auth(credentials, "execute-api", "us-east-1").add_auth(signed)
        assert signed.headers["Authorization"] == actual
        observed.append(request)
        return httpx.Response(200, content=b"accepted")

    async with httpx.AsyncClient(base_url="https://api.example.invalid/dev/agent", transport=httpx.MockTransport(handle)) as inner:
        client = IAMSignedClient(inner, credentials=credentials)
        if streaming:
            async with client.stream("POST", "/v1/messages", json={}, params={"limit": 1}) as response:
                assert await response.aread() == b"accepted"
            assert response.is_closed
        else:
            response = await client.post("/v1/messages", json={}, params={"limit": 1})
            assert response.content == b"accepted"
    assert len(observed) == 1
