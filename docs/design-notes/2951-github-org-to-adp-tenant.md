# Design Note: GitHub-org → ADP-tenant model (Issue #2951)

> **Status**: Design-review (spike output) — **Revision 4** (friction redesign, App visibility/ownership model, delivery phasing)
> **Author**: @agent-architect (rev 1–3), design session with user 2026-07-05 (rev 4)
> **Date**: 2026-07-05 (rev 4)
> **Issue**: #2951 — create org tenant at App registration + auto-join by GitHub membership + multi-tenant users
> **EPIC**: #2981 (delivery tracking; native sub-issue tree)
> **Mode**: Per-issue review
> **Verdict**: ⚠️ Ready with caveats — the three-rule model is sound and reuses the
> right code. D5 (user-level many:many) is the heavy lift. See §Verdict.
>
> **Revision 4 note:** From-scratch friction review with the user. D1–D7 remain
> LOCKED and unchanged; this revision changes *delivery phasing*, the *onboarding
> flow*, and adds the *App visibility/ownership model* as new locked decisions
> **D8–D12** — see **§10**. Headlines: D5 delivery is phased (v1 data / v1.5
> switcher — "Variant A"); register→install becomes one chained same-tab journey
> (#2682 is a prerequisite); the manifest's `"public"` field becomes an operator
> choice (today hardcoded `False`, `service.py:745` — an unstated hard dependency
> this note previously missed: a private App can only ever be installed on the org
> that owns it, so multi-org was unreachable); enterprise multi-org = per-org
> private Apps via a deferred app registry; Rule 3 (#2954) deferred to v2. The 1h
> switch-token TTL question in §9 is resolved: 15 min + silent refresh (§10.D8).
>
> **Revision 3 note:** Validated all design claims against live code; aligned the session
> model between this note and child issue #2961 — switch-tenant uses **gateway-issued
> JWTs** (reusing `token_manager.py`), not Cognito attribute updates. No
> `AdminUserGlobalSignOut` complexity. All prior content unchanged.
>
> **Revision 2 note:** Incorporated the **locked decisions D1–D7** from #2951. D5 is
> in-scope, not deferred. Multi-org users must join ALL matching tenants (not
> first-match), requiring a user↔tenant membership table, active-tenant in session,
> tenant-scoping everywhere, and a switcher UI.

---

## 1. Problem (confirmed against live code)

On a fresh deploy nothing ever creates a tenant keyed to a GitHub org:

- **Registering** the App (`register_app_callback`, `service.py:943`) only writes
  Secrets Manager credentials + invalidates caches. It creates **no** `Organization`
  row. It doesn't even *see* `owner_type`/`org` — `register_app_start` stores the
  nonce with `channel_context=None` (`service.py:920`), so the callback has no memory
  of what the admin registered against. (It *can* recover org identity: the GitHub
  conversions response `data` at `service.py:1024` includes an `owner` object with
  `login`/`id`/`type`.)
- **Installing** the App (`install_callback`, `service.py:191`) attaches the
  installation to **the installer's own tenant** — `caller_org_id = user_row.org_id`
  (`service.py:257`), resolved from the nonce's user. So installing on `aws-innovate`
  from a `platform-admin` session attaches `aws-innovate`'s installation to the
  `platform-admin` tenant, exactly as the #2899 evidence shows.
- **Login** (`submit_access_request`, `handler.py:481`) calls
  `_find_matching_tenant_for_user`, which only matches orgs that already have a
  non-empty `github_installation_ids` **and** verifies membership with an
  **installation token** (`handler.py:222-256`). With no org tenant and no install,
  it returns `None` → username-slug fallback (`_pick_tenant_id`).

Net: the enterprise "everyone shares one tenant" path is unreachable on a fresh
deploy. This is a chicken-and-egg gap, not a bug. The three-rule model closes it.

## 2. Alignment with existing code (reuse audit)

The issue's reuse table is **accurate** — I verified each claim:

| Reuse claim | Verified location | Notes |
|---|---|---|
| Org/tenant/user schema + `github_installation_ids`, `member_approval_policy` | `shared/models/organization.py:10-31` | ✅ exists (migrations 005, 014) |
| Membership matcher | `handler.py:_find_matching_tenant_for_user:205` | ✅ but installation-token-gated (open Q1) |
| Attach-to-existing-tenant | `handler.py:_attach_user_to_existing_tenant:294` | ✅ **requires a Team to exist** (`:350-366`) — rule 1 must create it |
| Username fallback | `handler.py:_pick_tenant_id:416` | ✅ unchanged |
| Org-create pattern | `approval.py:approve_request:202` | ✅ creates Org+Tenant+Dept+Team+User+2×identity in one txn — copy this |
| Register hook | `service.py:register_app_callback:943` | ✅ but does not carry owner_type/org — see §1 |
| Prior design | `docs/design-719-multi-user-tenant-matching.md` | ✅ decided admin=Option C, auto-approve default, install-id keying |
| `user_roles` table (many:many role assignments) | `src/admin/models.py:24-36` | ✅ exists — `(user_id, role_id, org_id)` but NOT used for membership/session |
| Pre-token-generation Lambda | `infra/modules/cognito/lambda/pre_token_generation.py` | ✅ copies `custom:org_id` → JWT (single-valued today) |
| Token context middleware | `src/auth/middleware.py:512-634` | ✅ `TokenContextMiddleware` reads single `org_id` from JWT |
| Org-scoped route guard | `src/auth/middleware.py:213-233` | ✅ `require_organization_access()` checks `token_context.org_id` |

**New finding for D5:** The current session model is **strictly single-tenant per user**.
`users.org_id` is non-nullable, Cognito `custom:org_id` is a single attribute, and the
`TokenContext` carries one `org_id`. D5 requires a new membership table + session concept.
The existing `user_roles` table is close but is for RBAC roles, not tenant membership; it
will inform but not replace the new membership table.

## 3. Open questions — resolutions (revised for D1–D7 lock)

### Q1. Membership verification needs an installation token → **accept option (a): auto-join requires ≥1 install.** *(Unchanged)*

The matcher (`handler.py:238`) calls `check_org_membership` with an installation token
(`github_client.py:176`, `GET /orgs/{org}/members/{username}`). A tenant created at
register-time has no installation, so membership can't be verified until the App is
installed on ≥1 repo. Option (b) (org/app-level token) is a larger change and GitHub's
App JWT can't read org membership without an installation anyway.

**Decision:** register creates the *tenant shell*; auto-join-by-membership activates
once the App is installed (which populates `github_installation_ids` via the existing
`_append_installation_id_to_org`, `service.py:328`). Document this as an explicit
two-step: register → install. This matches the real operator flow in #2899 and needs
**no matcher change**. Rule 1 just has to guarantee the install attaches to the *org*
tenant, not the installer's tenant — see §4 Rule 1.b.

### Q2. Multi-org users → **JOIN ALL matching tenants (D5 LOCKED).** *(REVISED — was "first-match")*

**D5 is locked**: a verified member of multiple orgs that each have tenants must join
ALL of them and switch between them via a UI tenant-switcher with an "active tenant"
in the session.

**What this replaces:** The Revision 1 design said "first-match, deterministic ordering;
user→tenant many:many out of scope." This is now **wrong**. The three-rule model must
include multi-tenant membership from day one.

**Concrete changes required:**
1. **New `tenant_memberships` table** — replaces the concept of a single `users.org_id`
   as the membership record. (§4a details the schema.)
2. **`_find_matching_tenant_for_user` returns ALL matches**, not the first. The caller
   creates a membership row for each.
3. **Active-tenant in session** — Cognito `custom:org_id` becomes the "active tenant"
   (writeable at login and on switch). The pre-token-generation Lambda reads it; the
   middleware scopes as before.
4. **Tenant-switcher endpoint** — `POST /api/auth/switch-tenant` updates Cognito
   `custom:org_id` + returns a refreshed token.
5. **UI switcher** — dropdown in the header; calls the switch endpoint.
6. **Backfill migration** — existing single-tenant users get one membership row seeded
   from their `users.org_id`.

**Why this works without breaking existing code:**
- `users.org_id` stays (TenantMixin is used for query scoping across ~15 tables); it
  becomes a *denormalized copy* of the user's active tenant, kept in sync on switch.
- The JWT still carries one `custom:org_id` (the active tenant). Existing middleware
  (`TokenContextMiddleware`, `require_organization_access`) works unchanged.
- The membership table is the source of truth for "which tenants can this user access";
  the Cognito attribute + `users.org_id` is the "which one is currently active."

### Q3. New-tenant admin owner → **Option C (per 719).** *(Unchanged)*

Consistent with `docs/design-719-multi-user-tenant-matching.md` and
`_determine_role_for_matched_user` (`handler.py:259`), which already promotes GitHub
org admins to `org_admin`. Rule 1 creates the org tenant with **no human owner yet**
(register is a platform-admin action, and the platform admin is not necessarily a
member of the org). The first GitHub-org-admin to log in becomes `org_admin` via the
existing role check; platform admins always retain access via `require_platform_admin`.

### Q4. `owner_type=user` registration → **out of scope; do not create a tenant.** *(Unchanged)*

Personal-account registration (`owner_type=user`) should **not** create an org tenant.
Personal repos stay on the existing path: the user logs in, gets a username-slug tenant
(`_pick_tenant_id`), and installs attach to it (`install_callback` already routes
personal installs to `caller_org_id`, `service.py:303-307`). Rule 1's creation block
must be **guarded on `owner.type == "Organization"`** (from the conversions `owner`
object) to avoid minting a spurious org tenant for a personal App.

### Q5. Merge semantics (rule 3) → **attach-forward-only in v1.** *(Unchanged)*

When a platform admin links org B to tenant A: **new** logins from org B resolve to A;
users/repos already under B's own tenant are **not** auto-migrated in v1 (migration is
a separate, atomic, cross-store operation — same hazard class as #2950/#2755). v1 ships
attach-forward-only + a loud warning in the admin UI that pre-existing B users stay put.
A migration tool is a follow-up child issue.

## 4. The three rules — concrete design

### Rule 1 — Register org → create org tenant shell *(Unchanged from Rev 1)*

**1.a Carry owner identity into the callback.** `register_app_start` must persist
`owner_type` + `org` (and later the numeric owner id) so the callback knows what was
registered. Two options: (i) store them in the nonce `channel_context` JSON (currently
`None`, `service.py:920`), or (ii) read the `owner` object off the conversions response
(`service.py:1024`) — **prefer (ii)** because GitHub is the source of truth for the
numeric org id and login, and it needs no nonce-schema change. Extract
`data["owner"]["login"]`, `["id"]`, `["type"]`.

**1.b Create the tenant shell.** When `owner.type == "Organization"`, upsert an
`Organization` + `Tenant` + default `Department` + default `Team` (the Team is
**mandatory** — `_attach_user_to_existing_tenant` bails to `pending` without one,
`handler.py:350-366`). Copy the transaction shape from `approval.py:202-270`, minus the
User/identity rows (no human owner at register time — see Q3). Guard the whole block
behind a feature flag (rollback per issue's plan).

**1.c Keying.** ⚠️ **The issue says "key by stable numeric org id".** The live schema
has **no such column** — `Organization.id` is the tenant-id/login-slug and
`Organization.name` is the login (unique). Keying by numeric id requires a **migration**
(new nullable `github_org_id` column + index). Two paths:
- **v1 minimal (recommended):** keep `Organization.id = slug(login)` (consistent with
  every existing tenant and `_pick_tenant_id`), and **store the numeric org id in a new
  `github_org_id` column** for rename-safety and as the merge key for rule 3. This is
  one additive migration, low risk.
- Do **not** make `Organization.id` itself the numeric id — it would diverge from every
  existing tenant, break `_slugify_tenant_id` expectations, and complicate the SPA
  routing that assumes slug tenant-ids.

**1.d Fix the install attachment for org tenants.** Today `install_callback` attaches to
the *installer's* tenant (`service.py:257`). For the org-tenant model to work E2E, an
install on org X must attach to X's tenant (created in 1.b), not the platform-admin's
tenant. Resolve the target org by the installation's `github_org_id`
(`service.py:292`) → look up the `Organization` with matching `github_org_id`; fall
back to `caller_org_id` when none (personal installs / pre-existing behavior). **This is
the crux of the #2899 bug and must be in rule 1's scope.**

### Rule 2 — Login → join ALL matching org tenants, else username fallback *(REVISED for D5)*

**D5 changes the semantics:** a user joining an org tenant is no longer "pick one"; it's
"join all matching + set one as active." The flow becomes:

1. **Matcher returns ALL matching orgs.** `_find_matching_tenant_for_user` today returns
   the first match (`handler.py:246`). Change: collect all orgs where membership verifies,
   return a list (or iterator). This is a one-line semantic change (accumulate instead of
   early-return).

2. **Create membership rows for ALL matches.** For each matched org-tenant, create a
   `tenant_memberships` row (see §4a below) with role derived from
   `_determine_role_for_matched_user` (GitHub org-admin → `org_admin`, member → `member`).
   If a membership already exists, skip (idempotent on re-login per D7).

3. **Set active tenant.** On first login: the first matched org-tenant (by
   `created_at` ordering, deterministic). On subsequent login with an existing active
   tenant: keep the current active tenant if still valid; otherwise pick the
   first valid one. On no match: username-slug fallback (D6, unchanged).

4. **Attach user to the active tenant.** Call `_attach_user_to_existing_tenant` with
   the active org (the one whose `org_id` goes into `users.org_id` and Cognito). This
   satisfies the existing machinery — User row, Cognito attributes, identity-index
   write all target the active tenant.

5. **Fallback (D6).** If no org-tenant matches, fall through to `_pick_tenant_id` exactly
   as today. The user gets a username-slug personal tenant + one membership row for it.

6. **D7 (later org setup).** When a user already has a personal tenant and their org gets
   set up later, on next login the matcher finds the org-tenant → step 2 adds a new
   membership row → user now has TWO memberships (personal + org). Active-tenant stays at
   whatever it was (personal, by default); user can switch via the UI. No auto-merge.

**Flag dependency:** `submit_access_request` returns `status="unavailable"` while
`USER_IDENTITY_INDEX_V2_WRITE=false` (`handler.py:498`) — must be `true` on 979 (issue's
noted flag), else nothing onboards.

### Rule 2 supplement — D5 multi-tenant membership model (§4a)

This is the **heavy lift** for D5. Detailed in the child issue (#2961), summarized here.

#### Data model: `tenant_memberships` table

```sql
CREATE TABLE tenant_memberships (
    id          VARCHAR(255) PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     VARCHAR(255) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id   VARCHAR(255) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role        VARCHAR(32)  NOT NULL DEFAULT 'member',  -- 'org_admin' | 'member'
    is_active   BOOLEAN      NOT NULL DEFAULT FALSE,
    joined_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_active TIMESTAMPTZ,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ,

    CONSTRAINT uq_tenant_memberships_user_tenant UNIQUE (user_id, tenant_id),
    CONSTRAINT ck_one_active_per_user CHECK (
        -- enforced application-side; DB check is advisory
        -- (Postgres doesn't support conditional uniques natively without partial index)
        TRUE
    )
);

CREATE UNIQUE INDEX uq_tenant_memberships_active
    ON tenant_memberships (user_id) WHERE is_active = TRUE;
-- Guarantees exactly one active tenant per user at the DB level.

CREATE INDEX ix_tenant_memberships_tenant_id ON tenant_memberships (tenant_id);
CREATE INDEX ix_tenant_memberships_user_id ON tenant_memberships (user_id);
```

**Key design choices:**
- **`is_active` with partial unique index** guarantees exactly one active tenant per user
  at the DB level (`WHERE is_active = TRUE`). Switch-tenant is: `UPDATE SET is_active=FALSE
  WHERE user_id=X AND is_active=TRUE; UPDATE SET is_active=TRUE WHERE user_id=X AND
  tenant_id=Y` — two statements in one transaction.
- **`role`** stores the tenant-scoped role (distinct from `user_roles` which is for
  platform RBAC). Derived from GitHub org role on join.
- **`users.org_id` persists** as a denormalized copy of the active tenant — required by
  `TenantMixin`-based query scoping across ~15 tables + the Cognito attribute. Kept in
  sync by the switch-tenant endpoint.
- **No FK to `user_roles`** — membership and RBAC are separate concerns.

#### Session model: gateway-issued switch tokens

The active-tenant concept is implemented via **gateway-issued JWTs** (not Cognito
attribute updates). This leverages the existing `token_manager.py` (`src/auth/
token_manager.py:63-100`) which already issues gateway JWTs for service accounts.

1. **Initial login** — user authenticates via Cognito. The pre-token-generation Lambda
   injects `custom:org_id` = the user's home tenant (from `users.org_id`). This Cognito
   JWT is used for the initial session.
2. **Switch-tenant endpoint** (`POST /api/auth/switch-tenant {"tenant_id": "..."}`)
   validates the user has a `tenant_memberships` row for that tenant, then:
   - Validates membership: `SELECT 1 FROM tenant_memberships WHERE user_id=? AND tenant_id=?`
   - Updates `users.org_id` = new tenant_id (keeps TenantMixin scoping correct)
   - Updates `users.team_id` = default team in new tenant
   - Flips `is_active` in `tenant_memberships` (atomic: SET false WHERE user_id=X AND
     is_active=TRUE; SET true WHERE user_id=X AND tenant_id=Y)
   - Issues a **new gateway JWT** via `TokenManager.generate_token()` with
     `org_id = tenant_id` — short-lived (1h, configurable)
   - Returns: `{token, expires_at, tenant_id}`
3. **Frontend** stores the switch-token and uses it for all subsequent requests until
   expiry. On expiry, re-authenticates via Cognito (lands on home tenant) then re-switches
   if needed.

**Why gateway-issued tokens, not Cognito attribute updates:**
- No Cognito `admin_update_user_attributes` call on every switch (avoids latency + API
  throttling for frequent switchers)
- No `AdminUserGlobalSignOut` needed (old Cognito tokens are irrelevant once the frontend
  uses the gateway token)
- Existing `TokenContextMiddleware` (`middleware.py:512-634`) already validates both
  Cognito JWTs and gateway JWTs — same `TokenContext` output with `org_id`
- No pre-token-generation Lambda change needed
- `token_manager.py` already handles token generation, storage, and validation

**Trade-off accepted:** Gateway switch-tokens are shorter-lived than Cognito tokens.
Users who switch frequently will see more re-auth prompts if they stay idle beyond 1h.
This is acceptable — multi-tenant users are a small fraction, and the UX cost of a
silent re-auth is minimal vs. the complexity of Cognito attribute mutation.

**No Cognito Lambda change required.** The Lambda continues to inject `custom:org_id`
from the user's `users.org_id` (their home tenant). The switch-token overrides this at
the gateway layer.

#### Tenant-scoping: what changes

The existing middleware (`TokenContextMiddleware`, `require_organization_access`) already
scopes by `token_context.org_id` — which IS the active tenant. **No middleware change
needed.** The active-tenant-in-JWT model means every request is already scoped to the
active tenant.

The only additional check: when loading data (e.g., "show me my tenants" for the
switcher), the endpoint queries `tenant_memberships WHERE user_id=X` directly — this is
NOT scoped by `org_id` (it's user-scoped, not tenant-scoped). Guard this endpoint with
authentication only (no org-scoping), same pattern as `submit_access_request`.

#### Switcher UI

A dropdown in the top nav showing the user's current active tenant with the ability to
switch. Minimum viable:
- `GET /api/auth/tenants` → list of `{tenant_id, display_name, role, is_active}` from
  the membership table.
- `POST /api/auth/switch-tenant` → switch active + signal re-auth.
- Frontend renders the list; on selection, calls switch, refreshes token, reloads the
  app's data context.

#### Backfill migration

Existing users all have exactly one `org_id`. The migration:
1. For each user in `users`: insert into `tenant_memberships(user_id, tenant_id, role,
   is_active)` values `(user.id, user.org_id, user.role OR 'member', TRUE)`.
2. This is idempotent (UNIQUE constraint on `user_id, tenant_id` means re-running skips
   existing rows).
3. After backfill, the system reads from `tenant_memberships` as source of truth for
   "which tenants does this user have access to."
4. `users.org_id` continues to be written on every switch (denormalized cache).

### Rule 3 — Link multiple orgs to one tenant (many:many) *(Unchanged from Rev 1)*

⚠️ **No schema support today.** The matcher keys on a single `org.name`
(`handler.py:240`) and one `Organization` row == one tenant == one login. To have
`sophos` and `sophos-research` both resolve to the `sophos` tenant, you need one of:
- **A: `organizations.parent_tenant_id`** (nullable self-FK). `sophos-research`'s row
  points at `sophos`; the matcher resolves `parent_tenant_id or id`. Simple, one column,
  covers the stated use case. **Recommended for v1.**
- **B: A dedicated `tenant_org_links` table** (`tenant_id`, `github_org_id`,
  `github_login`, unique on `github_org_id`). True many:many, cleaner for reporting,
  but more surface. Defer unless A proves insufficient.

Plus a **platform-admin endpoint** (`POST /api/admin/tenants/{tenant_id}/orgs` /
`DELETE …/{github_org_id}`) guarded by `access.require_platform_admin`
(`access_control.py:253`, pattern at `connections/routes.py:260`), and a small admin UI
screen. Attach-forward-only semantics (Q5). Must write **both** Postgres and the DDB
identity-index atomically-ish (post-commit best-effort with a reconcile metric, same
pattern as `approval.py:280-303`) — the #2950 hazard applies.

## 5. Storage-layer & isolation check

- Org/tenant/link data → **Postgres** (relational, admin-UI-queried, join-heavy). ✅
  correct layer. No DynamoDB table needed for rules 1/3 themselves; the DDB
  identity-index writes are the existing write-through, not new tables.
- `tenant_memberships` → **Postgres** (ACID for the active-tenant flip, admin-queryable,
  join with users/orgs). ✅ correct.
- Tenant isolation: rule 1.d closes a **cross-tenant** hole (installs landing on the
  wrong tenant). Rule 3 merge must reject non-platform-admins (authz test required).
  D5 switcher: must validate membership before allowing switch (prevents cross-tenant
  access via crafted switch request).
- No new IAM surface (gateway already has SecretsManager + Cognito + DDB grants). No new
  infra → **no Terraform apply**; only `gateway-deploy.yml` on merge.

## 6. Migration verdict (correcting the issue)

The issue's Deployment section says *"Migration expected for D5."*
**Confirmed — three migrations total:**

| Change | Migration needed? |
|---|---|
| Rule 1 org-tenant shell creation | No (uses existing tables) |
| Rule 1.c numeric-org-id keying | **Yes** — additive `github_org_id` column + index (pattern: migration 014) |
| Rule 3 many:many link | **Yes** — `parent_tenant_id` column (option A) or `tenant_org_links` table (option B) |
| **D5 membership table** | **Yes** — new `tenant_memberships` table + partial unique index + backfill |
| Rule 2 wiring | No (code-only once memberships table exists) |

All are **additive, forward-compatible** (down-migration = drop table/column). The D5
backfill is a data migration (INSERT from users.org_id) that can run online with no
downtime (additive inserts, no column drops).

## 7. Hard dependencies (unchanged from issue, confirmed OPEN)

- **#2949** (OPEN) — webhook `GATEWAY_API_URL`/`INTERNAL_API_KEY_ARN` empty.
- **#2950** (OPEN) — install-callback writes Postgres only, not DDB identity-index.
- **`USER_IDENTITY_INDEX_V2_WRITE=true`** — gates the entire onboarding write path
  (`handler.py:498`, `approval.py:169`). Must be flipped on 979.

Rules 1–3 + D5 are gateway-code changes that can be **built and unit-tested** without
#2949/#2950, but the **E2E smoke test** (register → member login → shared tenant, then
webhook-driven agent run) needs them landed + the flag on.

## 8. Child-issue breakdown (revised)

| # | Child issue | Scope | Depends on | Status |
|---|---|---|---|---|
| 1 | **#2952** — Rule 1 + install-fix | Owner-identity extraction, org-tenant shell (Org+Tenant+Dept+Team), `github_org_id` migration, feature flag, `install_callback` org-routing fix | Nothing (unblocks all) | Filed |
| 2 | **#2953** — Rule 2 (revised for D5) | Matcher returns ALL matches → creates membership rows → sets active tenant. Join-all semantics per D5. | #2952, #2961 | Filed (updated) |
| 3 | **#2954** — Rule 3 many:many | `parent_tenant_id` migration, platform-admin attach/detach endpoint + UI, attach-forward-only | #2952 | Filed |
| 4 | **#2961** — D5: multi-tenant membership model | `tenant_memberships` table + backfill migration + switch-tenant endpoint + Cognito re-auth + switcher UI + `users.org_id` sync | #2952 | Filed |
| 5 | *(Deferred)* Cross-tenant migration tool | For rule-3 merges (Q5) — separate, atomic, not in v1 | #2954 | Deferred |

**Ordering:** #2952 → (#2961 in parallel with #2954) → #2953. Rule 2 (#2953) needs both
the tenant shell (#2952) and the membership table (#2961) to implement join-all. Rule 3
(#2954) is independent of D5.

## 9. Verdict

⚠️ **Ready with caveats.**

The three-rule model + D5 multi-tenant membership is the right shape and reuses the
right code. Proceed **provided**:

1. ✅ Rule 1 includes the `install_callback` org-routing fix (#2952)
2. ✅ The three additive migrations are accepted (github_org_id, parent_tenant_id, tenant_memberships)
3. ✅ Rule 1 creates Tenant+Dept+Team not just Organization
4. ✅ D5 membership model uses the `is_active` partial-unique-index pattern (not a separate "current tenant" table)
5. ✅ `users.org_id` persists as denormalized active-tenant (no TenantMixin refactor needed)
6. ✅ Switch-tenant uses gateway-issued JWTs (reuses `token_manager.py`) — no Cognito attribute mutation or GlobalSignOut needed
7. ⚠️ Hard deps #2949/#2950 + `USER_IDENTITY_INDEX_V2_WRITE=true` gate the E2E smoke test but not the build/unit work

**Key risk (D5):** The D5 migration touches the core session model. The partial-unique-index
pattern is well-proven (used in e.g. Stripe's multi-org model). Gateway-issued switch
tokens are short-lived (1h) so stale-tenant-scoping risk is bounded to the token TTL
window; the frontend discards old tokens immediately on switch. Accept 1h TTL or shorten
to 15min for tighter scoping.

---

## 10. Revision 4 — friction redesign, App visibility/ownership model, delivery phasing

> Source: from-scratch friction review, design session 2026-07-05. D1–D7 unchanged.
> Delivery is tracked in **EPIC #2981** (native sub-issue tree).

### 10.1 New locked decisions (D8–D12)

| # | Decision | Summary |
|---|----------|---------|
| **D8** | **D5 phased ("Variant A")** | v1 ships the `tenant_memberships` table, join-all writes at login, backfill, and deterministic first-match active tenant (#2961, reduced scope). v1.5 ships the switch-tenant endpoint, gateway switch-tokens, and the switcher UI (#2982). **TTL resolved: 15 min + silent refresh** — the frontend re-calls switch-tenant for a fresh token while the Cognito session is still valid; full re-auth only when Cognito itself expires. Rationale: membership *data* is cheap and keeps D5/D7 coherent (skipping it forces a lossy retroactive backfill and risks tenant-flipping bugs on matcher-order changes); the switcher UI only serves the multi-org minority and blocks nothing in the single-org E2E story. |
| **D9** | **Chained onboarding** | `register_app_callback` 302s directly to `github.com/apps/{slug}/installations/new`; `install_callback` lands back on the dashboard success state. One continuous **same-tab** journey (~90 s). **#2682 is a named prerequisite** (same-tab flow, callback-entry logging, orphaned-App recovery). Q1's "tenant shell inert until install" window shrinks to seconds — no matcher change needed; option (a) stands. Register happens once per deployment: subsequent orgs get an install-only entry point ("Connect your organization"). |
| **D10** | **App visibility is an operator choice** | The manifest's `"public"` field — today hardcoded `False` (`_build_app_manifest`, `service.py:745`) — becomes a registration-time toggle. **Private** = installable only on the App-owner org (GitHub-enforced single-org boundary), org-owned key: the enterprise/security posture. **Public** = unlimited orgs + personal-account installs (solo users), all sharing the deployment's key: the trial/PLG funnel. "Public" ≠ discoverable (no Marketplace listing; install still requires org-owner consent). Asymmetry: private→public is a GitHub settings flip anytime; public→private only while installed on ≤1 account. Public requires an **install-time tenant upsert** for unknown orgs (they never register) — in #2952's scope, including the no-caller-nonce case. |
| **D11** | **Enterprise multi-org = per-org private Apps (app registry), NOT a shared public App** | A shared public App means one `.pem` whose compromise exposes every installed org — indefensible to banks / security-conscious orgs. Instead each org wanting key isolation registers its **own private App** (owned by that org) via the same chained flow. The **app registry** (0–1 public App + N private org Apps per deployment; per-app webhook-secret resolution via `X-GitHub-Hook-Installation-Target-ID`; per-app token minting; one designated login App for Cognito OIDC) is **designed but deferred** (#2985); its entire v1 schema footprint is a `github_org_id`-adjacent `github_app_id` column + per-app secret naming, seeded in #2952 (~1 day). A hybrid deployment with zero public Apps *is* the pure enterprise mode; orgs installing the public App become **trial tenants** with a graduation path to their own private App (#2986, deferred; v1 answer: platform admin migrates manually). |
| **D12** | **Registration authz widens; GitHub arbitrates** | `register_app_start` moves from platform-admin-only to authenticated-user (platform admin OR org admin), with an optional deployment toggle to restrict back. Safety is GitHub-side: only an org *owner* can create an org-owned App, so GitHub validates authority over the target org regardless of ADP role. Q3/Option C already handles ownerless tenant shells; D4 gives each org its admins automatically (GitHub-role mirroring). Platform admin remains a multi-holder role for deployment operations — it is not the org-onboarding bottleneck. |

### 10.2 Supporting locks

- **Auto-join defaults ON** (D3 made explicit) with an org-admin toggle
  (`member_approval_policy`). GitHub org membership is already the trust boundary
  for code access.
- **Reflect-GitHub-state, don't snapshot**: repo lists refresh from the
  `installation_repositories` (and `installation`) webhook events — fixes the live
  979 stale-count bug (installation 144415968: GitHub has 2 repos, UI shows 1).
  "Manage repositories" deep-link visible to **all tenant members**; authz is
  delegated entirely to GitHub (org owners edit directly; members get GitHub's
  native request-to-add flow). **Disconnect stays org_admin-only.** (#2983)
- **Guidance states everywhere a user can stall** (#2984): post-install
  "what's-next" panel; pending-approval status page instead of a 403 dead-end;
  no-org banner — on private-App deployments "ask your org owner to connect it"
  + copyable link, on public-App deployments a "Connect your repos" install CTA
  (the solo funnel).
- **Personal tenants** (Q4 revised): on private-App deployments they are a
  *waiting room* (D6 + banner + D7 upgrade path; an org-private App cannot be
  installed on personal accounts, so no personal installs). On public-App
  deployments personal installs are **first-class**: they route to the username
  tenant via the existing `install_callback` caller-tenant path — now deliberate,
  not incidental.
- **Rule 3 (#2954) deferred to v2.** Nothing in the first-run journey needs it;
  the `github_org_id` column from #2952 preserves the merge key.

### 10.3 App / installation / tenant model (reference)

- **App ↔ deployment**: 1:1 today (registry makes it 1:N later). The App is the
  deployment's GitHub identity — credentials, webhook URL, agent commits.
- **App ↔ installations**: 1:N — one installation per org (or personal account).
  GitHub is idempotent per account: re-install edits the repo list, never creates
  a duplicate (979 evidence: AISuperPlane folded into installation 144415968).
- **Installation ↔ tenant**: 1:1, keyed by `github_org_id` (Rule 1.d) — what makes
  webhook routing unambiguous.
- **Login**: one App serves OAuth login per deployment (Cognito has a single OIDC
  provider). Under the registry, the first/designated App is the login App;
  subsequent org Apps are agent/webhook-only. Login identity ("who is this GitHub
  user") is org-independent, so one login App serving all orgs is correct.

### 10.4 Day-1 (v1) scope and phasing

Critical path **#2952 → #2961(v1) → #2953**, with #2682 in parallel; #2983/#2984
parallel after #2952. Hard deps unchanged (§7): #2950 + `USER_IDENTITY_INDEX_V2_WRITE=true`.

| Tier | Issue | Scope delta vs. Rev 3 |
|---|---|---|
| v1 | #2952 | + chained redirect (D9), + `public` toggle (D10), + install-time tenant upsert for unknown orgs, + `github_app_id` column & per-app secret naming seed |
| v1 (prereq) | #2682 | same-tab flow, callback-entry logging, orphaned-App recovery |
| v1 | #2961 | **reduced**: table + partial-unique active index + backfill + join-all writes only |
| v1 | #2953 | unchanged; deps now #2952 + #2961(v1) |
| v1 | #2983 (new) | repo-list freshness + Manage-repositories button |
| v1 | #2984 (new) | guidance states + auto-join default ON |
| v1.5 | #2982 (new) | switch endpoint, 15-min tokens + silent refresh, switcher UI |
| v2 | #2985 (new, deferred) | app registry — trigger: first org demanding key isolation on a shared deployment |
| v2 | #2986 (new, deferred) | trial-org graduation — trigger: first trial org wanting to graduate in place |
| v2 | #2954 | Rule 3 many:many (deferred) |

**Day-1 UX bar:** admin completes register→install in one ~90 s same-tab journey
ending in a green check; teammates sign in with GitHub and land directly in the
shared workspace; `@agent-developer` works. On a public-App deployment, a solo
user goes sign-in → "Connect your repos" → install → agents on their own repos in
~2 minutes.

### 10.5 Engineering risk notes (for the deferred registry)

The single-App assumption is smeared across **config**, not code: hardcoded
secret names (`adp/dev/github-app/adp-agent-platform-{id,key,meta}`), webhook
Lambda env, worker token minting. The registry refactor therefore needs a
compatibility window where old secret names still resolve. Per-app webhook
signature verification fails *silently* if the app→secret lookup is wrong (same
failure signature as #2950) — ship an unknown-app metric from day one. The
trial-org cutover (#2986) is a cross-store Postgres+DDB update — the #2950 hazard
class; use the post-commit best-effort + reconcile-metric pattern
(`approval.py:280-303`).
