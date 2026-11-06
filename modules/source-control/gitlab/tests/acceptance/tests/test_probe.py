"""Network protocol errors must never count as isolation evidence."""
import importlib.util
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

SPEC = importlib.util.spec_from_file_location("qualification_probe", Path(__file__).resolve().parents[1] / "probe.py")
probe_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe_module)


@pytest.fixture
def listening_tcp():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    address, port = listener.getsockname()
    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                # Deliberately no TLS. A failed TLS handshake still demonstrates
                # that the denied destination accepted this TCP connection.
                connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield {"name": "denied-service", "address": address, "port": port}
    listener.close()
    thread.join(timeout=5)


def test_actual_tls_protocol_failure_is_reachable(listening_tcp):
    result = probe_module.probe({**listening_tcp, "kind": "https", "hostname": "example.test", "expected_status": 200})
    assert result["transport_connected"] is True
    assert result["result"] == "reachable_protocol_error"


def test_actual_tcp_positive_control(listening_tcp):
    result = probe_module.probe({**listening_tcp, "kind": "tcp"})
    assert result["transport_connected"] is True
    assert result["result"] == "allowed"


def test_connection_timeout_is_blocked_without_connection():
    with patch.object(probe_module.socket, "create_connection", side_effect=TimeoutError):
        result = probe_module.probe({"name": "timeout", "address": "192.0.2.1", "port": 443, "kind": "tcp"})
    assert result["transport_connected"] is False
    assert result["result"] == "blocked"


@pytest.mark.parametrize("failure", [ConnectionRefusedError(), OSError(101, "network unreachable"), OSError(113, "no route to host")])
def test_missing_listener_or_route_cannot_prove_isolation(failure):
    with patch.object(probe_module.socket, "create_connection", side_effect=failure):
        result = probe_module.probe({"name": "missing-route", "address": "192.0.2.1", "port": 443, "kind": "tcp"})
    assert result["transport_connected"] is False
    assert result["result"] == "unreachable"


def test_missing_ca_file_is_local_probe_error(tmp_path):
    with patch.object(probe_module.socket, "create_connection") as connection:
        result = probe_module.probe({"name": "bad-ca", "address": "192.0.2.1", "port": 443, "kind": "https", "hostname": "example.test", "expected_status": 200}, ca_file=str(tmp_path / "missing.pem"))
    connection.assert_not_called()
    assert result["transport_connected"] is False
    assert result["result"] == "probe_error"


@pytest.mark.parametrize("failure,accepted", [
    (OSError(101, "network unreachable"), True), (OSError(113, "host unreachable"), True),
    (OSError(97, "address family unsupported"), True), (TimeoutError(), True),
    (ConnectionRefusedError(111, "refused"), False), (PermissionError(13, "denied"), False),
])
def test_provider_disabled_mode_preserves_actual_attempt_and_errno(failure, accepted):
    target = {"name": "metadata_ipv6", "address": "fd00:ec2::254", "port": 80,
              "kind": "tcp", "expected": "disabled_by_provider"}
    with patch.object(probe_module.socket, "create_connection", side_effect=failure) as connection:
        result = probe_module.probe(target)
    connection.assert_called_once_with(("fd00:ec2::254", 80), timeout=3)
    assert result["connection_attempted"] is True and result["transport_connected"] is False
    assert result["errno"] == failure.errno
    assert result["result"] == ("blocked" if isinstance(failure, TimeoutError) else "unreachable")
    assert probe_module.expected_result(target, result) is accepted


@pytest.mark.parametrize("change", [{"name": "metadata_ipv4"}, {"address": "fd00:ec2::123"},
                                   {"port": 443}, {"kind": "https"}])
def test_provider_disabled_exception_is_only_for_exact_ipv6_metadata(change):
    target = {"name": "metadata_ipv6", "address": "fd00:ec2::254", "port": 80,
              "kind": "tcp", "expected": "disabled_by_provider", **change}
    result = {"result": "unreachable", "connection_attempted": True,
              "transport_connected": False, "errno": 101}
    assert not probe_module.expected_result(target, result)


def test_pipeline_network_artifact_rejects_reachable_internal_target(listening_tcp, tmp_path, monkeypatch):
    fixtures = tmp_path / "private-targets.json"
    output = tmp_path / "network-result.json"
    fixtures.write_text(json.dumps({"run_id": "synthetic-run", "targets": [
        {**listening_tcp, "kind": "tcp", "expected": "blocked"}]}))
    monkeypatch.setattr(sys, "argv", ["probe.py", "--fixtures", str(fixtures), "--output", str(output)])
    assert probe_module.main() == 1
    result = json.loads(output.read_text())
    assert result["run_id"] == "synthetic-run"
    assert result["probes"] == [{"name": "denied-service", "result": "allowed",
                                  "transport_connected": True, "elapsed_seconds": result["probes"][0]["elapsed_seconds"]}]
    assert listening_tcp["address"] not in output.read_text()


@pytest.fixture
def private_https(tmp_path):
    certificate, key = tmp_path / "ca.pem", tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=gitlab.example.test", "-addext", "subjectAltName=DNS:gitlab.example.test",
        "-keyout", str(key), "-out", str(certificate),
    ], capture_output=True, check=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield certificate, {"name": "gitlab", "address": "127.0.0.1", "port": server.server_port,
                        "kind": "https", "hostname": "gitlab.example.test", "expected_status": 200,
                        "expected": "allowed"}
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


@pytest.mark.parametrize("trusted,hostname,expected", [
    (True, "gitlab.example.test", "allowed"),
    (False, "gitlab.example.test", "reachable_protocol_error"),
    (True, "wrong.example.test", "reachable_protocol_error"),
])
def test_executor_pipeline_private_ca_and_hostname_verification(private_https, tmp_path, trusted, hostname, expected):
    certificate, target = private_https
    acceptance = Path(__file__).resolve().parents[1]
    pipeline = yaml.safe_load((acceptance / "executor.gitlab-ci.yml").read_text())
    script = "\n".join(pipeline["network-isolation"]["script"])
    (tmp_path / "modules").symlink_to(acceptance.parents[4] / "modules", target_is_directory=True)
    fixtures = tmp_path / "targets.json"
    fixtures.write_text(json.dumps({"run_id": "synthetic-run", "targets": [{**target, "hostname": hostname}]}))
    result = subprocess.run(["bash", "-euc", script], cwd=tmp_path, capture_output=True, text=True, timeout=10,
                            env={**os.environ, "ADP_QUALIFICATION_TARGETS_FILE": str(fixtures),
                                 "CI_SERVER_TLS_CA_FILE": str(certificate) if trusted else ""})
    assert result.returncode == (0 if expected == "allowed" else 1), result.stderr
    artifact = json.loads((tmp_path / "network-result.json").read_text())
    assert artifact["probes"][0]["result"] == expected
