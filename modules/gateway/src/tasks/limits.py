"""Frozen Task API v1 pilot limits, mirrored from the accepted contract.

These values are copied from ``docs/task-api/contracts/v1/limits.json`` at design
revision ``b5761a4a2502aceaa9133afef552b567a19cb46e`` rather than read from it at
runtime. The reason is deployment shape, not preference: the gateway ships as a
container built from ``modules/gateway/``, and ``docs/`` is not in that image, so a
runtime read would be a route that works in a test run and raises in the pod.

Mirroring creates a drift surface, so the agreement is asserted instead of trusted:
``tests/tasks/test_limits_match_contract.py`` loads the contract JSON and compares
every value below against it. A limit changed here without a reviewed contract
change fails that test, which is the property the contract's amendment rule needs
(an evaluator observing a failure records FAIL, never a looser limit).
"""

from __future__ import annotations

# Streaming and delivery (limits.json#/sse).
SSE_EVENT_PAGE_SIZE = 100
SSE_POLL_INTERVAL_SECONDS = 1
SSE_HEARTBEAT_INTERVAL_SECONDS = 15
SSE_MAX_STREAMS_PER_TASK = 2
SSE_MAX_STREAMS_PER_PRINCIPAL = 10
SSE_MAX_STREAMS_PER_ENVIRONMENT = 32
SSE_MAX_BUFFERED_FRAMES = 100
SSE_MAX_BUFFERED_BYTES = 262144
SSE_BLOCKED_WRITE_DISCONNECT_SECONDS = 10

#: The contract states this in minutes; seconds is the unit every timer in this
#: package works in, so the conversion happens once here rather than at each use
#: site. Held as its own constant (not inlined) so the drift test can assert the
#: conversion, which is the kind of arithmetic that silently becomes a 10-second
#: window if someone "simplifies" it.
SSE_CONNECTION_WINDOW_MINUTES = 10
SSE_CONNECTION_WINDOW_SECONDS = SSE_CONNECTION_WINDOW_MINUTES * 60

#: API Gateway's own ceiling. Recorded because the 10-minute window above is only
#: meaningful as a value strictly below it: the gateway must close and invite a
#: reconnect *before* the platform severs the connection, so that a client
#: observes an orderly handoff rather than an unexplained drop.
SSE_API_GATEWAY_LIMIT_MINUTES = 15

# Authorization recheck and revocation (limits.json#/reporting_and_access).
STREAM_AUTHORIZATION_RECHECK_SECONDS = 15
REVOCATION_STREAM_CLOSE_SECONDS = 30

#: T6-AC01's pass condition, as data rather than prose. Two *distinct authored*
#: updates must be externally observable within five seconds each while a run is
#: held open. The two false flags are kept here because they are the acceptance
#: criterion's explicit failure modes, and naming them in code is what lets a test
#: assert the surface does not satisfy the criterion by accident.
REQUIRED_DISTINCT_AUTHORED_PROGRESS_MARKERS = 2
MAX_EXTERNAL_MARKER_ARRIVAL_SECONDS = 5
HEARTBEAT_ONLY_SATISFIES_PROGRESS = False
BUFFERED_FINAL_STDOUT_SATISFIES_PROGRESS = False

# Event budget and report shape (limits.json#/process_and_reporting).
MAX_PROGRESS_EVENT_BYTES = 8192
MAX_EVENTS_PER_TASK = 10000
RESERVED_TERMINAL_EVENT_SLOTS = 100
MAX_REPORT_FRAME_BYTES = 65536

# Artifacts (limits.json#/artifacts).
MAX_INPUT_ARTIFACT_BYTES = 262144
PERMITTED_ARTIFACT_CONTENT_TYPES = ("text/plain", "application/json")
UNCLAIMED_UPLOAD_EXPIRY_HOURS = 24

# Retention (limits.json#/retention). A read of expired history answers 410 with
# the retained bounds, never an empty success.
TOMBSTONE_RESPONSE_CODE = 410

# Tool evidence and final outputs share storage; individual writes remain <= 1 MiB.
MAX_RUN_ARTIFACT_BYTES = 16 * 1024 * 1024
