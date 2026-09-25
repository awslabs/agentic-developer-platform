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

Not implemented here yet: authoritative grant verification/revocation, SDK
session provisioning, host tool/network/credential enforcement, durable queue and
input receipts, model reservation/price integration, authenticated provider
broker execution/idempotency and GitLab formal review, safe
resume, output-schema/completion enforcement, collector bootstrap/export,
dashboards/alerts, domain skills and live qualification. These remain acceptance
criteria of #6195 and its blocked persona stories. Preflight and sandbox options
are **not** a security boundary or a substitute for these missing host controls.

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

The current Task API model path is Anthropic Messages-only. Codex needs an
explicitly admitted Responses transport and bounded request/usage/history
contracts; it cannot be made compatible by relabeling an existing SDK request.
The local SDK probe is not a live model, cost or latency evaluation. Run it after
build with `python3 test/run-isolated.py -- node test/sdk-request-shape.mjs`.
The `--baseline` variant compares the pinned SDK's default tool advertisement.
Both hit only an ephemeral loopback server that rejects inference requests.

The successful SSE fixture (`test/sdk-turn.mjs`) runs the actual pinned SDK
through `runSdkTurn`, checks usage/progress, and resumes from its temporary
session store with previous input/output present. It also exercises HTTP fallback
after WebSocket refusal. This is protocol evidence, not Task API integration,
authority-safe resume, or a live quality/latency/cost result.
