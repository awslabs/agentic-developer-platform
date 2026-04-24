# User-Scoped Agents — Design Sketch

**Status:** Design sketch, not yet approved for implementation
**Author:** Pranav + Claude
**Last updated:** 2026-04-24
**Position:** Third service in `modules/user-services/` (after vault, knowledge). See `user-services-overview.md` for cluster invariants.

## What this is

A user's **personal set of agents** — named, customized, owned by them. Examples: `my-email-triager`, `my-code-reviewer`, `my-research-assistant`. They run on the user's vault and knowledge repo, fire on the user's triggers, and publish to the user's inboxes.

This is **not** arbitrary user code execution. Users customize platform-provided agent templates; they do not submit runnable agent code. The boundary between "configure" and "write code" is the load-bearing safety decision in this design.

## Why it fits `modules/user-services/`

User-scoped agents satisfy all 10 cluster invariants from `user-services-overview.md`:

- Scope is one human. ✅ Each agent instance FK'd to one `users.id`.
- Tenant-isolated. ✅ `org_id NOT NULL`.
- Owner-only by default. ✅ Only the user can create/modify/delete their agents.
- Namespaced. ✅ State under `adp/users/<cognito_sub>/agents/*`.
- Provenance-tagged. ✅ Every run and every edit recorded.
- Exportable / deletable. ✅ Standard patterns.
- Per-user metered. ✅ Budget caps per user, not per tenant.
- Capability-bounded. ✅ An agent never has more capability than its owner.
- No arbitrary code. ✅ See "customization model" below.

## The customization model

A user-scoped agent is **an instance of a platform-provided template**, with the user's personalization applied.

### What the user provides

- **Name & description** — "My email triager", "Summarizes unread Slack DMs daily at 8am"
- **Persona override** — markdown fragment merged into the template's persona. Style, tone, escalation preferences, output format.
- **Permission subset** — the user may *narrow* the template's declared permissions, never extend them. If the template allows `vault.read(github)` + `vault.read(gmail)`, the user may disable `github` but may not add `slack`.
- **Bound data sources** — which vault entries this agent may access, which knowledge-repo scopes it may read, which calendars / inboxes are in play.
- **Trigger configuration** — manual invocation, cron schedule, event subscription (e.g. "on new GitHub PR on my repos").
- **Outbox routing** — where results go: personal Slack DM, email, the user's UI inbox, or a custom webhook they own.

### What the template provides

- **The loop** — the actual agent code (reasoning logic, tool choreography, skill composition).
- **Declared permissions** — the *maximum* permission envelope the template can use. User personalizations narrow from this, never beyond.
- **Input/output contract** — inbox item shape, outbox item shape. Same versioned schemas as any platform agent.
- **Tests & evals** — template ships with regression tests.

### What is explicitly forbidden

Users **cannot**:

- Upload Python, TypeScript, or other runnable code.
- Add tools not declared by the template.
- Grant permissions exceeding their own role in the org.
- Bypass policy gates (TLP, HITL requirements, egress rules).
- Exceed tenant budget caps or their own per-user budget cap.

This is the bounded-autonomy principle from `ARCHITECTURE.md` applied to user agents: **the user's agent acts inside the user's own capability fence, not outside it.**

## Data model

Three tables, all in `modules/user-services/agents/` under Postgres:

### `user_agent_templates` (platform-managed)

The catalog of templates users can instantiate. Not user-writable.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `name` | string | e.g. `email-triager` |
| `version` | integer | templates are versioned |
| `owner_team` | string | who maintains this template |
| `persona_template` | text | markdown with `{{user.persona_override}}` merge points |
| `max_permissions` | JSONB | declared permission envelope (tools, skills, context scopes) |
| `inbox_schema` | JSONB | versioned contract |
| `outbox_schema` | JSONB | versioned contract |
| `default_triggers` | JSONB | what triggers users can configure |
| `description` | text | |
| `created_at`, `updated_at` | timestamp | |

### `user_agents` (user-managed)

Per-user instances of templates.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `org_id` | UUID FK → organizations | TenantMixin |
| `user_id` | UUID FK → users.id | ON DELETE CASCADE |
| `template_id` | UUID FK → user_agent_templates | |
| `template_version` | integer | pinned at creation; user opts in to upgrade |
| `name` | string | user-chosen name (unique per user) |
| `persona_override` | text | user's markdown fragment |
| `permissions` | JSONB | subset of template's `max_permissions`, validated at save |
| `bound_sources` | JSONB | list of vault entry IDs, knowledge scope paths, etc. |
| `triggers` | JSONB | cron expressions, event subscriptions, or `manual` |
| `outbox_routing` | JSONB | where results go |
| `budget_usd_per_day` | numeric | per-user, per-agent cap |
| `enabled` | boolean | soft disable |
| `last_run_at` | timestamp nullable | |
| `created_at`, `updated_at` | timestamp | |

**Indexes:**
- `UNIQUE (user_id, name)` — the user's names are their own.
- `(template_id, template_version)` — find all agents on an old template version for upgrades.

### `user_agent_runs` (runtime log)

One row per invocation. Durable log of what the user's agents have done.

| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `user_agent_id` | UUID FK → user_agents | ON DELETE CASCADE |
| `triggered_by` | enum(`manual`, `cron`, `event`, `chain`) | |
| `inbox_item_ref` | string | artifact ref / event id / user input |
| `outbox_item_ref` | string nullable | populated on success |
| `status` | enum(`running`, `succeeded`, `failed`, `canceled`, `budget_exceeded`) | |
| `started_at`, `ended_at` | timestamp | |
| `cost_usd` | numeric | attributed to user's budget |
| `error` | text nullable | |
| `provenance_id` | string | links to provenance DAG |

## The permission model in one diagram

```
  role_permissions   ⊇   template.max_permissions   ⊇   user_agent.permissions
  (what the user          (what this template              (what this instance
   is allowed to do)       is capable of)                   actually uses)
```

Every invocation checks:
1. Does the user still have the role permissions the agent was created under?
2. Do the agent's configured permissions still fit inside the template's max?
3. Does the invocation's specific action fit inside the agent's configured permissions?

If any check fails, the action is denied and logged. Defense in depth — three gates, not one.

## Triggers and invocation

Users can trigger agents four ways:

| Trigger | Shape | Rate-limit concern |
|---|---|---|
| **Manual** | User hits "run" in UI, or via CLI | Per-user cap on concurrent manual runs |
| **Cron** | Cron expression; gateway cron runner fires the agent | Per-user cap on total cron'd agents |
| **Event** | Subscribe to harness events; agent runs when event fires | Per-user cap on events/hour |
| **Chain** | One user agent's outbox is another's inbox (advanced) | Depth limit to prevent loops |

All four land on the same **invocation surface** platform agents use (today: `modules/agent-factory/gateway/`). No parallel infrastructure.

## Budget and metering

**This is the feature that most easily blows up in production, so it gets explicit attention.**

- Every user has a `user_budget_usd_per_day` ceiling (defaults to a conservative value; admin can raise per-user).
- Every user agent has its own `budget_usd_per_day` cap inside the user's total.
- The Bedrock Gateway already meters LLM spend per tenant; extend to per-user attribution.
- Tool calls (paid external APIs) and job costs (metal nodes, etc.) meter the same way.
- Runs exceeding either the agent cap or the user cap are terminated with `status='budget_exceeded'`, user is notified, and the agent is soft-disabled until the next budget window.

Without this: a user's accidentally-recursive agent can burn $500 overnight. With this: it burns `min(budget_cap, <threshold>)` and stops.

## The UI surface

Under `modules/user-services/agents/frontend/`, three pages:

1. **Catalog** — `/agents/catalog` lists available templates with description, max permissions, example use cases. Users browse and clone.
2. **My agents** — `/agents` lists the user's agents with status, last run, today's cost, enable/disable, edit. This is the primary surface.
3. **Runs** — `/agents/{id}/runs` shows per-agent run history: input, output, cost, duration, provenance link. One of the load-bearing trust surfaces — users need to see what their agents are doing.

All three reuse the `modules/user-services/shared/frontend/` shell for consistency with vault and knowledge UIs.

## Template lifecycle

Templates are versioned. User agents pin a template version at creation. When the template's maintainers ship a new version:

- The user's agent keeps working on the pinned version.
- The "My agents" UI surfaces an "Upgrade available" banner with a diff of persona/permissions/contract changes.
- The user opts in; upgrade rewrites the user's agent to reference the new version and re-validates their permissions against the new `max_permissions`. Any permission the user had that is no longer allowed is dropped with a warning.
- Force-upgrade is possible for security issues (e.g., a template with a prompt-injection vulnerability) — communicated via banner + email with a grace window.

This balances "users don't get ambushed by template changes" with "the platform team can push fixes."

## Sharing? No, not in v1.

A user cannot share their agent with another user. Rationale:

- Sharing requires cross-user permission model (whose permissions apply at runtime?), cross-user data bindings (can recipient see the sender's vault entries?), and re-consent UX.
- "Team-owned agents" is a different shape — belongs in a team-services or org-services cluster, not here.
- Users can share a *template* they wrote (future), but templates are platform-reviewed before publication.

v1 answer: one user, one agent, no sharing. If the pattern is useful enough to share, it graduates to a platform template.

## Security risks and mitigations

Named here so they are not discovered in production:

| Risk | Mitigation |
|---|---|
| **Budget runaway** | Per-user + per-agent daily caps; hard cut-off on breach |
| **Trigger abuse** | Per-user rate limits on trigger-fired runs; concurrent-run caps |
| **Prompt injection via bound data** | Same wrapping discipline as platform agents: sources tagged as untrusted, scrubbed before entering LLM context |
| **Credential exfiltration** | Agent inherits user's vault scope only; egress policy enforced; outbox-routing targets validated (no arbitrary webhook to untrusted domains by default) |
| **Capability drift across template upgrades** | Permission re-validation on upgrade; user must re-consent to new permission surface |
| **Runaway chains** | Depth limit on agent→agent chaining; loop detection |
| **Tenancy leakage via shared templates** | Template ID is platform-global but each instance is org-scoped; no cross-tenant state ever touches a user agent |

## Relationship to platform agents and domain apps

| Aspect | Platform / domain agent | User-scoped agent |
|---|---|---|
| Author | Domain team, reviewed | End user, self-service |
| Code | Written from scratch | Customization over a template |
| Trust tier | High | Low — bounded by user's own capabilities |
| Versioning | CI pipeline | Template versioned; user instance is mutable |
| Observability | Platform-level dashboards | User's own dashboard + platform overview |
| Cost attribution | Tenant | Tenant + user + agent |

User-scoped agents run on the **same runtime** (Agent Factory), the **same LLM egress** (Bedrock Gateway), the **same harness** (tools, jobs, events, HITL) — just with a tighter permission envelope and user-owned configuration.

## Relationship to chief-of-staff

Chief-of-staff is expected to be an **opinionated user-scoped agent** — a specific template that composes vault, knowledge, calendar bindings, and a particular persona optimized for goal/commitment tracking. In other words: once user-scoped agents exist, chief-of-staff is 80% a template + UI, not a new service. Same codepath, same infrastructure, new template.

This is why the cluster's build order matters: vault → knowledge → user-scoped agents → chief-of-staff. Each step compounds.

## Build order within this service

Not all of the above lands at once. Rough sequence:

1. Schema + the three tables; template-catalog seeded with two platform-written templates.
2. Backend CRUD for user agents (create from template, edit, enable/disable, delete).
3. Permission validation (the three-gate check) and budget enforcement.
4. Manual trigger support — users can click "run now". Cron and event triggers come after.
5. Runs log + UI for viewing run history.
6. Catalog UI + My Agents UI + Runs UI.
7. Cron trigger support.
8. Event trigger support (more complex; requires event bus integration).
9. Template upgrade flow.
10. Chaining (one agent's outbox → another's inbox) — deferred to after real usage shows it is needed.

Steps 1–6 are the MVP. Steps 7–10 are extensions that ship once the MVP is trusted.

## Out of scope for v1

- User-authored templates (users write their own templates rather than cloning platform ones).
- Cross-user agent sharing.
- Marketplace of user-written templates.
- Admin-on-behalf-of agent creation.
- Fine-grained observability beyond per-run cost and logs.
- Per-agent secrets separate from the vault (all secrets come from the user's vault).
- Team-scoped or org-scoped bespoke agents — belongs in a different cluster.

## Open questions

Flagged here for later resolution, not blocking the design sketch:

1. **Template authorship model.** Are templates written only by the platform team, or do domain teams contribute too? If the latter, templates need a submission/review process similar to adding a new platform agent.
2. **Template permission envelope review.** Who approves a new template's `max_permissions`? This determines the ceiling of what any user's clone can do.
3. **Event subscription shape.** What events can a user agent subscribe to? User-owned events (their calendar, their PRs) are obvious; broader platform events (e.g., any tenant threat event) would need a permissions review.
4. **Interaction with HITL.** A user agent requesting HITL — who is the human? By default, the user themselves. But what about "my agent is blocked waiting for approval I can't give without reading sensitive context"?
5. **Export format for agents.** When a user exports their agents (per cluster invariant #6), what does the export include? Definitely: config, persona, permissions, bindings, triggers. Probably not: run history (privacy-vs-portability tradeoff).

These are design conversations to have when this service moves from "sketch" to "approved for implementation."
