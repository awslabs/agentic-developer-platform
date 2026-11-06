"""Private process-lifetime AWS SDK credential endpoint; no AWS keys on disk."""

from contextlib import contextmanager
from datetime import UTC, datetime
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import shlex
import sys
import tempfile
from pathlib import Path

from .runtime_config import LifecycleRefused


@contextmanager
def credential_environment(session, verify, *, directory):
    token = secrets.token_urlsafe(48)
    closed = threading.Event()
    slots = threading.BoundedSemaphore(4)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def do_GET(self):
            if not slots.acquire(blocking=False):
                self.send_error(503, "Credential authority busy")
                return
            try:
                self.respond()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                # A stopped SDK client has no authority to keep this request alive.
                pass
            finally:
                slots.release()

        def respond(self):
            if (
                closed.is_set()
                or self.path != "/credentials"
                or not hmac.compare_digest(self.headers.get("Authorization", ""), token)
            ):
                self.send_error(403, "Credential authority refused")
                return
            try:
                verify()
                credentials = session.get_credentials()
                if credentials is None:
                    raise LifecycleRefused("Operation credentials unavailable")
                frozen = credentials.get_frozen_credentials()
                expiry = getattr(credentials, "_expiry_time", None)
                if (
                    expiry is None
                    or expiry.tzinfo is None
                    or expiry <= datetime.now(UTC)
                ):
                    raise LifecycleRefused("Expiring operation credentials required")
                verify()
                if closed.is_set():
                    raise LifecycleRefused("Operation finished")
                payload = json.dumps(
                    {
                        "AccessKeyId": frozen.access_key,
                        "SecretAccessKey": frozen.secret_key,
                        "Token": frozen.token,
                        "Expiration": expiry.isoformat(),
                    }
                ).encode()
            except Exception:
                self.send_error(403, "Credential authority refused")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    try:
        # SDK container-credential clients impose short metadata timeouts (for
        # example botocore's two seconds). Our checks deliberately perform live
        # authority I/O. Use the SDK's credential_process contract with a bounded
        # client, preserving verification before and after reading credentials.
        command = shlex.join(
            [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).with_name("credential_process.py")),
            ]
        )
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="aws-process-", suffix=".config", dir=directory
        ) as config:
            config.write("[default]\ncredential_process = " + command + "\n")
            config.flush()
            yield {
                "AWS_CONFIG_FILE": config.name,
                "AWS_SDK_LOAD_CONFIG": "1",
                "SUPERPLANE_CREDENTIAL_BRIDGE_URL": f"http://127.0.0.1:{server.server_port}/credentials",
                "SUPERPLANE_CREDENTIAL_BRIDGE_TOKEN": token,
            }
    finally:
        closed.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
