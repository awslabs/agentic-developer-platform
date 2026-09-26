"""Synthetic libcurl proxy credential reset regression; local loopback only."""

import ctypes
import http.server
import json
import threading

seen = []


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - standard-library callback
        seen.append(self.headers.get("Proxy-Authorization"))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
lib = ctypes.CDLL("libcurl.so.4")
lib.curl_easy_init.restype = ctypes.c_void_p
lib.curl_easy_setopt.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.curl_easy_setopt.restype = ctypes.c_int
lib.curl_easy_perform.argtypes = [ctypes.c_void_p]
lib.curl_easy_perform.restype = ctypes.c_int
lib.curl_easy_cleanup.argtypes = [ctypes.c_void_p]
handle = lib.curl_easy_init()
assert handle


def setopt(option, value):
    assert lib.curl_easy_setopt(handle, option, value) == 0


try:
    setopt(10002, ctypes.c_char_p(b"http://synthetic.invalid/"))
    setopt(10004, ctypes.c_char_p(f"http://127.0.0.1:{server.server_port}".encode()))
    setopt(10177, ctypes.c_char_p(b""))
    setopt(13, ctypes.c_long(5))
    setopt(10006, ctypes.c_char_p(b"synthetic-user:synthetic-secret"))
    assert lib.curl_easy_perform(handle) == 0
    setopt(10006, ctypes.c_void_p())
    assert lib.curl_easy_perform(handle) == 0
    setopt(10006, ctypes.c_char_p(b"synthetic-user:synthetic-secret"))
    assert lib.curl_easy_perform(handle) == 0
finally:
    lib.curl_easy_cleanup(handle)
    server.shutdown()
    thread.join()
    server.server_close()
assert len(seen) == 3
assert seen[0] is not None and seen[2] == seen[0], "Credential positive controls failed"
print(
    json.dumps(
        {
            "requests": len(seen),
            "credentials_before_and_after_present": True,
            "credentials_present_after_clear": seen[1] is not None,
            "network": "none; synthetic loopback proxy only",
        }
    )
)
assert seen[1] is None, "Cleared proxy credentials were reused"
