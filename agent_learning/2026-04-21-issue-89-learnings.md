# Issue #89 Learnings: status=completed frame delivered with 0 chars of content

**Date**: 2026-04-21
**Agent**: @agent-developer
**PR**: #91

## Root Cause

The response Lambda at `modules/agent-factory/gateway/lambdas/response/handler.py:67` had a conditional content extraction that branched on `status`:

```python
# BEFORE (broken)
content = response.get("text") if status == "progress" else response.get("result", response.get("content", ""))
```

The TS chat-agent worker (`agent/src/complex-task-chat/sqs-client.ts`) uses `text` for ALL payloads (both `TaskResponse` with `status: completed|failed` and `ProgressMessage` with `status: progress`). The response Lambda only read `text` for progress frames; for terminal frames it looked for `result` (legacy Python worker field) or `content`, found neither, and sent `""`.

## Fix

One-line change to unify content extraction:

```python
# AFTER (fixed)
content = response.get("text") or response.get("result") or response.get("content") or ""
```

Uses Python's truthiness chain (`or`) to check all three field names in priority order. This is backward-compatible — legacy Python workers that use `result` still work.

## Key Insights

1. **Field name mismatch between worker and Lambda is a recurring pattern.** The TS worker uses `text` universally; the Python Lambda had divergent extraction logic per status. When adding status-forwarding in PR #87, the content extraction wasn't unified.

2. **The `or` chain vs `get()` with defaults matters.** `response.get("text") or response.get("result")` correctly handles both missing keys AND empty strings (both are falsy). The old code used `response.get("result", response.get("content", ""))` which only handled missing keys — an explicit `""` value would still be returned.

3. **The Lambda log said "Sent to WebSocket" — it thought delivery succeeded.** API Gateway returned success for the frame send, because a frame WAS sent — it just had empty content. You can't debug content bugs from delivery logs alone; you need to inspect the frame payload.

4. **Existing tests used `result` field** (matching legacy Python worker shape), so they passed even with the bug. Tests must cover all known worker payload shapes (TS `text`, legacy Python `result`, generic `content`).

## Files Changed

- `modules/agent-factory/gateway/lambdas/response/handler.py` — line 67, content extraction
- `modules/agent-factory/tests/lambda/test_response_handler.py` — 6 new tests in `TestContentExtraction`
