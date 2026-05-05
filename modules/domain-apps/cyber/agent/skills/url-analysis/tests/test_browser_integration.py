"""Integration tests for URL analysis with mocked AgentCore Browser."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from browser_client import AgentCoreBrowserClient, BrowserEvidence, SessionInfo


@pytest.fixture
def mock_config() -> dict:
    """Test configuration that avoids real AWS calls."""
    return {
        "region": "us-east-1",
        "session_timeout": 60,
        "artifacts_bucket": "",  # Skip S3 uploads in tests
        "results_table": "",
        "denylist_hosts": [],
    }


@pytest.fixture
def mock_boto_client() -> MagicMock:
    """Mock boto3 client for AgentCore Browser."""
    client = MagicMock()
    client.start_browser_session.return_value = {
        "sessionId": "test-session-123",
        "liveViewUrl": "https://live.agentcore.aws/test-session-123",
    }
    client.invoke_browser.return_value = {
        "currentUrl": "https://example.com/",
        "httpStatus": 200,
        "redirectChain": [],
        "pageTitle": "Example Domain",
        "data": b"fake-screenshot-data",
        "result": "<html><body>Test</body></html>",
        "har": {"log": {"entries": []}},
    }
    client.stop_browser_session.return_value = {}
    return client


class TestFullPipeline:
    """Full pipeline integration: URL -> pre-flight -> browser -> enrichment -> verdict -> report."""

    @patch("analyze.run_enrichment")
    @patch("analyze.AgentCoreBrowserClient")
    @patch("denylist._resolve_hostname")
    def test_full_pipeline_clean_url(
        self, mock_resolve, mock_browser_cls, mock_enrichment, mock_config
    ) -> None:
        """Full pipeline with a clean URL produces complete envelope."""
        from analyze import analyze_url
        from enrichment import EnrichmentResult

        # DNS resolution returns public IP
        mock_resolve.return_value = ["93.184.216.34"]

        # Mock browser client
        mock_client = MagicMock()
        mock_client.create_session.return_value = SessionInfo(
            session_id="sess-001", live_view_url="https://live/sess-001"
        )
        mock_client.navigate_and_capture.return_value = BrowserEvidence(
            final_url="https://example.com/",
            http_status=200,
            redirect_chain=[],
            page_title="Example Domain",
            forms_detected=[],
            auto_downloads=[],
            network_requests=[
                {
                    "url": "https://example.com/",
                    "status": 200,
                    "mime": "text/html",
                    "size": 1256,
                }
            ],
        )
        mock_client.stop_session.return_value = True
        mock_browser_cls.return_value = mock_client

        # Mock enrichment
        mock_enrichment.return_value = EnrichmentResult(
            whois={"age_days": 9000, "registrar": "IANA"},
            virustotal={
                "found": True,
                "malicious": 0,
                "suspicious": 0,
                "harmless": 70,
                "undetected": 0,
            },
            urlhaus={"found": False},
            misp={"skipped": True},
        )

        result = analyze_url("https://example.com/", config_override=mock_config)

        # Verify envelope structure
        assert result["stage"] == "url-analysis"
        assert result["status"] == "ok"
        assert "findings" in result
        assert "verdict" in result
        assert result["verdict"]["severity"] in ("clean", "suspicious", "malicious")
        assert result["findings"]["url"] == "https://example.com/"
        assert result["findings"]["http_status"] == 200

        # Verify session was cleaned up
        mock_client.stop_session.assert_called_once_with("sess-001")

    @patch("denylist._resolve_hostname")
    def test_denylist_refuses_internal_url(self, mock_resolve, mock_config) -> None:
        """Internal URL refused before any browser session is created."""
        from analyze import analyze_url

        mock_resolve.return_value = ["10.0.0.5"]

        result = analyze_url("http://10.0.0.5/admin", config_override=mock_config)

        assert result["status"] == "refused"
        assert "refused_reason" in result["findings"]
        assert result["verdict"]["severity"] == "refused"


class TestTimeoutHandling:
    """Session timeout and error handling."""

    @patch("analyze.run_enrichment")
    @patch("analyze.AgentCoreBrowserClient")
    @patch("denylist._resolve_hostname")
    def test_browser_timeout_produces_partial_verdict(
        self, mock_resolve, mock_browser_cls, mock_enrichment, mock_config
    ) -> None:
        """Browser timeout produces partial status with notes."""
        from analyze import analyze_url
        from enrichment import EnrichmentResult

        mock_resolve.return_value = ["1.2.3.4"]

        mock_client = MagicMock()
        mock_client.create_session.return_value = SessionInfo(session_id="sess-timeout")
        mock_client.navigate_and_capture.return_value = BrowserEvidence(
            error="Session timeout after 300s",
            final_url="",
            http_status=0,
        )
        mock_client.stop_session.return_value = True
        mock_browser_cls.return_value = mock_client

        mock_enrichment.return_value = EnrichmentResult(
            whois={"age_days": 5},
            urlhaus={"found": False},
        )

        result = analyze_url("http://slow-site.com/", config_override=mock_config)

        assert result["status"] == "partial"
        assert (
            "Browser error" in result["notes"] or "timeout" in result["notes"].lower()
        )
        # Session still cleaned up
        mock_client.stop_session.assert_called_once()

    @patch("analyze.run_enrichment")
    @patch("analyze.AgentCoreBrowserClient")
    @patch("denylist._resolve_hostname")
    def test_session_cleanup_on_exception(
        self, mock_resolve, mock_browser_cls, mock_enrichment, mock_config
    ) -> None:
        """Session cleanup called even when browser raises an exception."""
        from analyze import analyze_url
        from enrichment import EnrichmentResult

        mock_resolve.return_value = ["1.2.3.4"]

        mock_client = MagicMock()
        mock_client.create_session.return_value = SessionInfo(session_id="sess-crash")
        mock_client.navigate_and_capture.side_effect = RuntimeError("Unexpected crash")
        mock_client.stop_session.return_value = True
        mock_browser_cls.return_value = mock_client

        mock_enrichment.return_value = EnrichmentResult()

        result = analyze_url("http://crash-site.com/", config_override=mock_config)

        # Session was still cleaned up despite the exception
        mock_client.stop_session.assert_called_once_with("sess-crash")
        assert result["status"] == "partial"


class TestBrowserClientRetry:
    """Browser client retry logic."""

    def test_retries_on_throttling(self, mock_boto_client: MagicMock) -> None:
        """Client retries on ThrottlingException."""
        from botocore.exceptions import ClientError

        error_response = {
            "Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}
        }
        mock_boto_client.start_browser_session.side_effect = [
            ClientError(error_response, "StartBrowserSession"),
            ClientError(error_response, "StartBrowserSession"),
            {"sessionId": "retry-success", "liveViewUrl": ""},
        ]

        client = AgentCoreBrowserClient(boto_client=mock_boto_client)
        with patch("time.sleep"):  # Skip actual sleep in tests
            session = client.create_session()

        assert session.session_id == "retry-success"
        assert mock_boto_client.start_browser_session.call_count == 3

    def test_raises_after_max_retries(self, mock_boto_client: MagicMock) -> None:
        """Client raises after exhausting retries."""
        from botocore.exceptions import ClientError

        error_response = {
            "Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}
        }
        mock_boto_client.start_browser_session.side_effect = ClientError(
            error_response, "StartBrowserSession"
        )

        client = AgentCoreBrowserClient(boto_client=mock_boto_client)
        with patch("time.sleep"):
            with pytest.raises(ClientError):
                client.create_session()

        assert mock_boto_client.start_browser_session.call_count == 3

    def test_stop_session_ignores_not_found(self, mock_boto_client: MagicMock) -> None:
        """StopSession gracefully handles already-terminated sessions."""
        from botocore.exceptions import ClientError

        error_response = {
            "Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}
        }
        mock_boto_client.stop_browser_session.side_effect = ClientError(
            error_response, "StopBrowserSession"
        )

        client = AgentCoreBrowserClient(boto_client=mock_boto_client)
        result = client.stop_session("already-gone-session")
        assert result is False


class TestMissingEnrichmentCredentials:
    """Skill proceeds with reduced evidence when credentials are missing."""

    @patch("analyze.run_enrichment")
    @patch("analyze.AgentCoreBrowserClient")
    @patch("denylist._resolve_hostname")
    def test_missing_vt_and_misp_still_produces_verdict(
        self, mock_resolve, mock_browser_cls, mock_enrichment, mock_config
    ) -> None:
        """Missing VT + MISP credentials still produce a verdict."""
        from analyze import analyze_url
        from enrichment import EnrichmentResult

        mock_resolve.return_value = ["1.2.3.4"]

        mock_client = MagicMock()
        mock_client.create_session.return_value = SessionInfo(
            session_id="sess-no-creds"
        )
        mock_client.navigate_and_capture.return_value = BrowserEvidence(
            final_url="http://test.com/",
            http_status=200,
        )
        mock_client.stop_session.return_value = True
        mock_browser_cls.return_value = mock_client

        mock_enrichment.return_value = EnrichmentResult(
            whois={"age_days": 100},
            virustotal={"skipped": True, "reason": "VT API key not available"},
            misp={"skipped": True, "reason": "MISP not configured"},
            skipped_sources=["virustotal", "misp"],
            urlhaus={"found": False},
        )

        result = analyze_url("http://test.com/", config_override=mock_config)

        assert result["status"] == "ok"
        assert "verdict" in result
        assert result["verdict"]["severity"] in ("clean", "suspicious", "malicious")
