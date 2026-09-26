# Shared Codex harness and declarative GPT personas

Design revision: 2026-09-25. Epic: #5433.

The owner directed implementation by the coding assistant, not assignment to ADP
agents. The decisions below capture the owner's requested architecture; they do
not assert that integrations have shipped or that performance has been measured.
Source inspected: `caec7c742`. Implementation must preserve the existing Claude
agents, Task API investigator, and Codex reviewer until an explicit migration is
qualified. The owner approved the architectural direction in the design session;
new security, rollout, and model-access decisions are not implicitly approved.

## 1. Composition

```
GitHub/GitLab events   Task API   explicit adp-trigger delegation
         \                |                /
          trusted invocation adapters / admission
                          |
              immutable admitted run context
                          |
              shared official Codex SDK harness
              + versioned persona definition
              + authorized skills/capabilities
                          |
          optional repository provider adapter
                 GitHub          GitLab
                          |
          shared gateway authority, model and evidence services
```

Invocation adapters and repository adapters are different interfaces. Neither
personas nor the SDK runtime require a GitHub issue, installation ID, PR, or
GitHub credential. A Task API run can operate solely on supplied evidence and
produce durable artifacts. A repository-backed task additionally needs an
explicit authorized repository binding; a URL in task text cannot create one.

Use the official TypeScript `@openai/codex-sdk`, initially evaluating the existing
exact `0.155.1` package pin from `codex-reviewer/package-lock.json`. The harness
calls SDK thread/turn APIs and consumes structured events. It does not implement
a direct Codex CLI subprocess/terminal parser. The SDK's own packaged transport
and runtime are part of the pinned compatibility evidence. Required controls
unsupported by that SDK must be proven through a supported SDK upgrade or an
app-server facility behind this adapter before enabling the affected capability.

Official reference: https://developers.openai.com/codex/sdk/ (read 2026-09-25).
The SDK supports starting, continuing, and resuming threads. This fact alone is
not evidence of safe ADP resume, tool authorization, metering, or tenant isolation.

### Required GitHub mention invocation

Owner requirement (2026-09-26): support `@agent-codex-<persona>` in GitHub
issue and PR comments through the existing webhook ingress and trusted admission
path. Examples include `@agent-codex-developer`, `@agent-codex-architect`,
`@agent-codex-reviewer`, `@agent-codex-operations` and `@agent-codex-aidlc`.
These are the required public mention names; the earlier proposed `@agent-gpt-*`
spelling is not a substitute. Public mentions map explicitly to the authoritative
persona registry, regardless of internal persona-key spelling.

Match complete mention tokens so `@agent-codex` cannot capture a longer persona
name and an unknown suffix cannot fall back to the legacy supervisor. Resolve
only registered, authorized and enabled personas; return a visible unsupported
or unavailable outcome otherwise. Newly admitted dynamic personas must gain
their registered mention through configuration without rebuilding the harness.
Preserve existing multi-mention, bot-loop and trusted delegation rules.

The existing `@agent-codex-reviewer` name requires an explicit qualified migration
to the shared SDK reviewer; do not register competing routes or silently switch
the runtime of an active run. Bare `@agent-codex` retains its existing meaning.
Signature verification, installation/repository binding, invoking human or
delegated root, tenant, budget, source revision and delivery deduplication remain
part of admission. A mention alone grants no repository or merge authority.

GitHub-started runs use the same shared pause/resume/abort/steer contract as
Task API runs, retain source-correlated progress and terminal results, and use
the provider adapter for authorized repository effects. End-to-end qualification
must submit real issue and PR comments, verify exact persona/SDK/model routing,
exercise remote controls, and confirm results in the originating GitHub context.
Cover webhook redelivery, unknown suffixes, prefix collisions, revoked access
and dynamic persona registration. This invocation path remains required work;
Task-only fixture tests do not qualify it.

## 2. Shared harness boundary

The harness owns admission consumption, lifecycle, workspace/session isolation,
SDK invocation, bounded recovery, model/effort configuration, structured outcomes,
validation evidence, telemetry and capability routing. Invocation adapters own
source-specific acknowledgements, durable control/input receipts and results.
Provider adapters own repository operations. Personas supply intent, workflow,
skills, requested capabilities and output contracts; they do not implement loops,
credential handling, queue acknowledgement, provider clients or telemetry clients.

The admitted context binds tenant, canonical root principal and principal kind,
run/task/chain IDs, source revision, persona digest, policy revision, harness
revision, canonical model, model destination, price revision, deadlines, budgets,
capability grants and optional repository binding. It is produced only by the
existing trusted gateway admission path. A structurally valid JSON object is not
a verified grant. Revalidate expiry/revocation at privileged operations.

Use PMM-03's existing persona/class registry, per the settled ownership in
`../5417-per-invoker-persona-model-mapping.md` §1.7a. Register new personas in
`codex-sdk`; never infer the harness from model prefixes or maintain a second
persona-to-harness authority. GPT personas require a separately proven compatible
GPT default and request shape. No Claude fallback or implicit model substitution.

## 3. Declarative personas

A platform/tenant-authorized catalogue publishes immutable definitions containing:

- Schema version, persona key/revision, display name and instruction text.
- Skill references pinned by digest, required/optional capabilities, supported
  invocation surfaces and supported repository-provider operations.
- Output schema and completion/validation policy identifiers.
- Model/effort preferences, context budget, turn/time limits and retry ceilings.

Canonical serialization produces a definition digest. Admission validates and
snapshots the definition and skill hashes. Editing a persona affects new runs;
resume and downstream propagation retain the admitted snapshot. Unknown fields,
capabilities, schemas or skill references fail before billing. A new definition
composing supported capabilities needs no worker rebuild. A new executable tool
requires reviewed implementation and capability registration. Dynamic definitions
cannot supply arbitrary host commands, SDK configuration, MCP endpoints, secrets,
export destinations, filesystem grants or approval bypasses.

Skills and repository AGENTS.md are task instructions, not an authorization layer.
The trusted context and host enforcement win over their contents. A prompt telling
the model not to use a forbidden tool is not capability enforcement.

## 4. Capabilities and provider adapters

Separate capabilities include repository read/write, branch push, change-request
create/update, review submit, merge, issue/story create, test execution, artifact
publication, agent delegation and gateway-mediated AWS role access. Effective
capabilities are the intersection of persona request, tenant/project policy,
root-principal authority, run grant, invocation surface and adapter support.
Missing required capabilities reject admission; missing optional capabilities are
reported before work. Credentials and network/tool access must make bypassing the
host broker impossible for enabled capabilities, not merely inconvenient.

GitHub and GitLab implement the same typed repository/change-request contract:
read source and discussions; prepare a workspace; compare commits; push a scoped
branch; create/update PR/MR; observe checks; submit review; and merge an exact head.
Use provider-native concurrency controls and durable gateway operation keys for
mutations. Check-then-merge without an atomic expected-head constraint is invalid.
Approvals, required checks and branch protections remain authoritative. Adapter
contract tests cover both providers, including unsupported operations and redelivery.

Reviewer owns review, in-scope repair, validation and merge of the assigned PR/MR.
After any repair, inspect and validate the new head, observe required checks, and
merge only that exact revision. Do not approve or merge an unvalidated replacement
head. Required independent approvals cannot be manufactured by self-review.

## 5. Task API integration

Reuse `docs/task-api/implementation-design.md`, its versioned contracts, the
service-principal admission path and host-mediated lifecycle. Do not route tasks
through GitHub webhooks or create placeholder issues/principals. Preserve the
investigator's no-SDK/no-repository pilot rather than changing its persona.

Add Codex personas through explicit task catalogue and command registration.
Public task submission remains a request, not authority. Resolve persona,
compatibility, model path, destination, price and grants before queue publication.
The task grant and generation fence remain the owner of execution. Duplicate
acceptance/queue delivery must not start two active SDK threads or duplicate
provider mutations. Cancellation and timeout revoke work and finalize through
the existing task report contract. A crashed exporter must not change task state.

Durable follow-up input uses existing logical-consumption and model-handoff
receipts. An SDK retry must not apply the same input twice. Retain replayable
progress and final artifacts; the model's final text alone is not a task terminal
receipt. Preserve clarification and cancellation semantics without requiring a
repository, issue, Check Run or GitHub token anywhere in the task-only path.

Task support for repository-writing personas is additive: admit an explicit
repository binding through existing gateway authorization or reject it before
billing. Never infer customer installation, AWS role or repository authority from
service-account task content. Test a tenant with no GitHub connection, including
Codex progress, follow-up input, reconnect, cancellation and result retrieval.

### Required remote controls for every Codex persona

Owner requirement (2026-09-26): pause, resume, abort and steer are shared-harness
capabilities required for persona qualification on both existing ADP remote-control
surfaces and the Task API. Invocation adapters translate into one durable control
contract; persona prompts cannot implement, disable or grant control authority.
The existing Task cancellation/input foundations do not establish pause/resume
support. Add the missing Task states, API/CLI contracts and worker transitions
before advertising parity.

| Control | Required behavior |
| --- | --- |
| Pause | Record the request durably and fence new model calls and tool actions. Confirm paused only after active work reaches a verified safe boundary or is interrupted and reconciled. Preserve workspace, thread and pending inputs. Report pause pending while an in-flight effect remains unresolved. |
| Resume | Resume only the same paused run with matching attempt/checkpoint, workspace/source and admitted persona/skill snapshot. Revalidate current authorization, revocation, deadline and remaining budget. Do not recreate or replay uncertain effects. |
| Abort | Stop current execution, prevent further effects and confirm child exit, terminal cancellation and queue acknowledgement. Abort also works while paused or pausing; acceptance alone is not terminal cancellation. |
| Steer | Persist an ordered instruction amendment and its command identity. Apply it to the active turn through a qualified runtime interface, or explicitly queue it for the next safe boundary. Distinguish accepted, queued, applied and rejected states; never claim mid-turn delivery from a queued message. Steering while paused must not resume execution. |

Use existing ADP authenticated owner/delegation checks, tenant/run/generation
fences and durable command receipts. Repeating an identical command ID returns
the existing receipt; changed payloads conflict. Stale controls cannot affect a
replacement attempt. Command ordering, pause/resume races, abort precedence and
terminal-run behavior must be explicit and consistent across adapters. No control
extends budget, deadline or authority implicitly, and idle pause must not cause
model polling or new billed inference. In-flight usage still requires settlement.

SDK thread restoration is not proof of live pause/resume or mid-turn steering.
Qualify the pinned SDK's interruption and continuation behavior. If a required
operation needs the supported app-server interface, keep it behind the shared
runtime adapter and include it in compatibility evidence. Do not substitute an OS
process freeze or silently degrade a control.

Expose requested and effective state, delivery mode, command identity and safe
refusal reason through CLI/API/UI progress and OTEL. Measure command acceptance,
pause/abort completion, resume and steering-application latency without recording
instruction text or secrets in telemetry. Confirmed paused state must prohibit
new commits, PR publication, merges, AWS mutations and child-agent dispatch.

Qualification must exercise real SDK/model runs through the actual control
surfaces: pause during inference and tool work, no new effects after confirmed
pause, continuation after resume, steering before/during a turn and while paused,
abort from every nonterminal state, repeated/conflicting commands, reconnect,
worker replacement and revocation. Verify actual application of steering in the
result, not merely a successful API response. Include developer publication,
reviewer repair/merge and operations/delegation boundaries. Fixture tests alone
cannot complete this requirement.

## 6. Delegation and AWS access

Retain `adp-trigger` semantics through the existing trusted delegation service:
explicit target persona/harness, authenticated root and tenant, immutable policy,
operation key, child budget reservation and linked trace. The child follows its
own registered model compatibility. No implicit Claude/GPT fallback and no local
forged lineage. Parent wait/retry must observe existing child state before dispatch.

Reuse `adp-cred assume` and the gateway's connected-credential contracts for AWS.
The role must be connected to the actual root principal and permitted to this run.
No arbitrary role ARN, host IRSA inheritance, reusable human token or raw credential
in prompt, log, SDK history or receipt. Handle expiry/refresh and revocation through
the broker; publish only redacted operation evidence. Operations mutations require
the existing action-specific authorization, verification and recovery policy.

## 7. Performance, cost and acceptance quality

Preserve canonical selected model and effort as auditable inputs. Begin with
persona defaults constrained by the compatibility registry; compare effort levels
on fixed tasks before changing defaults. No automatic expensive escalation or
model substitution. Reserve spend at the gateway before each billed operation;
record unknown usage after lost provider responses instead of treating it as zero.

Keep stable persona/skill instructions separate from bounded changing task context.
Select memory by task relevance, evidence and freshness; cap retrieved history and tool output,
retain full artifacts separately, and resume only a source/policy/workspace-bound
thread. Poll provider state outside model turns. Retry only classified interrupted
transport within the original deadline; do not retry policy denial, quota failure,
failed tests or unclear requirements as transport errors.

Use `adp-validate` from #6174 for isolated checks and final-commit receipts. Checks
with mutable external inputs opt out of cache. SDK completion cannot publish or
merge through the host without the configured completion policy. Requirements map
to changed locations and evidence; missing production wiring, error cases and
requested amendments are explicit gaps. Test passing is not semantic acceptance.

Define first-review acceptance as independent reviewer acceptance of the first
submitted candidate without repair or scope waiver. Measure it on a fixed,
versioned suite including missing wiring, failure paths, amendments, stale tests,
large context and ambiguous requirements. Report sample size, task complexity,
model/effort, tokens, cost, wall time and confidence; never promise 100% one-shot
acceptance or optimize speed by dropping checks. Live access and pricing evidence
are required before any cost/performance claim. Deterministic fixtures and live
results are reported separately.

### Swappable memory foundation

Owner clarification (2026-09-26): ADP does not yet have a supported platform memory
system. Existing logs/transcripts are historical evidence, not proof of a memory
capability. Existing experimental memory code does not dictate the backend for this
harness. Define a shared host-side memory port with configurable providers and an
explicit disabled provider; no persona or invocation adapter imports a backend SDK.

Logs record what happened. Memory selects useful evidence or lessons to supply to
a later run; it does not retrain model weights. A transcript-search adapter may
retrieve directly from the existing store. Another provider may maintain curated
records or a semantic index. Keep original transcripts/artifacts as evidence and
reference their stable IDs rather than copying entire histories into every prompt.

Separate retrieval, learning extraction, verification and persistence. At run end,
retain a compact candidate learning record covering the outcome, successful and
failed approaches, relevant conventions, tests and reviewer feedback, with source
run/evidence IDs. Cover failures and cancellations without delaying termination.
Use a durable bounded post-run job where extraction needs inference; include its
spend in total run cost. Replayed completion must not create duplicate lessons.
An agent's self-assessment cannot promote a proposed lesson to verified knowledge.
Use test/review evidence; conflicting or stale lessons can be corrected or retired.

The gateway selects provider, revision and authorized namespace from trusted
configuration and binds them to the run. Default to private tenant/user/repository
scope; repository/team sharing requires explicit authorization. Task-only runs use
an authorized namespace without inventing a repository. Backend selection and
sharing authority cannot come from prompts. Revalidate access on reads and writes;
repository-derived knowledge must not survive loss of repository access as a leak.
Memory is untrusted evidence and cannot override instructions or grant tool access.
Exclude credentials, raw reasoning and sensitive payloads from extracted lessons.

Provider contracts cover bounded retrieval, idempotent write receipts, stable IDs,
provenance, expiry, correction/deletion, capability discovery and export/import.
Version the common record format so changing providers need not change personas.
Provider changes affect new runs; active/resumed runs retain their binding unless
an explicit migration reconciles it. Interrupted writes stay unknown until receipt
reconciliation; do not replay blindly. Retention and deletion must cover indexes,
exports and derived records as well as the original store.

Budget retrieval by records, bytes, model tokens and deadline. Failures degrade
visibly to no memory when policy permits, without blocking the primary task or
switching to an unapproved provider. Record memory retrieval/use/write spans,
provider/revision, evidence IDs, latency and cost without content in default OTEL.
Measure p50/p95 execution time and cost per independently accepted change, including
retrieval, extraction, retries and review repairs. Compare disabled memory and each
provider on the same versioned tasks and model/effort settings; prevent evaluation
answer leakage. Do not equate a cache hit or retrieved record with improved quality.

Initial implementation: `src/memory.ts` defines the provider port, disabled backend
and a scoped boundary validating provenance fields, expiry, duplicate IDs, context
byte bounds and write receipts. Its tests use an injected provider. Gateway
authorization/configuration, live providers, lifecycle integration, learning jobs,
token budgets and telemetry still require implementation and qualification.

## 8. OpenTelemetry and operational evidence

Reuse ADP's OTLP collector/export path. Instrument admission, queue wait, execution,
SDK turns, tool operations, validation, delegated runs and provider finalization.
Propagate validated trace context across Task API, gateway, queue and delegation;
link retries/child runs without accepting external trace baggage as identity.

Spans carry bounded persona/model/harness/provider/surface/outcome fields and
correlate run/task/story/PR/commit IDs. IDs belong in traces/logs, not high-cardinality
metric labels. Metrics include queue/execution/tool/test duration, time to first
useful progress, token counts (including cached tokens), priced/unknown usage,
retries, repeated commands, cache reuse, stalls, requirement gaps and independent
first-review acceptance. Do not calculate dollar cost without the admitted price
revision. Histograms support p50/p95 by workload; no fabricated progress percentage.

Logs are structured and trace-correlated. Attribute allowlists exclude credentials,
prompts, source code, tool arguments/output and raw model reasoning by default.
Sensitive artifacts use the existing authorized evidence store, not span attributes.
Batch exports have bounded queues/timeouts; exporter loss is observable and never
blocks cancellation or changes task outcome. Test with in-memory exporters and an
OTLP collector fixture, including trace continuity, redaction, cardinality, exporter
outage and flush on shutdown. Deliver dashboard queries and actionable alerts for
queue delay, stalled execution, retry storms, unknown usage and delivery failure.

## 9. Persona inventory and sequencing

One shared harness story precedes independently testable persona stories. Each
persona inherits the same capabilities/adapters/telemetry and must qualify its own
workflow. First production wave: Developer and Architect; Reviewer, Operations
and AI-DLC follow their capability prerequisites. PM and Product are explicit
personas, not aliases. Specialist personas remain disabled until their tool and
security requirements are qualified.

| Existing key | New parallel definition | Qualification scope |
|---|---|---|
| developer | gpt-developer | requirements → code → isolated checks → PR/MR |
| architect | gpt-architect | design → native child stories; no implementation side effects |
| reviewer | gpt-reviewer | independent review → repair → final-head checks → merge |
| operations | gpt-operations | connected AWS role, authorized actions, verification/recovery |
| aidlc | gpt-aidlc | existing phase/artifact/approval contract and explicit handoffs |
| pm | gpt-pm | dependency-aware scheduling and evidence-based status |
| product | gpt-product | requirement refinement and testable acceptance |
| intent-refinement | gpt-intent-refinement | preserve clarification/intent contract |
| malware-analysis-agent | gpt-malware-analysis-agent | deferred until specialist isolation/tools qualify |
| pt-superpower | gpt-pt-superpower | deferred until existing sanctioned domain capabilities qualify |
| superplane-operator | gpt-superplane-operator | deferred until paid-compute controls qualify |
| superplane-researcher | gpt-superplane-researcher | deferred until domain tools/evidence qualify |
| codex | inapplicable | legacy Claude-supervised bridge; no native supervisor duplicate |
| agent-codex-reviewer | migration candidate | retain existing implementation; reuse evidence, no silent rebind |

## 10. Release boundary

Package alongside existing runtimes and keep new registrations disabled until
admission, tooling, identity, lifecycle, observability and provider/task contract
fixtures pass. Then qualify a read-only canary over the approved model path,
bounded sandbox-repository writing, review/repair/merge, and operations recovery.
Bind evidence to source/image, definition, SDK/runtime, model, environment and
policy. Disable new dispatch and drain/cancel affected runs on rollback; never
reroute to Claude. Implementation/PR creation is not production enablement.

## 11. Native child stories

- [HARNESS — #6195](https://github.com/aws-e/adp/issues/6195)
- [DEVELOPER — #6196](https://github.com/aws-e/adp/issues/6196)
- [ARCHITECT — #6197](https://github.com/aws-e/adp/issues/6197)
- [REVIEWER — #6198](https://github.com/aws-e/adp/issues/6198)
- [OPERATIONS — #6199](https://github.com/aws-e/adp/issues/6199)
- [AIDLC — #6200](https://github.com/aws-e/adp/issues/6200)
- [PM — #6201](https://github.com/aws-e/adp/issues/6201)
- [PRODUCT — #6202](https://github.com/aws-e/adp/issues/6202)
- [INTENT — #6203](https://github.com/aws-e/adp/issues/6203)
- [MALWARE — #6204](https://github.com/aws-e/adp/issues/6204)
- [SUPERPOWER — #6205](https://github.com/aws-e/adp/issues/6205)
- [SUPERPLANE-OPS — #6206](https://github.com/aws-e/adp/issues/6206)
- [SUPERPLANE-RESEARCH — #6207](https://github.com/aws-e/adp/issues/6207)

All persona stories have a native blocked-by relationship to #6195. No ADP agent
assignment or trigger was issued. Specialist qualification does not imply enablement.

### Task Responses integration milestone

The gateway now recognizes a separate, closed `responses_request` form on the
existing model operation endpoint. It uses `openai_responses` bindings, the
`task-codex-sdk-inline-responses-v2` contract and a distinct normalized probe digest;
Messages or existing reviewer evidence cannot certify it. Readiness resolves the
Task service principal through the authoritative persona compatibility registry.
No new persona is registered by this transport implementation.

The existing TaskModel journal owns claiming, generation fencing, reservation,
provider handoff, confirmed receipts, usage events and settlement. The provider
transport reuses the gateway's destination signer, inference-profile rewriting
and Responses pricing; it does not invoke the generic proxy settlement path a
second time. SDK metadata and message IDs are removed before handoff. There is
no remote history lookup, direct child credential, model fallback or retry after
an uncertain outcome. The worker IPC preserves request digests and committed
turn IDs; `HostBridge.responses` returns only confirmed model results.

This milestone supports messages and inline encrypted reasoning, preserving
assistant phase without server item references. Multi-step executable tool
history still requires implementation and qualification before general persona
readiness. The
current pilot task spend/turn limits must also be reconciled with the evidenced
Responses reservation ceiling through explicit policy and contract changes;
limits must not be silently bypassed to make a canary pass. Deployment, persona
registration, executable persona lifecycle, OTLP export and live story acceptance remain
open work under #6195 and the persona stories.

### Embedded Task lifecycle milestone

The packaged Codex Task process now runs through the real worker host, official
SDK and loopback bridge. Trusted bootstrap metadata binds the immutable persona
snapshot to model, Task alias, limits and deadline. Correlated control receipts
check current authority and deliver committed amendments before final output.
Reports reuse the Task schema and reject unsupported or undeclared citations;
a bounded correction turn can repair malformed output. The host owns artifact
publication, durable finalization and acknowledgement after process validation.

Relocated-package tests exercise seven fixture scenarios: success, repair,
repeated invalid output, cancellation during inference, an amendment requiring
a second canonical turn, unknown model outcome without replay, and tampered
instructions rejected before inference. These are runtime integration results,
not semantic quality or live provider qualification. The gateway now freezes/provides the harness snapshot as described below;
no new Task persona or command is enabled.
See [the lifecycle evidence](evidence/task-lifecycle-fixture.md).

### Protected gateway persona admission

Gateway admission loads reviewed SDK-generated snapshots from the server-owned
`ADP_CODEX_PERSONA_CATALOG_FILE`. It requires the existing authoritative persona
compatibility registry and standing principal policy, plus the resolved live model
binding. Catalogue content cannot grant executable permissions: the current Task
runtime supports only report publication. Missing, tampered, duplicate or
incompatible snapshots fail before budget reservation. Combined snapshot/input
size is checked against the worker's frame bound before acceptance.

The snapshot and intersected policy are stored within the existing protected run
grant, included in its digest and conditional operation checks, and returned by
bootstrap. A catalogue update affects future admissions; existing tasks and
idempotent retries retain their exact admitted snapshot. No second persona-class
registry is introduced. `scripts/catalogue.mjs` compiles reviewed persona JSON and
pinned skill content using the shared TypeScript validator. Changing configuration
does not register, enable or grant authority to a persona.

The Task schemas now describe Codex bootstrap metadata, current-authority IPC,
and closed Responses request/result frames. Pydantic-derived schema exports have
a drift check, and a shared bootstrap fixture validates in both the gateway and
TypeScript runtime. The evaluation manifest's lifetime threshold now matches the
already-existing 360-minute contract maximum; runtime/per-principal limits are
unchanged. See [gateway admission evidence](evidence/task-harness-admission.md).

The real turn API now admits autonomous continuations for protected Codex Task
runs; its previous cyber-only gate would have rejected Codex requests despite
the local worker fixture passing. Turn creation and model handoff enforce the
frozen persona budget/deadline, and model execution rejects non-admitted effort
before reservation or provider effects. The child also applies the smallest
persona, policy and Task operation limit across report repairs and amendments.
