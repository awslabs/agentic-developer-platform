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

## Shared MCP transport

The SDK fixture now uses `src/tool-server.ts`, the shared authenticated MCP
transport. Reviewed host code supplies tools and Zod schemas; the transport
requires each capability in the admitted set, forces strict argument parsing,
and checks current authority before calls and before disclosing receipts.
Personas cannot supply handlers, endpoints or schema extensions. Only confirmed
host receipts are returned. Unknown, oversized or revoked results close further
session admission. Duplicate MCP IDs are refused; they are not treated as durable
mutation keys. Gateway authorization and journaling remain required per operation.

Seven focused transport tests cover capability denial, invalid token/tool/arguments,
duplicate delivery, pre/post-execution revocation, unknown and oversized outcomes,
confirmed errors, call limits, overlap, request bounds, resource-method refusal and cancellation/deadline of an uncooperative host.
The real SDK fixture discovers and executes this shared transport successfully.
The report-only session/Task Responses path still refuses executable bindings;
this transport is not evidence of gateway tool integration or persona completion.

## Shared Responses bridge tool cycle

The real SDK fixture now also uses the shared Responses bridge, replacing its
custom inference HTTP/SSE server. With explicit host tool policy, the bridge
projects only the reviewed `mcp__adp` namespace and forces serial tool calls.
SDK-provided descriptions/schemas cannot replace host definitions. Native MCP
resource functions and residual built-ins are removed from model requests.

Closed function call/result types bound names, arguments and inline output;
SDK-only IDs/metadata are stripped. Input history must contain complete,
nonoverlapping call/result pairs and pass the host's explicit receipt validator.
Model responses may call only an admitted function with schema-valid arguments,
and cannot reissue a completed call ID. Tool opt-in requires a synchronous
validator returning `true`; absent authority retains the text-only behavior.
The Task gateway's report-only contract is not widened by this local opt-in.

The pinned SDK prepends a `Wall time: ... seconds` text part to MCP results.
The integration fixture validates that bounded-format wrapper separately and
compares the following evidence text exactly against its confirmed host receipt.
This observed SDK decoration must not be mistaken for gateway cost/latency or
execution evidence. Production gateway receipt binding remains outstanding.

Six additional bridge tests cover host schema projection, forged/unpaired/history
calls, namespace substitution, invalid/parallel output calls, refusal before
model handoff and completed-call replay. All 51 harness/IPC tests and the actual
SDK→Responses bridge→MCP→SDK continuation fixture pass. Inference and tool effects
remain deterministic local fixtures; no live story has been accepted by this test.


## Shared admitted session with receipt binding

`runAdmittedSession` now accepts an explicit reviewed host tool broker and an
admitted repository binding. Capability planning still intersects every policy
layer and provider support. Every executable capability needs a corresponding
host tool; the MCP catalogue is checked against the resulting admitted set.
The broker definition is snapshotted before asynchronous work. Without a broker,
report-only sessions retain their existing behavior and executable requests fail.

`ToolReceipts` binds the sole outstanding confirmed model call to the exact MCP
tool/arguments. It records only confirmed host receipts and validates the entire
ordered SDK tool history, including exact result text. It rejects foreign or
missing history, argument/output substitutions, duplicate or concurrent calls,
operation-budget exhaustion and unknown/oversized receipts. It is session-local,
not a durable journal or a safe-resume mechanism; gateway authorization and
mutation persistence remain the invocation adapter's responsibility.

The SDK tool fixture now runs through `runAdmittedSession` instead of assembling
its own SDK/MCP/model wiring. Success and confirmed tool error both exercise the
actual SDK, shared transports, shared receipt validator, live-authority callbacks,
model continuation and cleanup. Missing repository/broker authority fails before
inference. Five receipt-binding tests pass. The existing report-only actual-SDK
session fixture also passes. CI runs both tool outcomes explicitly.

This does not enable the Task API's executable contract, register personas or
claim story acceptance. The embedded Task entrypoint supplies no tool broker;
its gateway grant/Responses profile remains report-only. Durable tool receipt
validation, concrete repository/AWS/delegation adapters and completion gates
remain necessary for enabled personas.


The response bridge installs an executable session call only after validating
model output and usage; invalid usage cannot make a tool callable. Its regression
test passes alongside the 56-test suite run before adding that final check.

The post-test live-auth fingerprint check detected a changed gateway `tokens.json`
with modification time 2026-09-25 22:16:12 UTC. The earlier baseline and current
token were preserved; no restore or token/config rewrite was performed. Tests
used the isolation wrapper and temporary SDK storage. A fingerprint difference
alone cannot establish attribution. A separate current checkpoint is retained
for subsequent read-only comparisons; the original baseline is not replaced.
