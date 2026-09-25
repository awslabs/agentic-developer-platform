# Pinned SDK transport probe — 2026-09-25

Producer: `modules/agent-factory/codex-harness/test/sdk-request-shape.mjs`.
Runtime: Node.js 24.8.0, official @openai/codex-sdk 0.155.1 and its bundled runtime,
Linux x64. Run through `test/run-isolated.py`; HOME, CODEX_HOME, BG_CONFIG_DIR,
ADP/XDG stores and token files were temporary. No inherited provider/ADP tokens.
Live ADP/GitHub/AWS configuration/token file fingerprints were unchanged afterward.

The SDK received a constant, minimal prompt in an empty temporary workspace and
was configured with `gpt-5-codex`, medium effort, read-only sandbox, disabled web
search and a loopback `/v1` base URL with an invalid fixture-only key. The fixture
rejected every request with HTTP 403; no model output or tool execution occurred.
The configured model identifier is a fixture input, not a production model choice
or invocability proof. No source code, prompt bodies, credentials or provider
responses are retained in these records.

The SDK sent streaming `POST /v1/responses`. Default configuration advertised
native shell, goals and collaboration tools despite read-only sandbox mode. The
host-owned restricted profile removed those tools and reduced the minimal request
from approximately 40 KB to 25 KB. Exact byte counts are recorded in adjacent JSON
and include environment-dependent temporary paths. This is serialized-request
size, not billed tokens, model latency or dollar savings.

The restricted profile still advertised `request_user_input` and `view_image`.
Do not equate these feature flags with complete tool or filesystem isolation.
Runtime registration remains blocked until residual tools and per-operation broker
permissions are qualified. Production delegation must use ADP's trusted dispatch;
native SDK sub-agents/goals cannot silently introduce another orchestration loop.

Task API implications:

- The existing gateway task resolver and execution path admit Anthropic Messages,
  including the cyber SDK shape; Codex cannot use that binding or pricing receipt.
- Responses needs explicit source-bound admission, reservation, metering and
  durable operation/handoff receipts. Never relax the Messages validator globally.
- The current 65,536-byte task frame must budget for SDK instructions/tool overhead,
  not only the user prompt. Validate the complete serialized request before billing.
- Existing Responses quotation rejects unsupported stateful/history items. Native
  SDK multi-turn tool/response history must be qualified explicitly; a single
  text-only request is not proof of full Codex compatibility.

Reproduce from the harness package after compilation:

```
python3 test/run-isolated.py -- node test/sdk-request-shape.mjs --baseline
python3 test/run-isolated.py -- node test/sdk-request-shape.mjs
```

The restricted probe fails if shell, native collaboration or goal tools reappear.
CI executes that probe against the exact pinned SDK. Live model/security/performance
qualification remains separate and is not established by these fixture receipts.
