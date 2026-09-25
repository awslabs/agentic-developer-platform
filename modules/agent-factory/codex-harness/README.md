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
- Candidate definitions for eight core personas. Loading JSON is not registration,
  capability qualification, authorization or production enablement. Effort settings
  are candidates to evaluate, not proven quality/cost optima.

Not implemented here yet: authoritative grant verification/revocation, SDK
session provisioning, host tool/network/credential enforcement, durable queue and
input receipts, model reservation/price integration, provider operations, safe
resume, output-schema/completion enforcement, collector bootstrap/export,
dashboards/alerts, domain skills and live qualification. These remain acceptance
criteria of #6195 and its blocked persona stories. Preflight and sandbox options
are **not** a security boundary or a substitute for these missing host controls.

The TypeScript SDK is pinned to the existing reviewer version 0.155.1. Its own
packaged runtime transport is used by the SDK; this package does not spawn or
parse the Codex CLI. No model request is made by the tests.

Development: Node.js 24, `npm ci`, `npm test`. Tests cover API-only preflight,
configuration/skill tampering, capability intersections, expiry, failed/incomplete
SDK streams, cancellation, output limits and actual in-memory OTEL span redaction.
Application bootstrap must install the existing approved OTLP pipeline; the OTEL
API is a no-op without one. Trace IDs, run IDs and model strings are not metric
labels; prompts, tool text, provider errors and model reasoning are not exported.
