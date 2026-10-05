"""Real loopback requests: 303 must drop body, 307 must preserve it (no cloud)."""

import http.server
import threading
import unittest

import urllib3


class RedirectTests(unittest.TestCase):
    def check_redirect(self, manager, status, duplicates=False):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/start":
                    self.send_response(status)
                    self.send_header("Location", "/end")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    received.append(
                        (self.command, body, dict(self.headers), self.headers.get_all("X-Repeated"))
                    )
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")

            do_POST = handle_request
            do_GET = handle_request

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        headers = {
            "Content-Type": "application/json",
            "Content-Length": "17",
            "X-Control": "retained",
        }
        if duplicates:
            headers = urllib3._collections.HTTPHeaderDict(headers)
            headers.add("X-Repeated", "first")
            headers.add("X-Repeated", "second")
        original = list(headers.items())
        client = (
            urllib3.PoolManager()
            if manager
            else urllib3.HTTPConnectionPool("127.0.0.1", server.server_port)
        )
        url = f"http://127.0.0.1:{server.server_port}/start" if manager else "/start"
        try:
            response = client.urlopen(
                "POST", url, body=b"synthetic-private", headers=headers, timeout=3
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(list(headers.items()), original)
            self.assertEqual(len(received), 1)
            method, body, actual, repeated = received[0]
            if duplicates:
                self.assertEqual(repeated, ["first", "second"])
            self.assertEqual(actual["X-Control"], "retained")
            if status == 303:
                self.assertEqual((method, body), ("GET", b""))
                self.assertNotIn("Content-Type", actual)
            else:
                self.assertEqual((method, body), ("POST", b"synthetic-private"))
                self.assertEqual(actual["Content-Type"], "application/json")
        finally:
            client.clear() if manager else client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_pool_manager_duplicate_headers(self):
        self.check_redirect(True, 303, True)

    def test_connection_pool_duplicate_headers(self):
        self.check_redirect(False, 303, True)

    def test_pool_manager_303(self):
        self.check_redirect(True, 303)

    def test_connection_pool_303(self):
        self.check_redirect(False, 303)

    def test_pool_manager_307(self):
        self.check_redirect(True, 307)

    def test_connection_pool_307(self):
        self.check_redirect(False, 307)


if __name__ == "__main__":
    unittest.main()
