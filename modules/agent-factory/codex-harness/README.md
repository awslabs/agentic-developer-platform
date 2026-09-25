# Shared Codex harness foundations

Epic #5433; harness story #6195. See the source-bound
[design](../../../docs/design-notes/5433-native-codex/README.md).

This package is an **unregistered foundation**, not a production runtime or a
claim that #6195 is complete. It is deliberately absent from task command and
persona dispatch registries and the worker Dockerfile. The existing Claude,
Task API investigator and Codex reviewer packages are unchanged.

Implemented components:

- Strict declarative definitions with bounded instructions, immutable canonical
  digests, verified skill content, capability names and completion-policy checks.
- A pure preflight planner for already-verified gateway policy, separate
  invocation source and optional GitHub/GitLab repository binding. API-only
  evidence analysis has no GitHub fields. Constraints intersect every grant layer.
- A shared official SDK streamed-turn consumer with input/output/time bounds,
  abort propagation, terminal/usage validation and content-free OTEL spans,
  counters and progress. The caller supplies the admitted isolated SDK thread.
- GitHub/GitLab change-request translation through a host-owned provider broker:
  read/create/update and exact-head squash merge, plus GitHub formal review.
  GitLab formal review remains unadvertised; a comment is not review parity.
- A pinned SDK configuration disabling native shell, goals, collaboration,
  plugin installation and related ambient features. A local rejecting HTTP
  fixture probes the actual SDK request and verifies those tools are absent.
  Residual native image/input tools still require isolation qualification.
- Candidate definitions for eight core personas. Loading JSON is not registration,
  capability qualification, authorization or production enablement. Effort settings
  are candidates to evaluate, not proven quality/cost optima.

Still required before registration: embedded Task lifecycle wiring, authoritative
snapshot/grant bootstrap binding, executable tool/network/filesystem enforcement,
authenticated repository broker execution/idempotency and GitLab formal review,
authority-safe resume, completion/schema policies, collector export,
dashboards/alerts, domain skills and live qualification. The gateway's model
reservation and durable receipt implementation now exists; the embedded Codex
entrypoint must connect it to this runtime. These remain acceptance criteria of
#6195 and its persona stories. Preflight and sandbox options alone are not a
complete security boundary.

The TypeScript SDK is pinned to the existing reviewer version 0.155.1. Its own
packaged runtime transport is used by the SDK; this package does not spawn or
parse the Codex CLI. Tests use only local protocol fixtures; no live model request is made.

Development: Node.js 24, `npm ci --ignore-scripts`,
`python3 test/run-isolated.py -- npm test`. The wrapper replaces inherited
`BG_CONFIG_DIR`, ADP deployment/state paths, HOME/XDG paths and credential stores
with temporary directories/files, and drops ambient tokens and deployment flags.
Changing HOME alone does not isolate a dev box with inherited BG_CONFIG_DIR.
Tests cover API-only preflight,
configuration/skill tampering, capability intersections, expiry, failed/incomplete
SDK streams, cancellation, output limits and actual in-memory OTEL span redaction.
Application bootstrap must install the existing approved OTLP pipeline; the OTEL
API is a no-op without one. Trace IDs, run IDs and model strings are not metric
labels; prompts, tool text, provider errors and model reasoning are not exported.

The Task API now has an additive bounded text Responses transport alongside
Messages, including durable model-operation and host IPC handling. GPT persona
registration and complete SDK history/tool support remain required for live GPT Tasks.
The packaged embedded entrypoint now connects the report-only lifecycle.
The local SDK probe is not a live model, cost or latency evaluation. Run it after
build with `python3 test/run-isolated.py -- node test/sdk-request-shape.mjs`.
The `--baseline` variant compares the pinned SDK's default tool advertisement.
Both hit only an ephemeral loopback server that rejects inference requests.

The successful SSE fixture (`test/sdk-turn.mjs`) runs the actual pinned SDK
through `runSdkTurn`, checks usage/progress, and resumes from its temporary
session store with previous input/output present. It also exercises HTTP fallback
after WebSocket refusal. This is protocol evidence, not Task API integration,
authority-safe resume, or a live quality/latency/cost result.

A credential-free text Responses bridge now provides loopback token authentication,
strict SDK request normalization, model/effort checks, output/operation/deadline
limits, validated SSE completion and refusal of further requests after uncertain
host outcomes. It discards SDK cache IDs and metadata and removes the two residual
native tool declarations from the host request. The host callback must supply a
confirmed durable model receipt. `HostBridge.responses` provides the IPC method;
TaskHost and the gateway understand the corresponding request/receipt. The
packaged Codex Task entrypoint connects these components; gateway-bound snapshot
admission is still required before registration.

The current bridge accepts complete message history and inline encrypted
reasoning, preserving assistant phase. Function calls, hosted tools, external history, media and
unknown fields are refused before host dispatch. It does not claim full persona
or tool compatibility. `test/sdk-proxy.mjs` exercises the actual SDK through this
bridge with a deterministic fixture host, including resumed text history; no live
model, gateway, Task API, credential grant or billed reservation is exercised.

`runAdmittedSession` now provisions the official SDK with fresh temporary HOME,
CODEX_HOME and workspace, applies digest-bound persona instructions via SDK
configuration, and connects it to the credential-free Responses bridge. The host
checks current grant/generation before model dispatch and before output is
returned. Session files are removed on exit. This stage refuses executable
capabilities and repository bindings; it does not implement filesystem isolation,
semantic completion acceptance or executable persona tooling.

`test/sdk-session.mjs` runs the actual SDK through this shared runtime with a
fixture host and verifies instructions, encrypted reasoning output, progress,
revocation refusal and cleanup. `test/sdk-proxy.mjs` additionally verifies resumed
encrypted reasoning and assistant phase without server item IDs. The gateway's
inline Responses contract is now `task-codex-sdk-inline-responses-v2`; it needs new
invocability evidence. No live provider compatibility is established by fixtures.

The embedded entrypoint `dist/task-entry.mjs --embedded` accepts only trusted
Task bootstrap metadata and the host-mediated lane. It validates pinned persona,
model and deadline bindings, checks current authority through correlated control
receipts, and sends model requests through the durable host bridge. Reports use
the shared Task schema and must cite host-known evidence identities. One schema
repair is permitted per amendment cycle within the same global operation budget.
Amendments restart the SDK with the original task plus committed follow-up input;
unknown model outcomes terminate without child replay. Finalization and artifact
publication remain worker-owned. Structural citations do not prove semantic truth.

Build the investigator package first, then `npm run build` here. `build.mjs`
copies the shared Task protocol/artifact validator and IPC module into `dist`;
the worker Dockerfile packages this dependency tree without registering a persona.
The isolated CI job runs `integration/test_task_codex_host.py` against a relocated
package with the real Python TaskHost and official SDK, using fixture gateway
receipts and inference. It covers successful completion, correction, invalid
output, cancellation, committed amendments, unknown outcomes and snapshot
tampering. No live story acceptance or image deployment is implied.
