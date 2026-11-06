# ADP gateway client spike — dev observations and remaining limits

Issue [#7253](https://github.com/aws-e/adp/issues/7253) asks whether pinned Claude Code and Codex clients can list and call tools through `adp mcp` and the pipeline-deployed dev façade, and how latency, streaming, edge timeouts and quotas affect per-turn use. A platform operator supplied dev façade observations on 2026-10-15; the measured shim path and client-driven behavior are distinguished below. Source inspection must not substitute for live results.

## Client discovery (TLS-S01-AC01; operator-run 2026-10-15)

The operator used pinned clients in bearer laptop mode through the `adp mcp` stdio shim, the deployed dev façade and AgentCore gateway. The operator retains per-call records privately; this public report omits target addresses, user and resource identifiers, credentials and raw evidence. These outcomes are operator-supplied, not independently reproduced by this worker.

| Client | Pinned version | Bearer-mode `tools/list` through shim and façade | Client wall time |
|---|---|---|---|
| Claude Code | 2.1.300 | **Success:** six tools discovered, including `websearch___search` | 8.1 s for client interaction |
| Codex | codex-cli 0.160.0 | **Success:** the same six tools discovered (`mcp__adp_tools__*` names in Codex) | 15.9 s for client interaction |

The remaining tools were `adp___operations_list`, `adp___operation_retry`, `adp___operation_status`, `adp___artifact_read` and `adp___execution_close`. A direct stdio `initialize` + `tools/list` exchange took 1.55 s; the client wall times include model interaction and are **not** façade-only `tools/list` latency. Codex also probed `resources/list`; the shim returned `-32601 Method not found` without preventing discovery. Discovery is not proof of a subsequent `tools/call`: the Codex client did not invoke the search tool in the operator's end-to-end attempts.
## Search calls through the shim (TLS-S01-AC02; operator-run 2026-10-15)

The operator ran **100 sequential `websearch___search` calls**, 50 in each of two long-lived shim processes, with fresh request IDs and bounded, non-sensitive queries. Each process used the same shim → dev façade → AgentCore path that the clients configure as their MCP server. The lane names describe how the operator grouped calls; **these are not 50 model-driven calls by each client**. All 100 calls returned `status: confirmed` with execution, receipt and artifact references. The operator retains raw timings and request records privately; the aggregates below are operator-supplied, not independently recomputed from raw records by this worker.

| Measurement | Claude Code lane (shim, 50 calls) | Codex lane (shim, 50 calls) | Combined (100 calls) |
|---|---:|---:|---:|
| Confirmed / attempted | 50 / 50 | 50 / 50 | 100 / 100 |
| p50 (ms) | 3594 | 3622 | 3607 |
| p95 (ms) | 4155 | 4334 | 4204 |
| Longest completed call (ms) | 9997 | 4906 | 9997 |
| HTTP 429 / quota name | 0 / none observed | 0 / none observed | 0 / none observed |
| Failed / skipped shim calls | 0 / 0 | 0 / 0 | 0 / 0 |

The operator reported roughly 0.25 requests/s and attributed the 9997 ms outlier to the first (cold) call. Zero 429s at this tested rate does **not** establish a provider quota, a safe maximum rate or behavior under concurrency. No observed 429 means there is no quota name to report; this is not an assertion that rate limits do not exist. The operator enabled required catalogue access, tenant entitlement and tool policy before testing, so these measurements do not prove an unconfigured dev installation works out of the box.

## Client-driven calls and unverified boundaries

- **Claude Code 2.1.300:** a client-driven search returned a confirmed result with receipt and artifact (three model turns, 71.6 s client wall time). Two earlier attempts capped at four turns stopped before an answer; a tool call and reply may require a higher turn budget. Model wall time is not comparable to the shim-only call latency above.
- **Codex 0.160.0:** **not run as a client-driven search**. In three attempts the model declined to invoke the visible search tool; no search request from that client reached the façade. This is a Codex-to-ADP model-proxy tool-exposure gap to investigate, not a failed gateway call. Its successful `tools/list` remains valid.
- **Streaming:** the shim buffered one JSON response per call in the 100-call test. Neither client was exercised with a long-running streamed response, so client-visible partial-result streaming remains untested.
- **Edge and client timeouts:** none of the 100 calls approached 30 s. The longest completed call was 9.997 s; neither CloudFront nor API Gateway response-timeout limits or configured edge values were established. A bounded long/streaming call and deployed edge configuration are still needed to identify which hop times out first.
- **Token refresh:** the bearer token remained valid during the window; no post-expiry call was run, so refresh behavior in either client is unverified.

## Source findings and design decision

The bundled shim (`modules/gateway/cli/adp-mcp.py`) acquires a bearer token from `adp token` when constructing its gateway context. Its HTTP request has a **30-second timeout** and buffers a completed JSON response over stdio. The façade's AgentCore client (`modules/tools/facade/tools_facade/server.py`) also has a **30-second HTTP timeout** and parses a completed response, including a single event from an event stream. These are source-level limits, not measurements of CloudFront or API Gateway configuration, and no client-visible partial-result streaming was observed.

For TLS-S09, use the measured search latency (~3.6 s p50, ~4.2 s p95 at the tested sequential rate) to inform per-turn budgets; do **not** derive a quota or concurrency limit from zero 429s. For TLS-S10, treat this path as buffered for now and do **not** promise streaming or completion of calls past the shim/façade 30-second limits without an instrumented long-call test and deployed edge settings. Resolve the Codex tool-invocation gap before claiming end-to-end Codex tool use. An authorized L1 follow-up should test expiry/refresh, edge and streaming boundaries and any higher-rate quota separately; it must not turn a mock or shim-only result into client-driven acceptance.
