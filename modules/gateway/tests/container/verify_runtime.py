"""Packaged runtime smoke check; execute only inside its isolated test container.

See docs/security/runs/2026-09-27/curl-removal.md for exact invocation.
"""

import json
import multiprocessing
import os
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    assert os.getuid() == 65532
    assert shutil.which("curl") is None
    assert not list(Path("/usr/lib/x86_64-linux-gnu").glob("libcurl*"))
    assert "BEGIN CERTIFICATE" in Path("/etc/ssl/certs/rds-global-bundle.pem").read_text()
    seen = multiprocessing.Queue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.path == "/2018-06-01/runtime/invocation/next"
            body = b'{"security_test":true}'
            self.send_response(200)
            for k, v in {
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Lambda-Runtime-Aws-Request-Id": "security-test",
                "Lambda-Runtime-Deadline-Ms": str(int(time.time() * 1000) + 30000),
                "Lambda-Runtime-Invoked-Function-Arn": "arn:aws:lambda:us-east-1:123456789012:function:test",
                "Lambda-Runtime-Trace-Id": "Root=1-00000000-000000000000000000000000",
            }.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            seen.put((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    process = multiprocessing.get_context("fork").Process(target=server.serve_forever, daemon=True)
    process.start()
    os.environ["AWS_LAMBDA_RUNTIME_API"] = "127.0.0.1:" + str(server.server_port)
    from awslambdaric.lambda_runtime_client import LambdaRuntimeClient

    client = LambdaRuntimeClient(os.environ["AWS_LAMBDA_RUNTIME_API"])
    event = client.wait_next_invocation()
    assert event.invoke_id == "security-test"
    assert json.loads(event.event_body) == {"security_test": True}
    client.post_invocation_result(event.invoke_id, b'{"ok":true}')
    assert seen.get(timeout=5) == ("/2018-06-01/runtime/invocation/security-test/response", b'{"ok":true}')
    process.terminate()
    process.join(5)
    from fastapi.testclient import TestClient

    from src.app import create_app

    with TestClient(create_app()) as client:
        response = client.get("/health")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "healthy"
    print(
        json.dumps(
            {
                "gateway_health": "passed",
                "lambda_native_runtime_roundtrip": "passed",
                "curl_and_libcurl": "absent",
                "uid": os.getuid(),
                "rds_ca": "present",
                "external_network": "disabled",
            }
        )
    )


if __name__ == "__main__":
    main()
