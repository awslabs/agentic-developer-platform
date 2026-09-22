"""Focused tests for URL-analysis connection-boundary enforcement."""

from __future__ import annotations

import ast
import http.client
import socket
import ssl
from pathlib import Path
from unittest.mock import ANY, Mock, patch

import pytest
from browser_guard import (
    DEFAULT_ANALYSIS_TIMEOUT_SECONDS,
    REASON_FETCH_DEADLINE_EXCEEDED,
    REASON_RESPONSE_TOO_LARGE,
    DestinationRefused,
    NavigationGuard,
    PinnedHTTPTransport,
    PinnedResponse,
    _PinnedHTTPSConnection,
    open_guarded_browser,
    vet_destination,
)
from denylist import (
    REASON_ADDRESS_NOT_APPROVED,
    REASON_BLOCKED_ADDRESS,
    REASON_RESOLUTION_FAILED,
    REASON_SCHEME_NOT_ALLOWED,
    DenylistConfig,
    DenylistResult,
    check_url,
)


def _dns(*ips: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 80)) for ip in ips]


class FakeBrowserClient:
    def __init__(self, region: str = "us-east-1", events: list[str] | None = None):
        self.region = region
        self.events = events if events is not None else []
        self.stopped = False
        self.session_timeout_seconds = None

    def start(self, *, session_timeout_seconds: int) -> str:
        self.events.append("client.start")
        self.session_timeout_seconds = session_timeout_seconds
        return "session-abc123"

    def generate_ws_headers(self):
        self.events.append("client.headers")
        return "wss://browser.example", {"Authorization": "signed"}

    def stop(self) -> None:
        self.events.append("client.stop")
        self.stopped = True


class FakePage:
    def __init__(self, events: list[str]):
        self.events = events
        self.url = "about:blank"
        self.main_frame = object()
        self.closed = False

    def goto(self, url: str, **kwargs):
        self.url = url
        self.events.append("page.goto")
        return (url, kwargs)

    def screenshot(self, **kwargs):
        return kwargs

    def inner_text(self, selector: str, **kwargs):
        return (selector, kwargs)

    def title(self) -> str:
        return "title"

    def evaluate(self, expression: str, arg=None):
        return (expression, arg)

    def on(self, event: str, callback) -> None:
        self.events.append(f"page.on:{event}")

    def close(self) -> None:
        self.events.append("page.close")
        self.closed = True


class FakeContext:
    def __init__(self, events: list[str]):
        self.events = events
        self.route_handler = None
        self.websocket_handler = None
        self.closed = False

    def route(self, pattern, handler) -> None:
        self.events.append("context.route")
        self.route_handler = handler

    def route_web_socket(self, pattern, handler) -> None:
        self.events.append("context.route_web_socket")
        self.websocket_handler = handler

    def new_page(self) -> FakePage:
        self.events.append("context.new_page")
        return FakePage(self.events)

    def close(self) -> None:
        self.events.append("context.close")
        self.closed = True


class FakeBrowser:
    def __init__(self, events: list[str]):
        self.events = events
        self.context_options = None
        self.context = FakeContext(events)
        self.closed = False

    def new_context(self, **kwargs) -> FakeContext:
        self.events.append("browser.new_context")
        self.context_options = kwargs
        return self.context

    def close(self) -> None:
        self.events.append("browser.close")
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser, events: list[str]):
        self.browser = browser
        self.events = events

    def connect_over_cdp(self, ws_url: str, *, headers: dict[str, str]):
        self.events.append("chromium.connect")
        assert ws_url == "wss://browser.example"
        assert headers == {"Authorization": "signed"}
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser, events: list[str]):
        self.chromium = FakeChromium(browser, events)


class FakeRequest:
    def __init__(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        all_headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> None:
        self.url = url
        self.method = method
        self.headers = headers or {}
        self._all_headers = all_headers if all_headers is not None else self.headers
        self.post_data_buffer = body

    def all_headers(self) -> dict[str, str]:
        return self._all_headers


class FakeRoute:
    def __init__(self, url: str) -> None:
        self.request = FakeRequest(url)
        self.fulfilled_with = None
        self.aborted_with = None
        self.continued = False

    def continue_(self) -> None:
        self.continued = True

    def fulfill(self, **kwargs) -> None:
        self.fulfilled_with = kwargs

    def abort(self, error_code: str = "failed") -> None:
        self.aborted_with = error_code


class FakeTransport:
    def __init__(self, response: PinnedResponse | None = None) -> None:
        self.response = response or PinnedResponse(200, {"X-Test": "yes"}, b"ok")
        self.calls = []

    def fetch(self, request, decision, config=None) -> PinnedResponse:
        self.calls.append((request, decision, config))
        return self.response


class RefusingTransport:
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code

    def fetch(self, request, decision, config=None) -> PinnedResponse:
        raise DestinationRefused(
            request.url,
            DenylistResult(
                allowed=False,
                reason="transport budget exceeded",
                reason_code=self.reason_code,
            ),
        )


class FakeWebSocketRoute:
    def __init__(self, url: str) -> None:
        self.url = url
        self.connect_to_server = Mock()
        self.closed_with = None

    def close(self, *, code=None, reason=None) -> None:
        self.closed_with = (code, reason)


def _open_fake_session(url: str = "https://example.com/", **kwargs):
    events: list[str] = []
    browser = FakeBrowser(events)
    client = FakeBrowserClient(events=events)
    playwright = FakePlaywright(browser, events)
    session = open_guarded_browser(
        url,
        playwright,
        client_factory=lambda region: client,
        transport=kwargs.pop("transport", FakeTransport()),
        **kwargs,
    )
    return session, client, browser, events


class TestStructuralEnforcement:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8080/",
            "http://10.0.0.5/admin",
            "http://2130706433/",
            "http://[::ffff:169.254.169.254]/",
            "http://100.64.1.1/",
            "http://[fec0::]/",
            "http://[feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff]/",
        ],
    )
    def test_blocked_destination_constructs_no_client(self, url: str) -> None:
        factory = Mock()
        with pytest.raises(DestinationRefused):
            open_guarded_browser(url, Mock(), client_factory=factory)
        factory.assert_not_called()

    @patch("socket.getaddrinfo", side_effect=socket.gaierror("not found"))
    def test_unresolved_destination_constructs_no_client(self, mock_dns) -> None:
        factory = Mock()
        with pytest.raises(DestinationRefused) as raised:
            open_guarded_browser(
                "https://does-not-resolve.invalid/", Mock(), client_factory=factory
            )
        assert raised.value.reason_code == REASON_RESOLUTION_FAILED
        factory.assert_not_called()

    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_remote_session_lifetime_matches_analysis_deadline(self, mock_dns) -> None:
        session, client, _, _ = _open_fake_session()

        assert client.session_timeout_seconds == DEFAULT_ANALYSIS_TIMEOUT_SECONDS
        session.close()

    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_routes_are_installed_before_page_is_created(self, mock_dns) -> None:
        session, client, browser, events = _open_fake_session()

        assert browser.context_options["offline"] is True
        assert browser.context_options["service_workers"] == "block"
        assert events.index("context.route") < events.index("context.new_page")
        assert events.index("context.route_web_socket") < events.index(
            "context.new_page"
        )
        assert not hasattr(session, "page")
        assert not hasattr(session, "client")
        assert session.session_id == "session-abc123"

        session.close()
        assert client.stopped
        assert browser.context.closed
        assert browser.closed

    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_service_workers_cannot_be_reenabled_by_context_options(
        self, mock_dns
    ) -> None:
        session, _, browser, _ = _open_fake_session(
            context_options={
                "offline": False,
                "service_workers": "allow",
                "accept_downloads": True,
            }
        )
        assert browser.context_options == {
            "offline": True,
            "service_workers": "block",
            "accept_downloads": True,
        }
        session.close()

    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_session_facade_exposes_guarded_page_operations(self, mock_dns) -> None:
        session, _, _, _ = _open_fake_session()
        assert session.goto("https://example.com/path", timeout=1000) == (
            "https://example.com/path",
            {"timeout": 1000},
        )
        assert session.url == "https://example.com/path"
        assert session.title() == "title"
        session.close()

    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_setup_failure_stops_remote_session(self, mock_dns) -> None:
        events: list[str] = []
        browser = FakeBrowser(events)
        browser.new_context = Mock(side_effect=RuntimeError("context failed"))
        client = FakeBrowserClient(events=events)

        with pytest.raises(RuntimeError, match="context failed"):
            open_guarded_browser(
                "https://example.com/",
                FakePlaywright(browser, events),
                client_factory=lambda region: client,
            )

        assert client.stopped
        assert browser.closed

    @pytest.mark.parametrize(
        "example",
        sorted(
            (Path(__file__).parents[1] / "examples").glob("*.py"),
            key=lambda path: path.name,
        ),
    )
    def test_examples_use_only_unprivileged_broker_client(self, example: Path) -> None:
        tree = ast.parse(example.read_text())
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        call_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        assert "browser_client" in imported_modules
        assert "analyze_url" in call_names
        assert not imported_modules & {"boto3", "playwright", "bedrock_agentcore"}
        assert "open_guarded_browser" not in call_names


class TestPinnedTransport:
    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_allowed_route_is_fulfilled_without_browser_network(self, mock_dns) -> None:
        vetted = check_url("https://example.com/")
        transport = FakeTransport()
        guard = NavigationGuard(vetted, "example.com", transport=transport)
        route = FakeRoute("https://example.com/app.js")

        guard.handle_route(route)

        assert route.fulfilled_with == {
            "status": 200,
            "headers": {"X-Test": "yes"},
            "body": b"ok",
        }
        assert not route.continued
        assert route.aborted_with is None
        assert transport.calls[0][1].resolved_ips == ["93.184.216.34"]

    def test_transport_connects_to_vetted_ip_not_a_second_resolution(self) -> None:
        connection = Mock()
        connection.sock = Mock()
        response = Mock()
        response.status = 200
        response.getheaders.return_value = [("Content-Type", "text/plain")]
        response.read1.side_effect = [b"safe", b""]
        connection.getresponse.return_value = response
        transport = PinnedHTTPTransport()
        transport._open_connection = Mock(return_value=connection)
        request = FakeRequest(
            "https://attacker.example/path?q=1",
            headers={"Host": "wrong.example", "Accept": "text/plain"},
        )
        decision = DenylistResult(allowed=True, resolved_ips=["93.184.216.34"])

        result = transport.fetch(request, decision)

        transport._open_connection.assert_called_once_with(
            "https",
            "attacker.example",
            443,
            "93.184.216.34",
            ANY,
        )
        connection.request.assert_called_once_with(
            "GET", "/path?q=1", body=None, headers={"Accept": "text/plain"}
        )
        assert result.body == b"safe"
        connection.close.assert_called_once()

    def test_transport_forwards_cookie_from_complete_playwright_headers(self) -> None:
        connection = Mock()
        connection.sock = Mock()
        response = Mock(status=200)
        response.getheaders.return_value = []
        response.read1.return_value = b""
        connection.getresponse.return_value = response
        transport = PinnedHTTPTransport()
        transport._open_connection = Mock(return_value=connection)
        request = FakeRequest(
            "https://example.com/account",
            headers={"Accept": "text/html"},
            all_headers={
                "Accept": "text/html",
                "Cookie": "session=guarded",
                "Host": "example.com",
            },
        )
        decision = DenylistResult(allowed=True, resolved_ips=["93.184.216.34"])

        transport.fetch(request, decision)

        connection.request.assert_called_once_with(
            "GET",
            "/account",
            body=None,
            headers={"Accept": "text/html", "Cookie": "session=guarded"},
        )

    def test_declared_oversized_response_is_refused_before_read(self) -> None:
        connection = Mock()
        connection.sock = Mock()
        response = Mock(status=200)
        response.getheaders.return_value = [("Content-Length", "5")]
        connection.getresponse.return_value = response
        transport = PinnedHTTPTransport(max_response_bytes=4)
        transport._open_connection = Mock(return_value=connection)

        with pytest.raises(DestinationRefused) as raised:
            transport.fetch(
                FakeRequest("https://example.com/large"),
                DenylistResult(allowed=True, resolved_ips=["93.184.216.34"]),
            )

        assert raised.value.reason_code == REASON_RESPONSE_TOO_LARGE
        response.read1.assert_not_called()

    def test_streamed_response_is_bounded_without_content_length(self) -> None:
        connection = Mock()
        connection.sock = Mock()
        response = Mock(status=200)
        response.getheaders.return_value = []
        response.read1.side_effect = [b"1234", b"5"]
        connection.getresponse.return_value = response
        transport = PinnedHTTPTransport(max_response_bytes=4)
        transport._open_connection = Mock(return_value=connection)

        with pytest.raises(DestinationRefused) as raised:
            transport.fetch(
                FakeRequest("https://example.com/drip"),
                DenylistResult(allowed=True, resolved_ips=["93.184.216.34"]),
            )

        assert raised.value.reason_code == REASON_RESPONSE_TOO_LARGE

    def test_analysis_byte_budget_is_cumulative_across_requests(self) -> None:
        connections = []
        for body in (b"1234", b"567"):
            connection = Mock()
            connection.sock = Mock()
            response = Mock(status=200)
            response.getheaders.return_value = []
            response.read1.side_effect = [body, b""]
            connection.getresponse.return_value = response
            connections.append(connection)
        transport = PinnedHTTPTransport(max_response_bytes=10, max_analysis_bytes=6)
        transport._open_connection = Mock(side_effect=connections)
        decision = DenylistResult(allowed=True, resolved_ips=["93.184.216.34"])

        transport.fetch(FakeRequest("https://example.com/one"), decision)
        with pytest.raises(DestinationRefused) as raised:
            transport.fetch(FakeRequest("https://example.com/two"), decision)

        assert raised.value.reason_code == REASON_RESPONSE_TOO_LARGE

    def test_drip_response_cannot_exceed_wall_clock_deadline(self) -> None:
        now = [0.0]
        connection = Mock()
        connection.sock = Mock()
        response = Mock(status=200)
        response.getheaders.return_value = []

        def delayed_chunk(size: int) -> bytes:
            now[0] = 2.0
            return b"x"

        response.read1.side_effect = delayed_chunk
        connection.getresponse.return_value = response
        transport = PinnedHTTPTransport(
            response_timeout=1, analysis_timeout=10, clock=lambda: now[0]
        )
        transport._open_connection = Mock(return_value=connection)

        with pytest.raises(DestinationRefused) as raised:
            transport.fetch(
                FakeRequest("https://example.com/slow"),
                DenylistResult(allowed=True, resolved_ips=["93.184.216.34"]),
            )

        assert raised.value.reason_code == REASON_FETCH_DEADLINE_EXCEEDED

    def test_deadline_socket_timeout_has_distinct_refusal_reason(self) -> None:
        connection = Mock()
        connection.sock = Mock()
        response = Mock(status=200)
        response.getheaders.return_value = []
        response.read1.side_effect = TimeoutError("timed out")
        connection.getresponse.return_value = response
        transport = PinnedHTTPTransport()
        transport._open_connection = Mock(return_value=connection)

        with pytest.raises(DestinationRefused) as raised:
            transport.fetch(
                FakeRequest("https://example.com/slow"),
                DenylistResult(allowed=True, resolved_ips=["93.184.216.34"]),
            )

        assert raised.value.reason_code == REASON_FETCH_DEADLINE_EXCEEDED

    def test_tls_socket_uses_vetted_ip_and_original_hostname_for_sni(self) -> None:
        context = Mock(spec=ssl.SSLContext)
        raw_socket = object()
        wrapped_socket = object()
        context.wrap_socket.return_value = wrapped_socket
        connection = _PinnedHTTPSConnection(
            "attacker.example", "93.184.216.34", 443, 5, context
        )
        connection._create_connection = Mock(return_value=raw_socket)

        connection.connect()

        connection._create_connection.assert_called_once_with(
            ("93.184.216.34", 443), 5, None
        )
        context.wrap_socket.assert_called_once_with(
            raw_socket, server_hostname="attacker.example"
        )
        assert connection.sock is wrapped_socket

    def test_transport_rechecks_selected_address_before_socket_open(self) -> None:
        transport = PinnedHTTPTransport()
        transport._open_connection = Mock()
        decision = DenylistResult(allowed=True, resolved_ips=["169.254.169.254"])

        with pytest.raises(DestinationRefused) as raised:
            transport.fetch(FakeRequest("http://example.com/"), decision)

        assert raised.value.reason_code == REASON_BLOCKED_ADDRESS
        transport._open_connection.assert_not_called()


class TestNavigationAndSubresources:
    @pytest.mark.parametrize(
        "redirect_url",
        [
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            "http://127.0.0.1/",
            "http://192.168.1.1/",
            "http://2130706433/",
            "http://[::ffff:169.254.169.254]/",
            "http://[fec0::1]/",
        ],
    )
    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_internal_redirect_is_aborted(self, mock_dns, redirect_url: str) -> None:
        vetted = check_url("https://example.com/")
        guard = NavigationGuard(vetted, "example.com", transport=FakeTransport())
        route = FakeRoute(redirect_url)
        guard.handle_route(route)
        assert route.aborted_with == "blockedbyclient"
        assert route.fulfilled_with is None
        assert guard.refusals[0]["reason_code"] == REASON_BLOCKED_ADDRESS

    @patch("socket.getaddrinfo")
    def test_target_rebinding_to_unvetted_public_ip_is_aborted(self, mock_dns) -> None:
        mock_dns.return_value = _dns("93.184.216.34")
        vetted = check_url("https://rebind.example.com/")
        guard = NavigationGuard(vetted, "rebind.example.com", transport=FakeTransport())
        mock_dns.return_value = _dns("8.8.8.8")

        route = FakeRoute("https://rebind.example.com/next")
        guard.handle_route(route)

        assert route.aborted_with == "blockedbyclient"
        assert guard.refusals[0]["reason_code"] == REASON_ADDRESS_NOT_APPROVED

    @patch("socket.getaddrinfo")
    def test_terminal_dot_cannot_bypass_target_address_binding(self, mock_dns) -> None:
        mock_dns.return_value = _dns("93.184.216.34")
        vetted = check_url("https://rebind.example.com./")
        guard = NavigationGuard(vetted, "rebind.example.com", transport=FakeTransport())
        mock_dns.return_value = _dns("8.8.8.8")

        route = FakeRoute("https://rebind.example.com./next")
        guard.handle_route(route)

        assert route.aborted_with == "blockedbyclient"
        assert guard.refusals[0]["reason_code"] == REASON_ADDRESS_NOT_APPROVED

    @patch("socket.getaddrinfo")
    def test_subresource_is_reresolved_on_every_request(self, mock_dns) -> None:
        mock_dns.return_value = _dns("93.184.216.34")
        vetted = check_url("https://example.com/")
        guard = NavigationGuard(vetted, "example.com", transport=FakeTransport())

        mock_dns.return_value = _dns("151.101.1.1")
        first = FakeRoute("https://cdn.example.net/a.js")
        guard.handle_route(first)
        assert first.fulfilled_with is not None

        mock_dns.return_value = _dns("169.254.169.254")
        second = FakeRoute("https://cdn.example.net/b.js")
        guard.handle_route(second)
        assert second.aborted_with == "blockedbyclient"

    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_all_websockets_are_blocked_instead_of_opened_unpinned(
        self, mock_dns
    ) -> None:
        vetted = check_url("https://example.com/")
        guard = NavigationGuard(vetted, "example.com", transport=FakeTransport())
        websocket = FakeWebSocketRoute("wss://example.com/socket")

        guard.handle_websocket(websocket)

        assert websocket.closed_with == (1008, "destination refused")
        websocket.connect_to_server.assert_not_called()
        assert guard.refusals[0]["reason_code"] == REASON_SCHEME_NOT_ALLOWED

    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_transport_budget_refusal_aborts_route_with_distinct_reason(
        self, mock_dns
    ) -> None:
        vetted = check_url("https://example.com/")
        guard = NavigationGuard(
            vetted,
            "example.com",
            transport=RefusingTransport(REASON_RESPONSE_TOO_LARGE),
        )
        route = FakeRoute("https://example.com/large")

        guard.handle_route(route)

        assert route.aborted_with == "blockedbyclient"
        assert guard.refusals[0]["reason_code"] == REASON_RESPONSE_TOO_LARGE

    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_pinned_fetch_failure_does_not_log_exception_secrets(
        self, mock_dns, caplog
    ) -> None:
        url = (
            "https://alice:password@example.com/path?token=super-secret&"
            "code=oauth-code#fragment-token"
        )
        vetted = check_url(url)
        transport = FakeTransport()
        transport.fetch = Mock(side_effect=http.client.BadStatusLine(url))
        guard = NavigationGuard(vetted, "example.com", transport=transport)
        route = FakeRoute(url)

        with caplog.at_level("WARNING"):
            guard.handle_route(route)

        assert route.aborted_with == "connectionfailed"
        assert "BadStatusLine" in caplog.text
        for secret in [
            "alice",
            "password",
            "super-secret",
            "oauth-code",
            "fragment-token",
        ]:
            assert secret not in caplog.text

    def test_refusal_redacts_secrets_from_logs_results_and_exception(
        self, caplog
    ) -> None:
        url = (
            "http://alice:password@169.254.169.254/latest?token=super-secret&"
            "code=oauth-code#fragment-token"
        )
        result = check_url(url)
        guard = NavigationGuard(result, "169.254.169.254", transport=FakeTransport())
        route = FakeRoute(url)

        with caplog.at_level("WARNING"):
            guard.handle_route(route)
        refusal = DestinationRefused(url, result)
        outputs = [caplog.text, str(refusal), refusal.url, repr(guard.refusals)]

        for output in outputs:
            for secret in [
                "alice",
                "password",
                "super-secret",
                "oauth-code",
                "fragment-token",
            ]:
                assert secret not in output
        assert guard.refusals[0]["reason_code"] == REASON_BLOCKED_ADDRESS


class TestVetDestination:
    @patch("socket.getaddrinfo", return_value=_dns("93.184.216.34"))
    def test_permitted_destination_returns_vetted_addresses(self, mock_dns) -> None:
        result = vet_destination("https://example.com/")
        assert result.resolved_ips == ["93.184.216.34"]

    def test_custom_denied_host_pattern_is_enforced(self) -> None:
        config = DenylistConfig(denied_host_patterns=["*.corp.example"])
        with pytest.raises(DestinationRefused):
            vet_destination("https://private.corp.example/", config)
