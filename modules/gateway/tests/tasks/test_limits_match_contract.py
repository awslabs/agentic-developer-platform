"""Assert the mirrored pilot limits still equal the frozen contract.

``src/tasks/limits.py`` restates values that ``docs/task-api/contracts/v1/limits.json``
owns, because the gateway container is built from ``modules/gateway/`` and does not
contain ``docs/`` — a runtime read of the contract would be a route that passes in
a test run and raises in the pod.

Mirroring buys deployability and costs the possibility of drift, so the agreement
is asserted rather than trusted. If someone edits either side, this fails and names
the constant. That is the whole point: the duplication is only defensible while it
is checked.

Skipped rather than failed when the contract is absent, since the source tree is
not guaranteed to be present in every environment the gateway suite runs in — a
missing file is not evidence of drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tasks import limits

CONTRACT = Path(__file__).resolve().parents[4] / "docs" / "task-api" / "contracts" / "v1" / "limits.json"

pytestmark = pytest.mark.skipif(not CONTRACT.exists(), reason="Task API contract source tree is not present")


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT.read_text())


def _lookup(contract: dict, path: str):
    """Resolve a slash-separated path, failing loudly if the contract moved.

    A ``KeyError`` here means the contract was restructured, which must surface as
    a test failure naming the path rather than a silently-skipped assertion.
    """
    node = contract
    for part in path.split("/"):
        assert part in node, f"contract path {path!r} no longer resolves (missing {part!r})"
        node = node[part]
    return node


#: Each mirrored constant paired with its authoritative location. Kept as data so
#: adding a limit to ``limits.py`` without pinning it here is visible as an
#: unpaired constant in the completeness test below.
PAIRS: list[tuple[str, str]] = [
    ("SSE_EVENT_PAGE_SIZE", "sse/event_page_size"),
    ("SSE_POLL_INTERVAL_SECONDS", "sse/active_reader_poll_interval_seconds"),
    ("SSE_HEARTBEAT_INTERVAL_SECONDS", "sse/heartbeat_interval_seconds"),
    ("SSE_CONNECTION_WINDOW_MINUTES", "sse/connection_window_minutes"),
    ("SSE_API_GATEWAY_LIMIT_MINUTES", "sse/api_gateway_limit_minutes"),
    ("SSE_MAX_STREAMS_PER_TASK", "sse/max_streams_per_task"),
    ("SSE_MAX_STREAMS_PER_PRINCIPAL", "sse/max_streams_per_principal"),
    ("SSE_MAX_STREAMS_PER_ENVIRONMENT", "sse/max_streams_per_environment"),
    ("SSE_MAX_BUFFERED_FRAMES", "sse/max_buffered_frames_per_stream"),
    ("SSE_MAX_BUFFERED_BYTES", "sse/max_buffered_bytes_per_stream"),
    ("SSE_BLOCKED_WRITE_DISCONNECT_SECONDS", "sse/blocked_write_disconnect_seconds"),
    ("STREAM_AUTHORIZATION_RECHECK_SECONDS", "reporting_and_access/stream_authorization_recheck_max_interval_seconds"),
    ("REVOCATION_STREAM_CLOSE_SECONDS", "reporting_and_access/revocation_stream_close_seconds"),
    ("REQUIRED_DISTINCT_AUTHORED_PROGRESS_MARKERS", "reporting_and_access/required_distinct_authored_progress_markers"),
    ("MAX_EXTERNAL_MARKER_ARRIVAL_SECONDS", "reporting_and_access/max_external_marker_arrival_seconds"),
    ("HEARTBEAT_ONLY_SATISFIES_PROGRESS", "reporting_and_access/heartbeat_only_satisfies_progress"),
    ("BUFFERED_FINAL_STDOUT_SATISFIES_PROGRESS", "reporting_and_access/buffered_final_stdout_satisfies_progress"),
    ("MAX_PROGRESS_EVENT_BYTES", "process_and_reporting/max_progress_event_bytes"),
    ("MAX_EVENTS_PER_TASK", "process_and_reporting/max_events_per_task"),
    ("RESERVED_TERMINAL_EVENT_SLOTS", "process_and_reporting/reserved_terminal_event_slots"),
    ("MAX_REPORT_FRAME_BYTES", "process_and_reporting/max_frame_bytes"),
    ("MAX_RUN_ARTIFACT_BYTES", "artifacts/max_run_artifact_bytes"),
    ("MAX_INPUT_ARTIFACT_BYTES", "artifacts/max_input_artifact_bytes"),
    ("UNCLAIMED_UPLOAD_EXPIRY_HOURS", "artifacts/unclaimed_upload_expiry_hours"),
    ("TOMBSTONE_RESPONSE_CODE", "retention/tombstone_response_code"),
]


@pytest.mark.parametrize(("constant", "path"), PAIRS, ids=[name for name, _ in PAIRS])
def test_constant_equals_contract(contract: dict, constant: str, path: str) -> None:
    assert getattr(limits, constant) == _lookup(contract, path), f"{constant} has drifted from limits.json {path}"


def test_permitted_content_types_match_contract(contract: dict) -> None:
    """Compared as an ordered sequence, not a set.

    The contract lists these as an array and the upload route reports the
    permitted set back to a rejected caller; a reordering is harmless but an
    addition or removal is a change in what the surface accepts.
    """
    assert list(limits.PERMITTED_ARTIFACT_CONTENT_TYPES) == list(_lookup(contract, "artifacts/permitted_content_types_investigator_v1"))


def test_every_mirrored_constant_is_pinned() -> None:
    """No limit may be mirrored without a contract pairing.

    Without this, a future constant added to ``limits.py`` would be unchecked
    duplication — exactly the failure the module's existence is conditioned on
    avoiding.
    """
    mirrored = {name for name in vars(limits) if name.isupper() and not name.startswith("_")}
    #: Derived rather than mirrored: checked by its own conversion test below.
    derived = {"SSE_CONNECTION_WINDOW_SECONDS"}
    pinned = {name for name, _ in PAIRS} | {"PERMITTED_ARTIFACT_CONTENT_TYPES"} | derived
    assert mirrored == pinned, f"unpinned mirrored limits: {sorted(mirrored - pinned)}"


def test_connection_window_is_converted_and_stays_under_the_platform_ceiling(contract: dict) -> None:
    """The window is only meaningful as a value strictly below API Gateway's.

    The gateway must close and invite a reconnect *before* the platform severs the
    connection, so a client observes an orderly handoff rather than an unexplained
    drop. Asserted as a relationship, not two independent numbers, because raising
    the window past the ceiling would silently reintroduce the unexplained drop.
    """
    assert limits.SSE_CONNECTION_WINDOW_SECONDS == limits.SSE_CONNECTION_WINDOW_MINUTES * 60
    assert limits.SSE_CONNECTION_WINDOW_MINUTES < limits.SSE_API_GATEWAY_LIMIT_MINUTES


def test_reserved_terminal_slots_leave_usable_progress_budget() -> None:
    """The reserved tail must be a tail, not the whole budget.

    Asserted independently of the contract values because this is the property the
    reservation exists for: a task that floods progress still has room to record
    how it ended. If the two numbers were ever set equal, the budget arithmetic in
    the store would refuse every progress event while reading as configured.
    """
    assert 0 < limits.RESERVED_TERMINAL_EVENT_SLOTS < limits.MAX_EVENTS_PER_TASK


def test_task_duration_policy_ceiling_matches_contract():
    from src.agentauth.task_service_policy import MAX_DURATION_MINUTES

    contract = json.loads(CONTRACT.read_text())
    assert MAX_DURATION_MINUTES == contract["lifetime"]["task_lifetime_minutes"]
