# Learnings: Issue #1746 Story 2 — Fix Lost-Subprocess-Logs Gap

**Date:** 2026-06-24
**Agent:** @agent-developer
**Issue:** #1746 (Knowledge Layer observability)
**Story:** 2/7 — Fix lost-subprocess-logs gap

## What Worked

1. **Threading approach for subprocess streaming**: Using `threading.Thread` as a reader for `process.stdout` while `process.wait(timeout=N)` handles the deadline was the correct pattern. The naive approach (`for line in process.stdout`) blocks indefinitely if the child produces no output (e.g. `time.sleep(60)`), making timeout enforcement impossible without threads.

2. **Tail-of-output buffer**: Keeping the last 50 lines (and including last 10 in errors) provides excellent diagnostics without unbounded memory. Previous 500-char truncation was too aggressive — real SCIP/cgc errors often appear at the end of output.

3. **Env propagation via `child_env.setdefault()`**: Using `setdefault()` ensures we don't override env vars already set by the container orchestrator (K8s ConfigMap envFrom), while still providing defaults for the telemetry vars the child needs.

4. **Wiring all child scripts to `configure_telemetry()`**: Since the parent streams child stdout line-by-line through the parent's logger, the child's output doesn't need to be valid JSON for the system to work. But making it JSON-structured means CloudWatch Logs Insights queries can parse the child's fields too (double benefit).

## What Didn't Work / Gotchas

1. **Hyphenated filenames can't be `import`ed in Python**: `sqs-worker.py` requires `importlib.util.spec_from_file_location()` to import in tests. Used a `_load_sqs_worker()` helper at test module level.

2. **Module-level side effects**: `sqs-worker.py` calls `configure_telemetry()` and `boto3.client()` at import time. This means test imports execute real boto3 client creation (which succeeds silently with empty credentials but would fail on actual API calls). The tests only call `_run_subprocess()` which doesn't need SQS, so this is fine.

3. **E402 (import not at top of file)**: All ingestion scripts have a deliberate pattern: `configure_telemetry()` runs before importing `config`/`settings` because the telemetry configuration must happen before any other module's logging output. This creates unavoidable E402 violations. These are pre-existing (even before Story 2) and are a conscious design choice.

4. **`for line in process.stdout` doesn't raise on process death**: When a process is killed (timeout), the stdout pipe closes gracefully — the iterator simply ends. No special exception handling needed in the reader thread.

## Key Technical Decisions

- **Reader thread is `daemon=True`**: If the main thread exits (e.g. on timeout), the reader thread is automatically cleaned up. We still call `reader.join(timeout=5)` after kill to ensure we capture any remaining output.

- **`stderr=subprocess.STDOUT`**: Merge stderr into stdout so all output flows through one stream. This matches the previous behavior and avoids needing two reader threads.

- **Process.wait vs communicate**: We can't use `communicate()` because it reads all output into memory (defeating the streaming purpose). `process.wait()` + thread is the correct pattern for streaming with timeout.

## Version / Environment Notes

- Python 3.13.14 in CI
- `python-json-logger==2.0.7` (installed but unused by telemetry.py — it implements its own JSON formatter)
- pytest 9.1.1
- ruff 0.15.19

## Recommendations for Future Stories

- **Story 3 (traces)**: The threading pattern in `_run_subprocess` means spans must be created in the main thread (the reader thread won't inherit the parent's span context automatically). Use `opentelemetry.context.attach()` in the reader thread if per-line spans are desired.
- **Story 4 (Door traces)**: Door runs in a different process entirely — no subprocess concern. Standard FastAPI auto-instrumentation works.
- **Story 5 (metrics)**: The bookend events already carry `duration` — converting these to OTel histograms should be straightforward.
