"""Pytest fixtures for chat logging tests (Issue #143)."""

from datetime import UTC, datetime

import pytest

from src.chat_logging.s3_writer import CircuitBreaker, CircuitBreakerConfig
from src.chat_logging.scrubber import HeaderScrubber, RegexScrubber, ScrubPipeline


@pytest.fixture
def header_scrubber():
    """Fixture for header scrubber."""
    return HeaderScrubber()


@pytest.fixture
def regex_scrubber():
    """Fixture for regex scrubber."""
    return RegexScrubber()


@pytest.fixture
def scrub_pipeline(header_scrubber, regex_scrubber):
    """Fixture for complete scrub pipeline."""
    return ScrubPipeline(header_scrubber, regex_scrubber)


@pytest.fixture
def circuit_breaker():
    """Fixture for circuit breaker with fast timeouts for testing."""
    config = CircuitBreakerConfig(
        failure_threshold=3,
        recovery_timeout_seconds=0.1,  # Fast for testing
        success_threshold=2,
    )
    return CircuitBreaker(config)


@pytest.fixture
def sample_request_body():
    """Sample request body for testing."""
    return {
        "messages": [
            {"role": "user", "content": "Hello, can you help me?"},
            {"role": "assistant", "content": "Sure, how can I help you today?"},
        ],
        "model": "claude-3-sonnet",
        "max_tokens": 1024,
        "system": "You are a helpful assistant.",
    }


@pytest.fixture
def sample_response_body():
    """Sample response body for testing."""
    return {
        "content": [{"type": "text", "text": "I'd be happy to help you!"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 25, "output_tokens": 10},
    }


@pytest.fixture
def sample_timestamp():
    """Sample timestamp for testing."""
    return datetime(2025, 2, 20, 10, 30, 0, tzinfo=UTC)
