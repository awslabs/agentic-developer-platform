"""Bounded, ordinary-user protocol adapters; never a substitute for live case drivers."""

import base64
import hashlib
import json
import os
import re
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request


class ClientError(ValueError):
    pass


def http_failure(status, data=b""):
    message = f"Assistant HTTP request returned {status}"
    try:
        code = json.loads(data).get("detail", {}).get("error", "")
        if isinstance(code, str) and re.fullmatch(
            r"chat_qualification_[a-z_]{1,100}", code
        ):
            message += f" ({code})"
    except (ValueError, TypeError, AttributeError):
        pass
    return ClientError(message)


class UserSession:
    def __init__(self, tokens, refresh=None, clock=time.time):
        self.tokens = tokens
        self.refresh = refresh
        self.clock = clock

    def token(self, kind="access_token"):
        if self.tokens.get("expires_at", 0) <= self.clock() + 30:
            if self.refresh is None:
                raise ClientError(
                    "Ordinary-user session expired; refresh or sign in again"
                )
            renewed = self.refresh(self.tokens)
            if (
                not isinstance(renewed, dict)
                or renewed.get("expires_at", 0) <= self.clock() + 30
            ):
                raise ClientError("Ordinary-user refresh did not renew the session")
            self.tokens = renewed
        value = self.tokens.get(kind)
        if not isinstance(value, str) or not value or value == "<redacted>":
            raise ClientError(
                "Ordinary-user session has no usable authentication token"
            )
        return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def ordinary_env(home, environ=None):
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "AWS_EC2_METADATA_DISABLED": "true",
    }


def validate_http(url, method, body):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ClientError(
            "Assistant HTTP requests require an HTTPS target without embedded credentials"
        )
    path = parsed.path
    if any(segment in {"admin", "internal", "fault"} for segment in path.split("/")):
        raise ClientError("Ordinary assistant clients cannot request privileged routes")
    if (
        method not in {"GET", "POST", "PUT"}
        or (method == "GET" and body is not None)
        or (method == "PUT" and body is None)
    ):
        raise ClientError("Assistant HTTP method or body is invalid")
    if body is not None and (
        not isinstance(body, dict) or len(json.dumps(body)) > 8192
    ):
        raise ClientError("Assistant HTTP body exceeds limit or is invalid")
    if method == "PUT" and (
        parsed.query
        or parsed.fragment
        or re.fullmatch(r"/chat/sessions/[A-Za-z0-9_-]{1,160}/mode", path) is None
        or set(body) != {"mode"}
        or not isinstance(body["mode"], str)
        or body["mode"] not in {"ephemeral", "persistent"}
    ):
        raise ClientError("Assistant HTTP writes are limited to owned session modes")


def http_json(
    url, session, *, opener=None, method="GET", body=None, expected_status=200
):
    validate_http(url, method, body)
    if opener is None:
        try:
            from . import assistant_process
        except ImportError:
            import assistant_process
        with assistant_process.start(url, session, "http") as client:
            if method == "GET" and expected_status == 200:
                return client.call("http")
            return client.call(
                "http", method=method, document=body, expected_status=expected_status
            )
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + session.token(),
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with opener(request, timeout=15) as response:
            if response.status != expected_status:
                raise ClientError(f"Assistant HTTP request returned {response.status}")
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ClientError("Assistant HTTP response exceeds limit")
            return json.loads(data)
    except urllib.error.HTTPError as exc:
        raise http_failure(exc.code, exc.read(8192)) from None


def websocket_url(url, session):
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "wss"
        or not parsed.hostname
        or parsed.username
        or parsed.query
        or parsed.fragment
    ):
        raise ClientError("Assistant WebSocket target must be a clean wss URL")
    return url + "?token=" + urllib.parse.quote(session.token("id_token"), safe="")


def connect(url, session, *, dial=None, tls=ssl.create_default_context):
    if dial is None:
        try:
            from . import assistant_process
        except ImportError:
            import assistant_process
        return assistant_process.start(url, session, "websocket")
    parsed = urllib.parse.urlsplit(websocket_url(url, session))
    host = parsed.hostname
    port = parsed.port or 443
    stream = tls().wrap_socket(dial((host, port), timeout=15), server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    try:
        stream.sendall(request.encode("ascii"))
        headers = bytearray()
        while not headers.endswith(b"\r\n\r\n"):
            if len(headers) >= 8192:
                raise ClientError("Assistant WebSocket response headers exceed limit")
            chunk = stream.recv(1)
            if not chunk:
                raise ClientError("Assistant WebSocket closed during handshake")
            headers.extend(chunk)
        try:
            lines = headers.decode("ascii").split("\r\n")
        except UnicodeDecodeError:
            raise ClientError("Assistant WebSocket invalid upgrade headers") from None
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()
        fields = {}
        for line in lines[1:-2]:
            name, separator, value = line.partition(":")
            if not separator or not name or name != name.strip():
                raise ClientError("Assistant WebSocket invalid upgrade header")
            fields.setdefault(name.lower(), []).append(value.strip())
        connection_tokens = {
            token.strip().lower()
            for value in fields.get("connection", [])
            for token in value.split(",")
        }
        if (
            lines[0].split(" ", 2)[:2] != ["HTTP/1.1", "101"]
            or [value.lower() for value in fields.get("upgrade", [])] != ["websocket"]
            or "upgrade" not in connection_tokens
            or fields.get("sec-websocket-accept") != [expected]
            or "sec-websocket-extensions" in fields
            or "sec-websocket-protocol" in fields
        ):
            raise ClientError("Assistant WebSocket authentication or upgrade failed")
        return WebSocket(stream)
    except Exception:
        stream.close()
        raise


class WebSocket:
    def __init__(self, stream):
        self.stream = stream

    def send(self, document):
        payload = json.dumps(document, separators=(",", ":")).encode()
        self._send_frame(1, payload)

    def _send_frame(self, opcode, payload):
        if len(payload) > 65535:
            raise ClientError("Assistant WebSocket message exceeds limit")
        mask = os.urandom(4)
        length = len(payload)
        header = (
            bytes((0x80 | opcode, 0x80 | length))
            if length < 126
            else bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", length)
        )
        self.stream.sendall(
            header
            + mask
            + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        )

    def _read(self, length, deadline):
        result = bytearray()
        while len(result) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientError("Assistant WebSocket response timed out")
            self.stream.settimeout(remaining)
            chunk = self.stream.recv(length - len(result))
            if not chunk:
                raise ClientError("Assistant WebSocket disconnected during response")
            result.extend(chunk)
        return result

    def receive(self, timeout=15):
        deadline = time.monotonic() + timeout
        payload = bytearray()
        fragmented = False
        for _frame in range(1024):
            first, size = self._read(2, deadline)
            final, opcode = bool(first & 0x80), first & 0x0F
            if first & 0x70 or size & 0x80:
                raise ClientError("Assistant WebSocket invalid frame flags")
            length = size & 0x7F
            if opcode >= 8 and (not final or length > 125):
                raise ClientError("Assistant WebSocket invalid control frame")
            if length == 126:
                length = struct.unpack("!H", self._read(2, deadline))[0]
                if length < 126:
                    raise ClientError("Assistant WebSocket invalid frame length")
            elif length == 127:
                raise ClientError("Assistant WebSocket response exceeds limit")
            if opcode not in {0, 1, 8, 9, 10}:
                raise ClientError("Assistant WebSocket expected a text event")
            if opcode < 8 and len(payload) + length > 65535:
                raise ClientError("Assistant WebSocket response exceeds limit")
            chunk = self._read(length, deadline)
            if opcode == 8:
                if length == 1:
                    raise ClientError("Assistant WebSocket invalid close frame")
                if length >= 2:
                    code = struct.unpack("!H", chunk[:2])[0]
                    if (
                        code
                        not in {
                            1000,
                            1001,
                            1002,
                            1003,
                            1007,
                            1008,
                            1009,
                            1010,
                            1011,
                            1012,
                            1013,
                            1014,
                        }
                        and not 3000 <= code <= 4999
                    ):
                        raise ClientError("Assistant WebSocket invalid close status")
                    try:
                        chunk[2:].decode("utf-8")
                    except UnicodeDecodeError:
                        raise ClientError(
                            "Assistant WebSocket invalid close reason"
                        ) from None
                self._send_frame(8, chunk)
                self.close()
                raise ClientError("Assistant WebSocket closed before an event")
            if opcode == 9:
                self._send_frame(10, chunk)
                continue
            if opcode == 10:
                continue
            if (opcode == 0 and not fragmented) or (opcode == 1 and fragmented):
                raise ClientError("Assistant WebSocket invalid continuation sequence")
            payload.extend(chunk)
            fragmented = not final
            if final:
                try:
                    return json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise ClientError(
                        "Assistant WebSocket invalid JSON event"
                    ) from None
        raise ClientError("Assistant WebSocket frame count exceeds limit")

    def close(self):
        self.stream.close()
