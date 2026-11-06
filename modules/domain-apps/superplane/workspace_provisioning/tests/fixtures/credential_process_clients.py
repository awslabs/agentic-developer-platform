"""Manual native-image proof with fixture keys and local endpoints only.

Run in the reviewed paid-worker image with --network none, a tmpfs /tmp,
and this file mounted read-only. Argument `baseline` exercises installed source;
`candidate` uses the app directory mounted read-only at /reviewed. This runs the
actual AWS CLI credential provider and Terraform S3 backend; it creates no cloud
resources and does not need AWS credentials. Authority checks deliberately take
longer than metadata SDK timeouts. Output contains only verification summaries.
"""

import sys
import json
import time
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

if sys.argv[1] == "candidate":
    sys.path.insert(0, "/reviewed")
from workspace_provisioning.process import WorkerProcesses

checks = []


def verify():
    time.sleep(2.1)
    checks.append(True)


credentials = SimpleNamespace(
    _expiry_time=datetime.now(timezone.utc) + timedelta(minutes=30),
    get_frozen_credentials=lambda: SimpleNamespace(
        access_key="fixture-key", secret_key="fixture-secret", token="fixture-token"
    ),
)
with tempfile.TemporaryDirectory() as directory:
    worker = WorkerProcesses(
        binaries={"aws": "/usr/local/bin/aws", "terraform": "/usr/local/bin/terraform"},
        directory=directory,
        session=SimpleNamespace(get_credentials=lambda: credentials),
        region="us-east-1",
        verify=verify,
    )
    result = worker.run(
        ["aws", "configure", "export-credentials", "--format", "process"], timeout=45
    )
    success = (
        result.returncode == 0
        and json.loads(result.stdout)["AccessKeyId"] == "fixture-key"
        and json.loads(result.stdout)["SessionToken"] == "fixture-token"
    )
    print(
        json.dumps(
            {
                "client": "native-AWS-CLI",
                "source": sys.argv[1],
                "returncode": result.returncode,
                "credential_process_succeeded": success,
                "authority_checks": len(checks),
                "temporary_files_remaining": len(list(Path(directory).iterdir())),
            }
        ),
        flush=True,
    )
    assert success == (sys.argv[1] == "candidate")
    if sys.argv[1] != "candidate":
        sys.exit(0)
    requests = []

    class S3(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(
                "Credential=fixture-key/" in self.headers.get("Authorization", "")
                and self.headers.get("X-Amz-Security-Token") == "fixture-token"
            )
            if "list-type=" in self.path:
                status = 200
                body = b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>fixture-state</Name><KeyCount>0</KeyCount><IsTruncated>false</IsTruncated></ListBucketResult>'
            else:
                status = 404
                body = b"<Error><Code>NoSuchKey</Code><Message>Fixture object absent</Message></Error>"
            self.send_response(status)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), S3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (Path(directory) / "main.tf").write_text(
            """terraform {
 backend "s3" {
  bucket = "fixture-state"
  key = "fixture.tfstate"
  region = "us-east-1"
  use_path_style = true
  skip_credentials_validation = true
  skip_requesting_account_id = true
  skip_metadata_api_check = true
  endpoints = { s3 = "http://127.0.0.1:PORT" }
 }
}
""".replace("PORT", str(server.server_port))
        )
        result = worker.run(
            ["terraform", "init", "-input=false", "-no-color"], timeout=60
        )
        success = result.returncode == 0 and requests and all(requests)
        print(
            json.dumps(
                {
                    "client": "native-Terraform-S3-backend",
                    "credential_process_succeeded": bool(success),
                    "returncode": result.returncode,
                    "signed_local_requests": len(requests),
                    "authority_checks": len(checks),
                }
            ),
            flush=True,
        )
        if not success:
            print(result.stderr[:2500])
            print(result.stdout[:1500])
        assert success
        assert not any(
            b"fixture-secret" in f.read_bytes() or b"fixture-token" in f.read_bytes()
            for f in Path(directory).rglob("*")
            if f.is_file()
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
