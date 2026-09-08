# Design Note: Platform-native Org / Team / User structure — decoupling tenancy from GitHub orgs, AD-ready

> **Status**: Spike output — design note. **No code ships from this issue.**
> **Author**: @agent-architect
> **Date**: 2026-09-08
> **Issue**: #4828 — platform-native Org/Team/User, GitHub demoted to a connection, Active Directory / Entra readiness
> **Mode**: Per-issue spike
> **Verdict**: Design-complete on 8 of 9 questions. **§6 (repo→tenant dispatch) is blocked on rulings R3 / R4 / R10** — it collides with a shipped security invariant (migration 027 / #4070) *and* with an authorization check that treats a tenant id as a GitHub org login (`agent_trigger._repo_in_tenant`). Must not be implemented until those rulings land.
> **Child issues**: proposed in §6 as a table only. **Not filed** (per operator instruction on this issue).

---

## 0. Executive summary

The operator's direction is right, and the platform is **much closer to it than the brief assumes**. Three of the brief's premises are factually wrong against `main`, and one of its proposals collides head-on with a security invariant that shipped three weeks ago. Correcting these *shrinks* the work substantially and moves the risk to one place.

| Brief's premise | Code reality | Consequence for the spike |
|---|---|---|
| "teams are only a Cognito attribute (`custom:team_id`), not first-class records" | **Wrong.** `teams` and `departments` are real Postgres tables with `TenantMixin` (`shared/models/organization.py:79-106`), created in migration `001_initial_schema`. Full admin CRUD exists (`admin/routes.py:732-800`). `custom:team_id` is a *projection* of `users.team_id` written by the pre-token Lambda (`cognito/lambda/pre_token_generation.py:105`) | §2 is **largely already done**. No "promotion to DB rows", no attribute→table migration. The real gap is *membership cardinality* (one team per user) and the absence of a team-membership table — not first-classness |
| "admin CRUD for assigning users to orgs/teams" is to be designed | **Exists.** `POST /organizations`, `POST …/departments`, `POST …/departments/{id}/teams`, `POST …/teams/{id}/users`, `PUT …/users/{id}` (`admin/routes.py:150,648,732,803,873`). Org create is already **GitHub-free** — it never touches an installation (`admin/service.py:109-150`) | §3 is mostly a **frontend** gap (only 3 admin pages exist: `AccessRequests`, `IndexingStatus`, `TenantOrgLinks`), not a backend one |
| person anchor is `github:<numeric_id>`, needs "a generalized anchor" | **Already namespaced and already has a non-GitHub fallback.** `resolve_person_identity` returns `users:<canonical_id>` when no GitHub identity is linked (`budget/person_ledger.py:206`). The `github:` qualifier was introduced *explicitly* to prevent cross-provider id collision (`shared/identity/person_anchor.py:26-30`) | §4 is an **extension of an existing convention**, not a redesign. But there is a real asymmetry to fix — see §4.2 |
| repo→tenant mapping is needed because "one GitHub org hosts many platform orgs" | **Dispatch has no repo dimension at all**, and migration 027 enforces `UNIQUE (installation_id) WHERE ownership_disputed = false` as a deliberate anti-confusion guard (#4070). The resolver fails closed on `AMBIGUOUS` (`admin/installations/resolver.py:241-246`) | §6 is the **one genuinely hard, genuinely security-critical** part, and it is **not** a mapping-table exercise. It requires overturning or scoping a shipped invariant. **Rulings R3/R4/R10 required.** |

**The one-line finding:** platform-native orgs, teams, and admin-assigned users are ~70% shipped; what is actually missing is (a) team *membership* as a table, (b) admin UI, (c) a directory-provenance model, and (d) — the hard part — a repo-level dispatch key that does not reintroduce the cross-tenant installation-confusion class #4070 just closed.

**The finding the brief could not have anticipated:** the deepest GitHub coupling left in the platform is not a column or a table — it is that **a tenant id is sometimes literally a GitHub org login**, and one shipped authorization check *grants cross-repo agent dispatch on that string equality* (`agent_trigger.py:709-711`, docstring: *"Tenants are keyed by org login throughout the identity index"*). The webhook auto-register fail-open path writes exactly such ids (`handler.py:351`, `tenant_id = org_login`). Decoupling tenancy from GitHub orgs therefore has an authorization dimension, not just a data-model one — and §6's overlay is a **bypassable half-measure** unless that check is brought under the same mapping (**R10**, **R11**). Two further consequences fall out: the overlay must be readable from a Lambda with **no database driver** (§1.7), and allowing two orgs behind one GitHub org makes the tenant-less `correlation_store` key collide across a tenant boundary (§1.7c) — a leak class *created by the fix*, absent from the brief's table.

---

## 1. Current state, verified

Every claim below was read on `main` at `935094c`. Citations are `file:line`.

### 1.1 The tenant row is already internal — confirmed

`Organization.id` is a `String(255)` PK defaulting to `new_uuid` (`shared/models/organization.py:28`). GitHub linkage is carried in **separate, nullable** columns added later:

- `github_installation_ids` (JSON list) — `organization.py:35`
- `github_org_id` (nullable, indexed) — `organization.py:39`, migration `020`
- `github_app_id` (nullable) — `organization.py:41`
- `parent_tenant_id` (nullable self-FK, #2954 rule 3) — `organization.py:45-50`

**All GitHub coupling on the org row is already optional.** A tenant with every GitHub column NULL is a valid, fully-functional row. The synthetic test org is not a curiosity; it is the existence proof that the schema is already decoupled.

### 1.2 Org creation is already GitHub-free

`AdminService.create_organization` (`admin/service.py:109-150`) inserts an `Organization` and does a best-effort identity-index write-through. It never contacts GitHub. Gated by `Permission.ORG_CREATE` (`admin/routes.py:161`).

**Important side-effect, previously unremarked:** `create_organization` does **not** set `created_via`, so admin-created orgs take the column default `"operator"` (`organization.py:70-75`), which is in `TRUSTED_CREATED_VIA` (`organization.py:22`). Admin-created orgs therefore **already pass the #2724 provenance gate** and are promotable. This is correct behaviour and must be preserved deliberately rather than by accident — see ruling **R6**.

### 1.3 Teams and departments are first-class tables — the brief is wrong

```
Department(Base, TenantMixin)   organization.py:79    departments
Team(Base, TenantMixin)         organization.py:95    teams
User(Base, TenantMixin)         organization.py:109   users
```

`Team` carries `department_id` (`:99`), so the org-chart shape is already **org → department → team → user**. `User.team_id` is `nullable=False` (`:123`) — every user sits in exactly one team, and that is the real constraint to change.

`usage_logs` already carries `team_id`/`department_id` per #4487, so a per-row org-chart edge has precedent.

### 1.4 The directory-sync scaffolding already exists — and is vestigial

This is the most useful unremarked finding for AD readiness. `Department` and `Team` both carry:

- `identity_center_group_id` (`organization.py:89,103`) — legacy AWS Identity Center group linkage
- `cognito_group_name` (`organization.py:87`, departments only)
- `synced_at` (`organization.py:90,104`)

`User` also carries `identity_center_user_id` (`:131`) and `synced_at` (`:139`).

So the schema was **originally designed for an external directory as the source of truth** and retains the columns for it. `identity_center_group_id` is dead weight today (no reader outside the model); `cognito_group_name` is live for departments (`admin/service.py:829-1003`). An AD/Entra integration should **reuse the `synced_at` + external-id shape** rather than invent a parallel one — but must not overload `identity_center_*`, whose semantics are a different (retired) product. See §7 and ruling **R5**.

### 1.5 Membership: two models coexist, and `tenant_memberships` already has the provenance column

`TenantMembership` (`shared/models/onboarding.py:53-83`, migration `021`):

| Column | Note |
|---|---|
| `user_id` → `users.id` CASCADE | `:65-70` |
| `tenant_id` → `organizations.id` CASCADE | `:71-76` |
| `role` | `:77`, default `member` |
| `is_active` | `:78` — partial unique index `(user_id) WHERE is_active` = at most one active tenant |
| **`joined_via`** | `:79`, default `org_membership` — **the provenance column an AD sync needs already exists** |
| `github_org_id` | `:80`, nullable |
| `UNIQUE (user_id, tenant_id)` | `:62` |

Observed `joined_via` values in code: `org_membership` (`onboarding/approval.py:347`), `app_install` (`connections/service.py:1305`), **`admin_create`** (`identity/users_service.py:103`), `onboarding_approval` (`approval.py:73,172`), `admin_role_update` (`memberships.py:78`).

`admin_create` already exists. The externally-sourced-vs-admin-authored distinction §7 asks for is **one more value in a column that is already there**, not a schema change.

Note the redundancy: `users` carries `TenantMixin` (so `users.org_id`) *and* `tenant_memberships` exists. Per note 2951 §4a this is deliberate — `users.org_id` is a denormalized copy of the active tenant, kept because `TenantMixin` scoping spans ~15 tables. Authority for *admin-level* roles is the membership row (`identity/users_service.py:95-105`, #4006/#3987/#3998); the legacy `users.role` fallback is being retired. **New work must write memberships, not just `users.role`.**

### 1.5b Three isolation invariants are Postgres-only and invisible to CI — read this before adding constraints

This is the single most important *mechanical* finding for implementing anything in this note, because §2.1 and §6 both propose partial unique indexes.

The following invariants exist **only in Postgres**, are declared in raw SQL in migrations, and are **absent from the SQLAlchemy models**:

| Invariant | Migration | Not on model |
|---|---|---|
| `uq_tenant_memberships_one_active (user_id) WHERE is_active` | `021:52-54` | `onboarding.py:62` declares only the composite unique |
| `uq_users_cognito_sub (cognito_sub) WHERE NOT NULL` | `012:22-29` | declared (`organization.py:113-119`) |
| `uq_channel_tenant_map_installation_id` | `027:57-65` | `vault.py:208-211` declares only a non-unique index |

Tests run on SQLite via `Base.metadata.create_all`, which builds the **ORM** shape; production runs the **migration** shape. So a blind `INSERT ... is_active=true` passes CI and raises `IntegrityError` in production — a divergence explicitly warned about at `admin/memberships.py:13-21`.

**Consequence for this design:** the `team_memberships` primary-team partial index (§2.1) and the `repo_tenant_assignments` uniqueness (§6) will be **unenforced in tests** unless the invariant is *also* asserted at the application layer. Every new constraint here must ship with an application-level guard plus a migration-level test (the `tests/migrations/` pattern — note `034`, `036`, `037` have such tests and `035` does not).

### 1.5c `users.org_id` has no foreign key

`users.org_id` is a bare indexed column with **no FK** to `organizations.id` (`001:185,188`), while `tenant_memberships.tenant_id` *does* have one (`021:41`). The legacy single-org pointer is thus *less* constrained than the new many-to-many. Relevant to §2.1's decision to keep `users.team_id` as a denormalized pointer: that pointer should be treated as a cache with no referential guarantee, exactly as `users.org_id` already is.

### 1.5d Five org-creation paths, three of which inherit trusted provenance

Beyond the two admin endpoints, org creation happens in: `_upsert_org_tenant_shell` (`connections/service.py:3530-3641`), `approve_request` (`onboarding/approval.py:92-173`), and `bootstrap_first_admin` (`onboarding/bootstrap_admin.py:53-122` — which builds a **synthetic** access request with `provider="cognito"` and no GitHub at all; a second existence proof that GitHub-free tenancy works today).

Only `_upsert_org_tenant_shell` sets `created_via` explicitly. The other four inherit the `"operator"` default = maximally trusted. For the two platform-admin-gated endpoints that is defensible; for `approve_request` it means an approved access request yields the *same* provenance as an operator-provisioned org, so the trusted/untrusted distinction narrows in practice to "was it the no-nonce install callback or not" — narrower than the docstring at `organization.py:65-68` implies. **Ruling R6 should decide this deliberately rather than let it stand by inheritance.**

Also note a **second org-create API** exists (`POST /admin/identity/organizations`, `admin/identity/router.py:49`) with a different schema from `POST /admin/organizations`. Any new org lifecycle work must reconcile both or explicitly pick one.

### 1.5e Dead/vestigial structures adjacent to this work

- **`tenants` table is write-only** — two writers (`approval.py:103-107`, `connections/service.py:3613-3617`), **zero readers**; no `select(Tenant)` in `src/`. `tenant_memberships.tenant_id` FKs `organizations.id`, not `tenants.id`, confirming `organizations` is the real tenant. Do not build on `tenants`.
- **`user_roles` table exists with no code at all** — DDL live (`006:35-57`), ORM model deleted (`admin/models.py:24-33`) because it disagreed with the DDL on 6 of 9 columns. Not a reuse candidate.
- **`TokenContext.team_id` is populated end-to-end but read by no authz path** — it is only a budget-matching rung (`person_ledger.py:347-375`). So promoting teams to an authz dimension is *new* behaviour, not a re-wiring of something already load-bearing.
- **`users.team_id` is NOT NULL but written `""`** by shadow-user and approval paths (`internal/routes.py:349,359`, `approval.py:83`). §2.1's backfill must handle empty-string team ids, which are not valid `teams.id` values.

### 1.6 Person anchor: verified end to end

- **Read/fusion path:** `resolve_person_identity` (`budget/person_ledger.py:180-230`) → `f"github:{anchor_id}"` at `:230`, with fallback `f"users:{canonical_user_id}"` at `:206`.
- **Anchor id resolution is order-pinned:** `resolve_person_anchor_id` (`:157-177`) orders by `provider_user_id` ascending because `user_identities` has **no unique constraint on `(user_id, provider)`** — an unordered pick on the read side and another on the write side would key a cap under `github:A` and enforce under `github:B` (the #4511 inert-cap class). **Any new anchor namespace must adopt the same deterministic ordering rule.**
- **Authoring path:** `shared/identity/person_anchor.py`. `format_person_anchor` (`:80`) and `parse_person_anchor` (`:89`) are the single spelling point. `PERSON_ANCHOR_GITHUB_PREFIX` is pinned to `IdentityProvider.github` by a test (`:51-53`).
- **Durability:** `person_budget_configs.person_anchor` is `String(255) NOT NULL` with `UNIQUE (person_anchor, period_type)` (migration `034:84,100`). The anchor is **stored durably** in cap rows. The table has **no `org_id` at all** (asserted by `tests/migrations/test_034_person_budget_configs.py:187-197`) — it is deliberately partition-free.
- ⚠️ **The anchor string is composed in three places, and only the *prefix* constant is shared.** `format_person_anchor` (`person_anchor.py:87`) is the documented single source, but `person_ledger.py:230` hand-rolls `f"github:{anchor_id}"` and `enforcement_service.py:1569` hand-rolls `f"{PERSON_ANCHOR_GITHUB_PREFIX}{anchor_id}"`. Any namespace work must route all three through one composer first, or a write/read mismatch is near-certain.
- **`entity_key` is not a column.** Migration `035` creates an *index* on `(entity_type, entity_id)`; there is no `entity_key` column anywhere in the schema. The brief's phrasing implies otherwise — worth correcting so nobody designs a migration for it.
- **Ledger keys are NOT the anchor.** `budget_usage` rows are keyed `(org_id, entity_type, entity_id, period_type, period_start)` (`person_ledger.py:143-153`); `root_user` rows use canonical `users.id`, `user` rows use Cognito `sub` (`:233-244`). The anchor is a *join* key resolved at read time, not a stored ledger column.

**Therefore — the critical §4 answer:** changing or adding an anchor namespace **does not orphan spend history**, because history is keyed by `users.id`/`sub`, not by the anchor. It *would* orphan **caps** (`person_budget_configs` rows store the anchor string). This is a far smaller and more tractable blast radius than the brief assumes, and it is the precise thing a migration must handle.

### 1.7 Dispatch: keyed on `installation_id`, with **no repo dimension**

`identity_resolver.resolve_identity` (`webhook-ingress/lambda/common/identity_resolver.py`):

1. **Step 1** — installation → tenant, from the DDB identity-index; `org_id = tenant_item["org_id"]` (`:416`).
2. **Fallback** — Postgres via `POST /internal/v1/resolve-installation`, gated by `installation_gate` on provenance; a denied installation emits `AutoRegisterDenied` and returns `unknown_installation` (`:377-388`). On success it backfills DDB (`:399`).
3. **Step 1b** — drift safety-net: DDB vs Postgres, **trusts Postgres**, emits `InstallationTenantDrift` (`:430-445`).
4. **Not-found behaviour** — returns `(None, "unknown_installation")` (`:408,414`). **Fails closed.** Good.
5. **Step 2** — sender resolution, then the cross-tenant trigger gate: `trigger_policy` with `home_tenant_only` checking `member_org_ids`, fail-closed when absent (`:503-528`).

The only `repo` references in the resolver are in *comments* (`:499,503,547`). Repo appears in exactly one place platform-wide as a key:

```python
# sqs_publisher.py:61  MessageGroupId scoped per-run (tenant#repo#issue) — not per-tenant.
send_kwargs["MessageGroupId"] = f"{tenant_id}#{repo}#{issue}"[:128]
```

**That is a FIFO ordering group, not an authorization key.** The brief's "tenant#repo keying" refers to this; it carries no authz weight. Anyone reading the brief without checking would design against a key that does not exist.

Confirmed further: **webhook-ingress never touches Postgres.** `lambda/github/requirements.txt` is `boto3` only — no psycopg/sqlalchemy anywhere under `lambda/`. Postgres is reached solely over HTTPS via `gateway_client` (`POST /internal/v1/resolve-user`, `POST /internal/v1/resolve-installation`). So the entire tenant attribution of a GitHub webhook rests on **one DynamoDB item** — `identity_type=github_installation_id`, `identity_value=str(installation_id)` — plus an HTTPS fallback. Any repo-level overlay (§6) must therefore be readable from the Lambda, i.e. it needs either a DDB projection or a new internal endpoint. **A Postgres-only `repo_tenant_assignments` table is not reachable from the dispatch path.** This is a hard constraint on C5 that the brief does not mention.

#### 1.7a Correction: `resolve()` fails closed, but **auto-register fails OPEN**

My earlier reading ("fails closed, good") is right for `resolve()` and wrong for the self-heal path behind it. `installation_gate` (`gateway_client.py:329-394`) allows on **two** non-affirmative outcomes:

| gateway state | decision | reason |
|---|---|---|
| resolved + `created_via` ∈ {`operator`,`register_flow`} | allow | `trusted_provenance` |
| resolved + `install_autocreate` + `ORG_TENANT_AUTO_CREATE` | allow | `open_onboarding` |
| resolved + `install_autocreate`, flag off | **deny** | `self_created_shell` |
| resolved, provenance absent | **allow** | `provenance_unavailable` |
| `not_found` | **deny** | `not_a_known_tenant` |
| error / unreachable | **allow** | `gate_unavailable` |

On either fail-open branch `_auto_register_installation` does this (`handler.py:350-352`):

```python
_emit_metric("AutoRegisterGateUnavailable")
tenant_id = org_login          # ← the raw GitHub org login becomes the tenant id
authoritative = False
```

The blast radius is bounded — `authoritative=False` withholds per-tenant credential seeding (`handler.py:1445-1454`) and the metric is emitted — so this is a deliberate availability trade, not a hole. **But it is directly load-bearing for this issue**, see 1.7b.

#### 1.7b 🔴 The hardest coupling in the codebase: **tenant ids are sometimes literally GitHub org logins**

`tenant_id = org_login` above is not an edge case in the data model — it is a documented assumption *relied upon elsewhere*. `agent_trigger._repo_in_tenant` (`lambda/github/agent_trigger.py:682-732`) authorizes cross-repo agent dispatch on three signals, and **signal 2 is a string comparison of the repo owner against the tenant id**:

```python
owner = target_repo.split("/", 1)[0]
if owner == chain_tenant_id:
    return True
```

with the docstring stating plainly: *"Tenants are keyed by org login throughout the identity index, so this is the normal match for a sibling repo in the same org."*

**This is the single most consequential finding for issue #4828.** The issue's premise is that tenancy must be decoupled from GitHub orgs. But there is a shipped authorization check that *grants cross-repo dispatch* on the basis that a tenant id **is** a GitHub org login. Consequences:

- Platform-native orgs get UUID ids (`organization.py:28`, `default=new_uuid`), so signal 2 can never match for them. They degrade to signal 3 (`resolve_installation_for_tenant`), which fails closed — **correct, but silently more restrictive**. A GitHub-less org has no installation at all, so signal 3 also returns `None` → all cross-repo dispatch denied. Acceptable (fail-closed), but it must be a *stated* consequence, not a surprise.
- Far worse in the other direction: under R3=(a), if a platform org were ever *named* or keyed to match a GitHub org login, signal 2 would grant dispatch to **every repo in that GitHub org**, bypassing the repo overlay entirely. The overlay would narrow `resolve()` while `_repo_in_tenant` still widens `agent_trigger`.

**Therefore §6 is incomplete without `_repo_in_tenant`.** Any repo→tenant overlay must be consulted by *both* the webhook resolver and `_repo_in_tenant`, or the agent-trigger path becomes a bypass of the very mapping §6 introduces. This raises a new operator ruling (**R10**).

#### 1.7c The correlation store has no tenant in its key

`correlation_store.channel_key` (`correlation_store.py:52-57`):

```python
return f"{provider}:repo={repo},{kind}={number}"
```

PK is `channel_key` alone (`infra/dynamodb.tf:196-219`), read with `ConsistentRead=True`. **No tenant dimension.** Today that is safe *because* one repo belongs to exactly one tenant. The moment §6 allows two platform orgs behind one GitHub org, two orgs can legitimately touch the same repo — and their correlation pointers **collide on the same DDB item**, cross-wiring agent conversation threads across a tenant boundary.

This is a genuine new cross-tenant leak class that the brief's blast-radius table does not contain, and it is created *by the fix*, not present today. C5 must therefore include re-keying the correlation store to include the tenant (`f"{provider}:tenant={tenant},repo={repo},{kind}={number}"`) with a read-fallback to the legacy key during rollout. Recorded in §4.

### 1.8 The invariant §6 collides with

Migration `027_installation_tenant_uniqueness` creates:

```sql
CREATE UNIQUE INDEX CONCURRENTLY uq_channel_tenant_map_installation_id
    ON channel_tenant_map (installation_id)
 WHERE installation_id IS NOT NULL AND ownership_disputed = false
```

Its docstring is explicit that **one installation → one tenant** is the point (#4070, sub-EPIC #4068·A — "Cross-tenant GitHub App token/identity confusion, 1 CRIT + 2 HIGH"). The resolver fails closed as `AMBIGUOUS` when >1 tenant claims an installation (`admin/installations/resolver.py:207,241-246,312`), and `assert_installation_owned_by` raises on `NOT_FOUND`/`AMBIGUOUS`/`UNATTESTABLE` (`:59,336`).

`channel_tenant_map` does, however, already carry a `provider` column and a per-provider `provider_scope_id` whose docstring documents **four** value shapes including `personal:<gh_account_id>:<adp_user_id>` (`shared/models/vault.py:214-223`). So a compound scope key is an established pattern here — which is the seam §6 should use.

### 1.9 Sign-in

- Broker `_check_allowlist` (`lambda/github-auth-broker/handler.py:355-399`): `org` is **the only mode that grants**; `open` needs `ALLOW_OPEN_SIGNUP=true`; `explicit` is unimplemented and denies; unknown denies (#3986 fail-closed). `org` mode calls `check_org_membership(github_login, orgs, org_token)` against `ALLOWED_ORGS` — i.e. **GitHub is the gate today**, exactly as the brief says. Duplicated in two pre-signup Lambdas (`infra/modules/cognito/lambda/pre_signup.py:113-141`, `lambda/pre-signup/handler.py:115`) — three copies of this logic, all must move together.
- Cognito federation: **one** IdP, GitHub-as-OIDC (`infra/modules/cognito/github_idp.tf:34`), scopes `user:email read:org`. No SAML provider exists.
- `custom:team_id` is injected by `pre_token_generation.py:105` from the user's attributes; consumed by the agent-factory ingest Lambda (`gateway/lambdas/ingest/handler.py:270,343,366`).

### 1.10 AD / SCIM precedent: none

A repo-wide search for `scim|entra|active directory|azure ad` returns **zero** implementation hits (only unrelated substring noise). This is greenfield. The closest prior art is:

- **#3331** *[EPIC] Phase 3: Identity-Index Generalization* — provider-generalizing the DDB identity index (add `provider` to the PK or a GSI). **Directly overlapping; §4 and §7 must be designed as contributions to #3331, not around it.**
- **Note 3718** — GitLab portal SSO tenancy: the existing precedent for a non-GitHub identity provider.
- **`IdentityProvider`** (`shared/identity/providers.py`) — `cognito, github, slack, teams, discord, email, whatsapp`.

⚠️ **Gotcha for any AD provider addition:** `providers.py` claims "adding a new channel = one line in this set". **That is false as written.** Migration `009_provider_check_constraint` bakes `SUPPORTED_PROVIDERS` into a Postgres CHECK constraint *at that revision* (`009:21-34`). Adding a provider requires a **new migration** to rewrite the constraint, or every insert with the new provider fails. The docstring should be corrected.

---

## 2. Target model

The shape below is the minimum that satisfies the operator's direction while preserving every shipped invariant.

```
Organization  (platform-native, admin-created, GitHub columns all nullable)   ← EXISTS
   ├── Department                                                            ← EXISTS
   │      └── Team                                                           ← EXISTS
   ├── TenantMembership (user ↔ org, role, joined_via)                       ← EXISTS
   ├── TeamMembership   (user ↔ team, role, source)                          ← NEW (§2.1)
   └── OrgConnection    (org ↔ external system: GitHub org, AWS acct, …)     ← NEW/REFRAME (§1, §6)

Person (cross-org identity)  = anchor `<provider>:<id>`                       ← EXISTS, extend (§4)
DirectorySource (AD/Entra tenant + mapping rules)                             ← NEW, design-only (§7)
```

### 2.1 The one genuinely missing structural piece: team membership

`User.team_id` is `NOT NULL`, single-valued (`organization.py:123`). A corporate customer needs a person on more than one team (an SRE in "Platform" and "On-call"), and AD group sync is inherently many-to-many — a user is in N AD groups.

Recommended shape, mirroring `TenantMembership` exactly (same convention, per the "find ≥2 examples" rule — `tenant_memberships` and `user_roles` are both `(subject, scope, role)` tables):

```sql
CREATE TABLE team_memberships (
    id          VARCHAR(36)  PRIMARY KEY,
    user_id     VARCHAR(255) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    team_id     VARCHAR(255) NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    org_id      VARCHAR(255) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role        VARCHAR(32)  NOT NULL DEFAULT 'member',
    is_primary  BOOLEAN      NOT NULL DEFAULT false,
    source      VARCHAR(32)  NOT NULL DEFAULT 'admin',   -- see §7 precedence
    external_id VARCHAR(255),                            -- AD group objectId
    synced_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ,
    CONSTRAINT uq_team_memberships UNIQUE (user_id, team_id)
);
CREATE UNIQUE INDEX uq_team_memberships_primary ON team_memberships (user_id, org_id)
    WHERE is_primary = true;   -- exactly one primary team per user per org
```

`org_id` is denormalized onto the row deliberately: it lets the partial unique index scope "one primary per org" without a join, and it matches how `TenantMixin` scoping works everywhere else.

**`users.team_id` stays**, as the denormalized primary-team pointer — the same trick note 2951 §4a used for `users.org_id`. This is what keeps the migration additive: `custom:team_id`, the pre-token Lambda, and the agent-factory ingest Lambda (`ingest/handler.py:270`) all keep working untouched. Backfill = one row per existing user with `is_primary = true`, `source = 'admin'`.

Rollback: drop the table; `users.team_id` still carries the truth. Fully reversible.

---

## 3. Per-question design

### §1 — Org lifecycle without GitHub  ✅ mostly shipped

Admin-created orgs already work (§1.2). What is missing is **not** the lifecycle but the *reframe of GitHub as one connection among several*, plus a UI.

`Organization` already carries `aws_accounts` (JSON, `organization.py:30`) — a linked-resource list. GitHub linkage is instead spread across three scalar/list columns (`github_org_id`, `github_app_id`, `github_installation_ids`). The asymmetry is historical.

**Recommendation:** do **not** migrate the GitHub columns into a generic `org_connections` table in v1. They are load-bearing for the #4070 uniqueness guard, the provenance gate, and the identity-index write-through; moving them is a high-risk refactor with no user-visible benefit. Instead treat "GitHub is a connection" as a **presentation and lifecycle** claim (an org is created without it; it can be attached/detached later) and defer physical consolidation. Flag this explicitly so nobody reads "demote GitHub" as "restructure the org table". See ruling **R1**.

### §2 — Teams as first-class records  ✅ premise corrected

Already first-class (§1.3). Real work = `team_memberships` (§2.1) + admin UI. **No attribute→table migration exists to perform.**

### §3 — Membership management  ⚠️ backend exists, UI does not

Backend: `POST /organizations/{org}/teams/{team}/users` (`admin/routes.py:803`), `PUT …/users/{user_id}` (`:873`), `GET …/users` (`:826`), `GET …/teams/{team}/users` (`:849`). `AdminUsersService.create_user` already accepts **arbitrary provider identities** and writes `user_identities` rows with `verification_method="admin_manual"` (`identity/users_service.py:80-89`) — this is precisely the AD-provisioning seam, already built.

Multi-org membership: `tenant_memberships` handles it; #3074's invisible-tenancy direction is the committed end state.

Gap: the admin frontend has **three** pages (`pages/admin/`: `AccessRequests`, `IndexingStatus`, `TenantOrgLinks`). Org/dept/team/user management has **no UI**. Per the brief's §9 reuse intent, follow the #4691/#4745 panel patterns.

### §4 — Person identity beyond GitHub  ✅ extend, don't redesign

The anchor is already namespaced (`github:`) with a `users:` fallback (§1.6). The generalization is therefore: **admit additional provider namespaces into the same convention**, preserving GitHub-anchored enforcement byte-for-byte.

**Recommended anchor precedence** (deterministic, total order — required to avoid #4511):

1. `github:<numeric_id>` — if a GitHub identity is linked. **Unchanged, so all existing caps keep enforcing.**
2. `directory:<idp_object_id>` — if an AD/Entra identity is linked (stable, immutable, survives email/UPN change).
3. `users:<canonical_user_id>` — existing terminal fallback.

Why GitHub keeps priority 1: any reordering would silently re-key existing people and orphan live `person_budget_configs` rows. Priority is the whole safety property here.

**Two real defects to fix as part of this work:**

**(a) `parse_person_anchor` is GitHub-only and hard-rejects everything else** (`person_anchor.py:102-110`) — it raises unless the string starts with `github:`. But the read path *already produces* `users:<id>` (`person_ledger.py:206`). So a person with no GitHub identity gets a `users:`-shaped anchor from the read side that the authoring side **cannot parse**. Generalizing must make the parser namespace-aware over a registry of accepted prefixes, not just add one more `startswith`.

**(b) `user_identities` has no unique constraint on `(user_id, provider)`** (`person_ledger.py:160-167`). Today this is mitigated by an ordering convention (`ORDER BY provider_user_id ASC`) replicated in ≥3 places. Adding a provider multiplies the surface. **Recommendation: add the missing unique constraint** — or, if legitimate multi-account cases must persist, add a `is_primary` flag with a partial unique index. Ordering-by-convention across three call sites is how #4511 happened.

**Blast-radius correction:** because ledger rows are keyed by `users.id`/`sub` and *not* by the anchor (§1.6), an anchor change orphans **caps, not history**. A migration must re-key `person_budget_configs.person_anchor`; `budget_usage` needs no migration at all.

### §5 — Sign-in and org resolution  ⚠️ real change, three copies

Target: GitHub OAuth stays an *authentication* method; **authorization/org membership resolves from `tenant_memberships`**, not from a GitHub org-membership API call.

`ALLOWLIST_MODE` implications: `org` mode is the only granting mode and it gates on GitHub org membership (`handler.py:369-383`). Platform-native tenancy needs a **fourth mode** — e.g. `platform`: "allow if this GitHub identity resolves to a user with ≥1 `tenant_memberships` row". Note `explicit` is already a declared-but-unimplemented mode (`:393-395`); reusing that slot is tempting but its documented intent is a DDB allowlist — don't overload it.

⚠️ **The `mode` logic exists in three places** (§1.9): the broker plus two pre-signup Lambdas. #3986's fail-closed default and the CLAUDE.md-documented total-login-outage incident (`ALLOWLIST_MODE=open` without `ALLOW_OPEN_SIGNUP`) are both consequences of this surface. Any new mode must land in all three **in one change**, defaulting to deny on unknown, and must ship with the env var and the code in the *same* deploy unit — the outage in CLAUDE.md was caused precisely by code and env var landing separately.

⚠️ **The copies already disagree, which is a latent trap for this work.** The pre-signup trigger *does* implement `explicit` against a DynamoDB allowlist table, and its `open` branch auto-confirms **without** consulting `ALLOW_OPEN_SIGNUP` — whereas the broker denies `explicit` outright and requires the flag for `open`. On the broker path the trigger is dead code (`admin_create_user` never fires `PreSignUp_ExternalProvider`), which is why the divergence has gone unnoticed. Anyone adding a mode must not assume the three copies are equivalent.

**Terminology correction:** there is **no `custom:tenant_id` attribute anywhere** — `custom:org_id` *is* the tenant id. And per note 3074 §5, `custom:org_id` is a **claims cache, never authority**; authority is `tenant_memberships.role`. A membership-based org resolution is therefore *aligned* with the committed direction, not a departure from it.

**Prior commitment to respect:** SAML/Azure-AD SSO is currently an explicit **non-goal** in `docs/hosted-platform-design.md:72` ("future EPIC; GitHub sign-in covers the common case"). This issue is that future EPIC arriving — the non-goal should be formally superseded rather than silently contradicted.

AD auth half: add a Cognito SAML/OIDC IdP alongside `github_idp.tf`. Cognito supports multiple IdPs per pool, so this is additive. Note 2951 §10.3's "Cognito has a single OIDC provider" is about the *login App*, not a platform limit — but the assumption is load-bearing elsewhere and should be re-verified before relying on multi-IdP.

### §6 — Repo→tenant mapping  🔴 **BLOCKED — the security-critical core**

This is where the brief's proposal and shipped reality genuinely conflict, and it must not be hand-waved.

**The conflict.** The brief wants N platform orgs behind 1 GitHub org, which requires *sub-installation* routing granularity. But #4070 shipped `UNIQUE (installation_id)` (migration 027) and a fail-closed `AMBIGUOUS` resolver **specifically to make one installation resolve to exactly one tenant**, because the alternative was a confirmed CRIT cross-tenant token/identity confusion. Adding a second tenant behind one installation is, at the schema level, *precisely the state 027 exists to forbid*.

**What must not be done:** relax, drop, or widen `uq_channel_tenant_map_installation_id`. That reopens #4071's CRIT class.

**Direction that preserves the invariant.** Keep installation→tenant 1:1 as the *default* and introduce repo-level routing as a **strictly narrowing, explicit, deny-by-default overlay**:

- A new `repo_tenant_assignments` table keyed `UNIQUE (installation_id, repo_full_name)` → `org_id`, where every assigned `org_id` **must** be verified as sharing the installation's *connection owner*. Resolution order becomes: exact repo assignment → else the installation's 1:1 tenant (unchanged path) → else `unknown_installation` (unchanged fail-closed).
- The overlay only ever routes to an org the operator explicitly assigned, so an unassigned repo behaves **exactly as today**. That is what makes it non-regressive.
- `channel_tenant_map.provider_scope_id` already encodes compound scope keys including `personal:<gh_account_id>:<adp_user_id>` (`vault.py:214-223`), so a compound repo scope is an established convention — reuse it rather than inventing a second mapping store.

**Default/fallback tenant — recommend NO.** The brief floats a "default/fallback tenant". A default tenant means an unassigned or newly-created repo silently routes *somewhere*, which is the wrong-tenant-dispatch row of the brief's own blast-radius table. Recommend: **no default; unassigned repo under a multi-org installation fails closed** with a distinct `unassigned_repo` skip reason (the codebase already has a `skip_reasons.py` vocabulary). Silence is safer than a guess here.

**Interaction with `trigger_policy`.** `home_tenant_only` + `member_org_ids` (`identity_resolver.py:503-528`) is a *second*, independent gate that already fails closed. Repo assignment must compose with it, not replace it: assignment decides *which tenant*, `trigger_policy` decides *whether this sender may trigger there*. Both must pass.

**Three additional constraints surfaced by the dispatch trace (§1.7a–c) — all mandatory, none in the brief:**

1. **The overlay must be readable from the Lambda.** webhook-ingress has no database driver (§1.7). A Postgres-only table cannot be consulted at dispatch time. Either project assignments into the identity-index table as a third `identity_type` (e.g. `identity_type="github_repo"`, `identity_value="<installation_id>#<repo_full_name>"` — consistent with the existing composite-SK convention), or add `POST /internal/v1/resolve-repo`. **Recommend the DDB projection**, because the HTTPS path already fails open (§1.7a) and a fail-open lookup is unacceptable for an authorization narrowing. Postgres stays the system of record; DDB is the read replica, written through exactly as installation rows already are.

2. **`_repo_in_tenant` must consult the same overlay** (§1.7b), or `POST /agent/trigger` becomes a documented bypass: it grants cross-repo dispatch when `repo_owner == tenant_id`, on the explicit assumption that tenant ids *are* GitHub org logins. Two enforcement points, one mapping. See **R10**.

3. **The correlation store must be re-keyed to include the tenant** (§1.7c). Allowing two orgs behind one GitHub org makes `channel_key` — currently `provider:repo=…,issue=…` with no tenant — collide across a tenant boundary, cross-wiring conversation threads. This leak is *created by* §6 and must ship in the same change, with a legacy-key read-fallback for in-flight conversations.

**Also note the fail-open interaction.** Under R3=(a), a `gate_unavailable` auto-register writes `tenant_id = org_login` (§1.7a). If that org login collides with a platform-native org's id or name, the fail-open path can mint a routing row pointing at a real tenant. Recommend: when the repo overlay is enabled for an installation, the `org_login` fallback must be **disabled** for it — a multi-org installation has no unambiguous "the org", so guessing is exactly the bug class R4 rejects.

**This section is blocked on rulings R3/R4 and now R10** and should not be implemented before they land. It is also the section that most needs #4071/#4132 reviewer sign-off, per the issue's own Validation bar.

### §7 — AD / Entra readiness (design constraint only)

Provisioning model compatible with SCIM, built on columns that already exist:

- **Provenance:** reuse `tenant_memberships.joined_via` (already has `admin_create`); add e.g. `directory_sync`. Add the parallel `source` column to `team_memberships` (§2.1). **No new convention.**
- **Precedence:** externally-sourced rows are **directory-owned** — an admin edit to a `source='directory_sync'` membership must be either rejected or explicitly converted to `source='admin'` (an override that survives the next sync). Silent last-writer-wins is the "silent privilege drift" row of the brief's table. Recommend: reject-with-explain, plus an explicit "override" action that flips `source`.
- **Dedupe:** on directory `objectId` (immutable), never on email/UPN (mutable). Store it in `external_id` + `synced_at` — the shape §1.4 already established.
- **Do not reuse `identity_center_*`** columns: retired-product semantics, and overloading them makes two eras indistinguishable. See ruling **R5**.
- **Deprovisioning is the sharp edge:** an AD removal must revoke platform access, but a cascade delete of a user removes `tenant_memberships` and orphans `person_budget_configs` caps. Recommend soft-deactivate, never hard-delete, on the sync path.
- **This must be designed as part of #3331**, whose scope is exactly provider-generalizing the identity index.

### §8 — Migration / rollout

Every change above is **additive**: two new tables (`team_memberships`, `repo_tenant_assignments`), one new `joined_via`/`source` value, one new allowlist mode, one anchor namespace. No column drops, no type changes, no flag day.

Opt-in per tenant: an org with `github_org_id` set and no repo assignments behaves **bit-identically** to today. Platform-native features engage only when an admin creates an org without GitHub, adds a second team membership, or assigns a repo.

Rollback per piece: drop `team_memberships` (truth remains in `users.team_id`); drop `repo_tenant_assignments` (resolution falls back to the installation's 1:1 tenant); revert the allowlist mode (deny-on-unknown makes this safe); anchor namespace addition is code-only *except* the `person_budget_configs` re-key, which needs a documented down-migration.

⚠️ Migration-mechanics constraints, learned from shipped migrations: revision ids must be **≤32 chars** (`VARCHAR(32)`; SQLite hides the failure — the #4123 class, called out in `035`'s docstring), and `CREATE UNIQUE INDEX CONCURRENTLY` cannot run in a transaction — it needs `autocommit_block()` and must be split from any DML that must commit first (migration `027`'s docstring explains both).

### §9 — Reuse table

| Need | Already lives in | Decision |
|---|---|---|
| Tenant isolation | `TenantMixin` (`shared/models/base.py`) | **Unchanged** — stays the boundary |
| Org / dept / team / user CRUD | `admin/routes.py:150-956`, `admin/service.py` | **Extend**, don't fork |
| Org membership + provenance | `tenant_memberships` / `admin/memberships.py` | **Reuse**; add one `joined_via` value |
| Admin user create w/ arbitrary identities | `admin/identity/users_service.py:60-130` | **Reuse** — the AD-provisioning seam |
| Person anchor | `shared/identity/person_anchor.py` | **Extend** the namespace registry; fix §4(a)/(b) |
| Cross-org fusion | `budget/person_ledger.py:180-230` | **Reuse**; preserve ordering rule |
| Budget/routing ladders | #4690/#4692 | **Unchanged consumers** of org/team ids |
| Installation→tenant resolution | `admin/installations/resolver.py` | **Preserve fail-closed**; overlay only |
| Provider registry | `shared/identity/providers.py` + migration `009` | **Extend + new CHECK migration** |
| Identity-index generalization | **#3331** | **Design §4/§7 as part of it** |
| Admin UI panels | #4691/#4745 patterns | **Mirror** |

---

## 4. Blast-radius preventions

Per the issue's Validation bar, each bug class gets a **named structural** prevention — not "add a test".

| Bug class (from the issue) | Structural prevention |
|---|---|
| Webhook event mapped to the wrong tenant | Keep `uq_channel_tenant_map_installation_id` (027) intact; repo overlay is `UNIQUE (installation_id, repo_full_name)` and **strictly narrowing**; unassigned → fail closed with `unassigned_repo`, **no default tenant**; `trigger_policy` remains an independent second gate; overlay consulted at **both** enforcement points (`identity_resolver.resolve` *and* `agent_trigger._repo_in_tenant`) so neither can widen what the other narrows; `org_login` fail-open fallback disabled for overlay-enabled installations |
| *(added)* Agent-trigger bypasses the repo overlay | `_repo_in_tenant` signal 2 (`repo_owner == tenant_id`) is a GitHub-org-login string match (`agent_trigger.py:709-711`); overlay-enabled installations must **skip signal 2** and resolve through the overlay only — otherwise dispatch is granted to every repo in the GitHub org |
| *(added)* Two orgs behind one GitHub org cross-wire conversations | `correlation_store.channel_key` carries **no tenant** (`correlation_store.py:52-57`, PK = `channel_key`); re-key to include tenant with legacy-key read-fallback, shipped in the same change as the overlay |
| *(added)* Overlay unreadable at dispatch time | webhook-ingress has no DB driver; project assignments into the identity index (write-through, like installation rows) rather than adding a fail-open HTTPS lookup on an authorization narrowing |
| Login resolves the wrong org | Org membership reads `tenant_memberships` (FK-constrained to `organizations`), never a GitHub API answer; new allowlist mode lands in **all three** copies at once with deny-on-unknown preserved (#3986) |
| Person-anchor regression | `github:` keeps **priority 1** in the precedence order, so existing caps re-resolve to the identical string; add the missing `user_identities (user_id, provider)` unique constraint so anchor choice is a DB invariant, not a convention replicated in 3 files |
| Membership sync conflict (AD vs admin) | `source`/`joined_via` on every membership row + directory-owned rows reject silent admin edits, requiring an explicit override that flips `source`; dedupe on immutable `objectId`, never email |
| *(added)* AD deprovision orphans caps | Soft-deactivate only on the sync path; never cascade-delete a user with `person_budget_configs` rows |
| *(added)* New provider insert fails at runtime | Provider additions ship a **new CHECK-constraint migration** (009 is pinned at its revision) — correct the misleading "one line" docstring |

---

## 5. Operator rulings required

Numbered, each with options, recommendation, and consequence.

**R1 — Does "GitHub is a connection" require restructuring the org table now?**
(a) Presentation/lifecycle only; `github_*` columns stay ✅ **recommended** · (b) migrate to a generic `org_connections` table now.
*Consequence:* (b) touches the #4070 uniqueness guard, the #2724 provenance gate, and identity-index write-through simultaneously — high risk, no user-visible gain.

**R2 — Multi-team membership: ship `team_memberships` now, or defer?**
(a) Ship now, `users.team_id` becomes the primary-team pointer ✅ **recommended** · (b) defer; keep one team per user.
*Consequence:* (b) blocks AD group sync structurally (AD is inherently many-to-many) and leaves #4487's team rollup without an edge to filter on.

**R3 — 🔴 Do we accept N platform orgs behind 1 GitHub org at all?**
(a) Yes, via the strictly-narrowing repo overlay with the 1:1 invariant preserved ✅ **recommended** · (b) No — require one GitHub org per platform org (much simpler, but does not solve the SOPHOS case that motivated this issue).
*Consequence:* this is the load-bearing ruling; §6 cannot be implemented without it.

**R4 — 🔴 If R3=(a): confirm NO default/fallback tenant for unassigned repos.**
(a) Fail closed with `unassigned_repo` ✅ **strongly recommended** · (b) route to a configured default tenant.
*Consequence:* (b) *is* the wrong-tenant-dispatch bug class in the issue's own table — a new repo silently dispatches into whichever org is default. Requires #4071/#4132 reviewer sign-off either way.

**R5 — AD external ids: new columns, or reuse `identity_center_*`?**
(a) New `external_id`/`source` + reuse the `synced_at` shape ✅ **recommended** · (b) reuse `identity_center_group_id`.
*Consequence:* (b) conflates a retired product's semantics with AD and makes the two eras indistinguishable in data.

**R6 — Should admin-created orgs remain trusted provenance (`created_via='operator'`)?**
(a) Yes, but set it **explicitly** in `create_organization` rather than relying on the column default ✅ **recommended** · (b) introduce a distinct `admin_create` provenance value.
*Consequence:* today this is trusted **by default-inheritance, not by intent** (§1.2). (b) is cleaner but must be added to `TRUSTED_CREATED_VIA` *and* the webhook Lambda's mirrored copy — the two are a documented wire contract (`organization.py:9-13`) and must not diverge.

**R7 — Anchor precedence: is `github:` → `directory:` → `users:` accepted?**
(a) Yes ✅ **recommended** · (b) prefer `directory:` for AD-managed users.
*Consequence:* (b) silently re-keys anyone holding both identities and orphans their live caps.

**R8 — New allowlist mode name and rollout.**
(a) New `platform` mode ✅ **recommended** · (b) implement the dormant `explicit` slot.
*Consequence:* (b) overloads a slot whose documented intent is a DDB allowlist. Either way: all three Lambda copies in one change, env var and code in the same deploy unit (the CLAUDE.md outage precedent).

**R9 — Is this scoped under #3331 (Identity-Index Generalization)?**
(a) Yes — §4/§7 become #3331 children ✅ **recommended** · (b) independent track.
*Consequence:* (b) risks two parallel provider-generalization designs on the same DDB table.

**R10 — 🔴 If R3=(a): accept that `_repo_in_tenant` signal 2 must be disabled for overlay-enabled installations?**
(a) Yes — overlay-enabled installations resolve cross-repo dispatch through the overlay only, never the `repo_owner == tenant_id` string match ✅ **strongly recommended** · (b) leave `agent_trigger` untouched.
*Consequence:* (b) makes `POST /agent/trigger` a complete bypass of the §6 mapping — the overlay would narrow the webhook path while agent-trigger still grants every repo in the GitHub org. This ruling is new to this spike (the brief did not know `_repo_in_tenant` existed) and is co-equal with R4 in severity.

**R11 — Is a tenant id allowed to *be* a GitHub org login going forward?**
(a) No — deprecate the equivalence; tenant ids are opaque, and `_repo_in_tenant` signal 2 plus the `tenant_id = org_login` fail-open fallback are both retired on a timeline ✅ **recommended** · (b) keep it as a supported shape.
*Consequence:* the equivalence is the deepest remaining GitHub coupling in the platform (§1.7b) and is *load-bearing for authorization*, not merely cosmetic. (b) means "decouple tenancy from GitHub orgs" is not actually achieved — a GitHub org login remains an authorization-relevant tenant identifier indefinitely. Note (a) is a **multi-release deprecation**, not a single change: existing auto-registered tenants literally carry org-login ids today.

---

## 6. Proposed child breakdown — **NOT FILED**

Listed for operator sign-off only, per the instruction on this issue.

| # | Proposed child | Depends on | Gated by |
|---|---|---|---|
| C1 | `team_memberships` table + backfill + `users.team_id` as primary pointer | — | R2 |
| C2 | Admin UI: org / department / team / user management panels (#4691/#4745 patterns) | C1 | R1 |
| C3 | Explicit `created_via` on admin org create + connection attach/detach lifecycle | — | R1, R6 |
| C4 | Person-anchor namespace registry; fix GitHub-only `parse_person_anchor`; add `user_identities` unique constraint; `person_budget_configs` re-key migration | — | R7, R9 |
| C5 | 🔴 `repo_tenant_assignments` overlay + identity-index projection + admin surface + fail-closed `unassigned_repo` | C3 | **R3, R4** + #4071/#4132 review |
| C5b | 🔴 `_repo_in_tenant` consults the overlay; signal 2 disabled for overlay-enabled installations | C5 | **R10** |
| C5c | 🔴 Tenant-scoped `correlation_store.channel_key` + legacy-key read-fallback | C5 | R3 |
| C9 | Retire the tenant-id == GitHub-org-login equivalence (`_repo_in_tenant` signal 2, `tenant_id = org_login` fail-open) | C5b | **R11** |
| C6 | `platform` allowlist mode across all three Lambdas; membership-based org resolution | C1 | R8 |
| C7 | Cognito SAML/OIDC IdP for AD (auth half only; no connector) | C6 | R5 |
| C8 | Directory-sync provenance/precedence model (design → schema; no connector) | C1, C4 | R5, R9 |

Suggested order: C1 → (C2, C3, C4 parallel) → C6 → **C5 + C5b + C5c as one atomic slice** (after rulings) → (C7, C8) → C9.

⚠️ **C5/C5b/C5c must land together.** C5 alone introduces the correlation-key collision (§1.7c) and leaves the agent-trigger bypass (§1.7b) open. Shipping the overlay without both companions is strictly worse than shipping nothing.

---

## 7. Verdict

⚠️ **Design-complete with one blocked section.**

- §1, §2, §3 are **substantially already shipped**; the brief overstates the work. Remaining effort is `team_memberships` + admin UI.
- §4 is an **extension of an existing namespaced convention**, and the feared history-orphaning **does not apply** (ledger rows are not anchor-keyed). Two real latent defects surfaced (GitHub-only parser vs. `users:` producer; missing `user_identities` unique constraint) that should be fixed alongside.
- §5, §7, §8 are tractable and additive; §7 is greenfield and belongs under #3331.
- **§6 is not ready and must not be built** until R3/R4/R10 are ruled. It conflicts with a security invariant shipped three weeks ago (#4070/migration 027) whose whole purpose was closing a cross-tenant CRIT. The overlay direction preserves that invariant; a default fallback tenant would not.
- **§6 is also larger than the brief assumes**, in three ways the dispatch trace established: it needs a **second enforcement point** (`agent_trigger._repo_in_tenant`, else the overlay is bypassable), a **DDB projection** (webhook-ingress has no database driver, and its HTTPS fallback fails open — unacceptable for an authorization narrowing), and a **tenant-scoped correlation key** (else two orgs behind one GitHub org cross-wire conversation threads). C5, C5b, C5c must ship as one atomic slice; C5 alone is worse than nothing.
- **Two rulings are new to this spike and did not exist in the brief:** R10 (disable the org-login string match for overlay-enabled installations) and R11 (retire the tenant-id ≡ GitHub-org-login equivalence, a multi-release deprecation). R11 is the honest measure of whether "decouple tenancy from GitHub orgs" is actually achieved: without it, a GitHub org login remains an authorization-relevant tenant identifier indefinitely.
- **A CI blind spot affects everything proposed here:** three isolation invariants are Postgres-only partial indexes absent from the ORM models (§1.5b), and tests build the ORM shape on SQLite. Every new constraint in this note must ship with an application-layer guard *and* a `tests/migrations/` test, or it will be unenforced in CI while appearing to pass.

**Highest-value correction from this spike:** the brief's §6 premise of "tenant#repo keying" describes a FIFO `MessageGroupId` (`sqs_publisher.py:70`), not an authorization key. Dispatch authorization is `installation_id`-only. An implementer trusting the brief would have designed against a key that carries no authz weight — and might well have "fixed" uniqueness by relaxing the very index that closed #4071.
