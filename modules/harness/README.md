# Harness

The outbound surface that agents use while running. Tools, jobs, events, artifacts, context, and human-in-the-loop approvals — all mediated through a shared set of contracts and services.

## What It Does

When an agent is executing (reasoning, calling tools, producing artifacts), it interacts with the world through the harness. The harness provides six surfaces that handle the plumbing common to all agents: routing tool calls, managing long-running jobs, publishing events, storing artifacts, querying context, and requesting human approvals.

Think of it as the runtime API layer between agents and everything else. Agents don't import each other's code or call external services directly — they go through the harness, which enforces policy, propagates identity, tracks provenance, and provides observability.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Agent Runtime                            │
│                    (reasoning loop, any persona)                 │
└──────────────────────────────┬──────────────────────────────────┘
                               │ uses
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                          HARNESS                                 │
│                                                                 │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐             │
│  │  Tools  │ │  Jobs   │ │ Events  │ │Artifacts │             │
│  │  (MCP)  │ │         │ │         │ │          │             │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬─────┘             │
│       │            │           │            │                   │
│  ┌────┴────┐ ┌────┴────┐                                       │
│  │ Context │ │  HITL   │                                       │
│  │         │ │         │                                       │
│  └─────────┘ └─────────┘                                       │
│                                                                 │
│  Cross-cutting substrate:                                       │
│  ├── Policy (who may do what, when)                             │
│  ├── Identity propagation (user perms flow through every call)  │
│  ├── Provenance (DAG of derivations)                            │
│  └── Observability (traces, metrics, costs)                     │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Apps (Domain Packs)                           │
│                                                                 │
│  apps/cyber/tools/    apps/ml/tools/    apps/data/tools/        │
│  apps/cyber/jobs/     apps/ml/jobs/     apps/data/jobs/         │
│  apps/cyber/skills/   apps/ml/skills/   apps/data/skills/       │
└─────────────────────────────────────────────────────────────────┘
```

### The Six Surfaces

| Surface | Shape | Example |
|---------|-------|---------|
| Tools (MCP) | Synchronous, stateless, request/response | `yara_scan(sample)`, `query_database(sql)` |
| Jobs | Long-running, durable state, lifecycle events | CAPE detonation, model training |
| Events | Push-based pub/sub, subscribable | `rule.matched`, `deployment.completed` |
| Artifacts | Content-addressed blob storage | PCAPs, model files, reports |
| Context | Domain-shaped queries over Agent Context | "similar samples", "related PRs" |
| HITL | Durable approval/clarification tickets | "approve production deploy" |

### Decision Tree: Which Surface?

- Lifecycle outlives a single call? → **Job**, not tool
- Naturally push, not pull? → **Event**, not tool
- Large binary blob? → **Artifact**
- Need human sign-off? → **HITL**
- Query over prior knowledge? → **Context**
- Everything else → **Tool**

## Current Contents

### `contracts/`

The public API between the harness and apps. JSON Schema definitions for every registration type:

| Contract | Purpose |
|----------|---------|
| `tool.schema.json` | Tool registration (name, version, input/output schema, permissions, rate limits) |
| `job.schema.json` | Job kind registration (retry policy, worker pool) |
| `event.schema.json` | Event type (payload schema, ordering guarantees) |
| `artifact.schema.json` | Artifact kind (MIME type, retention, access policy) |
| `hitl-ticket.schema.json` | HITL ticket shape (scope, prompt, response schema, approvers) |
| `skill.manifest.json` | Skill declaration (required tools, scopes, inputs/outputs) |
| `agent.manifest.json` | Agent declaration (runtime, model, permissions, persona) |
| `inbox-item.schema.json` | Work item envelope (caller identity, priority, artifact refs) |
| `outbox-item.schema.json` | Result envelope (provenance link, artifact refs) |
| `policy.schema.json` | Policy rule (condition, effect, scope, precedence) |
| `provenance-record.schema.json` | Derivation DAG node |

Contracts are versioned. Breaking changes require a new version number. See [contracts/README.md](contracts/README.md) for the full versioning rules.

### `mcp-hub/`

The MCP Gateway — a reverse proxy that sits between agents and MCP servers, providing a single endpoint with routing, auth, rate limiting, and observability.

**Current state:** Design and requirements phase. Key documents:
- [mcp_gateway_requirements.md](mcp-hub/mcp_gateway_requirements.md) — Full functional/non-functional requirements
- [proposed_mcp_servers.md](mcp-hub/proposed_mcp_servers.md) — ~40 MCP servers planned for integration
- [agentcore_gateway.md](mcp-hub/agentcore_gateway.md) — AgentCore Gateway integration design

**Target architecture:**

```
Agent ──► MCP Gateway (single endpoint)
              │
              ├── github-mcp (GitHub operations)
              ├── terraform-mcp (IaC)
              ├── aws-mcp (45+ AWS services)
              ├── postgres-mcp (database)
              ├── playwright-mcp (browser automation)
              ├── dbt-mcp (data transformation)
              └── ... (~40 servers total)
```

The gateway handles: tool catalog aggregation, namespace collision prevention, per-tool auth/rate-limiting, health checks, circuit breakers, and audit logging.

Also contains `docker/agent-mail/` — an email gateway that forwards messages to agents (used by the cyber domain for SIEM alert ingestion).

## Current State

The harness is in early development:

- **Contracts:** README with 11 planned schemas documented. Schemas not yet written — they land incrementally as each surface is built.
- **MCP Hub:** Requirements and design complete. No running service yet.
- **Other surfaces (Jobs, Events, Artifacts, HITL):** Not yet started.

The ordering most likely to happen:
1. `tool.schema.json` — MCP Hub is the most-built surface
2. `agent.manifest.json` + `skill.manifest.json` — needed to formalize existing agents
3. `inbox-item.schema.json` / `outbox-item.schema.json` — invocation surface typing
4. The rest — as surfaces are implemented

## Relationship to Other Modules

| Module | Relationship |
|--------|-------------|
| `modules/gateway/` | Platform core — LLM egress. Harness tools may call Bedrock through it. |
| `modules/agent-factory/` | Platform core — agent runtime. Agents use the harness while running. |
| `modules/agent-context/` | Platform core — memory/search. The Context surface wraps it. |
| `modules/user-services/` | User-owned products. Agents access user credentials/knowledge via harness. |
| `modules/domain-apps/` | Domain packs. Apps register tools/jobs/events/skills with the harness. |

## Key Principle

**Apps declare. Harness operates.**

If an app starts implementing a queue, retry policy, or permission check — that's a harness concern that leaked. Apps register capabilities via contracts; the harness handles the operational plumbing.
