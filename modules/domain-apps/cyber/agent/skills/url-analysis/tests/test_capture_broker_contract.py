"""Exercise the research operation across a real local HTTP boundary."""

import io
import json
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from browser_broker import BrowserBrokerHandler, MAX_REQUEST_BYTES
from browser_client import BrowserBrokerError, capture_url
from browser_guard import DestinationRefused
from denylist import DenylistResult


@pytest.fixture
def broker():
    server = ThreadingHTTPServer(("127.0.0.1", 0), BrowserBrokerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    with patch("browser_broker.collect_case") as collector:
        collector.return_value = {"schema_version": "url-research/1"}
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", collector
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_capture_round_trip_preserves_bounded_options(broker):
    endpoint, collector = broker
    result = capture_url(
        "https://public.test/", profile="mobile", wait_seconds=15, broker_url=endpoint
    )
    assert result == {"schema_version": "url-research/1"}
    request = collector.call_args.args[0]
    assert request["profile"] == "mobile"
    assert request["wait_seconds"] == 15
    assert request["timeout_ms"] == 30000
    assert request["ignore_https_errors"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"profile": []},
        {"profile": "custom"},
        {"wait_seconds": True},
        {"wait_seconds": -1},
        {"wait_seconds": 16},
        {"wait_seconds": 1.5},
        {"ignore_https_errors": True},
        {"timeout_ms": 0},
        {"wait_until": []},
        {"javascript": "fetch('http://internal')"},
        {"url": "x" * 8193},
    ],
)
def test_invalid_capture_never_starts_browser(broker, overrides):
    endpoint, collector = broker
    request = Request(
        endpoint + "/v1/capture",
        data=json.dumps({"url": "https://public.test/", **overrides}).encode(),
        method="POST",
    )
    with pytest.raises(HTTPError) as raised:
        urlopen(request)
    assert raised.value.code == 400
    collector.assert_not_called()


def test_oversized_request_never_starts_browser(broker):
    endpoint, collector = broker
    with pytest.raises(HTTPError) as raised:
        urlopen(Request(endpoint + "/v1/capture", data=b" " * (MAX_REQUEST_BYTES + 1)))
    assert raised.value.code == 400
    collector.assert_not_called()


def test_policy_and_environment_failures_are_distinct(broker):
    endpoint, collector = broker
    collector.side_effect = DestinationRefused(
        "https://public.test/",
        DenylistResult(
            allowed=False, reason="Unresolvable host", reason_code="resolution_failed"
        ),
    )
    with pytest.raises(DestinationRefused) as raised:
        capture_url("https://public.test/", broker_url=endpoint)
    assert raised.value.reason_code == "resolution_failed"
    collector.side_effect = RuntimeError("internal details should not leave broker")
    with pytest.raises(BrowserBrokerError, match="guarded analysis failed"):
        capture_url("https://public.test/", broker_url=endpoint)


def test_server_enforces_response_size(broker):
    endpoint, collector = broker
    collector.return_value = {"evidence": "x" * 2000}
    with patch("browser_broker.MAX_RESPONSE_BYTES", 1000):
        with pytest.raises(BrowserBrokerError, match="response budget"):
            capture_url("https://public.test/", broker_url=endpoint)


def test_client_independently_enforces_response_size():
    response = io.BytesIO(b" " * 1001)
    with (
        patch("browser_client.urlopen", return_value=response),
        patch("browser_client.MAX_RESPONSE_BYTES", 1000),
        pytest.raises(BrowserBrokerError, match="byte budget"),
    ):
        capture_url("https://public.test/")


@pytest.mark.parametrize("body", [b"bad json", b"[]", b'{"status":"ok","analysis":[]}'])
def test_client_rejects_malformed_envelopes(body):
    with (
        patch("browser_client.urlopen", return_value=io.BytesIO(body)),
        pytest.raises(BrowserBrokerError, match="invalid response"),
    ):
        capture_url("https://public.test/")


@pytest.fixture(autouse=True)
def legacy_broker_mode(monkeypatch):
    """These tests exercise the explicitly selected legacy transport."""
    monkeypatch.setenv("URL_ANALYSIS_BROWSER_MODE", "broker")
