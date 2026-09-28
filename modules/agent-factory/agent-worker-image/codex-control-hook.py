"""Forward Codex hooks to this run's private control socket; never log tool input."""
import http.client
import os
import socket
import sys


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.host)


def main():
    path = os.environ.get("ADP_CODEX_CONTROL_SOCKET")
    if not path:
        print("{}")
        return
    data = sys.stdin.buffer.read(1024 * 1024)
    try:
        connection = Connection(path, timeout=1900)
        connection.request("POST", "/", body=data)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("control hook refused")
        print(response.read().decode())
    except Exception:
        # Exit 2 blocks the tool; an ordinary hook failure would fail open.
        print("ADP control boundary unavailable", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
