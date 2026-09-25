# Pinned SDK host-tool compatibility

Date: 2026-09-25. Official Codex SDK: 0.155.1. Node: 24.8.0.

`test/sdk-tools.mjs` runs the official SDK against an authenticated loopback MCP
server and deterministic local Responses inference. One streamed function call
executes the MCP evidence tool exactly once, then the SDK carries the matching
function call and result into a second model request and returns the final text.
No external model, repository, AWS or ADP action is performed.

The pinned SDK emits MCP tools as a Responses `namespace` declaration named
`mcp__adp`, containing a function named `read_evidence`. Function-call outputs
must include `namespace: "mcp__adp"` as well as the function name. Assuming flat
function declarations failed the real SDK fixture before any tool execution.
The fixture checks matching call IDs and the actual evidence receipt in history.

Adding MCP also advertises native resource discovery/read tools, in addition to
`view_image` and `request_user_input`. The production report-only bridge still
rejects this expanded surface. This fixture does not widen its allowlist or
executable capability grants. Production integration requires host-owned tool
catalogues, closed namespace/call/result contracts, durable authority and
mutation receipts, and filesystem/network isolation. SDK configuration alone
cannot establish those boundaries.

Official documentation fetched before the experiment:
https://developers.openai.com/codex/mcp/ describes Streamable HTTP, bearer-token
environment configuration, required startup and enabled-tool allowlists. The
namespace wire format above is observed evidence for the pinned runtime.

Reproduce after building the harness, with Node >=24:

```sh
python3 test/run-isolated.py -- node test/sdk-tools.mjs
```

The SDK environment contains only temporary configuration paths and a randomly
generated fixture token. All HTTP requests stay on loopback; servers and SDK
storage are cleaned up in `finally`. CI runs this compatibility fixture.
