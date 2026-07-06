# Design Note: Invisible Tenancy — Per-Action Tenant Resolution (Issue #3074)

> **Status**: Design-review (spike output)
> **Author**: @agent-architect
> **Date**: 2026-07-06
> **Issue**: #3074 — invisible tenancy per-action resolution replaces session-scoped active tenant
> **EPIC**: #2981 (delivery tracking)
> **Parent decision**: #3068 (Option A north star adopted; B-now/A-later migration path)
> **Mode**: Per-issue spike
> **Verdict**: Design-complete — this note defines the architecture for v2 migration; no code ships from this issue.

---

## 0. Executive Summary

The current tenant model (#2951 Rev 4.1, shipped via #2981) is **session-scoped**: a
user has one "active tenant" at a time (`TenantMembership.is_active` + `custom:org_id`
JWT claim), and every request is scoped to that tenant via `TokenContext.org_id`. This
forces UI workspace switching and creates cognitive overhead for multi-org users.

The v2 north star (#3068 Option A) replaces this with **invisible tenancy**: the UI
presents one flat surface; authorization resolves **per-action** by looking up the target
resource's owning tenant and checking the caller's membership/role in THAT tenant.
Tenancy becomes invisible infrastructure — an isolation and billing primitive, not a
user-visible mode.

This note specifies the resolution middleware, migration order, token-claim demotion,
`is_active` end-state, and frontend IA changes required to get there.

---

## 1. Resolution Middleware Spec

### 1.1 Core Concept: `effective_tenant_id` per Request

Today every request carries an implicit tenant scope from the JWT:
```
Request → JWT → custom:org_id → TokenContext.org_id → query filter
```

Under invisible tenancy, the tenant is resolved from the **target resource**:
```
Request → target resource → resource's owning tenant → membership check → proceed/deny
```

### 1.2 Resolution Function

A new FastAPI dependency, `resolve_action_tenant`, replaces `TokenContext.org_id` as
the authorization input for mutations:

```python
# src/auth/action_tenant_resolver.py (new file)

async def resolve_action_tenant(
    *,
    resource_type: ResourceType,
    resource_id: str,
    caller: TokenContext,
    db: AsyncSession,
) -> ActionContext:
    """Resolve the owning tenant of a target resource and verify caller access.

    Returns ActionContext with effective_tenant_id, caller_role, and the
    resolved resource. Raises 403 if caller has no membership in the owning tenant.
    """
    # 1. Look up resource → owning tenant
    tenant_id = await _resolve_owner(resource_type, resource_id, db)

    # 2. Check caller's membership + role in that tenant
    membership = await _get_membership(caller.user_id, tenant_id, db)
    if membership is None:
        raise HTTPException(status_code=403, detail="Not a member of the resource's tenant")

    # 3. Check role-based permission for the action
    # (e.g., disconnect requires org_admin OR installed_by grant per #3073)

    return ActionContext(
        effective_tenant_id=tenant_id,
        caller_role=membership.role,
        caller_user_id=caller.user_id,
    )
```

### 1.3 Resource-Type Resolution Table

Each resource type has a defined path from resource ID to owning tenant:

| Resource type | Lookup key | Owning tenant resolution | Table/column |
|---|---|---|---|
| **Connection (GitHub install)** | `installation_id` | `channel_tenant_map.org_id` WHERE `provider='github' AND provider_scope_id=str(installation_id)` | `channel_tenant_map.org_id` |
| **Agent (Cognito M2M)** | `client_id` | Agent's `org_id` from Cognito custom attributes (or DDB agent registry `tenant_id`) | `agent_registry.tenant_id` |
| **Agent (IAM registry)** | `agent_id` | `agent_registry` DynamoDB item `tenant_id` attribute | DDB `adp-dev-agent-registry` |
| **Budget config** | `org_id` (path param) | The path param IS the tenant — validate membership | Path param → membership check |
| **Rate limit config** | `org_id` (path param) | Same as budget | Path param → membership check |
| **User management** | `org_id` (path param) | Same — but requires `org_admin` role | Path param → role check |
| **Knowledge asset** | `asset_id` | `knowledge_assets.tenant_id` (already per-resource, see `_authorize_modify`) | `knowledge_assets.tenant_id` |
| **Service account** | `service_account_id` | `service_accounts.org_id` (TenantMixin) | `service_accounts.org_id` |

### 1.4 List Endpoints: Multi-Tenant Fan-Out

For **read/list** endpoints, the pattern is already proven by `get_connections` (#3018):

```python
# Pattern: list endpoint resolves ALL member tenants, queries across them
membership_stmt = select(TenantMembership.tenant_id).where(
    TenantMembership.user_id == pg_user_id,
)
tenant_ids = [row[0] for row in (await db.execute(membership_stmt))]
# Query with: WHERE resource.org_id IN :tenant_ids
```

Each list response item includes `tenant_id` and `tenant_name` for frontend grouping
(already shipped for connections; extend to agents, budgets, etc.).

### 1.5 ActionContext Schema

```python
@dataclass
class ActionContext:
    """Injected into route handlers after per-action resolution."""
    effective_tenant_id: str      # The tenant that owns the target resource
    caller_role: str              # Caller's role in that tenant (org_admin | member)
    caller_user_id: str           # Postgres user.id of the caller
    resource_grant: str | None    # Per-resource grant (e.g., "installed_by" from #3073)
```

### 1.6 Enforcement Layer

The per-action resolver IS the authorization check. It replaces:
- `require_admin` (today: checks `is_admin` on TokenContext — this is platform-admin, stays)
- `access.check_permission(target_org_id=org_id)` (today: compares path `org_id` to token `org_id` — replaced by membership lookup)
- `caller_org_id=current_user.org_id` comparisons in service functions (replaced by `action_context.effective_tenant_id`)

**What stays unchanged:**
- `require_platform_admin` — platform admin is orthogonal to tenant membership
- Authentication (JWT validation, IAM SigV4) — unchanged
- The `TokenContext` object itself (still extracted from JWT) — but `org_id` is no longer used for authorization

---

## 2. `is_active` End-State

### 2.1 Current Role of `is_active`

Today `is_active` on `TenantMembership` serves THREE purposes:
1. **Authorization input**: mutations are rejected if the target resource's tenant != caller's active tenant (the #3018 leak-guard)
2. **Read-default**: determines which tenant's dashboard/data loads by default
3. **Token claim source**: `users.org_id` (synced from `is_active`) feeds the pre-token-gen Lambda → `custom:org_id`

### 2.2 End-State: Keep as Read-Default, Remove as Authorization Input

Under invisible tenancy:

| Purpose | End-state | Rationale |
|---|---|---|
| Authorization input | **REMOVED** | Per-action resolution replaces it entirely. A caller can mutate ANY resource they have membership+role for, regardless of which tenant is "active." |
| Read-default | **KEPT** | When loading a dashboard/overview, `is_active` determines which tenant's data appears first (optimization: avoids loading all tenants' dashboards). User can expand to "all workspaces" view. |
| Token claim source | **DEMOTED** (see §4) | `custom:org_id` becomes a hint/default, not an authorization input. |

### 2.3 Partial Unique Index Remains

The partial unique index (`WHERE is_active = TRUE`) stays. It guarantees exactly one
default tenant per user — needed for:
- Dashboard default view (show this tenant's data first)
- New-session initial data load (before user expands to "all")
- `users.org_id` denormalization (TenantMixin query scoping for LEGACY endpoints not yet migrated)

### 2.4 Migration Safety: Parallel Guards During Transition

During the endpoint-by-endpoint migration (§3), BOTH guards run simultaneously:
1. The old session-level guard (`caller_org_id == resource.org_id`)
2. The new per-action guard (membership lookup)

An endpoint is "migrated" when the session-level guard is REMOVED and only per-action
remains. The parallel period catches resolution bugs: if per-action would allow
something session-level would deny (or vice versa), log a security warning.

```python
# Transition pattern: parallel validation
async def disconnect_github_v2(installation_id: int, ...):
    # NEW: per-action resolution
    action = await resolve_action_tenant(
        resource_type=ResourceType.CONNECTION,
        resource_id=str(installation_id),
        caller=current_user,
        db=db,
    )

    # OLD (transitional): session-level assertion — log-only, not blocking
    effective_org_id = await resolve_effective_org_id(current_user, db)
    if action.effective_tenant_id != effective_org_id:
        logger.warning(
            "PARALLEL_GUARD_MISMATCH: per-action resolved tenant=%s but session tenant=%s "
            "for user=%s on connection=%d",
            action.effective_tenant_id, effective_org_id,
            current_user.user_id, installation_id,
        )
        # During parallel period: DENY (prefer strictness)
        # After confidence: remove this block entirely

    return await delete_connection(
        installation_id=installation_id,
        caller_org_id=action.effective_tenant_id,
        db=db,
    )
```

---

## 3. Migration Order

### 3.1 Principles

1. **Start with the endpoint that's already closest** — connections/disconnect (#3073 already adds `installed_by_user_id` per-resource grant)
2. **Migrate mutations before reads** — reads already fan out (#3018); mutations are where cross-tenant bugs are dangerous
3. **One endpoint per PR, parallel guards active** — each PR adds per-action resolution AND keeps the session guard as a log-only assertion
4. **Remove session guards in a separate sweep** — only after N weeks with zero `PARALLEL_GUARD_MISMATCH` warnings in CloudWatch

### 3.2 Endpoint Migration Sequence

| Phase | Endpoint | Current auth pattern | Migration notes |
|---|---|---|---|
| **M1** | `DELETE /admin/connections/github/{installation_id}` | `require_admin` + `caller_org_id=current_user.org_id` | First candidate: #3073 already adds per-resource `installed_by_user_id` grant. Resolution: `installation_id` → `channel_tenant_map.org_id` → membership check. Permission: `org_admin` OR `installed_by` grant. |
| **M2** | `POST /admin/connections/github/install-start` | `get_current_user` (installs to active tenant) | Resolution: install attaches to the TARGET org's tenant (already fixed in #2952 Rule 1.d). No session dependency. |
| **M3** | Agent CRUD (`/admin/agents/*`, `/admin/registry/agents/*`) | `require_admin` + path `org_id` OR token `org_id` | Resolution: agent's `org_id` from registry. Multi-tenant agents show in flat list; mutations resolve per-agent. |
| **M4** | Budget/rate-limit CRUD (`/admin/organizations/{org_id}/budget/*`) | `access.check_permission(target_org_id=org_id)` | Resolution: path `org_id` IS the tenant — validate membership. Already has the right shape; just swap the check_permission internals to use membership lookup instead of comparing to session org_id. |
| **M5** | User management (`/admin/organizations/{org_id}/users/*`) | `access.check_permission` (org_admin required) | Same as M4 but requires `org_admin` role in the target tenant. Higher risk — test thoroughly. |
| **M6** | Knowledge assets (`/knowledge/*`) | `_authorize_modify` (already per-resource!) | Already uses `row.tenant_id != current_user.org_id` — swap to membership check. Closest to target pattern already. |
| **M7** | Service accounts | `require_admin` + path `org_id` | Same shape as M4/M5. |
| **M8** | Pool management (platform-admin only) | `require_platform_admin` | No tenant dimension — platform-wide. No migration needed. |

### 3.3 Test Strategy: Side-by-Side Validation

Each migration PR includes:
1. **Unit test**: mock membership lookup, verify per-action resolves correctly
2. **Integration test**: user with memberships in tenants A and B; mutation on tenant-B resource succeeds WITHOUT switching active tenant
3. **Negative test**: user WITHOUT membership in tenant C; mutation on tenant-C resource returns 403
4. **Parallel-guard test**: assert no `PARALLEL_GUARD_MISMATCH` log emission for legitimate actions

Test file pattern: `tests/admin/connections/test_per_action_resolution.py`

### 3.4 Session Guard Removal (Final Phase)

After all M1–M7 run in parallel mode for ≥2 weeks with zero mismatches:
1. Remove `resolve_effective_org_id` calls from migrated endpoints
2. Remove `PARALLEL_GUARD_MISMATCH` logging
3. Mark `POST /admin/connections/switch-tenant` (#3071) as admin-only / internal
4. Update `is_active` semantics (no longer authorization-relevant)

---

## 4. Token Claims — `custom:org_id` Demotion

### 4.1 Current State

```
Cognito user attributes → pre-token-gen Lambda → access token claim:
  custom:org_id = users.org_id (the active tenant)
```

Every request reads this claim via:
- `cognito_jwt.py:208` → `CognitoTokenClaims.org_id`
- `dependencies.py:91` → `TokenContext.org_id`
- `middleware.py:590` → `request.state.org_id`

### 4.2 Callers That Read `TokenContext.org_id` (Inventory)

This inventory must be produced by #3071's PR (the switch-endpoint PR documents which
endpoints still scope by raw token org_id). Preliminary list from codebase grep:

| File | Usage | Migration action |
|---|---|---|
| `connections/routes.py:218` | `resolve_effective_org_id(current_user, db)` | Replace with multi-tenant fan-out (already done for reads) |
| `connections/routes.py:268` | `caller_org_id=current_user.org_id` (disconnect) | Replace with per-action resolution (M1) |
| `connections/service.py:257` | Install target org resolution | Already fixed by #2952 Rule 1.d |
| `auth/vault_service.py:183` | `resolve_effective_org_id` for vault credential scoping | Replace with per-credential tenant lookup |
| `auth/aws_connect_routes.py:139,225` | `resolve_effective_org_id` for AWS account connections | Replace with per-connection tenant lookup |
| `middleware.py:127-141` | Request logging (`request.state.org_id`) | Keep as-is — logging the token claim is fine; it becomes "user's default tenant" |
| `admin/routes.py` (budget/ratelimit/user routes) | Path `{org_id}` + `access.check_permission(target_org_id=org_id)` | Swap permission check internals (M4–M7) |
| `knowledge/routes.py` | `current_user.org_id` in `_authorize_modify` | Swap to membership check (M6) |

### 4.3 End-State of `custom:org_id`

| Aspect | Before | After |
|---|---|---|
| Source | `users.org_id` (active tenant) | Same source, same write path |
| Semantics | "Authorization scope for this session" | "Default tenant hint for reads/dashboards" |
| Used for authz | Yes (every query is filtered by it) | **No** — per-action resolution handles authz |
| Used for reads | Implicitly (everything is scoped) | Explicitly (dashboard default; user can expand) |
| Used for logging | Yes | Yes (unchanged — audit trail of "which default") |

### 4.4 Pre-Token-Gen Lambda Changes

**None required.** The Lambda (`pre_token_generation.py:89-128`) continues to inject
`custom:org_id` from Cognito user attributes into the access token. The claim is still
useful as a read-default hint. The change is entirely in how **consumers** interpret
it — they stop using it as an authorization gate.

### 4.5 Gateway Switch-Token Interaction

The switch-tenant endpoint (#2982, ships as #3071) issues gateway JWTs with the new
`org_id`. Under invisible tenancy:
- The endpoint STAYS as an **admin/debug affordance** (operators can override the default)
- The issued token's `org_id` becomes the dashboard default, not an authz scope
- Regular users never call it — the flat UI removes the need

---

## 5. Frontend Information Architecture

### 5.1 End-State: One Flat Surface

| UI element | Current (session-scoped) | Target (invisible tenancy) |
|---|---|---|
| Workspace switcher | Header dropdown, user must switch | **Removed** for regular users. Admin-only affordance under Settings → Debug. |
| Connections page | Grouped by workspace, non-active read-only | **One flat list**, grouped by org display name (visual grouping only, not a mode). All connections are actionable — mutations resolve per-connection. |
| Agents page | Scoped to active workspace | **Flat list** with org-name labels. Create-agent resolves target tenant from the selected connection. |
| Budgets/Rate limits | Scoped to path `{org_id}` | **Tabbed by org** (admin multi-org view), or flat with org labels (if user has 1-2 orgs). |
| Dashboard | Active-workspace metrics | Default shows `is_active` tenant; "All workspaces" toggle expands to union view. |

### 5.2 Org Name as Label, Not Mode

Connections and agents display their owning org as a **label/badge** (e.g.,
`[aws-innovate]` or `[sophos-research]`). This is informational grouping, not a
filtering mode. The user sees everything they have access to in one scroll.

```
Connections
├── [aws-innovate] aws-innovate/repo-alpha    ✓ Connected    [Disconnect]
├── [aws-innovate] aws-innovate/repo-beta     ✓ Connected    [Disconnect]
├── [sophos-dev]   sophos-dev/platform        ✓ Connected    [Manage]
└── [personal]     myuser/side-project        ✓ Connected    [Disconnect]
```

### 5.3 "All Workspaces" Read Toggle

For dashboard/analytics views where showing everything at once is noisy:
- Default view: `is_active` tenant's data (the read-default purpose of `is_active`)
- Toggle: "Show all workspaces" expands to union of all member tenants
- Toggle state persists in localStorage, not in the backend

### 5.4 Switcher Demotion

The workspace switcher (#2982) becomes an **admin/support affordance**:
- Moves from header dropdown to Settings → Advanced → "Default workspace"
- Changes the `is_active` flag (which only affects read-defaults)
- Available to all users but not prominent — most never need it

### 5.5 Frontend API Changes

| API call | Current | Target |
|---|---|---|
| `GET /admin/connections` | Returns `is_active_tenant` boolean per item | Returns `tenant_id` + `tenant_name` per item; `is_active_tenant` deprecated (still sent for backward compat, removed after frontend migrates) |
| `DELETE /admin/connections/github/{id}` | Requires active-tenant match | Works on any connection where caller has membership + role. Frontend sends request; backend resolves. |
| `GET /admin/agents` | Scoped to active workspace | Returns agents from all member tenants (same fan-out pattern as connections) |
| Dashboard endpoints | Scoped to `org_id` from token | Accept optional `?tenant_id=` filter; default to `is_active` tenant; `?all=true` for union |

---

## 6. Explicit Non-Goals

The following are **out of scope** for the invisible-tenancy migration and will not be
addressed in any PR under this design:

| Non-goal | Rationale |
|---|---|
| **Billing semantics changes** | Billing remains per-tenant. The invisible UI doesn't change how costs are attributed — each request's resolved tenant is the billing unit. Billing UI may need a multi-tenant aggregation view eventually, but that's a separate product decision. |
| **Teammate auto-join (T3.2)** | Auto-join semantics (#2951 D3) are unchanged. Users still join tenants via the org-membership matcher at login. Invisible tenancy doesn't change WHO gets access, only how access is EXERCISED once granted. |
| **Rule 1 changes (tenant creation at registration)** | The three-rule model (#2951) is unchanged. Tenants are still created at App registration. Invisible tenancy only changes how users interact with existing tenants. |
| **Cross-tenant data migration** | If two tenants merge (#2954 Rule 3), data migration is a separate atomic operation. Invisible tenancy doesn't create new merge semantics. |
| **Multi-tenant billing dashboard** | A future product feature (see all orgs' spend in one view). Not required for invisible tenancy to ship — the per-org dashboard with "all workspaces" toggle is sufficient. |
| **Agent runtime changes** | Agent workers (`adp-dev-agent-scaledjob-role`) already receive their target tenant via the webhook event payload. Per-action resolution is a gateway-layer change; the agent runtime is unaffected. |
| **Platform-admin endpoint changes** | Endpoints gated by `require_platform_admin` have no tenant dimension — they're deployment-wide. No migration needed. |

---

## 7. Compatibility with B Quick Wins (#3070–#3073)

The B quick wins ship BEFORE invisible tenancy migrates any endpoint. This design
ensures they don't deepen session-coupling in ways that block migration:

| Quick win | Compatibility assessment |
|---|---|
| **#3070** (purge "tenant" from UI strings) | Fully compatible. Vocabulary alignment helps invisible tenancy — once it lands, even "workspace" may disappear from most surfaces. |
| **#3071** (switch endpoint + "Viewing" chip) | The switch endpoint (`POST /admin/connections/switch-tenant`) becomes the admin-only affordance under invisible tenancy. The "Viewing" chip becomes unnecessary (no read-only mode when everything is actionable). **Key output**: #3071's PR inventory of endpoints scoping by raw token org_id is a direct input to §4.2's migration list. |
| **#3072** (auto-switch on install) | Session-scoped behavior that becomes unnecessary under invisible tenancy (installs just add to the flat list). Implementation should isolate the auto-switch logic so it can be removed cleanly. |
| **#3073** (installed_by per-resource grant) | **Most aligned**. The `installed_by_user_id` column + `can_manage` server-side computation is exactly the per-resource authorization pattern invisible tenancy generalizes. This is why disconnect is the M1 migration target. |

---

## 8. Security Invariants

### 8.1 The Core Invariant

> A caller can act on a resource IFF they have a `TenantMembership` row for the
> resource's owning tenant AND their role in that tenant permits the action.

This is equivalent in strength to the current session-level guard:
- **Current**: you can only act on resources in your active tenant (which you have membership in by definition)
- **New**: you can act on resources in ANY tenant you have membership in (no switching required)

The difference is **convenience** (no mode-switching), not **security surface**
(membership is still the gate).

### 8.2 Attack Surfaces

| Attack | Current mitigation | Per-action mitigation |
|---|---|---|
| Mutation on unrelated tenant's resource | Session guard: token org_id must match resource org_id | Membership lookup: `SELECT 1 FROM tenant_memberships WHERE user_id=? AND tenant_id=?` must return a row |
| Privilege escalation via forged resource ID | Resource lookup returns 404 if ID invalid | Same — 404 before membership check even runs |
| Membership manipulation | Memberships created only by trusted paths (login matcher, install callback, admin) | Unchanged — same write paths |
| Stale membership after org removal | Not addressed (manual cleanup) | Same gap — needs a membership-sweep on org-removal (separate issue) |

### 8.3 Audit Trail

Every per-action resolution logs:
```json
{
  "event": "action_tenant_resolved",
  "user_id": "...",
  "resource_type": "connection",
  "resource_id": "144415968",
  "resolved_tenant": "aws-innovate",
  "caller_role": "member",
  "resource_grant": "installed_by",
  "action": "disconnect"
}
```

This is richer than the current audit trail (which only logs the session org_id).

---

## 9. Open Questions for Review

| # | Question | Proposed answer | Needs decision? |
|---|---|---|---|
| Q1 | Should per-action resolution be a **middleware** (runs on all routes) or a **per-route dependency** (explicit opt-in)? | Per-route dependency. Not all routes need it (platform-admin, unauthenticated callbacks). Middleware would over-apply. | No — per-route is clearly better. |
| Q2 | During parallel-guard period, should a mismatch DENY or LOG-ONLY? | DENY in first 2 PRs (M1–M2), then switch to LOG-ONLY once confidence is established. | Operator decision at deployment time. |
| Q3 | What happens to `users.org_id` (TenantMixin denormalization) long-term? | Stays. Too many tables use TenantMixin. `users.org_id` = "default tenant" and TenantMixin queries become "default-scoped" reads. Full decommissioning of TenantMixin is a v3 effort. | No — keeping is the safe choice. |
| Q4 | Knowledge assets already do per-resource auth (`_authorize_modify`). Should they migrate first as a proof-of-concept? | No — connections/disconnect (M1) is simpler and has #3073's per-resource grant already. Knowledge assets (M6) after confidence is established. | No. |

---

## 10. References

- **#3068** — Direction decision (Option A north star + B-now/A-later)
- **#2951** — Rev 4.1 current tenant model (D1–D7 locked decisions)
- **#3018** — Leak-guard + multi-tenant read fan-out (prototype for list endpoints)
- **#3070–#3073** — B quick wins (must stay compatible)
- **#2982** — Switch-tenant endpoint + switcher UI (demoted to admin under invisible tenancy)
- **#3071** — "Viewing" chip + switch endpoint (produces token-claim inventory for §4.2)
- **#2981** — EPIC (delivery tracking)
- **`modules/gateway/src/auth/org_id_resolver.py`** — Current `resolve_effective_org_id`
- **`modules/gateway/src/admin/connections/routes.py:218-246`** — Multi-tenant fan-out pattern
- **`modules/gateway/src/admin/connections/routes.py:254-286`** — Disconnect endpoint (M1 target)
- **`modules/gateway/alembic/versions/021_tenant_memberships.py`** — Partial unique index
- **`modules/gateway/src/shared/models/onboarding.py:53-87`** — TenantMembership model
- **`modules/gateway/infra/modules/cognito/lambda/pre_token_generation.py:89-128`** — Token claim injection
- **`modules/gateway/src/knowledge/routes.py:876-895`** — Existing per-resource auth pattern
