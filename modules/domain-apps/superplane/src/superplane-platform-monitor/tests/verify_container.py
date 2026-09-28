#!/usr/bin/env python3
"""Exercise a built monitor image with synthetic loopback observation responses."""

import argparse
import http.server
import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    args = parser.parse_args()
    mode = {"status": 200, "requests": 0}

    class Receiver(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            mode["requests"] += 1
            status = mode["status"]
            if self.headers.get("authorization") != "fixture-observer":
                status = 401
            if not self.path.startswith("/internal/observations/clusters?"):
                status = 404
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"[]" if status == 200 else b"{}")

        def log_message(self, *_args):
            pass

    receiver = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=receiver.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    name = "security-monitor-fixture-" + uuid.uuid4().hex[:12]
    url = f"http://127.0.0.1:{port}"

    def status(path):
        try:
            with urllib.request.urlopen(url + path, timeout=2) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "host",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt",
                "no-new-privileges",
                "-e",
                f"OBSERVATION_API_URL=http://127.0.0.1:{receiver.server_port}",
                "-e",
                "OBSERVATION_CREDENTIAL=fixture-observer",
                "-e",
                "OBSERVATION_SIGNING_KEY=fixture-signing-key-not-a-secret",
                "-e",
                f"METRICS_ADDR=127.0.0.1:{port}",
                "-e",
                "POLL_INTERVAL=1h",
                args.image,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        for _ in range(50):
            try:
                if status("/readyz") == 200:
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            raise AssertionError("packaged monitor did not start")
        assert status("/healthz") == 200
        mode["status"] = 401
        assert status("/healthz") == 503, "rejected observer must not be healthy"
        assert status("/readyz") == 200, "receiver rejection must not crash monitor"
        mode["status"] = 503
        assert status("/healthz") == 503
        mode["status"] = 200
        assert status("/healthz") == 200, "monitor must recover after receiver failure"
        assert mode["requests"] >= 4
        uid = subprocess.check_output(
            ["docker", "exec", name, "id", "-u"], text=True
        ).strip()
        assert uid == "65532"
        subprocess.run(
            [
                "docker",
                "exec",
                name,
                "test",
                "-s",
                "/etc/ssl/certs/ca-certificates.crt",
            ],
            check=True,
        )
        subprocess.run(
            ["docker", "exec", name, "test", "-s", "/usr/share/zoneinfo/UTC"],
            check=True,
        )
        logs = subprocess.check_output(
            ["docker", "logs", name], stderr=subprocess.STDOUT, text=True
        )
        assert "fixture-observer" not in logs
        assert "fixture-signing-key-not-a-secret" not in logs
        print(
            json.dumps(
                {
                    "image": args.image,
                    "result": "passed",
                    "checks": [
                        "packaged-startup",
                        "authenticated-health",
                        "unauthorized-unhealthy",
                        "receiver-outage",
                        "recovery",
                        "non-root",
                        "trust-store",
                        "timezone-data",
                        "synthetic-credentials-not-logged",
                    ],
                }
            )
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        receiver.shutdown()
        receiver.server_close()


if __name__ == "__main__":
    main()
