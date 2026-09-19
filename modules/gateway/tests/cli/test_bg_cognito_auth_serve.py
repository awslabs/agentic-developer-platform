# tests/cli/test_bg_cognito_auth_serve.py
"""Unit tests for `bg-cognito-auth.sh serve` and bg-gateway-proxy.py (Issue #4154).

`serve` runs a localhost-only proxy that injects a freshly-refreshed Cognito
token into every request, so Codex — which reads its credential from an env var
once at launch — gets zero-touch auth instead of dying with 401s after 60
minutes.

The tests drive the real shell script and the real Python proxy against:
- a mock `aws` CLI (conftest `mock_aws_cli`) answering `cognito-idp initiate-auth`,
- a mock **upstream gateway** that echoes what it received and can stream SSE,
- a sandboxed HOME so nothing touches the developer's real ~/.bedrock-gateway.

The proxy is exercised over a real socket (not in-process): the bind address,
the header rewriting and the streaming behaviour are all properties of the
deployed artifact, and an in-process test would not cover any of them.
"""

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

# A recognisable, secret-shaped value: asserting on this exact string is how the
# "token never leaks into logs" tests get their signal.
SEEDED_ACCESS_TOKEN = "eyJraWQiOiJzZWVkZWQtNDE1NCJ9.seeded-access-token-4154"
REFRESHED_ACCESS_TOKEN = "mock.access.token"  # what the mock aws CLI returns
SEEDED_REFRESH_TOKEN = "seeded-refresh-token-material-4154"

SSE_CHUNK_COUNT = 3
SSE_CHUNK_GAP_SECONDS = 0.5


# --------------------------------------------------------------------------
# Mock upstream gateway
# --------------------------------------------------------------------------


class UpstreamHandler(BaseHTTPRequestHandler):
    """Mock gateway. Records requests; can echo, stream SSE, or fail."""

    protocol_version = "HTTP/1.1"
    requests: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _record(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body,
            }
        )
        return body

    def _handle(self) -> None:
        body = self._record()
        path = self.path.split("?", 1)[0]

        if path == "/sse":
            self._send_sse()
        elif path == "/boom":
            self._send_bytes(503, b'{"error": "upstream_exploded", "detail": "gateway said no"}', "application/json")
        elif path in ("/echo", "/openai/v1/echo"):
            # /openai/v1/echo: an echo route under the OpenAI base path, for the
            # model-normalization tests. Other unknown paths (e.g. /api/echo in
            # the base-path test) must keep 404ing.
            payload = json.dumps(
                {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "x_api_key": self.headers.get("x-api-key"),
                    "custom": self.headers.get("x-custom-header"),
                    "body": body.decode("utf-8", "replace"),
                }
            ).encode("utf-8")
            self._send_bytes(200, payload, "application/json")
        else:
            self._send_bytes(404, b'{"error": "not_found"}', "application/json")

    # Names mandated by BaseHTTPRequestHandler's dispatch (see per-file-ignores).
    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_sse(self) -> None:
        """Stream chunks with real gaps, framed by close (no Content-Length).

        This is the shape a streaming completion has, and the gaps are what make
        a buffering proxy detectable: a proxy that waits for EOF delivers all
        chunks at once at the end.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for index in range(SSE_CHUNK_COUNT):
            self.wfile.write(f"data: chunk-{index}\n\n".encode())
            self.wfile.flush()
            time.sleep(SSE_CHUNK_GAP_SECONDS)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


class UpstreamGateway:
    """Threaded mock gateway on an ephemeral loopback port."""

    def __init__(self, base_path: str = "") -> None:
        self._base_path = base_path
        self._server: ThreadingHTTPServer | None = None

    def __enter__(self) -> "UpstreamGateway":
        UpstreamHandler.requests = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.port = self._server.server_address[1]
        import threading

        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self._base_path}"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return UpstreamHandler.requests


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def proxy_script(cli_dir: Path) -> Path:
    return cli_dir / "bg-gateway-proxy.py"


def _seed_session(home: Path, gateway_url: str, expires_at: int) -> None:
    """Write the config + token store `import`/`login` would have produced.

    Written directly rather than via `import` so the test controls the exact
    access-token value and expiry it is asserting on.
    """
    config_dir = home / ".bedrock-gateway"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "gateway_url": gateway_url,
                "user_pool_id": "us-east-1_serve4154",
                "client_id": "serveclientid0123456789",
                "identity_pool_id": "",
                "region": "us-east-1",
            }
        )
    )
    (config_dir / "tokens.json").write_text(
        json.dumps(
            {
                "id_token": "seeded.id.token",
                "access_token": SEEDED_ACCESS_TOKEN,
                "refresh_token": SEEDED_REFRESH_TOKEN,
                "expires_at": expires_at,
            }
        )
    )


def _wait_until_listening(port: int, deadline_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"proxy never started listening on 127.0.0.1:{port}")


def _dead_pid() -> int:
    """A pid that has certainly exited — what a killed proxy leaves behind.

    Not 0: `kill -0 0` signals the caller's own process group and succeeds, so
    it reads as "still running".
    """
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RunningProxy:
    """A live `serve` process plus the output it produced."""

    def __init__(self, port: int, process: subprocess.Popen) -> None:
        self.port = port
        self.process = process

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def start_proxy(bg_cognito_auth_script: Path, mock_aws_cli: Path, cognito_home: Path):
    """Start `bg-cognito-auth.sh serve` and yield it once it is accepting connections."""
    started: list[subprocess.Popen] = []

    def _start(
        gateway_url: str,
        expires_at: int | None = None,
        extra_env: dict[str, str] | None = None,
        port: int | None = None,
    ) -> RunningProxy:
        if expires_at is None:
            expires_at = int(time.time()) + 3600
        _seed_session(cognito_home, gateway_url, expires_at)

        chosen_port = port or _free_port()
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(cognito_home),
                "PATH": f"{mock_aws_cli}:{os.environ.get('PATH', '')}",
            }
        )
        if extra_env:
            env.update(extra_env)

        process = subprocess.Popen(
            ["bash", str(bg_cognito_auth_script), "serve", "--port", str(chosen_port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        started.append(process)
        _wait_until_listening(chosen_port)
        return RunningProxy(chosen_port, process)

    yield _start

    for process in started:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


@pytest.fixture
def upstream() -> Iterator[UpstreamGateway]:
    with UpstreamGateway() as gateway:
        yield gateway


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback test URL
        return response.status, json.loads(response.read().decode("utf-8"))


def _drain(process: subprocess.Popen) -> tuple[str, str]:
    """Stop the proxy and return everything it wrote to stdout/stderr."""
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=10)


# --------------------------------------------------------------------------
# Startup / configuration
# --------------------------------------------------------------------------


class TestServeStartup:
    """`serve` must fail fast and legibly rather than start half-configured."""

    def test_serve_without_config_fails(self, run_bg_cognito_auth, cognito_home: Path) -> None:
        result = run_bg_cognito_auth(["serve"])
        assert result.returncode != 0
        assert "Not configured" in result.stderr
        assert "import" in result.stderr
        assert not (cognito_home / ".bedrock-gateway" / "proxy.pid").exists()

    def test_serve_without_gateway_url_fails(self, run_bg_cognito_auth, cognito_home: Path) -> None:
        """A config that exists but carries no gateway_url is not usable."""
        config_dir = cognito_home / ".bedrock-gateway"
        config_dir.mkdir(mode=0o700, parents=True)
        (config_dir / "config.json").write_text(json.dumps({"client_id": "abc", "region": "us-east-1"}))

        result = run_bg_cognito_auth(["serve"])
        assert result.returncode != 0
        assert "No gateway_url" in result.stderr

    def test_serve_rejects_invalid_port(self, run_bg_cognito_auth, cognito_home: Path) -> None:
        _seed_session(cognito_home, "https://gw.example.com/api", int(time.time()) + 3600)
        result = run_bg_cognito_auth(["serve", "--port", "not-a-port"])
        assert result.returncode != 0
        assert "Invalid --port" in result.stderr

    def test_serve_rejects_unknown_option(self, run_bg_cognito_auth, cognito_home: Path) -> None:
        _seed_session(cognito_home, "https://gw.example.com/api", int(time.time()) + 3600)
        result = run_bg_cognito_auth(["serve", "--bind", "0.0.0.0"])
        assert result.returncode != 0
        assert "Unknown option" in result.stderr

    def test_second_serve_on_a_live_proxy_errors_cleanly(self, start_proxy, run_bg_cognito_auth, upstream: UpstreamGateway) -> None:
        """A stale proxy from a previous session must not produce an opaque bind error."""
        proxy = start_proxy(upstream.url)
        result = run_bg_cognito_auth(["serve", "--port", str(_free_port())])
        assert result.returncode != 0
        assert "already running" in result.stderr
        _drain(proxy.process)

    def test_stale_pidfile_does_not_block_startup(self, start_proxy, cognito_home: Path, upstream: UpstreamGateway) -> None:
        """A pidfile left by a killed session is cleaned up, not treated as live."""
        config_dir = cognito_home / ".bedrock-gateway"
        config_dir.mkdir(mode=0o700, exist_ok=True)
        (config_dir / "proxy.pid").write_text(f"{_dead_pid()}\n")

        proxy = start_proxy(upstream.url)
        status, _ = _post_json(f"{proxy.url}/echo", {"ok": True})
        assert status == 200
        _drain(proxy.process)

    def test_pidfile_tracks_the_live_proxy_process(self, start_proxy, cognito_home: Path, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)
        pidfile = cognito_home / ".bedrock-gateway" / "proxy.pid"
        assert pidfile.exists()
        # `exec` replaces the wrapper, so the recorded pid IS the proxy's pid.
        assert int(pidfile.read_text().strip()) == proxy.process.pid
        _drain(proxy.process)


# --------------------------------------------------------------------------
# Token injection
# --------------------------------------------------------------------------


class TestTokenInjection:
    """Every forwarded request must carry a current token, and only ours."""

    def test_request_arrives_with_bearer_token(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)
        status, echoed = _post_json(f"{proxy.url}/echo", {"prompt": "hello"})

        assert status == 200
        assert echoed["authorization"] == f"Bearer {SEEDED_ACCESS_TOKEN}"
        _drain(proxy.process)

    def test_client_supplied_authorization_is_replaced(self, start_proxy, upstream: UpstreamGateway) -> None:
        """Codex requires an env_key, so it sends a placeholder — it must not win."""
        proxy = start_proxy(upstream.url)
        status, echoed = _post_json(
            f"{proxy.url}/echo",
            {"prompt": "hi"},
            headers={"Authorization": "Bearer client-supplied-placeholder"},
        )

        assert status == 200
        assert echoed["authorization"] == f"Bearer {SEEDED_ACCESS_TOKEN}"
        assert "client-supplied-placeholder" not in json.dumps(echoed)
        _drain(proxy.process)

    def test_client_supplied_api_key_header_is_stripped(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)
        status, echoed = _post_json(
            f"{proxy.url}/echo",
            {"prompt": "hi"},
            headers={"x-api-key": "client-supplied-key"},
        )

        assert status == 200
        assert echoed["x_api_key"] is None
        _drain(proxy.process)

    def test_near_expiry_token_is_refreshed_before_forwarding(self, start_proxy, upstream: UpstreamGateway, tmp_path: Path) -> None:
        """Inside the helper's 5-min buffer the proxy must forward a NEW token."""
        log = tmp_path / "aws-calls.log"
        proxy = start_proxy(
            upstream.url,
            expires_at=1,  # long past — well inside the buffer
            extra_env={"MOCK_AWS_LOG": str(log)},
        )

        status, echoed = _post_json(f"{proxy.url}/echo", {"prompt": "hi"})

        assert status == 200
        assert "--auth-flow REFRESH_TOKEN_AUTH" in log.read_text()
        # The refreshed token, not the stale seeded one, reaches the gateway.
        assert echoed["authorization"] == f"Bearer {REFRESHED_ACCESS_TOKEN}"
        _drain(proxy.process)

    def test_valid_token_is_reused_without_a_refresh_call(self, start_proxy, upstream: UpstreamGateway, tmp_path: Path) -> None:
        """Regression guard: a healthy token must not trigger a Cognito call per request."""
        log = tmp_path / "aws-calls.log"
        proxy = start_proxy(upstream.url, extra_env={"MOCK_AWS_LOG": str(log)})

        for _ in range(3):
            status, _ = _post_json(f"{proxy.url}/echo", {"prompt": "hi"})
            assert status == 200

        assert not log.exists() or log.read_text().strip() == ""
        _drain(proxy.process)

    def test_refresh_failure_surfaces_as_a_proxy_error(self, start_proxy, upstream: UpstreamGateway) -> None:
        """A dead refresh token must not look like a gateway fault."""
        proxy = start_proxy(
            upstream.url,
            expires_at=1,
            extra_env={"MOCK_COGNITO_RESULT": "notauthorized"},
        )

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(f"{proxy.url}/echo", {"prompt": "hi"})

        assert excinfo.value.code == 502
        body = json.loads(excinfo.value.read().decode("utf-8"))
        assert body["error"] == "proxy_token_error"
        assert "bg-cognito-auth.sh status" in body["message"]
        # Nothing was forwarded: the request never reached the gateway.
        assert upstream.requests == []
        _drain(proxy.process)


# --------------------------------------------------------------------------
# Forwarding fidelity
# --------------------------------------------------------------------------


class TestForwarding:
    """The proxy must be transparent apart from the auth header."""

    def test_method_path_query_and_body_are_preserved(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)
        status, echoed = _post_json(f"{proxy.url}/echo?stream=true", {"prompt": "keep me"})

        assert status == 200
        assert echoed["method"] == "POST"
        assert echoed["path"] == "/echo?stream=true"
        assert json.loads(echoed["body"]) == {"prompt": "keep me"}
        _drain(proxy.process)

    def test_unrelated_client_headers_pass_through(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)
        status, echoed = _post_json(f"{proxy.url}/echo", {"a": 1}, headers={"x-custom-header": "kept"})

        assert status == 200
        assert echoed["custom"] == "kept"
        _drain(proxy.process)

    def test_gateway_base_path_is_prepended(self, start_proxy) -> None:
        """A gateway_url ending in /api must front the client's path (CloudFront needs it)."""
        with UpstreamGateway(base_path="/api") as gateway:
            proxy = start_proxy(gateway.url)
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                _post_json(f"{proxy.url}/echo", {"a": 1})
            # The mock upstream has no /api/echo route, which is exactly the proof.
            assert excinfo.value.code == 404
            assert gateway.requests[-1]["path"] == "/api/echo"
            _drain(proxy.process)

    def test_non_2xx_status_and_body_pass_through_unchanged(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(f"{proxy.url}/boom", {"a": 1})

        assert excinfo.value.code == 503
        body = json.loads(excinfo.value.read().decode("utf-8"))
        assert body == {"error": "upstream_exploded", "detail": "gateway said no"}
        _drain(proxy.process)

    def test_unreachable_gateway_reports_a_proxy_upstream_error(self, start_proxy) -> None:
        dead_port = _free_port()  # nothing is listening there
        proxy = start_proxy(f"http://127.0.0.1:{dead_port}")

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post_json(f"{proxy.url}/echo", {"a": 1})

        assert excinfo.value.code == 502
        assert json.loads(excinfo.value.read().decode("utf-8"))["error"] == "proxy_upstream_error"
        _drain(proxy.process)

    def test_get_requests_are_proxied(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)
        with urllib.request.urlopen(f"{proxy.url}/echo", timeout=20) as response:  # noqa: S310 - loopback test URL
            echoed = json.loads(response.read().decode("utf-8"))
        assert echoed["method"] == "GET"
        assert echoed["authorization"] == f"Bearer {SEEDED_ACCESS_TOKEN}"
        _drain(proxy.process)


# --------------------------------------------------------------------------
# Model-name normalization (OpenAI route)
# --------------------------------------------------------------------------


class TestModelNormalization:
    """Bare model slugs on the OpenAI route are prefixed for the gateway.

    Codex's in-app model picker writes short slugs (``gpt-5.6-sol``) into
    config.toml, but the gateway's OpenAI passthrough only serves models under
    their prefixed ids (``openai.gpt-5.6-sol``). The proxy closes that gap so
    switching models inside Codex does not 400 every subsequent request.
    """

    def _post_raw(self, url: str, raw: bytes) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - loopback test URL
            return json.loads(response.read().decode("utf-8"))

    def test_bare_model_on_openai_route_is_prefixed(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)
        status, echoed = _post_json(
            f"{proxy.url}/openai/v1/echo",
            {"model": "gpt-5.6-sol", "input": "hi", "stream": True},
        )

        assert status == 200
        forwarded = json.loads(echoed["body"])
        assert forwarded["model"] == "openai.gpt-5.6-sol"
        # The rest of the payload rides along unchanged.
        assert forwarded["input"] == "hi"
        assert forwarded["stream"] is True
        _drain(proxy.process)

    def test_already_prefixed_model_passes_through_byte_for_byte(self, start_proxy, upstream: UpstreamGateway) -> None:
        """No rewrite means no re-serialization: the exact client bytes arrive."""
        proxy = start_proxy(upstream.url)
        raw = b'{"model": "openai.gpt-5.6-sol",\n  "input": "hi"}'
        echoed = self._post_raw(f"{proxy.url}/openai/v1/echo", raw)
        assert echoed["body"].encode("utf-8") == raw
        _drain(proxy.process)

    def test_model_outside_openai_route_is_untouched(self, start_proxy, upstream: UpstreamGateway) -> None:
        """Bedrock/Anthropic model ids must never be prefixed."""
        proxy = start_proxy(upstream.url)
        raw = b'{"model": "global.anthropic.claude-opus-4-6-v1"}'
        echoed = self._post_raw(f"{proxy.url}/echo", raw)
        assert echoed["body"].encode("utf-8") == raw
        _drain(proxy.process)

    def test_non_json_body_on_openai_route_passes_through(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)
        raw = b"model=gpt-5.6-sol&not=json"
        echoed = self._post_raw(f"{proxy.url}/openai/v1/echo", raw)
        assert echoed["body"].encode("utf-8") == raw
        _drain(proxy.process)

    def test_rewrite_logs_model_names_but_never_the_prompt(self, start_proxy, upstream: UpstreamGateway) -> None:
        """The rewrite log line names the model — and nothing else from the body."""
        proxy = start_proxy(upstream.url)
        status, _ = _post_json(
            f"{proxy.url}/openai/v1/echo",
            {"model": "gpt-5.6-sol", "input": "SENTINEL-PROMPT-CONTENT"},
        )
        assert status == 200

        stdout, stderr = _drain(proxy.process)
        assert "'gpt-5.6-sol' -> 'openai.gpt-5.6-sol'" in stderr
        assert "SENTINEL-PROMPT-CONTENT" not in stdout + stderr


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


class TestStreaming:
    """Codex sends stream=true; a buffering proxy hangs it."""

    def test_sse_chunks_arrive_incrementally(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)

        request = urllib.request.Request(f"{proxy.url}/sse", data=b"{}", method="POST")
        arrivals: list[tuple[float, bytes]] = []
        start = time.monotonic()
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - loopback test URL
            assert response.headers.get("Content-Type") == "text/event-stream"
            while True:
                line = response.readline()
                if not line:
                    break
                if line.strip():
                    arrivals.append((time.monotonic() - start, line))

        payloads = [line.decode().strip() for _, line in arrivals]
        assert payloads == [f"data: chunk-{index}" for index in range(SSE_CHUNK_COUNT)] + ["data: [DONE]"]

        # The decisive assertion: the first chunk landed long before the last.
        # A proxy that buffered to EOF would deliver every chunk at ~the same,
        # late, timestamp. Measure the interval between chunks, not from the
        # request start: resolving/refreshing authentication happens before the
        # upstream stream starts and is independent of response buffering.
        first_arrival = arrivals[0][0]
        last_arrival = arrivals[-1][0]
        assert last_arrival - first_arrival > SSE_CHUNK_GAP_SECONDS, "chunks arrived in one batch — response was buffered"
        _drain(proxy.process)

    def test_streaming_response_does_not_forward_upstream_transfer_encoding(self, start_proxy, upstream: UpstreamGateway) -> None:
        """We relay a decoded body, so upstream framing headers must be dropped."""
        proxy = start_proxy(upstream.url)
        request = urllib.request.Request(f"{proxy.url}/sse", data=b"{}", method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - loopback test URL
            assert response.headers.get("Transfer-Encoding") is None
            response.read()
        _drain(proxy.process)


# --------------------------------------------------------------------------
# Security properties
# --------------------------------------------------------------------------


def _non_loopback_ipv4_addresses() -> list[str]:
    """Local non-loopback IPv4 addresses, if this host has any."""
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except socket.gaierror:
        pass
    return sorted(addresses)


class TestSecurityProperties:
    """The two properties that make this safe to run on a laptop."""

    def test_proxy_binds_loopback_only(self, start_proxy, upstream: UpstreamGateway) -> None:
        """Anything wider means anyone on the LAN can spend as the user."""
        proxy = start_proxy(upstream.url)

        # Loopback works...
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=5):
            pass

        # ...and every non-loopback local address refuses.
        for address in _non_loopback_ipv4_addresses():
            with pytest.raises(OSError):
                with socket.create_connection((address, proxy.port), timeout=3):
                    pass

        stderr = _drain(proxy.process)[1]
        assert f"listening on 127.0.0.1:{proxy.port}" in stderr

    def test_bind_address_is_not_configurable(self, proxy_script: Path) -> None:
        """Source-level guard: no flag may widen the bind address."""
        source = proxy_script.read_text()
        assert 'BIND_HOST = "127.0.0.1"' in source
        assert "0.0.0.0" not in source
        # The bind literal is the only host the server is ever handed.
        assert "ThreadingHTTPServer((BIND_HOST, port)" in source

    def test_token_never_appears_in_proxy_output(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)
        status, _ = _post_json(f"{proxy.url}/echo?api_key=should-not-be-logged", {"prompt": "hi"})
        assert status == 200

        stdout, stderr = _drain(proxy.process)
        assert SEEDED_ACCESS_TOKEN not in stdout
        assert SEEDED_ACCESS_TOKEN not in stderr
        assert SEEDED_REFRESH_TOKEN not in stdout
        assert SEEDED_REFRESH_TOKEN not in stderr
        # Query strings are the one URL component that leaks credentials.
        assert "should-not-be-logged" not in stderr

    def test_token_not_leaked_when_refresh_fails(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url, expires_at=1, extra_env={"MOCK_COGNITO_RESULT": "notauthorized"})
        with pytest.raises(urllib.error.HTTPError):
            _post_json(f"{proxy.url}/echo", {"prompt": "hi"})

        stdout, stderr = _drain(proxy.process)
        assert SEEDED_ACCESS_TOKEN not in stdout + stderr
        assert SEEDED_REFRESH_TOKEN not in stdout + stderr

    def test_request_bodies_are_never_logged(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)
        status, _ = _post_json(f"{proxy.url}/echo", {"prompt": "SENTINEL-PROMPT-CONTENT"})
        assert status == 200

        stdout, stderr = _drain(proxy.process)
        assert "SENTINEL-PROMPT-CONTENT" not in stdout + stderr

    def test_one_log_line_per_request_with_method_path_status(self, start_proxy, upstream: UpstreamGateway) -> None:
        proxy = start_proxy(upstream.url)
        for _ in range(2):
            _post_json(f"{proxy.url}/echo", {"a": 1})

        stderr = _drain(proxy.process)[1]
        request_lines = [line for line in stderr.splitlines() if "POST /echo" in line]
        assert len(request_lines) == 2
        assert all(line.endswith("-> 200") for line in request_lines)

    def test_pidfile_is_owner_only(self, start_proxy, cognito_home: Path, upstream: UpstreamGateway) -> None:
        import stat

        proxy = start_proxy(upstream.url)
        pidfile = cognito_home / ".bedrock-gateway" / "proxy.pid"
        assert stat.S_IMODE(pidfile.stat().st_mode) == 0o600
        _drain(proxy.process)


# --------------------------------------------------------------------------
# Regression
# --------------------------------------------------------------------------


class TestExistingCommandsUnchanged:
    """`serve` is purely additive."""

    def test_help_lists_serve(self, run_bg_cognito_auth) -> None:
        result = run_bg_cognito_auth(["help"])
        assert result.returncode == 0
        assert "serve" in result.stdout
        assert "Serve Options" in result.stdout
        for command in ("login", "import", "refresh", "logout", "status", "token"):
            assert command in result.stdout

    def test_script_syntax_is_valid(self, bg_cognito_auth_script: Path) -> None:
        result = subprocess.run(["bash", "-n", str(bg_cognito_auth_script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_proxy_script_compiles(self, proxy_script: Path) -> None:
        result = subprocess.run(["python3", "-m", "py_compile", str(proxy_script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_proxy_script_imports_only_stdlib(self, proxy_script: Path) -> None:
        """Zero-install property: no pip dependencies may creep in."""
        import ast

        tree = ast.parse(proxy_script.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])

        import sys as _sys

        assert imported <= _sys.stdlib_module_names, f"non-stdlib imports: {imported - _sys.stdlib_module_names}"

    def test_token_subcommand_still_works_alongside_serve(self, run_bg_cognito_auth, cognito_home: Path) -> None:
        _seed_session(cognito_home, "https://gw.example.com/api", int(time.time()) + 3600)
        result = run_bg_cognito_auth(["token"])
        assert result.returncode == 0, result.stderr
        assert result.stdout == SEEDED_ACCESS_TOKEN

    def test_status_still_works_alongside_serve(self, run_bg_cognito_auth, cognito_home: Path) -> None:
        _seed_session(cognito_home, "https://gw.example.com/api", int(time.time()) + 3600)
        result = run_bg_cognito_auth(["status"])
        assert result.returncode == 0
        assert "Token Status: Valid" in result.stdout
