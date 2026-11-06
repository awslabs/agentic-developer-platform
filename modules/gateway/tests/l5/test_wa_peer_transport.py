"""Gateway peer calls use IAM-authenticated HTTP without a Lambda environment."""

from types import SimpleNamespace

import httpx
import pytest
from botocore.credentials import Credentials

from src.l5_backend_well_architected import PeerTransport


@pytest.fixture
def peer_http(monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setenv("L5_GATEWAY_URL", "https://exampleapi.execute-api.us-east-1.amazonaws.com/dev")
    requests, clients = [], []

    def respond(request):
        requests.append(request)
        return httpx.Response(404, json={"code": "peer_not_installed"})

    original = httpx.AsyncClient

    def client(**kwargs):
        result = original(**kwargs, transport=httpx.MockTransport(respond))
        clients.append(result)
        return result

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr("boto3.Session", lambda: SimpleNamespace(get_credentials=lambda: Credentials("fictional", "fictional", "fictional")))
    return requests, clients


async def test_peer_transport_signs_get_and_post_without_lambda(peer_http):
    requests, clients = peer_http
    transport = PeerTransport()
    headers = {"X-L5-Delegation-ID": "delegation-example"}
    path = "/internal/v1/context/peer-example"
    response = await transport.get(path, headers=headers, follow_redirects=False)
    assert response.status_code == 404
    response = await transport.post(path, json={"task_id": "task-example"}, headers=headers, follow_redirects=False)
    assert response.status_code == 404
    assert [request.method for request in requests] == ["GET", "POST"]
    for request in requests:
        assert str(request.url) == "https://exampleapi.execute-api.us-east-1.amazonaws.com/dev" + path
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "/us-east-1/execute-api/aws4_request" in request.headers["Authorization"]
        assert request.headers["X-Amz-Security-Token"] == "fictional"
        assert request.headers["X-L5-Delegation-ID"] == "delegation-example"
    assert all(client.is_closed for client in clients)


async def test_peer_transport_never_falls_back_to_unsigned_http(peer_http, monkeypatch):
    requests, clients = peer_http
    monkeypatch.setattr("boto3.Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    with pytest.raises(RuntimeError, match="credentials unavailable"):
        await PeerTransport().get("/internal/v1/context/peer-example")
    assert requests == []
    assert all(client.is_closed for client in clients)
