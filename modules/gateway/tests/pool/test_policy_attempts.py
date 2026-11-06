"""A policy reservation cannot trigger hidden SDK retries of a provider call."""

import asyncio
import logging
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
        assert bounded._http_attempts["stream" if stream else "invoke"] == 1
        for client in (bounded._invoke_client, bounded._streaming_client):
            assert client.meta.config.retries["total_max_attempts"] == 1
    finally:
        await pool.aclose()
        assert normal._closed and bounded._closed
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


async def test_cold_idle_and_concurrent_gateway_attempts_match_direct_loopback_controls(monkeypatch, caplog):
    import boto3

    caplog.set_level(logging.INFO, logger="src.pool.simple_pool")
    received = []
    payload = b"{}"
    model = "anthropic.claude-sonnet-4-6"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, body))
            response = b'{"message":"unavailable"}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("x-amzn-errortype", "ServiceUnavailableException")
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = boto3.client
    endpoint = f"http://127.0.0.1:{server.server_port}"
    identity = {"aws_access_key_id": "local-key", "aws_secret_access_key": "local-secret"}

    def local_client(service, **kwargs):
        return original(service, **kwargs, endpoint_url=endpoint, **identity)

    monkeypatch.setattr("src.pool.simple_pool.boto3.client", local_client)
    pool = SimplePoolService(region="us-east-1")
    direct = None
    try:
        gateway = await pool.get_client(single_attempt=True)
        direct = original("bedrock-runtime", region_name="us-east-1", config=gateway._invoke_client.meta.config, endpoint_url=endpoint, **identity)
        request = {"modelId": model, "body": payload, "contentType": "application/json"}

        async def gateway_attempt():
            with pytest.raises(ClientError):
                await gateway.invoke_model(**request)

        async def direct_attempt():
            with pytest.raises(ClientError):
                await asyncio.to_thread(direct.invoke_model, **request)

        await gateway_attempt()
        await direct_attempt()
        await asyncio.sleep(0.02)
        await gateway_attempt()
        await direct_attempt()
        await asyncio.gather(gateway_attempt(), gateway_attempt())
        await asyncio.gather(direct_attempt(), direct_attempt())
        assert len(received) == 8
        assert len(set(received)) == 1
        assert gateway._http_attempts["invoke"] == 4
        records = [record for record in caplog.records if record.message == "Bedrock SDK before-send attempt"]
        assert len(records) == 4
        assert {record.client_correlation_id for record in records} == {gateway._correlation_id}
        assert [record.attempt for record in records] == [1, 2, 3, 4]
        assert all("local-key" not in record.getMessage() and endpoint not in record.getMessage() for record in records)
    finally:
        if direct is not None:
            direct.close()
        await pool.aclose()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
