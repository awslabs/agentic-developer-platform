import io
import json
import logging
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest
from browser_broker import (
    InvalidBrokerRequest,
    _log_analysis_failure,
    _validated_request,
    analyze_destination,
)
from browser_client import BrowserBrokerError, analyze_url
from browser_guard import DestinationRefused
from denylist import REASON_BLOCKED_ADDRESS, DenylistResult


class FakeResponse:
    status = 200


class FakeFrame:
    url = "https://public.example/final"


class FakeDownload:
    url = "https://public.example/file.exe"
    suggested_filename = "file.exe"

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeSession:
    session_id = "session-1"
    url = "https://public.example/final"

    def __init__(self) -> None:
        self.callbacks = {}
        self.main_frame = FakeFrame()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def on(self, event, callback) -> None:
        self.callbacks[event] = callback

    def goto(self, *args, **kwargs):
        download = FakeDownload()
        self.callbacks["download"](download)
        assert download.cancelled
        self.callbacks["framenavigated"](self.main_frame)
        return FakeResponse()

    def screenshot(self, **kwargs) -> bytes:
        return b"png"

    def inner_text(self, selector) -> str:
        return "visible"

    def title(self) -> str:
        return "Public"

    def evaluate(self, expression):
        return []

    @property
    def refusals(self):
        return []


class RefusedNavigationSession(FakeSession):
    def goto(self, *args, **kwargs):
        raise RuntimeError("navigation aborted")

    @property
    def refusals(self):
        return [
            {
                "url": "http://169.254.169.254/latest",
                "reason": "blocked redirect",
                "reason_code": REASON_BLOCKED_ADDRESS,
                "navigation": True,
            }
        ]


class DownloadNavigationSession(FakeSession):
    url = "about:blank"

    def goto(self, *args, **kwargs):
        download = FakeDownload()
        self.callbacks["download"](download)
        assert download.cancelled
        raise RuntimeError("Download is starting")


def test_broker_accepts_only_bounded_capture_options() -> None:
    assert _validated_request({"url": "https://public.example"})["timeout_ms"] == 30_000
    with pytest.raises(InvalidBrokerRequest, match="unsupported fields"):
        _validated_request(
            {"url": "https://public.example", "command": "connect_over_cdp"}
        )


def test_broker_refuses_destination_before_agentcore_session() -> None:
    refusal = DestinationRefused(
        "http://169.254.169.254/",
        DenylistResult(
            allowed=False,
            reason="blocked",
            reason_code=REASON_BLOCKED_ADDRESS,
        ),
    )
    with (
        patch("browser_broker.open_guarded_browser", side_effect=refusal) as opener,
        pytest.raises(DestinationRefused),
    ):
        analyze_destination(
            _validated_request({"url": "http://169.254.169.254/"}), Mock()
        )
    opener.assert_called_once()


def test_broker_returns_bounded_capture_and_cancels_downloads() -> None:
    with patch("browser_broker.open_guarded_browser", return_value=FakeSession()):
        result = analyze_destination(
            _validated_request({"url": "https://public.example"}), Mock()
        )

    assert result["session_id"] == "session-1"
    assert result["screenshot_base64"] == "cG5n"
    assert result["downloads"] == [
        {
            "url": "https://public.example/file.exe",
            "suggested_filename": "file.exe",
        }
    ]
    assert "ws_url" not in result
    assert "headers" not in result


def test_broker_returns_download_evidence_when_navigation_becomes_download() -> None:
    session = DownloadNavigationSession()
    session.screenshot = Mock(side_effect=AssertionError("must not capture"))
    session.inner_text = Mock(side_effect=AssertionError("must not inspect page"))
    session.title = Mock(side_effect=AssertionError("must not inspect page"))
    session.evaluate = Mock(side_effect=AssertionError("must not inspect page"))
    with patch("browser_broker.open_guarded_browser", return_value=session):
        result = analyze_destination(
            _validated_request({"url": "https://public.example/file.exe"}), Mock()
        )

    assert result == {
        "session_id": "session-1",
        "final_url": "about:blank",
        "http_status": 0,
        "page_title": "",
        "screenshot_base64": "",
        "visible_text": "",
        "forms": [],
        "orphan_inputs": [],
        "redirects": [],
        "frame_navigations": [],
        "downloads": [
            {
                "url": "https://public.example/file.exe",
                "suggested_filename": "file.exe",
            }
        ],
        "refusals": [],
    }
    session.screenshot.assert_not_called()
    session.inner_text.assert_not_called()
    session.title.assert_not_called()
    session.evaluate.assert_not_called()


def test_broker_turns_blocked_redirect_into_refusal_before_capture() -> None:
    session = RefusedNavigationSession()
    session.screenshot = Mock(side_effect=AssertionError("must not capture"))
    with (
        patch("browser_broker.open_guarded_browser", return_value=session),
        pytest.raises(DestinationRefused) as raised,
    ):
        analyze_destination(
            _validated_request({"url": "https://public.example"}), Mock()
        )

    assert raised.value.reason_code == REASON_BLOCKED_ADDRESS
    session.screenshot.assert_not_called()


def test_broker_logs_browser_failure_without_url_secrets(caplog) -> None:
    secret_url = "https://user:password@example.com/path?token=supersecret#fragment"

    with caplog.at_level(logging.ERROR, logger="browser_broker"):
        _log_analysis_failure(secret_url, RuntimeError(secret_url))

    assert "https://[REDACTED]@example.com/path?token=REDACTED#REDACTED" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "user:password" not in caplog.text
    assert "password" not in caplog.text
    assert "supersecret" not in caplog.text
    assert "fragment" not in caplog.text


@pytest.mark.parametrize("unattempted", [True, False, None, "true"])
def test_client_maps_policy_refusal_without_fallback(unattempted) -> None:
    body = json.dumps(
        {
            "error": "destination_refused",
            "reason": "blocked",
            "reason_code": REASON_BLOCKED_ADDRESS,
            "browser_start_unattempted": unattempted,
        }
    ).encode()
    error = HTTPError(
        "http://broker/v1/analyze", 403, "Forbidden", {}, io.BytesIO(body)
    )
    with (
        patch("browser_client.urlopen", side_effect=error) as request,
        pytest.raises(DestinationRefused) as raised,
    ):
        analyze_url("http://169.254.169.254/", broker_url="http://broker")

    request.assert_called_once()
    assert raised.value.reason_code == REASON_BLOCKED_ADDRESS
    assert raised.value.browser_start_unattempted is (unattempted is True)


def test_client_does_not_treat_broker_failure_as_allow() -> None:
    body = json.dumps({"error": "analysis_failed", "message": "failed"}).encode()
    error = HTTPError(
        "http://broker/v1/analyze", 502, "Bad Gateway", {}, io.BytesIO(body)
    )
    with (
        patch("browser_client.urlopen", side_effect=error),
        pytest.raises(BrowserBrokerError, match="failed"),
    ):
        analyze_url("https://public.example", broker_url="http://broker")


@pytest.fixture(autouse=True)
def legacy_broker_mode(monkeypatch):
    """These tests exercise the explicitly selected legacy transport."""
    monkeypatch.setenv("URL_ANALYSIS_BROWSER_MODE", "broker")
