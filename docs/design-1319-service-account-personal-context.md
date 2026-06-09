# Design: Service-Account Personal Context for Autonomous / Event-Driven Runs

**Issue:** #1319 (follow-up to merged PR #1322)
**Parent design:** `docs/design-1319-github-sender-to-cognito-sub.md`
**EPIC:** #1287 (Personal Context)
**Status:** Design + Risk Register (no implementation)
**Author:** @agent-architect
**Date:** 2026-06-09

---

## 1. Problem Statement

The core #1319 design (PR #1322, merged) fixes the `cognito_sub` resolution for
**human-triggered** GitHub-webhook runs and **human-rooted chains** (bot sender
with `root_human_id`). However, it intentionally leaves autonomous/event-driven
runs with `cognito_sub = ""` (fail-closed: no personal context).

This means the **most repetitive** agents — pipeline responders, security
scanners, cost monitors, scheduled housekeepers — never accumulate experiential
knowledge. They rediscover the same patterns, make the same mistakes, and cannot
learn from prior runs. This defeats the "improve with experience" goal for
exactly the agents that run most frequently.

**Decision (from maintainer, non-negotiable):** Use a **service account per
automation** as the personal-context owner. NOT a single global system user.
Rationale:
- A single shared system owner pools every automation's lessons into one
  undifferentiated store — useless for retrieval.
- Worse: it breaks tenant isolation in a multi-tenant platform (org-A's scanner
  reads org-B's secrets learned by the same global system user).
- Per-automation service identities keep each automation's learning store
  separate and tenant-scoped.

---

## 2. Alignment with Current Repo State

Verified by reading the following files:

| File | What it confirms |
|------|-----------------|
| `modules/gateway/src/shared/models/organization.py:98-108` | `ServiceAccount` model: UUID `id`, `org_id` (TenantMixin), `department_id`, `team_id`, `name`, `iam_role_arn` (unique). No `cognito_sub`. |
| `modules/gateway/src/auth/service_account_service.py` | Full CRUD service; org-scoped queries; find-by-role-arn. |
| `modules/agent-context/personal_context/identity.py:31-34,46-48,79-81` | `CallerIdentity(owner_sub, tenant_id)`. `owner_sub` validated as UUID format only — any UUID passes. No Cognito-specific check. |
| `modules/agent-context/personal_context/models.py:45,59-68` | `PersonalContextEntry.owner_sub`: UUID format validator. `ServiceAccount.id` (UUID) passes this check. |
| `modules/agent-factory/webhook-ingress/lambda/github/handler.py:273-312` | `determine_correlation()`: bot sender + no DDB pointer → `is_human_rooted=False`, `root_human_id = bot's user_id`. |
| `modules/agent-factory/webhook-ingress/lambda/common/correlation_store.py` | Pointer stores `{channel_key, correlation_id, root_human_id, is_human_rooted, expires_at}`. |
| `modules/agent-factory/agent/src/complex-task-chat/personal-context-headers.ts:47-62` | `buildPersonalContextIdentity()`: returns null if ownerSub or tenantId is empty. Uses `cognito_sub || user_id || ''`. |
| `modules/agent-factory/agent-worker-image/entrypoint.py:142-151` | Sets `ADP_OWNER_SUB` from envelope `cognito_sub` only if truthy. |
| `modules/gateway/src/internal/routes.py:83-96` | `ResolveUserResponse` — does not return service-account info today. |
| `modules/agent-factory/webhook-ingress/infra/iam.tf` | Lambda has DDB + Secrets Manager + SQS; NO direct Postgres/RDS access. |
| `modules/agent-factory/infra/agent-registry-seed.tf` | Worker IRSA registered as `agent_id: "scaledjob-worker"` with `org_id: "__platform__"`. |
| `docs/design-1319-github-sender-to-cognito-sub.md` (merged) | Defines fail-closed contract, chained-run propagation, `POST /internal/v1/resolve-user-by-id` for resolving user_id → cognito_sub. |

---

## 3. Recommended Design

### 3.1 Owner Identity for Autonomous Runs: `ServiceAccount.id` as `owner_sub`

**Recommendation:** Use `ServiceAccount.id` (an existing UUID primary key) as
the `owner_sub` for autonomous-run personal context.

**Why this works:**
1. The personal-context MCP server validates `owner_sub` as a UUID — `ServiceAccount.id` passes.
2. `ServiceAccount` is already tenant-scoped (via `TenantMixin` / `org_id`) — tenant isolation is structural.
3. Each automation has its own service account → its own `owner_sub` → its own store.
4. No Cognito sub is needed because the MCP server never verifies against Cognito — it just uses the UUID as a partition key.

**Why NOT alternatives:**

| Option | Verdict | Reason |
|--------|---------|--------|
| Synthetic `system:<tenant_id>:<automation_kind>` | Rejected | Not a UUID — fails `identity.py:79` UUID format check; would require MCP server changes. |
| `User.id` of a bot-kind user | Rejected | Conflates service-account concept with bot-user concept; bot users already exist for GitHub bots (different lifecycle). |
| Mint a fake Cognito sub per SA | Rejected | Semantically wrong — pollutes the Cognito namespace with non-Cognito identities; confuses the dual-store detection query. |
| **`ServiceAccount.id`** | **Recommended** | UUID, tenant-scoped, already provisioned via admin API, stable, no new infra. |

**Envelope field reuse:** The existing `cognito_sub` field in the SQS envelope is
reused to carry `ServiceAccount.id` for autonomous runs. This is acceptable
because the MCP server treats it as an opaque UUID — it has no Cognito-specific
semantics downstream of dispatch. However, the field name is misleading, so:

- **Short-term:** Reuse `cognito_sub` field (zero downstream changes).
- **Long-term (optional refactor, out of scope):** Rename to `owner_sub` in envelope v2.

### 3.2 Distinguishing Human vs. Service-Account Context

Add a new **optional** envelope field `owner_kind` to signal which type of
identity `cognito_sub` carries:

```python
envelope = {
    ...
    "cognito_sub": owner_sub_value,       # UUID (human's cognito_sub OR ServiceAccount.id)
    "owner_kind": "human" | "service_account",  # NEW — distinguishes identity type
    ...
}
```

**Why:** Enables downstream consumers (MCP server, observability, audit) to
differentiate human vs. service-account stores without reverse-querying the
identity layer. Also enables future access-control decisions (e.g., "can a
human read a service account's learnings?").

**Backward compat:** Field defaults to `"human"` if absent → existing envelopes
treated as human-owned (correct).

---

## 4. Two-Level Resolution for Event-Driven Invocations

### 4.1 Level 1: Tenant Resolution

**How an autonomous invocation resolves its tenant:**

| Event source | Tenant anchor | Resolution path |
|---|---|---|
| GitHub webhook (bot-triggered comment/label) | `installation_id` | Same as today: `identity_resolver.resolve_tenant(installation_id)` → DDB tenant-registry → `org_id` |
| EventBridge / scheduled rule (future) | Event payload `detail.tenant_id` | New: EventBridge rule tags carry `tenant_id`; ingest Lambda reads from event detail. |
| SQS direct dispatch (internal) | Message attribute `tenant_id` | Already in envelope schema. |

**Blocker if tenant is unresolvable:** If an event source cannot resolve a
tenant, dispatch MUST fail (no dispatch, not a fail-closed-for-context). Without
a tenant, the service account cannot be scoped, and the automation's store cannot
be partitioned. This is a hard prerequisite.

**Current state:** All autonomous GitHub-triggered runs already resolve a tenant
(via `installation_id`). Future EventBridge sources must include `tenant_id` in
their event schema — document this as a contract for new event-source onboarding.

### 4.2 Level 2: Automation-Kind Identification

**How the specific automation is identified within a tenant:**

The automation is identified by the **service account** that the bot sender maps
to. The mapping is:

```
Bot sender (GitHub user_id) 
  → DDB user-identity-index: {provider: "github", provider_user_id: <id>} → {user_id, user_kind: "bot", bot_kind: "agent-developer"}
  → ServiceAccount lookup: bot_kind → service account name match within the tenant
```

**Proposed resolution chain:**

1. `identity_resolver.resolve()` returns `ResolvedIdentity` with `user_kind="bot"`, `bot_kind="pipeline-responder"`.
2. Check `is_human_rooted`:
   - If `true` → use root human's cognito_sub (existing #1319 design; human takes precedence).
   - If `false` → this is an autonomous run. Proceed to step 3.
3. Call gateway: `POST /internal/v1/resolve-service-account-owner` with `{org_id, bot_kind}`.
4. Gateway looks up: `SELECT id FROM service_accounts WHERE org_id = :org_id AND name = :bot_kind`.
5. If found → set `cognito_sub = service_account.id`, `owner_kind = "service_account"`.
6. If not found → fail-closed (`cognito_sub = ""`, no personal context).

**Why key on `bot_kind` → service account `name`:** Each automation type
(pipeline-responder, security-scanner, cost-monitor) already has a distinct
`bot_kind` slug. The natural mapping is: one service account per automation type
per tenant, named after the automation (e.g., `name = "pipeline-responder"`).
This avoids a new mapping table and makes the admin UI intuitive.

**Alternative considered:** Key on `user_id` directly → `ServiceAccount.id`
mapping table. Rejected: adds a new table, no clear benefit over the
`bot_kind → name` convention.

---

## 5. Precedence with Human-Rooted Chains

### Decision Table

| Sender | `is_human_rooted` | `root_human_id` | Resolution | `owner_sub` | `owner_kind` |
|--------|-------------------|-----------------|------------|-------------|--------------|
| Human | true | self | Gateway: user_id → cognito_sub | Human's cognito_sub | `"human"` |
| Bot | true | present | Gateway: root_human_id → cognito_sub | Root human's cognito_sub | `"human"` |
| Bot | false | self (bot) | Gateway: org_id + bot_kind → SA.id | ServiceAccount.id | `"service_account"` |
| Bot | false | self (bot) | SA not found → fail-closed | `""` | N/A |

### Precedence Rule

> **Human-rooted chains ALWAYS attribute to the human.** The service-account
> path activates ONLY for truly autonomous runs (`is_human_rooted == false`).

**Rationale:** When a human triggers work that cascades through bot agents, the
human is the principal whose context matters. The automation is just an executor.
Only when there is genuinely no human in the chain does the automation need its
own experiential store.

### Edge Case: Human triggers automation that runs autonomously later

Example: Human sets up a monitor (one-time setup). Monitor triggers daily.
- The setup run is human-rooted → human's context.
- Daily runs are autonomous (no correlation pointer, or pointer expired after 7 days) → service account's context.
- This is correct: the daily run's learnings are *operational* (about the monitored system), not *personal* to the human who configured it.

---

## 6. Lifecycle: Service Account Provisioning

### Provisioning Model: Registered Ahead of Time

**Decision:** Service accounts that own personal-context stores MUST be
provisioned ahead of time (via admin API or Terraform). They are NOT created
on-demand on first event.

**Why not on-demand:**
1. **Unbounded creation risk:** A misconfigured event source could create a new
   `owner_sub` per event → store sprawl (see Risk R13).
2. **No approval workflow:** On-demand creation bypasses tenant-admin control over
   which automations accumulate knowledge.
3. **Naming inconsistency:** On-demand would need to guess the name from
   `bot_kind` — better to have the admin explicitly set it.

### Provisioning Flow

1. **Tenant admin** creates a service account via existing
   `POST /admin/organizations/{org_id}/service-accounts` with:
   ```json
   {
     "name": "pipeline-responder",
     "department_id": "<ops-dept-id>",
     "team_id": "<platform-team-id>",
     "iam_role_arn": "arn:aws:iam::123456789012:role/adp-dev-agent-scaledjob-role"
   }
   ```
2. The `name` field matches the `bot_kind` slug used by the automation's bot user.
3. When the automation triggers (bot sender with `bot_kind = "pipeline-responder"`
   and `is_human_rooted = false`), the webhook handler resolves:
   - `org_id` (from tenant) + `bot_kind` → `service_accounts.name` match → `service_accounts.id`.
4. If no matching service account exists, personal context fails closed (the
   automation still runs, just without experiential memory).

### Does a Service Account Need a Cognito Sub?

**No.** The `ServiceAccount.id` (UUID) IS the `owner_sub`. No Cognito identity
is needed because:
- The personal-context MCP server validates `owner_sub` as a UUID — any UUID works.
- There is no JWT-based authentication for autonomous runs (they authenticate via IRSA/IAM).
- Adding a Cognito identity to service accounts would conflate two auth models.

---

## 7. Consistency Guarantees

### 7.1 One Store Per Automation Per Tenant

Each automation type within a tenant has exactly one service account → one
`owner_sub` → one personal-context store. Enforced by:
- `ServiceAccount.id` is a UUID PK (unique by definition).
- The `bot_kind → service_account.name` lookup is scoped by `org_id`.
- Unique constraint on `(org_id, name)` for service accounts (proposed new
  migration — see Section 9).

### 7.2 No Cross-Contamination Between Human and SA Stores

- Human stores keyed by `users.cognito_sub` (from Cognito JWKS, set during OAuth).
- SA stores keyed by `service_accounts.id` (generated UUID, set at provisioning).
- These UUID namespaces are disjoint: Cognito generates v4 UUIDs;
  ServiceAccount uses SQLAlchemy-generated v4 UUIDs from a different source.
  Collision probability: 2^-122 — structurally infeasible.
- **Additional guard:** The `owner_kind` envelope field allows the MCP server to
  partition stores by kind in future (e.g., separate S3 prefix per kind).

### 7.3 No Cross-Tenant Leakage

- Service account lookup is `WHERE org_id = :org_id AND name = :bot_kind`.
- Even if two tenants have a service account named "pipeline-responder", they
  have different `ServiceAccount.id` UUIDs → different stores.
- Tenant scoping is structural (SQL WHERE clause), not convention.

---

## 8. Integration with Existing Personal-Context Middleware

### Changes Required (Minimal)

The existing middleware stack (`personal-context-headers.ts` →
`entrypoint.py` → MCP server `identity.py`) requires **zero changes** for the
service-account path. Here's why:

| Layer | Current behavior | SA behavior | Change needed? |
|---|---|---|---|
| `handler.py:635` | Sets `cognito_sub` from resolved identity | Sets `cognito_sub` from `ServiceAccount.id` | **Yes** — new branch for autonomous bot |
| `entrypoint.py:146-148` | Sets `ADP_OWNER_SUB` if `cognito_sub` truthy | Same — SA.id is truthy | No |
| `personal-context-headers.ts:54` | `ownerSub = cognito_sub \|\| user_id \|\| ''` | SA.id flows through `cognito_sub` | No |
| `identity.py:79-81` | Validates UUID format | SA.id is UUID | No |
| `storage.py:120-123` | Force-stamps `owner_sub` from identity | Works with SA.id | No |

**Only the webhook handler** (`handler.py`) needs modification to:
1. Detect autonomous bot sender (`user_kind == "bot"` AND `is_human_rooted == false`).
2. Call gateway to resolve `bot_kind` → `ServiceAccount.id`.
3. Set `cognito_sub = service_account_id` and add `owner_kind = "service_account"`.

### New Gateway Endpoint

```
POST /internal/v1/resolve-service-account
Body: {"org_id": "...", "automation_name": "..."}
Response: {"service_account_id": "...", "org_id": "..."}  OR  404
```

This is a thin read — single-row lookup by `(org_id, name)`.

---

## 9. Database Changes

### New Migration: Unique Constraint on (org_id, name)

```sql
-- Alembic migration: 017_service_account_unique_name.py
CREATE UNIQUE INDEX uq_service_accounts_org_name
  ON service_accounts (org_id, name);
```

**Why:** The `bot_kind → service_account.name` resolution assumes uniqueness
within a tenant. Without this constraint, an admin could create two service
accounts named "pipeline-responder" in the same org, causing ambiguous resolution.

**Risk:** If duplicates already exist in production, the migration will fail.
Add a pre-check: `SELECT org_id, name, COUNT(*) FROM service_accounts GROUP BY org_id, name HAVING COUNT(*) > 1`. If any exist, log and refuse migration.

No other schema changes needed — `ServiceAccount` already has everything.

---

## 10. Extended Risk Register (Non-Human Cases)

Extends the 10-risk register from the parent design (`design-1319-github-sender-to-cognito-sub.md`).

| # | Risk | Severity | Likelihood | Impact | Mitigation |
|---|------|----------|------------|--------|------------|
| **R11** | **Cross-tenant attribution** — Autonomous run resolves wrong tenant → writes into wrong org's SA store | **Critical** | Very Low | One tenant's automation learnings leak into another tenant's store. Confidentiality breach. | (a) Tenant resolution is gated by `installation_id` (GitHub App-scoped, one app per tenant). Cannot resolve to wrong tenant without compromising the GitHub App. (b) SA lookup is `WHERE org_id = :org_id AND name = :bot_kind` — tenant-scoped by SQL. (c) Even if tenant is wrong, SA.id differs per org → data physically separated. (d) Add assertion: `envelope.tenant_id == service_account.org_id` before dispatch. |
| **R12** | **Unbounded service-identity creation (store sprawl)** — A new `owner_sub` per event if SA creation is on-demand | **High** | Medium (if on-demand) | DynamoDB/S3 storage grows without bound; query performance degrades; cost explodes. | (a) **Design decision: NO on-demand SA creation.** SA must be pre-provisioned by admin. (b) If `bot_kind` doesn't match a registered SA → fail-closed (empty cognito_sub, no store). (c) CloudWatch metric `PersonalContext.SANotFound` alerts on unregistered automation types attempting to use personal context. (d) Quota: max 50 SAs per org (enforced at admin API). |
| **R13** | **SA store readable by humans in tenant** — A human queries personal-context and sees automation learnings | **Medium** | Low | Automation-internal knowledge (e.g., which workarounds work, which endpoints are flaky) exposed to humans who may misinterpret. Not a security breach per se, but noisy. | (a) Default: **SA stores are private to the automation** (same isolation as human stores — `owner_sub` scoping means only the same `owner_sub` can read). (b) A human with a different `owner_sub` cannot query an SA's store without explicitly passing the SA's UUID as `X-Owner-Sub` — which the dispatch layer prevents (headers are set from trusted metadata). (c) Future: admin-read-only API for SA stores (out of scope; file as follow-up if needed). |
| **R14** | **SA impersonation** — Attacker discovers SA UUID and injects `X-Owner-Sub: <SA.id>` to read/write SA store | **High** | Very Low | Unauthorized access to automation's learned knowledge. Could poison the automation's memories (write attack). | (a) `X-Owner-Sub` is set by the trusted dispatch layer (worker harness), never by agent code. Headers are injected from `ADP_OWNER_SUB` env var set by `entrypoint.py`. (b) MCP server is behind K8s NetworkPolicy — only pods in agent-context namespace can reach it. (c) Agent subprocess inherits env but cannot modify parent env → cannot change owner_sub. (d) UUID is opaque — discovering it requires DB access or API call that only admins have. |
| **R15** | **Stale SA after deletion** — Admin deletes SA, but existing correlation pointers or cached envelope still reference the SA.id | **Low** | Low | Runs dispatched with deleted SA.id → personal-context writes succeed (MCP server doesn't validate SA existence), orphaned data. | (a) Acceptable for soft-delete: SA data remains but new runs fail-closed at gateway (404 from resolve endpoint). (b) Hard-delete: add MCP-side TTL or periodic garbage collection for `owner_sub` values with no matching SA or user. (c) Short-term: leave orphaned data (storage cost trivial; no security impact). |
| **R16** | **bot_kind → SA name mismatch** — Admin creates SA with name "pipeline-agent" but bot_kind is "pipeline-responder" | **Medium** | Medium | Automation resolves to wrong SA (if partial match) or no SA (if strict match). Wrong: cross-automation contamination. None: silent feature loss. | (a) **Strict equality match** — `service_accounts.name = bot_kind` exactly. No fuzzy/partial matching. (b) Admin UI shows mapping: "This service account will receive personal context for automation type `<name>`." (c) Validation on SA create: warn if `name` doesn't match any known `bot_kind` in the identity-index. (d) Documentation: "SA name MUST equal the automation's `bot_kind` slug." |
| **R17** | **Shared IAM role across multiple SAs** — Multiple service accounts share one IAM role (current: unique constraint prevents this) | **Low** | Very Low | Ambiguous: which SA owns the run? | (a) `iam_role_arn` already has UNIQUE constraint on `ServiceAccount`. (b) For personal context, resolution is by `(org_id, name)` not by role_arn — role isn't involved in owner resolution. (c) Non-issue for this design. |
| **R18** | **Autonomous run claiming human-rooted** — A truly autonomous run has a stale correlation pointer with `is_human_rooted=true` from a prior human interaction on the same channel | **Medium** | Low | Autonomous run incorrectly uses the stale human's cognito_sub → writes to wrong human's store. | (a) Correlation pointers have 7-day TTL — stale pointers expire naturally. (b) **Additional guard:** If sender is bot AND pointer's `root_human_id` was written >24h ago AND no human activity since, treat as autonomous (emit metric, use SA path). (c) Alternatively: human runs always overwrite the pointer (they do: `determine_correlation()` unconditionally starts a new chain for humans). So a stale pointer means no human has touched this channel in 7 days → likely safe to treat as autonomous. |

---

## 11. Reconciliation with Parent Design (#1319 PR #1322)

The parent design defined this resolution hierarchy:

```
1. Human sender → user.cognito_sub (direct personal context)
2. Bot sender + human-rooted chain → root_human.cognito_sub (delegated personal context)
3. Bot sender + NOT human-rooted → "" (no personal context)
```

This extension adds a **fourth case** between cases 2 and 3:

```
1. Human sender → user.cognito_sub (direct personal context)           [owner_kind: "human"]
2. Bot sender + human-rooted chain → root_human.cognito_sub            [owner_kind: "human"]
3. Bot sender + NOT human-rooted + registered SA → ServiceAccount.id   [owner_kind: "service_account"]  ← NEW
4. Bot sender + NOT human-rooted + NO registered SA → ""               [no personal context]
```

**The fail-closed invariant is preserved:** Case 4 ensures that unregistered
automations never get personal context. The new case 3 is additive only.

---

## 12. Implementation Issues (Updated)

### Issue A (unchanged): Core Fix — `cognito_sub` Resolution for Human Path

As specified in the parent design. Ships first. No changes.

### Issue D (NEW): Service-Account Personal Context for Autonomous Runs

**Scope:** Developer implementation issue. Ships AFTER Issue A.

**Prerequisites:** Issue A merged (gateway returns `cognito_sub` for humans).

**Changes:**

1. **New migration `017_service_account_unique_name.py`:**
   ```sql
   CREATE UNIQUE INDEX uq_service_accounts_org_name ON service_accounts (org_id, name);
   ```

2. **New gateway endpoint `POST /internal/v1/resolve-service-account`:**
   - Input: `{"org_id": "...", "automation_name": "..."}`
   - Logic: `SELECT id FROM service_accounts WHERE org_id = :org_id AND name = :automation_name`
   - Output: `{"service_account_id": "..."}` or 404

3. **`gateway_client.py` — new method `resolve_service_account()`:**
   - Call the new endpoint.
   - Return `{"service_account_id": "..."}` or `None`.

4. **`handler.py` — extended logic at line 635:**
   ```python
   if resolved.user_kind == "human":
       cognito_sub = resolved.cognito_sub if resolved.cognito_sub else ""
       owner_kind = "human"
   elif correlation_ctx and correlation_ctx["is_human_rooted"]:
       # Chained run — resolve root human (Issue A)
       cognito_sub = _resolve_root_human_cognito_sub(correlation_ctx["root_human_id"])
       owner_kind = "human"
   elif resolved.user_kind == "bot" and resolved.bot_kind:
       # Autonomous bot — resolve service account
       sa = _get_gateway_client().resolve_service_account(tenant_id, resolved.bot_kind)
       cognito_sub = sa["service_account_id"] if sa else ""
       owner_kind = "service_account" if sa else ""
   else:
       cognito_sub = ""
       owner_kind = ""
   ```

5. **Envelope extension:**
   ```python
   envelope["owner_kind"] = owner_kind  # "human" | "service_account" | ""
   ```

6. **Seed service accounts for existing bot users:**
   - For each `bot_kind` in the identity-index (e.g., `agent-developer`, `agent-architect`, `agent-reviewer`, `agent-operations`), create a service account in the dev tenant.
   - Terraform data resource or one-time seed script.

**Tests:**
- Unit: Autonomous bot with registered SA → envelope has SA.id as cognito_sub + owner_kind="service_account".
- Unit: Autonomous bot without SA → empty cognito_sub (fail-closed).
- Unit: Human-rooted bot → ignores SA path, uses root human's cognito_sub.
- Unit: Gateway failure on SA lookup → empty cognito_sub (fail-closed).
- Integration: Full webhook → SQS envelope for autonomous bot has correct SA.id.

**Deployment:**
- Gateway first (new endpoint + migration), then webhook-ingress.
- Feature flag: reuse `RESOLVE_CANONICAL_VIA_GATEWAY` (SA lookup follows same gateway-call pattern).
- Seed service accounts before enabling (otherwise all autonomous runs hit 404 → no context).

### Issue B (unchanged): E2E Assertion (#1295 addition)

Add assertion for SA path:
- Autonomous bot with registered SA → envelope `cognito_sub == service_accounts.id` for matching `(org_id, name)`.
- Autonomous bot without SA → empty.

### Issue C (unchanged): Observability Metrics

Add additional metrics:
- `PersonalContext.SAResolutionSuccess` — SA found for autonomous bot.
- `PersonalContext.SANotFound` — bot_kind didn't match any registered SA.
- `PersonalContext.AutonomousRunNoContext` — autonomous run proceeded without personal context.

---

## 13. Deployment Sequence (Full Personal-Context Identity Fix)

```
Phase 1: Gateway deploy (Issue A + Issue D gateway changes)
  - Migration 017 (unique index)
  - New endpoint: /internal/v1/resolve-service-account
  - Extend /internal/v1/resolve-user to return cognito_sub
  - Extend /internal/v1/resolve-user-by-id for chained runs

Phase 2: Seed service accounts (Issue D)
  - Create SAs for known bot_kinds in each tenant (admin API or Terraform)

Phase 3: Webhook-ingress deploy (Issue A + Issue D webhook changes)
  - Fix handler.py:635 — human path uses resolved.cognito_sub
  - Add SA resolution for autonomous bots
  - Add owner_kind field
  - Feature flag gates all new behavior

Phase 4: Validate (Issue B + Issue C)
  - E2E assertions pass
  - Metrics flowing
  - Dual-store detection finds no old-bug evidence

Phase 5: Remove feature flag (optional future)
  - Make RESOLVE_CANONICAL_VIA_GATEWAY always-on
```

---

## 14. Open Questions

1. **Should SA stores be visible to tenant admins?** Currently, personal-context
   stores are owner-scoped (only the same owner_sub can read). Should there be an
   admin override to inspect what an automation has learned? (Recommend: yes, but
   as a separate admin-read API — out of scope for this issue.)

2. **Shared service accounts across tenants?** Some automations (platform-level
   scanners) serve multiple tenants. Should there be a `__platform__` scoped SA?
   (Recommend: no — use one SA per tenant even for platform agents. Keeps tenant
   isolation absolute. The platform can have its own tenant.)

3. **Retention/eviction for SA stores?** Human context has no TTL currently. SA
   context for a decommissioned automation could accumulate indefinitely. Should
   there be a TTL or manual purge? (Recommend: defer to EPIC-level decision on
   context lifecycle.)

4. **Can an automation's personal context grow unbounded?** High-frequency agents
   (running 100s of times/day) may accumulate thousands of entries. Need a
   per-owner entry cap or synthesis/compaction strategy. (Recommend: document as
   follow-up, apply same strategy as human stores once designed.)

---

## 15. Decision Log

| Decision | Rationale | Alternatives rejected |
|----------|-----------|----------------------|
| `ServiceAccount.id` as `owner_sub` | UUID format (passes MCP validation); tenant-scoped; already provisioned; stable | Synthetic string (fails UUID check), User.id (wrong model), fake Cognito sub (semantic pollution) |
| Pre-provisioned, not on-demand | Prevents unbounded store creation; gives admins control; naming consistency | On-demand (sprawl risk, no approval), hybrid (complexity for no clear benefit) |
| `bot_kind → SA name` mapping | Natural 1:1 (each automation type has one bot_kind and one SA); simple; observable | Explicit mapping table (more infra, no clear benefit), role-arn lookup (irrelevant to context) |
| Human-rooted takes precedence | The human initiated the work; their context is relevant to the execution | SA takes precedence (loses human context), both (complex, unclear semantics) |
| Reuse `cognito_sub` envelope field | Zero downstream changes; MCP server is agnostic | New `owner_sub` field (breaks backward compat, requires header changes) |
| Add `owner_kind` field | Enables observability/audit distinction; minimal schema addition | Infer from UUID cross-reference (expensive), no distinction (confusing metrics) |
| Unique (org_id, name) constraint | Prevents ambiguous SA resolution; enforces one-SA-per-automation-per-tenant | Allow duplicates + pick first (non-deterministic), allow duplicates + fail (unnecessary) |
| 7-day pointer TTL sufficient for autonomy detection | After 7 days without human activity, pointer expires → bot starts autonomous chain (correct) | Shorter TTL (loses human context on long-running issues), longer (delays autonomy detection) |

---

## 16. Verdict

**Ready for implementation** (additive to the parent #1319 design, ships as
Issue D after Issue A). The design:
- Reuses existing ServiceAccount model (zero new tables).
- Requires one migration (unique index) + one new endpoint + handler logic extension.
- Preserves the fail-closed invariant (no SA → no context, never wrong owner).
- Maintains tenant isolation structurally (SQL WHERE + UUID separation).
- Adds minimal risk surface (6 new risks, all mitigated by pre-provisioning + strict matching).
