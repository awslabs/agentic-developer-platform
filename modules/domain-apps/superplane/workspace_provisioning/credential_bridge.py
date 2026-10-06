"""Private process-lifetime AWS SDK credential endpoint; no AWS keys on disk."""

from contextlib import contextmanager
from datetime import UTC, datetime
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import threading

from .runtime_config import LifecycleRefused


@contextmanager
def credential_environment(session, verify):
    token = secrets.token_urlsafe(48)
    closed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def do_GET(self):
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

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    try:
        yield {
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": f"http://127.0.0.1:{server.server_port}/credentials",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN": token,
        }
    finally:
        closed.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
