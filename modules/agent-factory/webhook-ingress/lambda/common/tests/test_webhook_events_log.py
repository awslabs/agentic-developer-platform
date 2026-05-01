"""Tests for webhook events audit logging."""

import json
import logging
from unittest.mock import patch

from common.webhook_events_log import log_event

_LOGGER_NAME = "common.webhook_events_log"


class TestLogEvent:
    def test_log_basic_event(self) -> None:
        logger = logging.getLogger(_LOGGER_NAME)
        with patch.object(logger, "info") as mock_info:
            log_event(
                channel="github",
                event_type="issues",
                action="labeled",
                installation_id=12345,
                tenant_id="tenant-abc",
                repo="org/repo",
                intent_persona="dev",
                outcome="published",
                latency_ms=42.5,
            )

            mock_info.assert_called_once()
            record = json.loads(mock_info.call_args[0][0])
            assert record["channel"] == "github"
            assert record["event_type"] == "issues"
            assert record["action"] == "labeled"
            assert record["installation_id"] == 12345
            assert record["tenant_id"] == "tenant-abc"
            assert record["repo"] == "org/repo"
            assert record["intent_persona"] == "dev"
            assert record["outcome"] == "published"
            assert record["latency_ms"] == 42.5
            assert "ts" in record

    def test_log_event_with_error(self) -> None:
        logger = logging.getLogger(_LOGGER_NAME)
        with patch.object(logger, "info") as mock_info:
            log_event(
                channel="github",
                event_type="pull_request",
                action="opened",
                installation_id=999,
                tenant_id=None,
                repo="org/repo",
                intent_persona=None,
                outcome="error",
                error="DDB timeout",
            )

            record = json.loads(mock_info.call_args[0][0])
            assert record["outcome"] == "error"
            assert record["error"] == "DDB timeout"
            assert record["tenant_id"] is None

    def test_log_event_no_error_field_when_none(self) -> None:
        logger = logging.getLogger(_LOGGER_NAME)
        with patch.object(logger, "info") as mock_info:
            log_event(
                channel="slack",
                event_type="message",
                action="sent",
                installation_id=100,
                tenant_id="t1",
                repo="org/r",
                intent_persona="ops",
                outcome="published",
            )

            record = json.loads(mock_info.call_args[0][0])
            assert "error" not in record

    def test_latency_rounded(self) -> None:
        logger = logging.getLogger(_LOGGER_NAME)
        with patch.object(logger, "info") as mock_info:
            log_event(
                channel="github",
                event_type="issues",
                action="opened",
                installation_id=1,
                tenant_id="t",
                repo="o/r",
                intent_persona="dev",
                outcome="published",
                latency_ms=3.14159,
            )

            record = json.loads(mock_info.call_args[0][0])
            assert record["latency_ms"] == 3.1
