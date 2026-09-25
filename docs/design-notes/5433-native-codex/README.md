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
Select relevant memory chronologically; cap retrieved history and tool output,
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
