"""The loopback auth proxy serves entitled CLI tools, not web pages (Issue #5686).

The proxy injects the user's Cognito token into every request it relays. Binding
to 127.0.0.1 decides which *machines* can reach it; it does not decide which
*callers* may spend that token, and conflating the two is the bug under test.

Two callers reach loopback without being the user's CLI tool:

* **A web page.** Browsers allow ``fetch('http://127.0.0.1:9191/...')`` from any
  site. The page never sees the token — it is injected here — but it does not
  need to: it makes the proxy spend the token and reads the reply. A *simple*
  request (``Content-Type: text/plain``) is not preflighted at all, so a CORS
  response policy cannot prevent the request from being relayed.
* **Any other local process**, since loopback is not a boundary between users or
  processes on one machine.

Before this change all of the following were relayed upstream with a valid
``Authorization: Bearer <user token>`` attached, each verified against the code
under test: a cross-origin POST, a POST whose ``Host`` named an attacker domain
(DNS rebinding), a non-preflighted simple request, and a request with no
credential at all. ``GET /_adp/proxy`` also disclosed the deployment and gateway
URL to any page that asked.

Every test here asserts BOTH halves of the property: the request is refused
*and* it never reached the upstream gateway. Asserting only the status code
would keep passing if a future refactor moved the guard after the relay, and
asserting only that the upstream saw nothing would pass vacuously whenever the
proxy failed for an unrelated reason (a failing token helper returns 502 before
any upstream is opened). A recording upstream is used rather than a mock,
because "did the user's credential leave this machine" is the actual claim.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

CLI = Path(__file__).parents[2] / "cli"
PROXY = CLI / "bg-gateway-proxy.py"

_spec = importlib.util.spec_from_file_location("bg_gateway_proxy_origin_guard", PROXY)
proxy_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proxy_module)

# The token the fake auth helper mints. If this string ever reaches the recording
# upstream from a refused request, the guard has failed in the way that matters.
USER_TOKEN = "USER-COGNITO-TOKEN-MUST-NOT-LEAK"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"nothing came up on 127.0.0.1:{port}")


class RecordingUpstream:
    """A stand-in gateway that records every request it receives.

    Runs as a separate process, like the proxy, so the proxy talks to it over a
    real socket exactly as it would talk to CloudFront.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.port = _free_port()
        self.hits = tmp_path / "upstream-hits.log"
        script = tmp_path / "upstream.py"
        script.write_text(
            "import sys\n"
            "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
            "port, log = int(sys.argv[1]), sys.argv[2]\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def log_message(self, *a): pass\n"
            "    def _record(self):\n"
            "        n = int(self.headers.get('Content-Length') or 0)\n"
            "        self.rfile.read(n)\n"
            "        open(log, 'a').write(\n"
            "            self.command + ' ' + self.path + ' ' + (self.headers.get('Authorization') or '-') + '\\n'\n"
            "        )\n"
            "        body = b'{}'\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Type', 'application/json')\n"
            "        self.send_header('Content-Length', str(len(body)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(body)\n"
            "    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _record\n"
            "ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()\n"
        )
        self.process = subprocess.Popen([sys.executable, str(script), str(self.port), str(self.hits)], stdin=subprocess.DEVNULL)
        _wait_for_port(self.port)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def received(self) -> str:
        # The proxy relays asynchronously from the client's point of view; a short
        # settle window keeps "saw nothing" from being a race rather than a refusal.
        time.sleep(0.3)
        return self.hits.read_text() if self.hits.exists() else ""

    def stop(self) -> None:
        self.process.kill()
        self.process.wait(timeout=5)


class RunningProxy:
    """A real ``bg-gateway-proxy.py`` process with a helper that mints a token.

    The helper must SUCCEED. With a failing helper the proxy returns 502 before
    opening any upstream, so every "the gateway saw nothing" assertion would hold
    regardless of whether the origin guard works.
    """

    def __init__(self, tmp_path: Path, gateway_url: str) -> None:
        self.runtime = tmp_path / "proxy"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.identity_file = self.runtime / "proxy.json"
        helper = self.runtime / "helper.sh"
        helper.write_text(f"#!/usr/bin/env bash\nprintf '%s' {USER_TOKEN!r}\n")
        helper.chmod(0o755)
        self.log_path = self.runtime / "proxy.log"
        self._argv = [
            sys.executable,
            str(PROXY),
            "--gateway-url",
            gateway_url,
            "--auth-helper",
            str(helper),
            "--port",
            "0",
            "--identity-file",
            str(self.identity_file),
            "--deployment-id",
            "d1111111",
            "--deployment",
            "dev",
        ]
        self.process: subprocess.Popen | None = None
        self.identity: dict = {}

    def start(self) -> RunningProxy:
        self.log = open(self.log_path, "w")  # noqa: SIM115 - closed in stop()
        self.process = subprocess.Popen(self._argv, stdout=self.log, stderr=self.log, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.identity_file.exists():
                try:
                    self.identity = json.loads(self.identity_file.read_text())
                    break
                except ValueError:
                    pass  # atomic write, so a racing reader just retries
            time.sleep(0.05)
        else:
            raise AssertionError("the proxy never published its identity")
        return self

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.identity['port']}"

    @property
    def capability(self) -> str:
        return str(self.identity[proxy_module.IDENTITY_CAPABILITY_KEY])

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        self.log.close()


@pytest.fixture
def upstream(tmp_path: Path):
    server = RecordingUpstream(tmp_path)
    yield server
    server.stop()


@pytest.fixture
def proxy(tmp_path: Path, upstream: RecordingUpstream):
    instance = RunningProxy(tmp_path, upstream.url).start()
    yield instance
    instance.stop()


def _request(proxy: RunningProxy, headers: dict[str, str], method: str = "POST", path: str = "/openai/v1/responses") -> int:
    """Issue a request and return its status, treating a refusal as a status."""
    request = urllib.request.Request(f"{proxy.base}{path}", data=b"{}" if method == "POST" else None, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - loopback literal
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


class TestAMaliciousWebsiteCannotSpendTheToken:
    """The headline attack: a page the user visits drives their credential."""

    def test_a_cross_origin_post_is_refused_and_never_relayed(self, proxy, upstream):
        status = _request(proxy, {"Origin": "https://evil.example.com", "Authorization": f"Bearer {proxy.capability}"})

        assert status == 403
        # Even WITH a valid capability, because a page that somehow obtained one
        # must still be refused — that is what makes this defence in depth rather
        # than a single point of failure.
        assert upstream.received() == "", "the user's token was relayed for a web page"

    def test_a_simple_request_that_is_never_preflighted_is_refused(self, proxy, upstream):
        """A CORS *response* policy cannot stop this, which is why the guard runs
        on the actual request rather than only on the preflight."""
        status = _request(
            proxy,
            {"Origin": "https://evil.example.com", "Content-Type": "text/plain", "Authorization": f"Bearer {proxy.capability}"},
        )

        assert status == 403
        assert upstream.received() == ""

    @pytest.mark.parametrize("origin", ["null", "https://evil.example.com", "http://localhost:3000", "file://"])
    def test_every_browser_origin_is_refused_including_null(self, proxy, upstream, origin):
        """``null`` is what a sandboxed iframe, a ``file://`` page and some
        redirects send. It is the least trustworthy origin, not an absent one, so
        it is refused like any other rather than treated as "no origin".

        A localhost origin is included deliberately: a page served from the user's
        own dev server is still a page, and must not be allowlisted.
        """
        status = _request(proxy, {"Origin": origin, "Authorization": f"Bearer {proxy.capability}"})

        assert status == 403
        assert upstream.received() == ""

    @pytest.mark.parametrize("site", ["cross-site", "same-site"])
    def test_a_browsers_fetch_metadata_alone_is_enough_to_refuse(self, proxy, upstream, site):
        """``Sec-Fetch-Site`` cannot be forged by page JavaScript, so it is a
        reliable second signal when ``Origin`` is stripped by an intermediary."""
        status = _request(proxy, {"Sec-Fetch-Site": site, "Authorization": f"Bearer {proxy.capability}"})

        assert status == 403
        assert upstream.received() == ""

    def test_a_preflight_is_answered_locally_and_grants_nothing(self, proxy, upstream):
        """The refusal must be ours, not the gateway's.

        Forwarding the preflight would spend a round trip asking the gateway about
        *this* proxy's policy, and would inherit a permissive gateway CORS policy
        as our own. The response must carry no ``Access-Control-Allow-*`` header,
        which is what makes a browser abandon the real request.
        """
        request = urllib.request.Request(  # noqa: S310
            f"{proxy.base}/openai/v1/responses",
            method="OPTIONS",
            headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "POST"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - loopback literal
                status, headers = response.status, response.headers
        except urllib.error.HTTPError as exc:
            status, headers = exc.code, exc.headers

        assert status == 403
        assert not [name for name in headers if name.lower().startswith("access-control-allow")]
        assert upstream.received() == "", "the preflight was forwarded to the gateway"


class TestDnsRebindingIsDefeated:
    """``evil.example`` resolving to 127.0.0.1 arrives on loopback legitimately.

    The address the packet came from cannot distinguish this case, so the guard
    checks the host the caller *asked for* instead.
    """

    @pytest.mark.parametrize("host", ["evil.example.com", "attacker.test:9191", "gateway.internal"])
    def test_a_foreign_host_header_is_refused_and_never_relayed(self, proxy, upstream, host):
        status = _request(proxy, {"Host": host, "Authorization": f"Bearer {proxy.capability}"})

        assert status == 403
        assert upstream.received() == ""

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
    def test_the_real_loopback_names_are_accepted(self, proxy, upstream, host):
        """The guard must not break a legitimate client that spells loopback
        differently, including with an explicit port."""
        port = proxy.identity["port"]
        status = _request(proxy, {"Host": f"{host}:{port}", "Authorization": f"Bearer {proxy.capability}"})

        assert status == 200
        assert USER_TOKEN in upstream.received()


class TestOnlyAnEntitledLocalCallerMayspendTheToken:
    """Loopback is not a boundary between processes, so entitlement is proven."""

    def test_a_request_with_no_capability_is_refused(self, proxy, upstream):
        """This is the other local process case: no browser markers at all, and
        still refused, because reaching loopback is not entitlement."""
        status = _request(proxy, {})

        assert status == 403
        assert upstream.received() == ""

    def test_a_wrong_capability_is_refused(self, proxy, upstream):
        status = _request(proxy, {"Authorization": "Bearer " + "z" * len(proxy.capability)})

        assert status == 403
        assert upstream.received() == ""

    def test_the_legacy_placeholder_is_not_a_capability(self, proxy, upstream):
        """``ADP_GATEWAY_DUMMY=unused`` is printed in the README and exported by
        many users' shell rc files. If the placeholder were accepted, the
        capability would be a publicly known word and the guard would be
        decorative."""
        status = _request(proxy, {"Authorization": "Bearer unused"})

        assert status == 403
        assert upstream.received() == ""

    def test_a_valid_cli_request_still_works_end_to_end(self, proxy, upstream):
        """The regression that matters most: the user's own tool must keep working,
        and the REAL token — not the capability — must arrive upstream."""
        status = _request(proxy, {"Authorization": f"Bearer {proxy.capability}"})

        assert status == 200
        received = upstream.received()
        assert f"Bearer {USER_TOKEN}" in received, "the gateway did not get the user's token"
        assert proxy.capability not in received, "the local capability leaked to the gateway"

    def test_the_capability_is_unguessable_and_per_process(self, tmp_path, upstream):
        """Two proxies must not share a capability, or one deployment's client
        would be entitled to another's proxy."""
        first = RunningProxy(tmp_path / "a", upstream.url).start()
        second = RunningProxy(tmp_path / "b", upstream.url).start()
        try:
            assert first.capability != second.capability
            assert len(first.capability) >= proxy_module.MIN_CAPABILITY_LENGTH
        finally:
            first.stop()
            second.stop()

    def test_the_capability_is_readable_only_by_its_owner(self, proxy):
        """It lives in the identity file, so that file's mode IS the boundary."""
        assert proxy.identity_file.stat().st_mode & 0o077 == 0


class TestTheGuardLeaksNothing:
    def test_the_capability_never_appears_in_the_log(self, proxy, upstream):
        """Users paste this log into issues, and a refusal logs the reason rather
        than the offending credential."""
        _request(proxy, {"Authorization": f"Bearer {proxy.capability}"})
        _request(proxy, {"Origin": "https://evil.example.com"})
        _request(proxy, {"Authorization": "Bearer wrong-but-long-enough-value"})

        log = proxy.log_path.read_text()

        assert proxy.capability not in log
        assert USER_TOKEN not in log
        assert "wrong-but-long-enough-value" not in log
        # The refusal is still diagnosable without those values.
        assert "403" in log

    def test_a_refusal_body_does_not_echo_the_attackers_origin(self, proxy):
        """Echoing it back would make the proxy a small reflection surface and
        could confuse a reader into thinking the origin was recognised."""
        request = urllib.request.Request(  # noqa: S310
            f"{proxy.base}/openai/v1/responses",
            data=b"{}",
            method="POST",
            headers={"Origin": "https://evil.example.com"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10):  # noqa: S310 - loopback literal
                raise AssertionError("expected a refusal")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()

        assert "evil.example.com" not in body
        assert json.loads(body)["error"] == "proxy_forbidden_origin"


class TestTheLauncherCanStillFindAndReuseAProxy:
    """The identity route predates this change and must keep working (#5413).

    It is answered WITHOUT a capability on purpose: a launcher for deployment B
    asks a running proxy for deployment A "whose are you?" in order to decline to
    reuse it, and cannot know A's capability. It must still be closed to the web.
    """

    def test_the_identity_route_answers_an_entitled_local_launcher(self, proxy):
        request = urllib.request.Request(f"{proxy.base}{proxy_module.IDENTITY_PATH}")  # noqa: S310
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - loopback literal
            identity = json.loads(response.read())

        assert identity["proxy"] == "adp-gateway-proxy"
        assert identity["deployment_id"] == "d1111111"

    def test_the_identity_route_is_closed_to_a_web_page(self, proxy):
        """Otherwise a page could read which gateway the user is pointed at."""
        status = _request(proxy, {"Origin": "https://evil.example.com"}, method="GET", path=proxy_module.IDENTITY_PATH)

        assert status == 403

    def test_the_identity_route_refuses_a_rebinding_host(self, proxy):
        status = _request(proxy, {"Host": "evil.example.com"}, method="GET", path=proxy_module.IDENTITY_PATH)

        assert status == 403

    def test_the_identity_route_never_reveals_the_capability(self, proxy):
        """It is reachable without a capability, so echoing one would hand the
        secret to precisely the callers this guard excludes."""
        request = urllib.request.Request(f"{proxy.base}{proxy_module.IDENTITY_PATH}")  # noqa: S310
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - loopback literal
            body = response.read().decode()

        assert proxy.capability not in body
        assert proxy_module.IDENTITY_CAPABILITY_KEY not in json.loads(body)


class TestTheUnitLevelHelpers:
    """Focused checks on the pure helpers, where the process-level tests above
    cannot easily reach every branch."""

    @pytest.mark.parametrize(
        ("supplied", "expected"),
        [
            ("unused", ""),  # the documented legacy placeholder
            ("UNUSED", ""),  # case must not be an escape hatch
            ("dummy", ""),
            ("  unused  ", ""),  # whitespace must not be an escape hatch
            ("short", ""),  # brute-forceable
            ("", ""),
            (None, ""),
            ("a-genuinely-long-supplied-capability", "a-genuinely-long-supplied-capability"),
        ],
    )
    def test_a_placeholder_is_never_trusted_as_a_capability(self, supplied, expected):
        assert proxy_module.usable_capability(supplied) == expected

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Bearer abc123", "abc123"),
            ("bearer abc123", "abc123"),  # scheme is case-insensitive per RFC 9110
            ("abc123", "abc123"),  # a bare value, for a hand-rolled curl client
            ("  Bearer   abc123  ", "abc123"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_the_presented_capability_is_parsed_from_either_form(self, header, expected):
        assert proxy_module.presented_capability(header) == expected

    def test_a_minted_capability_is_random_and_long(self):
        minted = {proxy_module.mint_capability() for _ in range(50)}

        assert len(minted) == 50
        assert all(len(value) >= proxy_module.MIN_CAPABILITY_LENGTH for value in minted)


@pytest.mark.parametrize("model", ["global.moonshotai.kimi-k3", "us.moonshotai.kimi-k3", "us.openai.gpt-6-sol", "openai.gpt-6-sol"])
def test_qualified_model_ids_are_not_rewritten(model):
    handler = object.__new__(proxy_module.GatewayProxyHandler)
    handler.path = "/openai/v1/responses"
    body = json.dumps({"model": model, "input": "hello"}).encode()
    assert handler._normalize_model(body) == body


def test_bare_gpt_slug_still_normalizes():
    handler = object.__new__(proxy_module.GatewayProxyHandler)
    handler.path = "/openai/v1/responses"
    handler.log_message = lambda *args: None
    body = json.dumps({"model": "gpt-6-sol"}).encode()
    assert json.loads(handler._normalize_model(body))["model"] == "openai.gpt-6-sol"
