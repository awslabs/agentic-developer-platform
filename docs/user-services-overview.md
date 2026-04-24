# User Services — Overview

**Status:** Design sketch (invariants approved; per-service designs vary — see individual docs)
**Last updated:** 2026-04-24

Per-user products the user owns. Everything in `modules/user-services/` is scoped to a single human: their credentials, their personal knowledge, their personal workflows, their bespoke agents.

## Why this cluster exists

Several features share the same shape — user-scoped, tenant-isolated, self-service, agent-consumed, durable, privacy-sensitive. Filing each as a peer top-level module would scatter them and force every new service to reinvent the same conventions. Grouping them under `modules/user-services/` keeps them findable, and lets shared conventions (auth middleware, per-user storage patterns, provenance hooks, frontend shell) live next to their consumers.

## The cluster today

| Order | Service | Purpose | Design doc |
|---|---|---|---|
| v1 | `vault/` | Per-user credentials (API tokens, SSH keys, certs, config files) agents use on the user's behalf | `user-identity-and-credentials-design.md` |
| v2 | `knowledge/` | Personal knowledge repo — notes, meeting history, preferences, docs the user owns; agents reason over it | (to be written) |
| v3 | `agents/` | User-scoped bespoke agents — per-user instances of platform-provided agent templates | `user-scoped-agents-design.md` |
| v3+ | `chief-of-staff/` | Opinionated preconfigured agent built on the three above; goals, commitments, routines | (to be written) |

The order reflects real dependency: agents need credentials (vault) before they can act on the user's behalf; bespoke user agents need a knowledge source (knowledge repo) to reason over; chief-of-staff is a preconfigured composition of all three.

## Folder shape

```
modules/user-services/
├── README.md
├── shared/                      # cross-cutting: auth middleware, storage conventions,
│                                # provenance hooks, frontend shell, common SDK
├── vault/
│   ├── src/
│   ├── alembic/
│   ├── frontend/
│   ├── infra/
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── tests/
├── knowledge/
│   └── (same shape)
├── agents/
│   └── (same shape)
└── chief-of-staff/
    └── (same shape)
```

Each service is self-contained — its own Terraform, its own Docker image, its own deploy. Shared concerns are pulled into `shared/` only as real duplication forces them out, never speculatively.

## Invariants for every user service

These apply to every service in `modules/user-services/`. They are what make this a category, not just a folder.

1. **Scope is one human.** Every record is FK'd to `users.id`. No service here holds data shared between users.
2. **Tenant-isolated.** `org_id NOT NULL` on every row, enforced via `TenantMixin`. A user's data cannot cross org boundaries even by accident.
3. **Owner-only by default.** Only the user themselves can read or mutate their data. Admins have break-glass **delete** only — never read, never create-on-behalf-of. Every admin action is audited.
4. **Namespace user content consistently.** Across Secrets Manager, S3, DynamoDB, and Postgres, user content is under `adp/users/<cognito_sub>/<service>/*`. This makes audit, export, and deletion uniform.
5. **Provenance-tagged.** Every write records who/what produced it — the user directly, or an agent (which agent, which task, at whose request).
6. **Exportable.** Every service exposes `GET /user-services/<svc>/export` that returns the user's content in a portable, self-describing format. Right-to-be-forgotten requires this.
7. **Deletable.** Every service exposes a delete endpoint that removes the row AND the underlying storage (secret, blob, vector). Synchronous delete + nightly sweeper, following the vault's pattern.
8. **Per-user metered.** Costs (LLM tokens, storage, API calls) are attributable to the user, not just the tenant. Budget caps are per-user, not per-tenant.
9. **Capability-bounded.** No user service grants a user capability beyond what their role already allows. The fence is their own scope, not the service's scope.
10. **No arbitrary code execution from users.** Users can configure, customize, and compose — they cannot submit executable code that runs on platform infra. (See `user-scoped-agents-design.md` for how this applies to user-scoped agents.)

## What goes here vs. not here

Rule of thumb — one human, or not?

| Scope | Example | Home |
|---|---|---|
| **Per-user** | My vault, my notes, my bespoke agents | `modules/user-services/` |
| **Per-tenant** | Org-wide API key, shared team notes, tenant-wide policies | Not here. Gateway's org tables or (later) an `org-services/` cluster. |
| **Platform-wide** | Bedrock egress, agent runtime, invocation surface | Platform core modules (`gateway/`, `agent-factory/`, etc.) |
| **Per-domain** | Threat-research tool adapters, ML registry shims | `apps/<domain>/` |
| **Per-agent** | A specific platform agent's persona and permissions | `apps/<domain>/agents/` (for domain agents) or `modules/user-services/agents/` (for user agents) |

If a feature touches more than one human's data, it is not a user service.

## Shared module contents (as they emerge)

Not pre-built; extracted when real duplication forces it. Likely candidates:

- **`shared/auth/`** — middleware that enforces "caller must own this resource" + admin break-glass audit. First written for vault; extended for knowledge.
- **`shared/storage/`** — wrappers for the `adp/users/<sub>/<service>/*` naming across Secrets Manager, S3, DynamoDB.
- **`shared/db/`** — conventions for user-scoped Postgres tables (TenantMixin usage, cascade rules, export/delete patterns).
- **`shared/provenance/`** — "this record was written by agent X on user Y's behalf at time T" recording hooks.
- **`shared/frontend/`** — React shell for `/settings/*` pages; side navigation, consistent auth UX, export/delete buttons.
- **`shared/metering/`** — per-user cost attribution hooks.

Each of these starts life inside `vault/`. When the second service in the cluster needs the same thing, extraction into `shared/` becomes a scoped refactor driven by observed duplication.

## Relationship to the rest of ADP

User services are **consumers** of platform substrate (Bedrock Gateway for LLM calls, Agent Factory for runtime, Agent Context for shared memory). They are **producers** of the user-scoped data that platform and domain agents reason over. They are **invoked** through the same invocation surface agents use.

```
                ┌──────────────┐
                │    User UI   │
                └──────┬───────┘
                       │
                       ▼
       ┌──────────────────────────────────┐
       │   modules/user-services/         │
       │   ├─ vault/                      │
       │   ├─ knowledge/                  │
       │   ├─ agents/                     │
       │   └─ chief-of-staff/             │
       └──────┬───────────────────────────┘
              │  consumed by
              ▼
     ┌─────────────────────────────┐
     │  Platform agents & apps     │
     │  (threat-research, ML, …)   │
     │  acting on user's behalf    │
     └─────────────────────────────┘
```

The cluster is neither platform core nor app code — it is the **personal layer** between the two.

## Risks this grouping is meant to avoid

- **Scatter.** Without the cluster, you end up with `modules/vault/`, `modules/user-knowledge/`, `modules/user-agents/`, `modules/chief-of-staff/` — four top-level peers that share substantial code and invariants but are not filed together.
- **Scope creep into per-tenant territory.** Without the invariant list, it is easy to let a "user service" accrete tenant-shared state. The 10 invariants are what keep the category honest.
- **Inconsistent UX.** Four services with four slightly different auth UIs, four export formats, four deletion semantics. The shared frontend and the invariants prevent that.
- **Unbounded cost.** User-scoped agents especially can spend real money; the per-user metering invariant is declared for the cluster, not rediscovered per service.

## Out of scope

- **Team-shared user-service content.** A team having a shared "team notes" doc, or a shared team vault, is *not* a user service — it is tenant-scoped. Belongs elsewhere (org-services cluster, or per-domain).
- **Cross-tenant user content.** Explicitly rejected. A user belongs to one org per user-service entry; the contractor-across-two-orgs case is an identity problem, solved separately.
- **User-authored code.** Users can configure and compose; they cannot upload executable code. This is a hard line for the cluster.
