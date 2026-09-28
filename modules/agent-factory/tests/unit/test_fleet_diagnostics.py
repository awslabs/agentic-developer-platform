"""
Unit tests for the live-fleet expiry diagnostics (#5354).

These cover the two properties that matter for the diagnostics to be trustworthy:

  1. When the fleet state IS available, the assertion message names the numbers
     an investigator needs — queue depth, how many workers claimed a message,
     and the sqs join key.
  2. When collecting that state FAILS, the failure degrades to a note and never
     replaces or masks the assertion it is describing. A diagnostics bug must not
     be able to turn a real test failure into a confusing AWS traceback.

No live AWS calls: SQS and CloudWatch Logs clients are stubbed.
"""

from __future__ import annotations

import sys

import pytest

from tests.e2e.helpers.fleet_diagnostics import (
    FleetState,
    bootstrap_log_group,
    collect_fleet_state,
    collect_queue_depth,
    collect_worker_streams,
    format_expiry_diagnostics,
)

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/adp-dev-agent-submit.fifo"
ENQUEUE_MS = 1_700_000_000_000


class StubSqs:
    """Minimal SQS stub returning fixed queue attributes."""

    def __init__(self, in_flight: str = "6", visible: str = "0", raises: Exception | None = None):
        self._in_flight = in_flight
        self._visible = visible
        self._raises = raises
        self.calls: list[dict] = []

    def get_queue_attributes(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return {
            "Attributes": {
                "ApproximateNumberOfMessagesNotVisible": self._in_flight,
                "ApproximateNumberOfMessages": self._visible,
            }
        }


class StubLogs:
    """Minimal CloudWatch Logs stub returning fixed log streams."""

    def __init__(self, streams: list[dict] | None = None, raises: Exception | None = None):
        self._streams = streams or []
        self._raises = raises
        self.calls: list[dict] = []

    def describe_log_streams(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return {"logStreams": self._streams}


# ---------------------------------------------------------------------------
# Queue depth
# ---------------------------------------------------------------------------


def test_queue_depth_records_in_flight_and_visible():
    state = FleetState()
    collect_queue_depth(QUEUE_URL, state, sqs_client=StubSqs(in_flight="11", visible="2"))
    assert state.in_flight == 11
    assert state.visible == 2
    assert state.errors == []


def test_queue_depth_requests_both_attributes():
    stub = StubSqs()
    collect_queue_depth(QUEUE_URL, stub_state := FleetState(), sqs_client=stub)
    requested = stub.calls[0]["AttributeNames"]
    assert "ApproximateNumberOfMessagesNotVisible" in requested
    assert "ApproximateNumberOfMessages" in requested
    assert stub_state.errors == []


def test_queue_depth_without_queue_url_is_noted_not_raised():
    state = FleetState()
    collect_queue_depth("", state, sqs_client=StubSqs())
    assert state.in_flight is None
    assert len(state.errors) == 1
    assert "no queue URL" in state.errors[0]


def test_queue_depth_api_error_degrades_to_note():
    state = FleetState()
    collect_queue_depth(QUEUE_URL, state, sqs_client=StubSqs(raises=RuntimeError("AccessDenied")))
    assert state.in_flight is None
    assert "RuntimeError" in state.errors[0]
    assert "AccessDenied" in state.errors[0]


# ---------------------------------------------------------------------------
# Worker stream counting
# ---------------------------------------------------------------------------


def test_worker_streams_counts_only_streams_created_after_enqueue():
    streams = [
        {
            "logStreamName": "8f14e45f-ceea-467a-9f1e-1f1f1f1f1f1f",
            "creationTime": ENQUEUE_MS + 5_000,
        },
        {
            "logStreamName": "c9f0f895-fb98-4b1e-8b8c-2c2c2c2c2c2c",
            "creationTime": ENQUEUE_MS + 1_000,
        },
        # Predates the enqueue — a worker already running, not one that started.
        {
            "logStreamName": "45c48cce-2e2d-4fa8-a8b1-3d3d3d3d3d3d",
            "creationTime": ENQUEUE_MS - 60_000,
        },
    ]
    state = FleetState()
    collect_worker_streams(ENQUEUE_MS, state, env="dev", logs_client=StubLogs(streams))
    assert state.worker_streams_since_enqueue == 2


def test_worker_streams_counts_the_gitlab_ack_path():
    """#5354 regression: the GitLab path must be countable.

    The GitLab mention handler returns before it would exec the Node agent, so it
    never writes an ``agent-<persona>-issue-<n>`` stream. Counting the agent log
    group reported 0 for every GitLab run — "nothing picked it up" — which is the
    opposite of the truth when a worker did claim the message. Bootstrap streams
    are keyed by correlation id and carry no persona/issue naming at all, so the
    counter must not require any name shape.
    """
    streams = [
        {"logStreamName": "d3d9446a-0b1e-4c9e-a1a1-4e4e4e4e4e4e", "creationTime": ENQUEUE_MS + 900},
    ]
    state = FleetState()
    collect_worker_streams(ENQUEUE_MS, state, env="dev", logs_client=StubLogs(streams))
    assert state.worker_streams_since_enqueue == 1
    assert state.recent_stream_names == ["d3d9446a-0b1e-4c9e-a1a1-4e4e4e4e4e4e"]


def test_worker_streams_reads_the_bootstrap_group_not_the_agent_group():
    """The agent group is provider-specific; the bootstrap group is not."""
    stub = StubLogs([])
    collect_worker_streams(ENQUEUE_MS, FleetState(), env="dev", logs_client=stub)
    assert stub.calls[0]["logGroupName"] == "/adp/dev/agent-factory/bootstrap"


def test_worker_streams_handles_missing_creation_time():
    streams = [{"logStreamName": "6f4922f4-5568-4423-b937-5f5f5f5f5f5f"}]
    state = FleetState()
    collect_worker_streams(ENQUEUE_MS, state, env="dev", logs_client=StubLogs(streams))
    assert state.worker_streams_since_enqueue == 0
    assert state.errors == []


def test_worker_streams_caps_reported_names():
    streams = [
        {"logStreamName": f"correlation-{n:04d}", "creationTime": ENQUEUE_MS + n} for n in range(20)
    ]
    state = FleetState()
    collect_worker_streams(ENQUEUE_MS, state, env="dev", logs_client=StubLogs(streams), max_names=3)
    assert state.worker_streams_since_enqueue == 20
    assert len(state.recent_stream_names) == 3


def test_worker_streams_api_error_degrades_to_note():
    state = FleetState()
    collect_worker_streams(
        ENQUEUE_MS, state, env="dev", logs_client=StubLogs(raises=RuntimeError("Throttled"))
    )
    assert state.worker_streams_since_enqueue is None
    assert "Throttled" in state.errors[0]


def test_worker_streams_queries_newest_first():
    stub = StubLogs([])
    collect_worker_streams(ENQUEUE_MS, FleetState(), env="dev", logs_client=stub)
    assert stub.calls[0]["descending"] is True


def test_bootstrap_log_group_uses_environment(monkeypatch):
    monkeypatch.setenv("TEST_ENV", "staging")
    assert bootstrap_log_group() == "/adp/staging/agent-factory/bootstrap"
    assert bootstrap_log_group("dev") == "/adp/dev/agent-factory/bootstrap"


# ---------------------------------------------------------------------------
# Combined collection never raises
# ---------------------------------------------------------------------------


def test_collect_fleet_state_never_raises_when_both_sources_fail():
    state = collect_fleet_state(
        QUEUE_URL,
        ENQUEUE_MS,
        env="dev",
        sqs_client=StubSqs(raises=RuntimeError("sqs down")),
        logs_client=StubLogs(raises=RuntimeError("logs down")),
    )
    assert state.in_flight is None
    assert state.worker_streams_since_enqueue is None
    assert len(state.errors) == 2


# ---------------------------------------------------------------------------
# Message formatting — the actual investigator-facing output
# ---------------------------------------------------------------------------


def test_expiry_message_names_the_numbers_an_investigator_needs():
    state = collect_fleet_state(
        QUEUE_URL,
        ENQUEUE_MS,
        env="dev",
        sqs_client=StubSqs(in_flight="6", visible="3"),
        logs_client=StubLogs(
            [
                {
                    "logStreamName": "e3d3f8c4-865d-432f-b143-378b7b7ca012",
                    "creationTime": ENQUEUE_MS + 1,
                }
            ]
        ),
    )
    msg = format_expiry_diagnostics(
        "No `agent` acknowledgement on GitLab issue #697", 60, "e3d3f8c4-865d", state
    )
    # The original assertion text is preserved as the headline.
    assert "No `agent` acknowledgement on GitLab issue #697" in msg
    assert "within 60s" in msg
    # And the diagnostics the old message lacked.
    assert "6" in msg and "3" in msg
    assert "e3d3f8c4-865d" in msg
    assert "e3d3f8c4-865d-432f-b143-378b7b7ca012" in msg
    # The count must be labelled as a claim, not as "workers started" — a pod
    # that started but claimed nothing is not what this number measures.
    assert "claimed a message" in msg


def test_expiry_message_flags_saturation_reading():
    """Nothing queued but work in flight is the saturation signature from #5354."""
    state = collect_fleet_state(
        QUEUE_URL,
        ENQUEUE_MS,
        env="dev",
        sqs_client=StubSqs(in_flight="6", visible="0"),
        logs_client=StubLogs([]),
    )
    msg = format_expiry_diagnostics("No ack", 60, "sqs-1", state)
    assert "saturated fleet" in msg


def test_expiry_message_omits_saturation_reading_when_work_is_queued():
    state = collect_fleet_state(
        QUEUE_URL,
        ENQUEUE_MS,
        env="dev",
        sqs_client=StubSqs(in_flight="6", visible="4"),
        logs_client=StubLogs([]),
    )
    msg = format_expiry_diagnostics("No ack", 60, "sqs-1", state)
    assert "saturated fleet" not in msg


def test_expiry_message_still_readable_when_diagnostics_unavailable():
    """A diagnostics outage must not hide what actually failed."""
    state = collect_fleet_state(
        "",
        ENQUEUE_MS,
        env="dev",
        logs_client=StubLogs(raises=RuntimeError("no creds")),
    )
    msg = format_expiry_diagnostics("No developer acknowledgement", 60, None, state)
    assert "No developer acknowledgement" in msg
    assert "unavailable" in msg
    assert "diagnostics not fully collected" in msg


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
