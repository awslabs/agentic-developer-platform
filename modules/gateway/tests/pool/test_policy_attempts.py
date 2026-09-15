"""A policy reservation cannot trigger hidden SDK retries of a provider call."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from src.pool.simple_pool import SimplePoolService
from src.proxy.bedrock_enforcement import RoutingDecision
from src.proxy.service import ProxyService


@pytest.mark.parametrize("stream", [False, True])
async def test_policy_provider_failure_is_one_http_attempt_even_with_sdk_retry_environment(monkeypatch, stream):
    """Exercise botocore's real retry loop against a retryable HTTP failure."""
    import boto3

    attempts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler callback
            self.rfile.read(int(self.headers["Content-Length"]))
            attempts.append(self.path)
            body = b'{"message":"provider unavailable"}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-amzn-errortype", "ServiceUnavailableException")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = boto3.client

    def local_client(service, **kwargs):
        return original(
            service,
            **kwargs,
            endpoint_url=f"http://127.0.0.1:{server.server_port}",
            aws_access_key_id="test-key",
            aws_secret_access_key="test-secret",
        )

    monkeypatch.setattr("src.pool.simple_pool.boto3.client", local_client)
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "10")
    pool = SimplePoolService(region="us-east-1")
    normal = await pool.get_client()
    proxy = ProxyService.__new__(ProxyService)
    proxy._pool_service = pool
    bounded = await proxy._client_for_request(RoutingDecision(), SimpleNamespace(_policy_flow_target=object()))
    assert bounded is not normal
    assert await pool.get_client() is normal
    try:
        invoke = bounded.invoke_model_with_response_stream if stream else bounded.invoke_model
        with pytest.raises(ClientError):
            await invoke(modelId="anthropic.claude-sonnet-4-6", body=b"{}", contentType="application/json")
        assert len(attempts) == 1
        for client in (bounded._invoke_client, bounded._streaming_client):
            assert client.meta.config.retries["total_max_attempts"] == 1
    finally:
        for wrapper in (normal, bounded):
            wrapper._invoke_client.close()
            wrapper._streaming_client.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
