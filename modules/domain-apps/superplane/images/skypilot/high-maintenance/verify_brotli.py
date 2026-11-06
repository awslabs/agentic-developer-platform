"""Verify Brotli 1.2.1 through its codec and the requests HTTP consumer."""

import http.server
import json
import threading

import brotli
import requests


def main():
    if not __debug__:
        raise RuntimeError("Acceptance requires assertions")
    assert brotli.__version__ == "1.2.1"
    payload = bytes(range(256)) * 4096
    encoded = brotli.compress(payload)
    assert brotli.decompress(encoded) == payload
    for malformed in (b"", b"invalid brotli stream", encoded[:-1]):
        try:
            brotli.decompress(malformed)
        except brotli.error:
            pass
        else:
            raise AssertionError("Malformed Brotli stream was accepted")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Encoding", "br")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = requests.get(f"http://127.0.0.1:{server.server_port}/", timeout=5)
        assert response.status_code == 200 and response.content == payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    print(
        json.dumps(
            {
                "brotli": brotli.__version__,
                "roundtrip": "passed",
                "malformed_rejected": 3,
                "requests_content_encoding_br": "passed",
            }
        )
    )


if __name__ == "__main__":
    main()
