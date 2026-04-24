# ADP Architecture

One page. The mental model, how the pieces fit, and where new work goes.

If you only have time for a paragraph: **ADP is a substrate for running agents.** The substrate is split into three layers — platform core (compute, egress, memory), the harness (what agents use while running), and apps (domain packs that plug into the harness). Agents are the consumers; humans and services invoke them through a separate inbound surface. Domain authors add new capabilities by writing declarations — tools, jobs, events, skills, agents — that register with the harness. The harness handles the plumbing common to all of them.

## The three layers

```
┌─────────────────────────────────────────────────────────────┐
│  apps/                                                      │
│  Domain packs. Threat research, ML platform, data platform. │
│  Each pack brings: agents, tools, jobs, events, skills,     │
│  schemas, domain UX, domain-specific infra.                 │
├─────────────────────────────────────────────────────────────┤
│  modules/harness/                                           │
│  What agents use while running. Six surfaces + substrate.   │
│  One implementation, shared by every app.                   │
├─────────────────────────────────────────────────────────────┤
│  platform core (modules/gateway, agent-factory,             │
│                 agent-gateway, agent-context, platform/)    │
│  LLM egress, agent runtime, invocation surface, memory,     │
│  VPC / EKS / ECR / IAM.                                     │
└─────────────────────────────────────────────────────────────┘
```

**Apps declare. Harness operates. Platform runs.**

## The two directions of traffic

An agent has two skins. Don't conflate them.

```
                          ┌─────────────┐
   "How do I summon       │             │    "How do I reach
    the agent?"           │    AGENT    │     the world?"
                          │             │
   ─── INVOCATION ───────►│             │─────── HARNESS ────►
   (inbound, from humans  │             │       (outbound, to
    and services)         │             │        tools, jobs,
                          │             │        events, etc.)
                          └─────────────┘
```

- **Invocation surface** — how the outside world summons the agent and receives results. Today this lives in `modules/agent-factory/gateway/` (WebSocket API, SQS tasks, response Lambdas). Concerns: caller auth, triggers, streaming responses, session resumption.
- **Harness** — what the agent uses while it's running. Concerns: tool routing, job submission, event pub/sub, artifact storage, context queries, HITL approvals. Plus the cross-cutting substrate (policy, identity propagation, provenance, audit, observability).

Different audiences, different protocols, different contracts. Built as separate things.

## The agent as inbox → loop → outbox

Every agent has the same shape:

```
         ┌─────────────────┐
INBOX → │                 │ → OUTBOX
 (typed  │     AGENT       │   (typed
  work    │     (loop)      │   result
  items) │                 │   items)
         └─────────────────┘
                │
                │ uses
                ▼
         harness surfaces
         (tools, jobs, events,
          artifacts, context, HITL)
```

- **Inbox** — typed queue of work items that arrive for this agent kind. Durable, observable, policy-gated.
- **Loop** — the agent's reasoning runtime (Bedrock Gateway handles LLM calls). Thinks, calls harness surfaces, updates plan, decides when done.
- **Outbox** — typed queue of results the agent publishes. Subscribable, immutable once published, provenance-linked to the inbox item.

One agent's outbox can be another agent's inbox. That's how agents compose.

## The harness: six surfaces + substrate

Six surfaces agents call into. Four cross-cutting concerns that apply to all of them.

### The surfaces

| Surface | Shape | Example |
|---|---|---|
| **Tools** (MCP) | Synchronous, stateless, small-payload verbs | `yara_scan(sample)` |
| **Jobs** | Long-running, durable-state, lifecycle-bearing work | CAPE detonation |
| **Events** | Push-based pub/sub, subscribable by agents and services | `rule.matched` |
| **Artifacts** | Content-addressed storage for large blobs | PCAP, model file |
| **Context** | Domain-shaped queries over Agent Context | "similar samples" |
| **HITL** | Durable tickets, approvals, clarification requests | "approve detonation" |

Rule of thumb for where a new capability goes:

1. Lifecycle outlives a single call? → job, not tool.
2. Naturally push, not pull? → event, not tool.
3. Non-agent consumers will need it too? → its own surface with its own API; MCP is one projection among several.

If none are true, it's a tool.

### The substrate (applies across all surfaces)

- **Policy** — who may do what, when, on what. TLP, approvals, budgets, rate limits, egress rules. Declared centrally, enforced at every surface.
- **Identity propagation** — end-user identity carried through invocation → agent → every tool/job/event call. Writes use user permissions, not a shared service account.
- **Provenance** — DAG of derivations. Every artifact, every verdict, every decision traces back to the tool calls and inputs that produced it.
- **Observability** — traces, metrics, costs, SLOs, one pane across all surfaces.

## What an agent is

Four things:

1. **Contract** — inbox type and outbox type (JSON schemas).
2. **Loop** — runtime (platform provides; author configures).
3. **Permission set** — which tools, jobs, context views, HITL scopes it may touch. Enforced by the harness, not by the prompt.
4. **Persona** — markdown instructions: role, style, escalation rules, output format.

Building a new agent is a five-file task: `agent.yaml`, `persona.md`, `inbox.schema.json`, `outbox.schema.json`, `tests/`. Everything else — LLM calls, tool dispatch, retries, memory, scaling, audit — the platform provides.

### Autonomy, bounded

- **Reasoning** — what to do next. Full autonomy; that's why the LLM exists.
- **Action** — whether to act. Bounded by the declared permission set.
- **Scope** — when done. Bounded by max turns, cost caps, timeouts, and the inbox item's success criteria.

Autonomy inside a fenced yard. The LLM decides what happens inside. The harness decides where the fences are.

## Skills

Skills are playbooks the agent follows. Tools are capabilities the agent invokes. Rule: if removing the LLM from the middle makes the thing pointless, it's a skill; if removing the LLM still leaves something useful, it's a tool.

Three tiers, three homes:

- **Platform skills** (`modules/harness/skills/`) — cross-domain, platform-owned, high bar. `summarize-artifact`, `escalate-with-hitl`.
- **Domain skills** (`apps/<domain>/skills/`) — domain-specific, domain-owned. `triage-unknown-pe`, `promote-model-to-prod`.
- **Personal/team skills** — out-of-repo, self-service, graduated trust.

All three share the same skill contract (SKILL.md + manifest.yaml + tests). The skill registry loads, validates, permission-checks, and exposes them uniformly.

## Apps: the domain pack shape

A domain on ADP is an `apps/<domain>/` folder containing:

```
apps/<domain>/
├── agents/        # agent declarations (contract + persona + permissions + tests)
├── tools/         # adapters — register with harness/mcp-hub
├── jobs/          # handlers — register with harness/jobs
├── events/        # event type schemas + producers/consumers
├── skills/        # domain skills (playbooks)
├── schemas/       # domain types (Sample, Model, Dataset, …)
├── policy/        # domain-informed policy rules
├── frontend/      # optional domain UX
└── infra/         # domain-specific infra (CAPE cluster, GPU pool, etc.)
```

Apps are peers. They talk to each other only through the harness — events, tools, context. No cross-app imports.

## The invariants

Four rules the structure depends on:

1. **Apps declare; harness operates.** If an app starts implementing a queue, retry policy, or permission check, that's a harness concern that leaked.
2. **Apps talk via the harness, never directly.** Tools, events, context. No import between `apps/a/` and `apps/b/`.
3. **Contracts are versioned; implementations are not.** `modules/harness/contracts/` is a public API.
4. **Identity flows through every layer.** The user who invoked the agent is the user whose permissions get checked on every tool, job, event, artifact, and HITL call.

## Today vs. target

This document describes the target shape. Today, the repo has:

- Platform core (gateway, agent-factory, agent-context) deployed and working.
- Invocation surface working inside `modules/agent-factory/gateway/` (not yet promoted to a standalone module).
- `modules/mcp-gateway/` exists as design docs — the seed of `modules/harness/mcp-hub/`.
- Agents exist as GitHub Actions workflows (architect, developer, pm, ops, product, reviewer), not as uniform declarations.
- No `modules/harness/contracts/` yet — contracts are not formalized.
- No `apps/` folder yet — domain packs don't have a home.

The gap is bridged incrementally: formalize contracts first, then grow surfaces and apps around them as real use cases arrive. Most of the scaffolding should not be stubbed out preemptively — add folders when the first real consumer exists.

## Glossary

- **Platform core** — the always-on substrate: EKS, VPC, IAM, Bedrock Gateway, Agent Factory, Agent Context, invocation surface.
- **Harness** — the outbound surface an agent uses while running. Six surfaces + substrate.
- **App** — a domain pack (threat-research, ml-platform, …). Peer to other apps.
- **Tool** — synchronous verb, request/response, MCP-typed.
- **Job** — long-running work with durable state, submitted and tracked.
- **Event** — push-based notification on a bus, subscribable.
- **Artifact** — large immutable blob referenced by hash, with metadata.
- **Context** — queryable knowledge store over prior analyses, decisions, runbooks.
- **HITL** — human-in-the-loop approval/clarification ticket.
- **Skill** — a playbook composing tools/jobs/events for a specific outcome.
- **Agent** — a declared unit: contract + loop + permissions + persona.
- **Inbox / outbox** — typed queues of work-in and results-out for a given agent kind.
- **Invocation surface** — how agents are summoned (inbound).
- **Provenance** — DAG tying every output to the inputs and tool versions that produced it.

## Further reading (as docs land)

- `docs/architecture/layers.md` — deeper on platform/harness/apps
- `docs/architecture/invocation-vs-harness.md` — the two-direction distinction
- `docs/architecture/six-surfaces.md` — each surface in detail
- `docs/architecture/inbox-outbox.md` — the agent contract shape
- `docs/architecture/policy-and-provenance.md` — cross-cutting concerns
- `docs/guides/add-a-tool.md`, `add-a-job.md`, `add-an-event.md`, `add-a-skill.md`, `add-an-agent.md`, `add-a-new-app.md`
- `modules/harness/contracts/README.md` — the public API surface between harness and apps
