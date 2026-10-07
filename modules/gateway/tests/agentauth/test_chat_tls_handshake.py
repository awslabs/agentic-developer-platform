"""Real local TLS transport test; not deployed sandbox qualification."""

import importlib.util
import os
import socket
import ssl
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn

from src.agentauth.chat_tls import HOST, SandboxTransport


def certificate_fixture():
    path = Path(__file__).resolve().parents[4] / "platform/scripts/tests/test_render_chat_tls.py"
    spec = importlib.util.spec_from_file_location("chat_tls_certificate_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.NOW = datetime.now(UTC)
    return module.certificates()


def test_real_tls_requires_pinned_ca_and_hostname(tmp_path):
    cert, key, ca = certificate_fixture()
    cert_path, key_path = tmp_path / "server.crt", tmp_path / "server.key"
    ca_path = tmp_path / "ca.crt"
    ca_path.write_bytes(ca)
    cert_path.write_bytes(cert)
    key_path.write_bytes(key)

    async def gateway(scope, receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b"ordinary authentication still required"})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            SandboxTransport(gateway),
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            lifespan="off",
            proxy_headers=False,
            access_log=False,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        script = """
const https = require('node:https');
const [hostname, port] = process.argv.slice(1);
const connection = https.request({ hostname, servername: hostname, port: Number(port), method: 'POST',
  path: '/v1/chat/data/bootstrap', headers: { Host: 'chat-sandbox-gateway.adp-gateway.svc:8443' },
  lookup: (_hostname, options, callback) => options.all ? callback(null, [{ address: '127.0.0.1', family: 4 }]) : callback(null, '127.0.0.1', 4),
}, response => { response.resume(); response.on('end', () => process.exit(response.statusCode === 401 ? 0 : 2)); });
connection.on('error', error => { process.stderr.write(error.code); process.exit(1); });
connection.end();
"""
        node_env = {"PATH": os.environ["PATH"], "NODE_EXTRA_CA_CERTS": str(ca_path)}
        node = subprocess.run(["node", "-e", script, HOST, str(port)], env=node_env, capture_output=True, timeout=5)
        assert node.returncode == 0, node.stderr
        node = subprocess.run(["node", "-e", script, "wrong.example", str(port)], env=node_env, capture_output=True, timeout=5)
        assert node.returncode == 1 and b"ERR_TLS_CERT_ALTNAME_INVALID" in node.stderr
        node = subprocess.run(["node", "-e", script, HOST, str(port)], env={"PATH": os.environ["PATH"]}, capture_output=True, timeout=5)
        assert node.returncode == 1 and node.stderr == b"UNABLE_TO_VERIFY_LEAF_SIGNATURE"
        trusted = ssl.create_default_context(cadata=ca.decode())
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            with trusted.wrap_socket(raw, server_hostname=HOST) as tls:
                tls.sendall(f"POST /v1/chat/data/bootstrap HTTP/1.1\r\nHost: {HOST}:8443\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
                response = tls.recv(4096)
                assert b"401 Unauthorized" in response
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            with pytest.raises(ssl.SSLCertVerificationError):
                ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            with pytest.raises(ssl.SSLCertVerificationError):
                trusted.wrap_socket(raw, server_hostname="wrong.example")
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            raw.sendall(b"GET /health HTTP/1.1\r\nHost: example\r\n\r\n")
            try:
                assert not raw.recv(4096).startswith(b"HTTP/")
            except (ConnectionResetError, TimeoutError):
                pass  # TLS-only server never serves a plaintext HTTP response.
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()
