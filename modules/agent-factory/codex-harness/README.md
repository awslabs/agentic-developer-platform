# Shared Codex harness foundations

Epic #5433; harness story #6195. See the source-bound
[design](../../../docs/design-notes/5433-native-codex/README.md).

This package is packaged in the worker image with explicitly gated Task-only registrations and remains
unqualified for production. The developer Task flow now runs the official SDK,
host-mediated edits, immutable-image validation, scoped GitHub publication, and
fresh provider-state completion checks. See the
[2026-09-26 qualification evidence](../../../docs/design-notes/5433-native-codex/qualification-20260926.md)
for the exact test boundary and remaining story requirements.

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

Still required for story closure: production qualification and validation executor,
GitHub mention and GitLab execution parity, pause/resume, connected role/delegation
qualification, memory lifecycle/gateway integration, dashboards/alerts and a
measured Claude comparison. The implemented Task path preserves admission,
generation fences, budget reservations and durable model/tool receipts. Current
local Docker validation is not a Kubernetes deployment qualification.

The TypeScript SDK is pinned to the existing reviewer version 0.155.1. Its own
packaged runtime transport is used by the SDK; this package does not spawn or
parse the Codex CLI. Default tests use fixtures. Live inference requires an explicit opt-in and isolated copied credentials.

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
admission is implemented; persona/model qualification is still required before registration.

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

Gateway configuration can be compiled after building this package:

```sh
node scripts/catalogue.mjs --skills ./reviewed-skills personas/intent-refinement.json > reviewed-catalogue.json
```

Each referenced skill is `<id>.md` in that directory and must match its manifest
SHA-256. Mount the reviewed output as deployment-owned configuration and set
`ADP_CODEX_PERSONA_CATALOG_FILE` in the gateway. This command emits configuration;
it does not change the live gateway, register a persona or grant capabilities.
Admission verifies the authoritative persona class, standing service policy,
model binding and snapshot. It freezes the snapshot inside the protected Task
grant before reservations, and refuses a combined bootstrap exceeding the IPC
frame bound. Existing tasks never reload instructions from a changed catalogue.
The current runtime's capability ceiling remains report publication until the
executable brokers and persona completion policies are qualified.


Developer completion and telemetry (2026-09-26)

A developer requires explicit acceptance criteria mapped by repository policy
`acceptance_checks` to immutable-image `/opt/adp-checks/<name>` entrypoints.
The key is SHA-256 of the decimal criterion index, a NUL separator and the exact
criterion text. Repository-owned tests may supplement these checks but cannot
certify their own requirements. `change.create` publishes the scoped Task branch
and PR together; separate generic `branch.push` authority is not required.
Completion checks the current clean workspace, all required checks, durable
publication receipt, current PR head/base/tree/open/non-draft state and current
attempt. Finalization observes provider state again. Amended requirements are
refused until renewed independent acceptance evidence is supported.

Repository service policy may set `limits.codex_max_turns` up to 32 (developer
snapshot currently caps at 20). Omission preserves the existing eight-turn
limit. Non-Responses Tasks retain the legacy cap. Time, token and spend limits
still apply; more available turns do not imply more default model work.

Host-only `ADP_CODEX_OTEL_ENDPOINT` selects an OTLP HTTP collector base URL.
Admission freezes valid gateway trace context; SDK IPC carries W3C trace context
and the signed worker transport emits W3C and X-Ray headers. Default telemetry
contains identifiers, bounded metrics, model/tool/completion spans and correlated
operation logs, not task/source content.
Exporter queues/timeouts are bounded and shutdown waits at most 750 ms. The
collector setting is not copied into the isolated SDK subprocess. This provides
export plumbing; production collector routing, dashboards and alerts remain to
be qualified.


The packaged Task keys `agent-task-gpt-developer` and
`agent-task-gpt-intent-refinement`, `agent-task-gpt-architect`,
`agent-task-gpt-product` and `agent-task-gpt-pm` have authoritative Codex compatibility metadata
and resolve to this shared entrypoint only when the host explicitly includes them
in `ADP_CODEX_TASK_PERSONAS`. The Terraform `codex_task_personas` setting defaults
to an empty set in every environment; `codex_otel_endpoint` uses the existing ADOT collector when
`enable_agent_otel` is true, and otherwise defaults to disabled export. No legacy mention or automatic routing is added. The gateway
still requires a reviewed catalogue, current model evidence and explicit service
policy; enabling a worker is not an admission grant.
