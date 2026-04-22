# Learnings: Issue #97 Phase 2 — AG-UI Event Protocol

**Date**: 2026-04-22
**Agent**: @agent-developer
**Issue**: #97 (Phase 2)
**PR**: #100

## What Worked

### Dual-format backward compat via envelope detection
The `ag_ui_event: true` field in the SQS envelope lets the response Lambda distinguish AG-UI events from legacy frames without changing the transport (SQS queue, Lambda trigger, WS router). The Lambda checks one field and branches — clean and rollback-safe.

### Feature flag (`AGUI_EVENTS_ENABLED`)
Defaulting to `'1'` (enabled) but allowing `'0'` to disable means the worker can be rolled back to legacy-only in seconds via env var, without a code deploy. Essential for the backward-compat window.

### Hook API compatibility
`useAgUiEvents` has the same public API as `useAgentChat` (plus `sessionMeta` and `activeToolCalls`). The page component change was minimal — just swap the import and destructure the new fields. This kept the diff small and testable.

### AG-UI WS frame wrapper
Wrapping AG-UI events in `{ type: "ag_ui", event: {...} }` at the WS router level means the frontend can distinguish AG-UI frames from legacy frames with a simple `frame.type === 'ag_ui'` check. The event payload is forwarded as-is — no re-serialization.

## Key Decisions

### Own types vs importing `@ag-ui/core`
We define our own AG-UI types on both server and client rather than importing `@ag-ui/core`. Reasons:
1. Worker Docker image stays lean (no extra npm package)
2. Frontend bundle stays small (no tree-shaking risk)
3. We control the exact shapes — if AG-UI v2 changes, we update our types on our schedule
4. Contract tests validate conformance against the spec

### AG-UI events are ephemera; legacy sendResponse handles thread bookkeeping
AG-UI events like RUN_FINISHED are paired with a legacy `sendResponse()` call. The AG-UI path (`status: 'ag_ui'`) skips thread bookkeeping (re-enqueue, lock clearing) — that still happens via the legacy `status: 'completed'` path. This means removing the legacy path later requires moving thread bookkeeping to the AG-UI path.

### Tool calls: START/ARGS/END bundled per progress event
Since the agent's progress callback fires once per tool_use event (not streaming args), we emit TOOL_CALL_START → TOOL_CALL_ARGS → TOOL_CALL_END in quick succession. The frontend tracks tool calls by ID in a Map ref.

## Technical Gotchas

### `async act()` for hook tests with async connect
The `useAgUiEvents` hook's `connect()` is async (awaits token). In tests, `await vi.advanceTimersByTimeAsync(10)` alone isn't enough — need `await act(async () => { await vi.advanceTimersByTimeAsync(50) })` to flush both microtasks and timer callbacks.

### FIFO dedup IDs for AG-UI events
Each AG-UI event needs a unique MessageDeduplicationId on FIFO queues. Using `agui_${task_id}_${event_type}_${Date.now()}` handles this, but rapid-fire events within 1ms could theoretically collide. Not a real risk in practice (SQS has 5-minute dedup window, and Date.now() resolution is 1ms).

### Response Lambda AG-UI routing: content vs metadata
For AG-UI events, the response Lambda puts the serialized event in `content` (for the WS router's chunk-splitting) but ALSO in `metadata["ag_ui_payload"]` (for the WS router to build the structured `type: "ag_ui"` frame). The WS router prefers the structured path; chunk-splitting is a fallback for oversized events.

## File Reference

| File | Purpose |
|------|---------|
| `modules/agent-factory/agent/src/complex-task-chat/ag-ui-events.ts` | Server-side AG-UI event types |
| `modules/agent-factory/agent/src/complex-task-chat/ag-ui-events.test.ts` | Contract tests (23 tests) |
| `modules/agent-factory/agent/src/complex-task-chat/sqs-client.ts` | `sendAgUiEvent()` method |
| `modules/agent-factory/agent/src/complex-task-chat/complex-task-chat-agent.ts` | AG-UI emission logic |
| `modules/agent-factory/gateway/lambdas/response/handler.py` | AG-UI event routing |
| `modules/agent-factory/gateway/lambdas/response/routers/websocket.py` | AG-UI WS frame wrapping |
| `modules/gateway/frontend/src/types/ag-ui-events.ts` | Frontend AG-UI types |
| `modules/gateway/frontend/src/hooks/useAgUiEvents.ts` | AG-UI event consumer hook |
| `modules/gateway/frontend/src/components/chat/ToolCallRow.tsx` | Collapsible tool call UI |
| `modules/gateway/frontend/src/components/chat/SessionMetaPanel.tsx` | Session metadata display |

## Next Steps

- After 1 week of stable operation with both formats, remove legacy `sendProgress()` calls and the `handleLegacy*` paths in the frontend hook
- Phase 3: A2UI renderer + catalog + `render_ui` worker tool
