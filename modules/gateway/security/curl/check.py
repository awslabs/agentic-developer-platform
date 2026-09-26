"""Synthetic PSL-cookie regression and valid same-origin control; loopback only."""

import http.server
import json
import subprocess
import threading

seen = []


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - standard-library HTTP handler callback
        seen.append({"host": self.headers.get("Host"), "cookie": self.headers.get("Cookie")})
        self.send_response(200)
        if self.headers.get("Host", "").startswith("foo."):
            self.send_header("Set-Cookie", "synthetic=fixture; Domain=co.uk.; Path=/")
            self.send_header("Set-Cookie", "same_origin=fixture; Path=/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
port = server.server_port
try:
    subprocess.run(
        [
            "curl",
            "--disable",
            "--silent",
            "--show-error",
            "--fail",
            "--noproxy",
            "*",
            "--cookie",
            "",
            "--resolve",
            f"foo.co.uk.:{port}:127.0.0.1",
            "--resolve",
            f"bar.co.uk.:{port}:127.0.0.1",
            f"http://foo.co.uk.:{port}/",
            f"http://bar.co.uk.:{port}/",
            f"http://foo.co.uk.:{port}/",
        ],
        check=True,
        timeout=10,
    )
finally:
    server.shutdown()
    thread.join()
    server.server_close()
if len(seen) != 3:
    raise RuntimeError("Expected three loopback requests")
if "same_origin=fixture" not in (seen[2]["cookie"] or ""):
    raise RuntimeError("Valid same-origin cookie was lost")
leak = seen[1]["cookie"] is not None
print(
    json.dumps(
        {
            "requests": len(seen),
            "cross_origin_supercookie_sent": leak,
            "same_origin_cookie_preserved": True,
            "data": "synthetic only",
            "network": "none with local loopback",
        }
    )
)
if leak:
    raise RuntimeError("Public suffix cookie crossed unrelated host boundary")
