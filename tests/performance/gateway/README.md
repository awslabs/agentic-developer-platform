# Gateway streaming load test

This Locust client sends real authenticated model requests. Use a dedicated test tenant and an approved target. Keep its JWT in a file readable only by the operator. The client never prints authentication headers.

```bash
python3 -m venv /tmp/gateway-perf-venv
/tmp/gateway-perf-venv/bin/pip install -r tests/performance/gateway/requirements.txt
export ADP_PERF_TOKEN_FILE=/private/path/test-token
export ADP_PERF_OUTPUT_DIR=/private/path/results
export PERF_LABEL=baseline
export PERF_MAX_REQUESTS=2000
export PERF_STAGES=60:3,120:10,210:30,330:60,390:90
/tmp/gateway-perf-venv/bin/locust -f tests/performance/gateway/locustfile.py \
  --headless --host https://gateway.example.com/api --stop-timeout 120 \
  --only-summary --csv "$ADP_PERF_OUTPUT_DIR/$PERF_LABEL" \
  --html "$ADP_PERF_OUTPUT_DIR/$PERF_LABEL.html"
```

Each stage is `cumulative_end_seconds:simultaneous_sessions`. The example models 3, 10, 30, 60 and 90 sessions. These are continuously requesting synthetic model clients, not complete developer or agent workflows. A zero-user final stage provides a cooldown window. `PERF_MAX_REQUESTS` caps generations, independently of run duration.

Defaults: Haiku 4.5, 70% longer requests with synthetic context and a 768-token output cap, 30% short requests with a 128-token cap, and 0.5–2 seconds think time. `PERF_MODEL`, `PERF_LONG_TOKENS`, `PERF_CONTEXT_REPEATS` (default 60), and `PERF_LONG_RATIO` change those parameters. Larger tests incur model charges; keep request counts, token limits, and tenant quotas bounded.

Set `PERF_API=responses` for models served by the gateway's `/openai/v1/responses` route. Set `PERF_MODEL` to the configured model ID, `PERF_REASONING_EFFORT` to the desired reasoning level (default `medium`), and `PERF_RESPONSES_MAX_TOKENS` to the total output budget (default 2048, including reasoning tokens). The client records terminal usage and incomplete-response details. This route requires a content-bearing `response.completed` event with completed status; `response.incomplete`, `response.failed`, and streamed errors count as failures.

`PERF_LONG_WORDS` adds a requested word count to the long prompt. Use it when you want a complete answer within the output budget. Without it, long token caps above 1000 request a deliberately long response for drain testing; Responses API token-budget exhaustion can then produce an incomplete response. `PERF_READ_TIMEOUT` controls the idle socket timeout (default 180 seconds), not a total generation deadline. Match reasoning, prompt/output budgets and caching assumptions when comparing models, and record the distinct API routes.

Set `PERF_UNIQUE_PROMPTS=true` to prepend a unique synthetic request ID. This varies the prompt prefix to probe cache sensitivity; inspect provider usage/cache counters rather than assuming cache misses. By default, prompts repeat. Zero-user stages stop all users together, allowing existing streams to drain within Locust's stop timeout.

Every successful generation requires nonempty streamed content, no stream error, and a terminal `[DONE]` marker. JSONL evidence records first-content latency, full completion latency, largest content-chunk gap, response status, stream completion and client-observed concurrency. Stop-timeout interruptions are explicitly identified in new runs. The full-response duration replaces Locust's default headers-only timing.

Locust also records TTFT under the `STREAM` request type. Its combined aggregate count therefore includes both timing and generation records. Use `POST LLM/*` rows or the JSONL generation records for request/error counts. Keep rejected, interrupted, and incomplete streams visible; do not count HTTP 200 alone as success.

For scaling validation, collect per-pod CPU/memory, HPA desired/current counts, readiness/restarts, database CPU/connections and Redis health alongside these files. Test the same image and instrumentation on each candidate. Verify scale-out, return to the minimum replica count, and graceful draining under active long streams. A quota rejection or overloaded shared database is not gateway CPU saturation.

Run the local SSE validation tests with the same virtual environment; these use a loopback server and make no cloud requests:

```bash
/tmp/gateway-perf-venv/bin/python tests/performance/gateway/test_stream_validation.py
```
