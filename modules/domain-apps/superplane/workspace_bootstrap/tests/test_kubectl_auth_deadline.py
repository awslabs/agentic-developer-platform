"""Exercise client-go's real exec-auth deadline with an inert loopback API."""

import base64
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from superplane_bootstrap.adapters import CommandResult, KubectlClusterAccess


@pytest.mark.skipif(
    not shutil.which("kubectl") or not shutil.which("openssl"),
    reason="local client-go regression requires kubectl and openssl",
)
def test_write_waits_for_exec_authentication_before_sending_once(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            shutil.which("openssl"),
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.headers.get("Transfer-Encoding") == "chunked":
                chunks = []
                while True:
                    size = int(self.rfile.readline().split(b";")[0], 16)
                    if not size:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.read(2)
                raw = b"".join(chunks)
            else:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            received.append((self.path, body))
            body["metadata"]["uid"] = "synthetic-created-uid"
            response = json.dumps(body).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Cross the former 30s request deadline without any AWS calls or keys.
        plugin = tmp_path / "exec_auth.py"
        plugin.write_text(
            "import json,time\ntime.sleep(31)\n"
            "print(json.dumps({'apiVersion':'client.authentication.k8s.io/v1beta1',"
            "'kind':'ExecCredential','status':{'token':'synthetic-loopback-only'}}))\n"
        )
        kubeconfig = tmp_path / "config.json"
        kubeconfig.write_text(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "clusters": [
                        {
                            "name": "local",
                            "cluster": {
                                "server": f"https://localhost:{server.server_port}",
                                "certificate-authority-data": base64.b64encode(
                                    cert.read_bytes()
                                ).decode(),
                            },
                        }
                    ],
                    "users": [
                        {
                            "name": "local",
                            "user": {
                                "exec": {
                                    "apiVersion": "client.authentication.k8s.io/v1beta1",
                                    "command": sys.executable,
                                    "args": [str(plugin)],
                                    "interactiveMode": "Never",
                                }
                            },
                        }
                    ],
                    "contexts": [
                        {
                            "name": "local",
                            "context": {"cluster": "local", "user": "local"},
                        }
                    ],
                    "current-context": "local",
                }
            )
        )

        class LocalRunner:
            calls = 0

            def run(self, args, *, data=None, timeout=120):
                self.calls += 1
                result = subprocess.run(
                    [shutil.which("kubectl"), *args[1:]],
                    check=False,
                    input=data,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env={"PATH": os.defpath, "HOME": str(tmp_path), "LANG": "C.UTF-8"},
                )
                return CommandResult(
                    tuple(args), result.returncode, result.stdout, result.stderr
                )

        runner = LocalRunner()
        access = KubectlClusterAccess(
            runner=runner,
            kubeconfig=Path(kubeconfig),
            controller_namespace="synthetic",
            controller_service_account="synthetic",
            controller_image="synthetic/superplane-controller:v1",
            node_role=None,
            imds_probe_image="synthetic/python@sha256:" + "0" * 64,
        )
        path = "/api/v1/namespaces/synthetic/serviceaccounts"
        body = {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "synthetic"},
        }
        result = access._kube(
            "create", "--raw=" + path, "-f", "-", data=json.dumps(body)
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["metadata"]["uid"] == "synthetic-created-uid"
        assert runner.calls == 1
        assert len(received) == 1 and urlsplit(received[0][0]).path == path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
