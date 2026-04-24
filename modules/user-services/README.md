# modules/user-services/

Per-user products the user owns. Each subdirectory is a **self-contained service** scoped to a single human: their credentials, their personal knowledge, their bespoke agents, their chief-of-staff.

If `ARCHITECTURE.md` names the three layers (platform core / harness / apps), this cluster is a fourth category: **the personal layer**. Features here are not platform substrate (every agent uses them), not harness (what agents use while running), not domain apps (team / org / tenant scope). They are *user-owned*.

For the full cluster-wide framing and invariants, see [`docs/user-services-overview.md`](../../docs/user-services-overview.md).

## The cluster

| Order | Service | Purpose | Design doc |
|---|---|---|---|
| v1 | `vault/` | Per-user credentials (API tokens, SSH keys, certs, config files) agents use on the user's behalf | [`docs/user-identity-and-credentials-design.md`](../../docs/user-identity-and-credentials-design.md) |
| v2 | `knowledge/` | Personal knowledge repo — notes, meeting history, preferences, docs the user owns; agents reason over it | (to be written) |
| v3 | `agents/` | User-scoped bespoke agents — per-user instances of platform-provided agent templates | [`docs/user-scoped-agents-design.md`](../../docs/user-scoped-agents-design.md) |
| v3+ | `chief-of-staff/` | Opinionated preconfigured agent built on the three above | (to be written) |

The order reflects real dependency: agents need credentials (vault) before they can act; bespoke user agents need a knowledge source (knowledge repo) to reason over; chief-of-staff is a preconfigured composition of all three.

## Current state

No services built yet. Only this README and the design docs linked above exist. The vault is the first service planned for implementation.

## Folder shape (target)

Each service is self-contained. Not all are built at once; each lands on its own schedule.

```
modules/user-services/
├── README.md                    # this file
├── shared/                      # extracted as duplication forces it; starts thin
│   ├── auth/                    # middleware: "caller must own this resource"
│   ├── storage/                 # adp/users/<sub>/<service>/* naming across stores
│   ├── db/                      # user-scoped Postgres table conventions
│   ├── provenance/              # write-side hooks for who/what/when
│   ├── frontend/                # React shell for /settings/* pages
│   └── metering/                # per-user cost attribution
├── vault/                       # v1
│   ├── src/
│   ├── alembic/
│   ├── frontend/
│   ├── infra/
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── tests/
├── knowledge/                   # v2 — same shape
├── agents/                      # v3 — same shape
└── chief-of-staff/              # v3+ — same shape
```

## Invariants every service here must satisfy

(Summary — see `docs/user-services-overview.md` for the full version with rationale.)

1. Scope is one human. Every record FK'd to `users.id`.
2. Tenant-isolated. `org_id NOT NULL` on every row.
3. Owner-only by default. Admins have break-glass delete only, audited.
4. Namespace user content under `adp/users/<cognito_sub>/<service>/*` across all backing stores.
5. Provenance-tagged. Every write records who/what produced it.
6. Exportable. Every service exposes a portable export endpoint.
7. Deletable. Synchronous delete of row + underlying storage + nightly sweeper.
8. Per-user metered. Costs attributable to the user, not just the tenant. Budget caps per user.
9. Capability-bounded. No service grants capability beyond what the user's role allows.
10. No arbitrary user code. Users configure, customize, compose — they do not submit runnable code.

If a new service does not fit all ten, it is not a user service. It belongs elsewhere.

## What does not belong here

| Kind | Example | Where it belongs instead |
|---|---|---|
| Team-shared state | Shared team notes, team deploy bot PAT | A future `modules/team-services/` or `modules/org-services/` cluster |
| Tenant-wide state | Org-wide Bedrock quota, shared tenant policies | `modules/gateway/` (where org state lives) |
| Platform substrate | Tool routing, job scheduling, event bus | `modules/harness/` |
| Domain capabilities | Threat-research tools, ML platform adapters | `apps/<domain>/` |
| Agent runtime | LLM calls, worker scaling, invocation | Platform core (`gateway`, `agent-factory`) |

**Rule of thumb:** one human, or not? If not, not a user service.

## Relationship to the rest of ADP

```
                ┌──────────────────┐
                │     User UI      │
                └────────┬─────────┘
                         │
                         ▼
    ┌────────────────────────────────────────┐
    │   modules/user-services/               │  ← this cluster
    │   ├─ vault/                            │
    │   ├─ knowledge/                        │
    │   ├─ agents/                           │
    │   └─ chief-of-staff/                   │
    └────────┬───────────────────────────────┘
             │  consumed by
             ▼
  ┌────────────────────────────────────┐
  │  Platform + domain agents          │
  │  (apps/threat-research, apps/ml,   │
  │   apps/data, …) acting on the      │
  │   user's behalf                    │
  └────────────────────────────────────┘
```

User services sit between the user and the agents that act on their behalf. They are **consumed by** platform and domain agents; they are **configured by** the user directly.

## Adding a new service

Not a self-service process — a new service in this cluster is a platform decision because it inherits the ten invariants and can reach every ADP user. Steps:

1. Open an ADR under `docs/decisions/` proposing the service and how it satisfies all ten invariants.
2. Write a design doc under `docs/` (same pattern as the existing vault and agents docs).
3. Create `modules/user-services/<service>/` with a README and the standard folder shape.
4. Build incrementally — schema first, then CRUD, then UI, then agent integration.

If the proposal cannot cleanly satisfy all ten invariants, the feature belongs elsewhere, not here.

## Shared module

`shared/` does not yet exist. It is extracted from the first service (vault) when a second service (knowledge) needs the same primitive — driven by observed duplication, not speculation. Likely first extractions:

- `shared/auth/` — owner-only middleware
- `shared/storage/` — the `adp/users/<sub>/<service>/*` naming wrapper
- `shared/frontend/` — the `/settings/*` React shell

Nothing is extracted today. This is a placeholder to name the eventual home so the first extraction is a scoped refactor, not a cross-module redesign.
