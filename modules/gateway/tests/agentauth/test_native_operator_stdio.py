"""Installed stdio→local HTTPS→real approval/reservation/facade/provider code.

JWT signatures, canonical SQL membership, Moto journal and Lua reservations are
real local code. AWS control, IAM/KMS and provider effects are explicit offline
substitutes. These checks make no live authorization or settlement claim.
"""

import asyncio
import base64
import json
import os
import shlex
import shutil
import socket
import ssl
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import tests
import tests.e2e
from tests.agentauth import test_native_operator_producer as producer
from tests.agentauth import test_native_test_interpreter_registration as compiler
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

ROOT = Path(__file__).resolve().parents[4]
tests.__path__.append(str(ROOT / "tests"))
tests.e2e.__path__.append(str(ROOT / "tests/e2e"))
sys.path[:0] = [str(ROOT), str(ROOT / "modules/tools"), str(ROOT / "modules/tools/facade"), str(ROOT / "modules/tools/agentcore")]
from agentcore_tools.gateway_interpreter import GatewayInterpreter  # noqa: E402
from tools_facade import server  # noqa: E402

from tests.e2e.tools import interpreter_operator_driver as driver  # noqa: E402 - cross-component source integration

admission, sessions, native, runtime, operator = producer.admission, producer.sessions, producer.native, producer.runtime, producer.operator


@pytest.fixture
def quote_inputs():
    inputs, evidence = compiler.quote_inputs.__wrapped__()
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    return inputs.model_copy(
        update={
            "binding": inputs.binding.model_copy(
                update={"gateway_origin": f"https://localhost:{port}", "execute_code": driver.CODE, "file_path": driver.FILE_PATH}
            )
        }
    ), evidence


def certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "offline-cert.pem", tmp_path / "offline-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.chmod(0o600)
    key_path.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, cert_path


@pytest.mark.parametrize("fault", ["none", "execute_reply_lost", "pending_task", "start_reply_lost"])
async def test_real_installed_operator_journey_keeps_original_funding_and_task(operator, tmp_path, monkeypatch, fault):
    from src.internal import tools_authorize

    f, n = operator, operator.native
    client = n.fixture.client
    client.create_table(
        TableName=f.binding.execution_table,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"}],
    )
    client.create_table(
        TableName="offline-code-operations",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
    )
    for name, value in {
        "ADP_TOOLS_EXECUTION_TABLE": f.binding.execution_table,
        "ADP_TOOLS_TABLE": "offline-code-operations",
        "ADP_CODE_INTERPRETER_ID": "CodeInterpreterExample-0123456789",
        "AWS_REGION": "us-east-1",
        "ADP_TOOLS_AGENTCORE_URL": "https://offline.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        "ADP_TOOLS_ASSERTION_KEY_ARN": "arn:aws:kms:us-east-1:123456789012:key/offline",
        "ADP_TOOLS_INTERNAL_AUTH_URL": "https://gateway.internal/internal/v1/tools/authorize",
    }.items():
        monkeypatch.setenv(name, value)
    f.app.dependency_overrides[tools_authorize.require_agent_transport] = lambda: None
    f.app.dependency_overrides[tools_authorize.get_agent_runtime] = lambda: None
    monkeypatch.setattr(
        tools_authorize,
        "verified_caller_identity",
        lambda request, settings: "arn:aws:sts::123456789012:assumed-role/offline-facade/local" if request.headers.get("x-offline-facade") else None,
    )
    monkeypatch.setattr(
        tools_authorize,
        "get_rate_limit_service",
        lambda request: SimpleNamespace(consume_tool_rate_limit=AsyncMock(return_value=SimpleNamespace(allowed=True))),
    )
    monkeypatch.setattr(
        server,
        "signed_headers",
        lambda url, body, service, region, extra_headers=None: {
            "Content-Type": "application/json",
            "x-offline-facade": "verified",
            **(extra_headers or {}),
        },
    )
    original_sdk = server.boto3.client
    monkeypatch.setattr(
        server.boto3,
        "client",
        lambda service, **kw: SimpleNamespace(sign=lambda **args: {"Signature": b"offline-kms"}) if service == "kms" else original_sdk(service, **kw),
    )
    calls, wire, public_requests = [], [], []

    class Provider:
        closed = False

        def start_code_interpreter_session(self, **kwargs):
            calls.append("start")
            return {"sessionId": "offline-session"}

        def invoke_code_interpreter(self, **kwargs):
            operation = kwargs["name"]
            calls.append(operation)
            if operation == "startCommandExecution":
                assert shlex.split(kwargs["arguments"]["command"])[2] == driver.CODE
                result = {"taskId": "offline-task"}
            elif operation == "getTask":
                result = (
                    {"taskStatus": "running"}
                    if fault == "pending_task"
                    else {"taskStatus": "completed", "stdout": '{"ok": true, "bytes": 4}\n', "stderr": "", "exitCode": 0}
                )
            else:
                assert operation == "executeCommand"
                result = {"stdout": json.dumps({"encoding": "base64", "data": base64.b64encode(driver.FILE_BYTES).decode()})}
            return {"stream": iter([{"result": {"structuredContent": result}}])}

        def get_code_interpreter_session(self, **kwargs):
            calls.append("getSession")
            return {"status": "TERMINATED" if self.closed else "READY"}

        def stop_code_interpreter_session(self, **kwargs):
            calls.append("stop")
            self.closed = True
            return {}

    provider = Provider()
    interpreter = GatewayInterpreter(client, provider)
    original_http = httpx.AsyncClient
    gateway_transport = httpx.ASGITransport(app=f.app)

    async def internal(request):
        if request.url.host == "gateway.internal":
            return await gateway_transport.handle_async_request(request)
        packet = json.loads(request.content)
        claims = json.loads(base64.urlsafe_b64decode(request.headers["x-adp-assertion"] + "=="))["claims"]
        wire.append(packet)
        if packet["method"] == "tools/list":
            result = {
                "tools": [
                    {"name": "code-interpreter___" + step, "inputSchema": {"type": "object"}} for step in ("start", "execute", "result", "file")
                ],
                "_meta": {"adp_receipt_id": claims["receipt_id"]},
            }
        else:
            args = packet["params"]["arguments"]
            action = packet["params"]["name"].split("___")[1]
            if action in {"start", "execute", "file", "execution_close"}:
                found = getattr(interpreter, action)(args, claims, lambda: (claims, args))
            else:
                found = getattr(interpreter, action)(args, claims)
            if (action == "execute" and fault == "execute_reply_lost") or (action == "start" and fault == "start_reply_lost"):
                raise httpx.ReadTimeout("Explicit offline lost original response")
            result = {
                "structuredContent": {
                    "result": found,
                    "receipt_id": found["receipt_id"],
                    "user_id": claims["principal"]["user_id"],
                    "tenant_id": claims["principal"]["tenant_id"],
                }
            }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": packet["id"], "result": result})

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: original_http(transport=httpx.MockTransport(internal), **kw))
    async with original_http(transport=gateway_transport, base_url=f.binding.gateway_origin) as gateway_client:
        headers, approval_reply, exchange = await producer.issue(f, gateway_client)
        approved = n.store.approval("tenant-one", f.operator_request.run_id)
        value = {
            "version": 2,
            "reference": exchange.json()["reference"],
            "binding": f.binding.model_dump(),
            "access_token": headers["Authorization"].removeprefix("Bearer "),
            "capability": exchange.json()["capability"],
            "started_at": approved.approved_at,
            "models": [],
            "max_discoveries": 1,
        }
        transport_path = tmp_path / "original-operator.json"
        transport_path.write_text(json.dumps(value))
        transport_path.chmod(0o600)
        session = driver.OperatorLocalSession.read(transport_path)
        home = tmp_path / "installed-home"
        home.mkdir(mode=0o700)
        installed = home / ".adp/bin/adp"
        installed.parent.mkdir(parents=True)
        for name in ("adp", "adp-mcp.py", "adp_common.py", "adp_native_test.py", "adp_native_credential_handoff.py", "adp_deployments.py"):
            shutil.copy(ROOT / "modules/gateway/cli" / name, installed.parent / name)
        installed.chmod(0o700)
        monkeypatch.setattr(driver, "source_file", lambda revision, name: (ROOT / "modules/gateway/cli" / name).read_bytes())
        context, cert_path = certificate(tmp_path)
        loop = asyncio.get_running_loop()
        async with original_http(transport=httpx.ASGITransport(app=server.create_app()), base_url="https://localhost") as facade:

            async def dispatch(method, path, headers, body):
                public_requests.append((method, path))
                target = facade if path.startswith("/api/agent/") else gateway_client
                # The public API ingress removes /api before the gateway app.
                route = path if target is facade else path.removeprefix("/api")
                response = await target.request(method, route, headers=headers, content=body)
                return response.status_code, dict(response.headers), response.content

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def handle_request(self):
                    raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    status, headers, content = asyncio.run_coroutine_threadsafe(
                        dispatch(self.command, self.path, dict(self.headers), raw), loop
                    ).result(timeout=35)
                    self.send_response(status)
                    for name, value in headers.items():
                        if name.lower() not in {"content-length", "connection", "transfer-encoding"}:
                            self.send_header(name, value)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)

                do_GET = do_POST = handle_request  # noqa: N815 - stdlib HTTP handler dispatch names

            httpd = ThreadingHTTPServer(("127.0.0.1", urlsplit(f.binding.gateway_origin).port), Handler)
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
            try:
                task = asyncio.to_thread(
                    driver.run_operator_interpreter,
                    installed,
                    {"HOME": str(home), "PATH": os.environ["PATH"], "SSL_CERT_FILE": str(cert_path)},
                    session,
                )
                if fault in {"pending_task", "start_reply_lost"}:
                    with pytest.raises(ValueError, match="unresolved|unconfirmed"):
                        await task
                else:
                    report = await task
                    retained = json.loads((transport_path.parent / approved.run_id / "attempts.json").read_text())["attempts"]
                    root, execute = retained[1]["receipt"], retained[2]
                    before = list(calls)
                    denied = await facade.post(
                        "/api/agent/tools/operations/recover",
                        headers=headers,
                        json={
                            "tool": "code_interpreter___execute",
                            "request_id": execute["request_id"],
                            "execution_id": root["execution_id"],
                            "arguments": {"session_id": root["session_id"], "code": driver.CODE, "language": "python"},
                        },
                    )
                    assert denied.status_code == 403, denied.text
                    denied = await facade.get(
                        f"/api/agent/tools/executions/{root['execution_id']}/operations/{execute['request_id']}",
                        params={"tool": "code_interpreter___execute"},
                        headers=headers,
                    )
                    assert denied.status_code == 403, denied.text
                    assert calls == before
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=3)
    if fault not in {"pending_task", "start_reply_lost"}:
        assert report["behavioral_success"] and report["cleanup_confirmed"] and report["model_requests"] == 0
        assert report["funding_status"] == "held" and report["accounting_status"] == "pending" and not report["governance_complete"]
    expected_calls = ["start"]
    if fault != "start_reply_lost":
        expected_calls += ["startCommandExecution", "getTask"]
    if fault not in {"pending_task", "start_reply_lost"}:
        expected_calls += ["executeCommand"]
    assert calls == expected_calls + ["getSession", "stop", "getSession"]
    assert not f.provider_calls and not f.authority_calls
    # Native static discovery uses the protected, freshly read control-plane catalogue.
    assert not any(packet["method"] == "tools/list" for packet in wire)
    assert public_requests[0] == ("GET", "/api/agent/tools")
    assert sum(path == "/api/agent/tools" for _, path in public_requests) == 1
    assert len(public_requests) <= 9
    assert not any("/approvals" in path or "/exchange" in path or "/messages" in path or "/responses" in path for _, path in public_requests)
    journal = json.loads((transport_path.parent / approved.run_id / "attempts.json").read_text())
    steps = ["list", "start"]
    if fault != "start_reply_lost":
        steps += ["execute", "execute_status"]
    if fault not in {"pending_task", "start_reply_lost"}:
        steps += ["result", "file"]
    assert [row["slot"] for row in journal["attempts"]] == steps + ["close"]
    originals = [n.store.read_operation("tenant-one", approved.run_id, row["operation_id"]) for row in journal["attempts"][:-1]]
    assert all(row["status"] == "dispatch_started" and row["reservation_confirmed"] for row in originals)
    assert len({row["reservation_id"] for row in originals}) == len(steps)
