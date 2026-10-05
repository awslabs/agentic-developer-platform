"""Task API v1 read, streaming, artifact and host-reporting surface (T6).

This package implements the caller-facing half of the Task API defined by the
accepted design at revision ``b5761a4a2502aceaa9133afef552b567a19cb46e``:

* ``GET /v1/tasks/{task_id}`` — strongly consistent snapshot
* ``GET /v1/tasks/{task_id}/events`` — durable, resumable SSE progress
* ``POST /v1/task-artifacts`` and ``GET /v1/tasks/{task_id}/artifacts/{id}``
* ``POST /internal/v1/agent/task/report`` — host-authenticated progress ingest

It is additive. No existing route, storage record or GitHub persona streaming
path is modified, and every route is gated by a default-off feature flag.
"""
