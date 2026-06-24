# Issue #1746 — Knowledge Layer Observability: Telemetry Foundation

**Date**: 2026-06-24
**Agent**: @agent-developer
**Scope**: Story 1 of EPIC #1746 — structured logging + correlation context

## What Was Done
Implemented the telemetry foundation module (`telemetry.py`) for the Knowledge Layer
ingestion pipeline. This provides JSON structured logging with a correlation context
that carries `asset_id`, `owner_sub`, `tenant_id`, `project_id`, `run_id`, `stage`,
and `asset_type` on every log line.

## Key Technical Decisions

### Why contextvars (not thread-local)
The ingestion pipeline may evolve to use async paths (Door server already uses FastAPI/async).
contextvars are the correct choice for both sync and async Python — they propagate through
`await` boundaries. This matches the gateway's pattern in `modules/gateway/src/shared/logging.py`.

### Why stdlib json.dumps (not python-json-logger for the formatter)
The formatter uses `json.dumps()` directly to avoid a hard runtime dependency on python-json-logger.
The package is still added to the Dockerfile for downstream Story 4 (Door instrumentation via
FastAPI middleware), but the core ingestion path works with zero external deps for formatting.

### Why fail-open with safe_emit()
The #1695 lesson: telemetry must never block ingestion. `safe_emit()` wraps any callable
with a blanket try/except. The JSON formatter also catches broken contextvars. The kill switch
(`KNOWLEDGE_LAYER_TELEMETRY_ENABLED=false`) drops to plain text output.

## Gotchas and Non-Obvious Findings

1. **The remote branch already existed** — the architect agent created it with the design note
   (PR #1750). Had to `fetch` + `rebase` before pushing implementation commits.

2. **E402 lint errors in sqs-worker.py are pre-existing** — `from config import settings` has
   always been below the module-level code. Don't try to fix these in this PR (surgical changes).

3. **Test isolation matters** — the `_configured` flag in `telemetry.py` is module-level state.
   Tests must reset it in a fixture (`telemetry._configured = False`) or they'll interfere.
   Also reset root logger handlers between tests.

4. **The ADOT Collector endpoint** is at `adot-collector.adp-agents.svc.cluster.local:4317`
   (cross-namespace from `agent-context`). This is fine for ClusterIP in default K8s, but
   verify NetworkPolicy doesn't block it before Story 3 (traces).

5. **python-json-logger version pinned to 2.0.7** — matches the gateway's pin exactly.
   Version 3.x has breaking API changes.

6. **SQS message envelope fields for correlation** — `owner_sub`, `tenant_id`, `project_id`
   are not yet present in all published messages (they come from #1721 and #1728). The code
   gracefully handles `None` values (they're excluded from logs via `as_dict()`).

## Files Created/Modified
- **New**: `modules/agent-context/images/ingestion/telemetry.py` (230 lines)
- **New**: `modules/agent-context/tests/unit/test_telemetry.py` (18 tests)
- **Modified**: `modules/agent-context/images/ingestion/sqs-worker.py`
- **Modified**: `modules/agent-context/images/ingestion/stage_tracker.py`
- **Modified**: `modules/agent-context/images/ingestion/config.py`
- **Modified**: `modules/agent-context/images/ingestion/Dockerfile`

## Next Steps (Stories 2-7)
- **Story 2**: Fix lost-subprocess-logs — change `subprocess.run(capture_output=True)` to
  streaming `Popen` with line-by-line forwarding. Child processes should inherit OTLP env vars.
- **Story 3**: Add OTel SDK to ingestion image, instrument `StageTracker.stage()` to emit spans.
- **Story 4**: Add OTel FastAPI auto-instrumentation to Door server.
- **Story 5**: Define metrics (counters/histograms), extend ADOT IAM.
- **Story 6**: Terraform the CloudWatch dashboard + alarms.
- **Story 7**: Fail-open regression test + kill switch documentation.

## PR
- PR #1750: https://github.com/aws-e/adp/pull/1750
