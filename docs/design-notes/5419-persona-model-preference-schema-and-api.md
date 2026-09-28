# Design Note: Persona-Model Preference Schema, API and Audit (PMM-02, Issue #5419)

**Status:** proposed — reconciled with the **binding unified rulings** on #5417
(2026-09-18T16:42:26Z) and the focused review of this PR at head `e2c7d099`.
**Not** a merge authorisation and not an implementation authorisation. Every decision this
note previously referred to the operator is now **settled** (§12 records the answers and their
consequences). What remains is a small number of **cross-story reconciliations** — places where
a sibling story's current head must agree with this one — plus the scope re-pricing in §6.1,
which is now a stated fact about this story rather than an open question.
**Parent:** #5417 (EPIC). **Depends on:** #5418 (PMM-01), #5420 (PMM-03), #5433 (GPT/Codex
harness class — see §8).
**Scope of this note:** the storage, service layer, authenticated API and audit records
for per-principal persona→model preferences. No UI (#5422), no CLI (#5423), no snapshot
(#5424), no resolver wiring (#5425).
**Checked against:** `origin/main` at `ae598410` (the merge base of this branch), with the
Alembic head and every `file:line` citation re-verified at `c4809bb1` (2026-09-18) — the
current `origin/main` tip. Where the two differ, the note follows `c4809bb1`; §1 lists the
claims the issue text got wrong, and §6.1 records why the migration number must be re-derived
at implementation time rather than copied from here.

**This revision (6)** applies the focused review of 2026-09-18T18:09:53Z on top of the binding
#5417 unified rulings (2026-09-18T16:42:26Z). Per the review's sixth point, the running
revision-by-revision history has been **deleted rather than annotated**: superseded
alternatives are gone, so no reader can implement a withdrawn recommendation by reading past the
correction. §12 records what was decided; this block records only what changed here and the few
corrections whose *reasoning* is load-bearing enough that deleting it would invite re-litigation.

**What this revision changes:**

1. **`principal_kind` is back in the binding preference key** (§4.2). UNIQUE is now
   `(org_id, principal_kind, principal_id, persona_key)`. §4.2 states the cost this carries
   rather than assuming it away: kind is *derivable* from the canonical ID, so as key material it
   is redundant, and redundant key material widens what the database permits — it no longer
   refuses two rows for one canonical ID and one persona differing only in kind. The
   one-kind-per-canonical-ID invariant therefore **moves to the service layer**, where §7.3 names
   it and §11's AC-06 records that the DB-level half is now narrower. `principal_source` stays
   out of the key and remains evidence only. Checking the siblings made the direction easier to
   accept than the review alone did: **PMM-01, the epic note, has defined the key with kind in it
   throughout** (`5417:21`, `:143`, `:397` @ `b8045dbf`), so this restores alignment with the
   parent rather than trading one disagreement for another.
2. **A canonical service-principal *entity* is defined** (§4.6), separate from its aliases:
   `service_principals` carries `display_name`, `status` and lifecycle; `service_principal_aliases`
   carries the alias rows. §4.6 gives three reasons the split is required rather than tidier.
3. **Alias uniqueness now permits revoke-then-re-register** (§4.6.1) — partial over active rows
   only, with a **portable** `COALESCE(revoked_at, '')` expression index recommended over a
   Postgres-only `postgresql_where` partial index, because only the former is provable in CI.
4. **The canonical ID reaches handlers additively** (§5.2.2): one new optional
   `canonical_principal_id` field on `TokenContext`. `user_id` keeps its current meaning and
   value — it is read in 340 places in `src/` — and empty means "not resolved", never a silent
   fall back to the token subject.
5. **`updated_by_source` is declared as a column** (§4.1). Earlier revisions constrained it
   (§4.2), wrote it into the audit record (§5.5) and cited it in §9 without ever listing it in the
   table — a note that reads as complete but cannot be implemented as written.
6. **Migration renumbered and backfill specified** (§6.1): `055_persona_model_preferences.py`,
   `down_revision = "054_execution_tenant_guards"`, **four** tables, and an explicit
   registration-only (fail-closed) recommendation over a migration backfill that could only
   ever cover one of the three alias paths.
7. **§10.1 re-read against the siblings' current heads**, not the SHAs the previous revision
   quoted — all five have moved. Three of the conflicts it reported have since been fixed by the
   sibling itself and are recorded as closed rather than left asserting a dead disagreement. Two
   remain open, both internal to PMM-06 (`:708`, `:744`) rather than disagreements with this note.
   Two *new* staleness items in the siblings surfaced in the same pass and are now recorded:
   PMM-04 `:371` still describes this note's authority as platform-admin-only (B2 settled on a
   human org admin in-tenant), and PMM-06 `:578-580` still flags a key divergence that item 1
   closes. §7.1 and §5.3.1 were rewritten against PMM-03's and PMM-04's published contracts
   instead of paraphrases of them.
8. **Citations re-verified, and one had drifted.** `execution_store.py:788` was correct at this
   branch's merge base and is `:963` at `c4809bb1`; §4.4 now cites the current line and tells the
   reader to grep for the predicate rather than trust the number. Every other `file:line` in the
   note was checked at `c4809bb1` and holds.

**Three corrections kept because their reasoning still constrains implementation:**

- **`service_accounts` is live, and there are three service-principal subject forms, not one.**
  An early revision of this note claimed the Postgres table had "no production reader or writer"
  and keyed the schema on the DynamoDB `agent_name` on that basis. The table has full CRUD plus an
  authentication reader. This is why the schema keys on a **canonical** ID rather than any of the
  three subjects (§3.2).
- **A Cognito `client_id` is not identity-less, but it is not an identity either.**
  `Organization.cognito_client_ids` exists and is writable (`shared/models/organization.py:36`;
  `admin/service.py:337-338`) and is mirrored into the identity index
  (`admin/identity_index.py:601`). It is an **org-level approved-client list**, unread by any
  authentication path, naming no individual principal — several clients share one org entry. So
  it cannot disambiguate *which* principal is calling, and a Cognito caller must be registered
  as an alias before it can own a preference (§3.2.1, §7.3).
- **A non-tenant-scoped platform-defaults table needs no sentinel tenant.**
  `PersonBudgetDefault` (`shared/models/budget.py:128`) is `class …(Base)` **without**
  `TenantMixin`, created by `036_person_budget_defaults.py`. The objection that a platform row
  "would need a tenant" was wrong on the facts, which is why §8.3 stores the platform default in
  Postgres rather than configuration.

---

## 0. Executive summary

This story builds the persistence and API for one model choice per principal per persona, for
two principal kinds: a signed-in human and a registered service principal. Under the unified
rulings it also builds the **service-principal identity slice** those preferences are keyed on.
That is **four tables** (§4.1's preferences, §4.6's service-principal entity and its alias table,
and §8.3's non-tenant-scoped platform settings record — the first three are tenant-scoped), a service
layer, one self route surface shared by both caller kinds, one administration surface, a
discovery endpoint, and an audit trail.

**Every decision this note previously escalated is now settled** (§12). Tenant isolation,
concurrency, audit and migration/rollback resolve against existing verified platform precedent.
Two things are worth the reader's attention before implementation is scheduled.

**First: this story is materially larger than AC-01 was written for, and that is now a fact
rather than a question.** The rulings assign PMM-02 the canonical service-principal ID, the
alias registry, canonical resolution inside authentication, and the endpoint that tells an
administrator which principals they may manage (§4.6, §5.3.1). Those are *identity* mechanisms
with a stronger authorisation model than "who may pick a model" — the alias registry answers
"who may declare that this IAM role is that principal". The issue promised "one table,
additive, alters nothing"; the real change is four tables plus a change to the authentication
path, and existing machine identities need a defined route onto the scheme (§6.1 recommends
registration-only and fail-closed, because a backfill can only reach one of the three alias
paths). **AC-01 must be re-scoped to match before implementation starts** (§6.1) — not because
the design is wrong, but because a story that silently quadruples is how a wave slips.

**Second: the identity work has one honest limit the rulings cannot remove.** "Principals you
may manage" has **no predicate narrower than the whole tenant**, because `service_accounts` has
no owner or `created_by` column (`shared/models/organization.py:209-218`, verified). The
endpoint is buildable and correct for org-admin authority, but it must be **named and documented
as tenant enumeration**, not as per-caller entitlement, or it will read as a stronger guarantee
than it makes. A related pre-existing gap: the existing list endpoint at `auth/routes.py:346-370`
has **no authorization check at all** beyond authentication, while its sibling
`create_service_account` does gate (`:327`). That is not this story's to fix, but this story's
picker must not be built on it (§5.3.1).

**One correction carried from the second-pass review**, because it narrows a claim rather than
adding one: a Cognito `client_id` is not identity-less, but the org-level approved-client list
that mentions it is **not read during authentication and does not identify an individual
principal** (§3.2.1). So a Cognito caller must be registered as an alias before it can own a
preference — fail-closed until then (§7.3).

**Third: one invariant is no longer enforced by the database, and that is a deliberate trade.**
`principal_kind` is in the binding key by review direction (§4.2), which costs the constraint that
refused one canonical ID holding two kinds for one persona. The invariant is not abandoned — it
moves to the service layer, where §7.3 states it, names the one-word spelling difference between
this column's `service_account` and the runtime's `account_type == "service"` that would defeat it,
and §11's AC-06 records that the database-level half is now narrower than its wording suggests.

§2–§9 are buildable as written. §10 states what may run in parallel and §10.1 lists the
cross-story reconciliations, now down to two open items from five — three were closed by the
siblings themselves. §11 maps AC-01..AC-11 to a mechanism and marks what is not deterministically
provable. §12 records the settled decisions.

---

## 1. Stale claims in the issue, corrected

The issue was written against `18099d59`. Seven of its load-bearing claims have moved or
were wrong. Each correction changes the design, so none is cosmetic.

| # | Issue claim | Verified state at `c4809bb1` | Consequence |
|---|---|---|---|
| 1.1 | "Current head is `052_orchestration_executions.py`"; proposed file `053_persona_model_mappings.py` | **Stale twice over.** The head is `054_execution_tenant_guards` (`054_execution_tenant_guards.py:11-12`, `down_revision = "053_flow_slug_unique"`, re-verified at `c4809bb1`); `053` and `054` are both taken, and `053` is `053_orchestration_flow_slug_unique.py` rather than anything persona-related | Migration is `055_persona_model_preferences.py`, `down_revision = "054_execution_tenant_guards"` — **and the developer re-derives it rather than copying this number**, because the head moves faster than this note. §6.1 |
| 1.2 | Migration 037's unique index is guarded by `bind.dialect.name == "postgresql"` | **Wrong.** There is no dialect guard anywhere in `037_bedrock_account_routing.py`; the `COALESCE` expression index at `:209-219` is unconditional DDL, and SQLite supports it | The uniqueness invariant **is** provable in CI. §4.3 — this materially improves AC-01/AC-06 evidence |
| 1.3 | Reuse `security_audit_logs` "if its shape fits" | It fits only partly: `AuditLog` (`src/shared/models/audit.py:23-43`) has `actor_id` + JSON `details` but **no target and no before/after columns**. A second model `admin/models.py:59` has those columns but **no migration** and no writer — metadata only | Reuse `security_audit_logs`; carry target/previous/new in `details`. Do **not** adopt `audit_logs`. §5.5 |
| 1.4 | `service_accounts` is *the* service-account principal reference; authentication "surfaces as `account_type == "service"`" | Incomplete, not wrong. `account_type == "service"` is correct, but there is no single service-account identity: **three live paths produce three different subject forms** — Postgres `service_accounts.id` (`auth/tenant_resolver.py:284-303` → `entity_id`, minted as the JWT `sub` at `token_manager.py:85`, read back as `user_id` at `:184`), DynamoDB `agent_name` (`auth/agent_registry.py:256-263`), and Cognito `client_id` (`auth/auth_service.py:309-312`) | Was **B1**; settled by ruling 1. §3.2 — the schema stores one **canonical** principal ID that all three paths resolve to, not one of the three subject forms. Note the Cognito nuance corrected in §3.2.1: an org-level approved-client list does exist, but is unread by authentication and names no individual principal |
| 1.5 | Delegated administration by "an authorized service-account owner" | `service_accounts` has no owner/created_by/user_id column (`organization.py:209-218`); `Permission` (`admin/config.py:21-80`) has no service-account member | Was **B2**; settled as org-admin-in-tenant. §5.3 — AC-10 still needs rewording, since "owner" remains unimplementable |
| 1.6 | `persona_key` "must be validated against the authoritative catalogue, not a copy of it" | The gateway has **no** authoritative catalogue. Three diverging partial lists exist, and the authoritative 12 live in a separately deployed Lambda the gateway cannot import | Validation is an interface to PMM-03 (#5420), which owns the catalogue. §7.1 |
| 1.7 | "The platform has no ETag/`If-Match` support anywhere … concurrency must use a revision field in the body plus a 409" | **Confirmed**, and there is now a precedent to copy: hand-rolled integer compare-and-set in `orchestration/execution_store.py:963` | §4.4 adopts that precedent rather than inventing one |

Also note: **PMM-01's design note has not merged.** `docs/design-notes/5417-*.md` does not
exist on `origin/main`. The binding inputs for this note are therefore the six operator
rulings posted as comments on #5418 on 2026-09-18 (D1–D6), cited throughout as "D*n*".

---

## 2. What the locked rulings decide for this story

D1–D6 constrain this schema directly. Recording the consequence, not re-litigating:

- **D1 → no org/team columns.** Personal mapping *selects*; organization policy
  *constrains*. D1 states explicitly: "No org/team selection columns are required in the
  PMM-02 preference table." The table carries `org_id` as the **tenant partition** only
  (§4.1), never as a scope rung. This is the single biggest simplification versus migration
  037, which needed three scope columns and a `scope_type` CHECK.
- **D1 → absence is the only path to the default.** "After selecting the principal mapping
  (or system default when absent)". §4.5.
- **D2 → a saved mapping is fail-closed when broken.** Only *absence* selects the default.
  This is why save-time validation must reject rather than store (§7), and why `list`
  must distinguish five states (§5.4).
- **D3 → save-time validation is a real gate**, intersecting catalogue, tenant allowlist,
  harness compatibility, service-account restrictions and proven invocability. PMM-03 owns
  it; this story calls it. §7.
- **D4 → the platform default is `us.anthropic.claude-sonnet-4-6`**, which the synthesis
  qualifies as the **Claude-class candidate pending live proof** rather than a single global
  value. §8 — and note the gateway's own alias map currently pins `global.` profiles, not
  `us.` (§8.2).
- **D5 → snapshots are out of scope here** but constrain the read contract: PMM-06 needs a
  `policy_revision` it can bind into signed claims. §4.4 supplies it.
- **D6 → harness compatibility is part of "selectable".** It is a property PMM-03 reports
  and this story enforces at write time; the table stores no harness column, because the
  harness belongs to the persona's runtime, not to the preference. §7.2.

---

## 3. The principal model

A row must name a principal in a way that still means the same thing months later and
cannot be pointed at somebody else. That requires, per kind: which stored identifier is
canonical, which store proves it exists, and what happens when the principal disappears.

### 3.1 Human principals — settled

**Canonical identifier: `users.id`** (`String(255)`, `organization.py:130`).

The trap here is verified and has bitten before. `TokenContext.user_id`
(`shared/schemas/auth.py:50`) holds a **Cognito sub** on the JWT path, while `users.id` is
canonical. The field itself is an undocumented bare `str` — the sub semantics are recorded at
`auth/token_manager.py:184` (`user_id=token_claims.sub`) and
`shared/identity/resolver.py:77-78`, not on the schema, which is part of why this is easy to
get wrong. Comparing them directly makes every row silently never match (#4744), and a row
written with a sub "would read as a configured selection and govern no call" —
`self_routes.py`'s `_caller_id` docstring says exactly this about #4647.

**Reuse the existing resolution helper, not a new one.** `resolve_canonical_user_id`
(`shared/identity/resolver.py:64`, in `shared.identity` — **not** the same-named method on
`proxy/bedrock_routing.py:124`, which has a different signature and a nullable return)
returns canonical `users.id` and falls back to the raw
sub when no `users` row exists. That fallback is safe for *reads* but is **not** a licence
to write a phantom row: the write path must additionally confirm a real `users` row in the
caller's tenant before storing (§7.3), because unlike `bedrock_routing` — whose writes are
scoped by a real FK on `user_credentials.user_id` — this table has no such natural anchor.

Do **not** use `resolve_user_entity_id` (`:138`): it returns a *sub*, not `users.id`. The
two helpers are deliberately asymmetric and picking the wrong one reintroduces #4744.

**Pass `org_id=context.org_id` explicitly.** The helper's `org_id` kwarg is optional and
load-bearing: `resolver.py:88-97` constrains the lookup to `User.org_id` *only* when it is
supplied, and only then consults `workspace_user` for verified org-placement links. Omitting
it resolves against `cognito_sub` alone across every workspace. §7.3's independent in-tenant
`users` check and the `org_id`-leading UNIQUE both backstop an omission, so this is
defence-in-depth rather than a hole — but the call must name the authoritative workspace from
the authenticated context, which is exactly what the helper's own docstring warns about
("resolve the account in that workspace… never another workspace's account").

**One human subject form, stated as an invariant.** Two live paths authenticate a human: the
JWT path (Cognito sub, resolved as above) and the SigV4 path (`tenant_resolver.py:360`, which
sets `entity_id=user.id` directly). Both land on canonical `users.id`, so a single
`principal_source` value of `self` is sound today. Record that agreement as a **required
invariant** rather than relying on it as a coincidence: if either path ever stores a different
subject form for a human, `self` becomes a second polymorphic namespace and reintroduces the
exact B1 collision this design exists to prevent. Worth an assertion in the AC-04 test.

### 3.2 Service-account principals — settled by the synthesis (was B1)

The issue's premise — that `service_accounts.id` is *the* service-account identifier — does
not hold, but not because that table is dead. **A previous revision of this note claimed the
Postgres `service_accounts` table has "no production reader or writer". That was wrong and is
corrected here.** It has a full CRUD surface and a live authentication reader. The real
problem is the opposite of a dead table: there are **three** live service-account subject
forms and nothing distinguishes them.

#### 3.2.1 Verified inventory — three subject forms, three stores

| # | Authentication path | Subject stored in `TokenContext.user_id` | Evidence | `auth_source` |
|---|---|---|---|---|
| S1 | SigV4 → STS → Postgres role registration | **`service_accounts.id`** (row UUID) | `auth/tenant_resolver.py:284-303` resolves `iam_role_arn` → row and returns `entity_id=service_account.id`; `token_manager.py:85` mints it as the JWT `sub`; `:184` reads it back as `user_id` | `jwt` (default) |
| S2 | API Gateway SigV4 → Agent Registry | **`agent_name`** (DynamoDB) | `auth/agent_registry.py:256-263`, `agent_entry_to_token_context`: `user_id=entry["agent_name"]`, `account_type="service"`, `auth_source="iam"` | `iam` |
| S3 | Cognito `client_credentials` | **`client_id`** | `auth/auth_service.py:309-312`: "client_credentials tokens use client_id as the subject", `user_id = claims.client_id or claims.sub` | `jwt` |

**What exists on the Cognito side, stated precisely.** It is tempting to say an S3 `client_id`
"resolves to no ADP-side record carrying an `org_id`", and this note said so until the
second-pass review. That is too strong, and the narrower truth is the load-bearing one.
`Organization.cognito_client_ids` is a real, writable column
(`shared/models/organization.py:36`, a JSON list), maintained through the admin org-update path
(`admin/service.py:337-338`) and mirrored into the DynamoDB identity index as
`cognito_client_id → org_id` rows (`admin/identity_index.py:601`, via `sync_identities_for_org`).
So a tenant mapping for a client id can exist.

Three properties make it **unusable as a service-principal identity**, and they are why the
conclusion does not change:

1. **No authentication path reads it.** Verified absent: `cognito_client_ids` appears nowhere in
   `src/auth/` or `src/shared/identity/`. Every reference is in `src/admin/` org management or
   the identity-index writer. The S3 token path (`auth_service.py:295-312`) never consults it, so
   presenting a client id in the approved list confers nothing at authentication time.
2. **It is an org-level approved-client list, not a principal.** It answers "is this client
   approved for this tenant", one-to-many. It cannot answer "*which* principal is calling", so it
   cannot own a preference row — several clients sharing one org entry would share one owner.
3. **It carries no per-principal attributes** — no name, no status, no registration authority —
   so it cannot support the alias lifecycle (§3.3) or the "approved service identities" record
   ruling 1 requires.

**Consequence:** an S3 caller must be **registered as an alias** in this story's registry (§4.6)
before it may own or administer a preference; the approved-client list may be used as a
*precondition check* during that registration (the client must be approved for the tenant it
claims), but never as the identity itself. Until registered, S3 writes are refused (§7.3).

The Postgres table backing S1 is live, not inert:

- **Writers and readers:** `auth/service_account_service.py` implements create/get/update/
  delete/list plus role lookup (`:126`, `:200`, `:241`, `:277`, `:356`); `auth/routes.py`
  exposes `/service-accounts` CRUD (`:305`, `:347`, `:374`, `:398`, `:432`) and
  `admin/routes.py:1204-1260` exposes an org-scoped create/list/delete surface gated on
  `Permission.ORG_UPDATE` / `ORG_READ`.
- **Authentication reader:** `tenant_resolver._resolve_service_account` (`:261-305`).

Two caveats that matter for weighting S1, and neither makes it dead:

- The `/auth/exchange` credential-exchange endpoint that mints S1 tokens is **disabled by
  default** — `BG_ENABLE_LEGACY_AUTH_EXCHANGE` defaults to `false` and the route returns
  **410 Gone** (`auth/routes.py:54`, `:118-131`). So S1 *token minting* is off unless an
  operator enables it, but the CRUD surface that creates the rows is always on, and an
  already-issued S1 token still validates.
- `service_accounts` rows are therefore real, addressable and administered today, while the
  caller identity they anchor is gated behind a flag.

#### 3.2.2 Why this is a harder problem than the issue assumed

Storing any single one of the three forms produces a table that is silently correct for one
authentication path and silently inert for the other two — rows that read as configured and
govern nothing, which is exactly the #4511 class this epic exists to prevent. Worse, the
three namespaces are **not mutually exclusive**: all three arrive as a bare string in the
same `TokenContext.user_id` field (`shared/schemas/auth.py:50`) with `account_type ==
"service"`, and nothing on the context records which path produced it. A UUID, an
`agent_name` and a Cognito `client_id` are distinguishable only by shape, which is not a
contract. Storing an unqualified polymorphic string means a row written for one principal
can be matched by a different principal that happens to present the same string.

`auth_source` partially discriminates (S2 is `iam`; S1 and S3 are both `jwt`), so it is
**not** sufficient on its own to separate S1 from S3.

#### 3.2.3 The settled contract: alias → one immutable canonical service principal ID

The synthesis direction of 2026-09-18T14:04:49Z decides this, and chooses the alias mechanism
over the source-qualified pair that earlier revisions of this note leaned toward:

> "Add a canonical service-principal/alias contract so IAM role registrations, Agent Registry
> names, Cognito client IDs and approved service identities resolve server-side to one
> immutable service principal ID. The self API derives it from authentication; admin APIs
> accept only IDs returned by the manageable-principals endpoint. No raw caller-supplied alias
> becomes a preference owner."

Design consequences for this story, each stated as a requirement:

1. **The preference row stores the canonical ID only.** Not the alias, not a source-qualified
   pair. `principal_id` holds an ADP-minted immutable identifier, and the three subject forms
   of §3.2.1 become *aliases* pointing at it. This is what makes the four-story agreement of
   §10 achievable: PMM-04, PMM-05 and PMM-06 all name the same value.
2. **`principal_source` stays in the schema, but changes meaning.** It no longer disambiguates
   the key — the canonical ID is unambiguous by construction. It is retained as **provenance**:
   which alias path authenticated the caller at the moment the row was written. It is therefore
   dropped from the UNIQUE constraint (§4.2) and kept as an audited attribute. Leaving it in
   the key would defeat the contract by allowing one canonical principal to hold two
   preference rows for one persona, reached by two different alias paths.
3. **Resolution is server-side and total.** Every authentication path must resolve to a
   canonical ID *before* any preference read or write. A caller-presented alias is an input to
   resolution, never an owner — the synthesis's "no raw caller-supplied alias becomes a
   preference owner" is the same structural rule as §5.2's no-target-parameter property, one
   layer down.
4. **Immutability is the property that does the work.** "Immutable" must mean the canonical ID
   survives re-registration of an alias. This is precisely what the bare subject forms could
   not do: §3.3 records that an S2 `agent_name` and an S3 `client_id` are **reusable names**,
   so a re-registered name would otherwise silently inherit the previous principal's
   preferences. The alias table is what makes re-registration a *re-binding decision* rather
   than a silent inheritance — and that decision needs an answer (§12, decision 2).

**Still unacceptable in any variant:** a raw caller-supplied principal ID, and an unqualified
polymorphic string relying on shape to disambiguate.

**The S3 case is settled by ruling 1, and the corrected fact makes it cleaner rather than
sharper.** Ruling 1 states it directly: "a client must be registered and tenant-bound before
service-self preference access." An approved-client entry exists but is org-level, unread by
authentication, and names no individual principal (§3.2.1) — so registration is the tenant
anchor, and an unregistered S3 caller is refused (§7.3). Registration **may** verify that the
client id appears in the claimed tenant's approved list as a precondition, which is a genuine
use for that column; it does not make the column an identity.

#### 3.2.4 Scope consequence: the alias table is a second table, and PMM-02 owns it

Stated plainly because the issue's AC-01 was written against one table: an alias→canonical
contract **requires a second table** — `(org_id, alias_source, alias_id) → canonical_principal_id`
(tenant-scoped and source-qualified per ruling 1; see §4.6 for why global keying is unsafe), with
its own write authority, since who may register an alias is an authorisation question, not a data
question.

**Ruling 1 assigns this to PMM-02**, together with canonical resolution inside authentication and
the manageable-principals endpoint. The ownership question earlier revisions asked is therefore
closed. What does *not* close is the consequence: this story's "one table, additive, alters
nothing" contract **no longer describes it**, and the change now reaches the authentication path
(§5.2.1), not just new tables. AC-01 must be re-scoped before implementation (§6.1). Three other
stories — PMM-04, PMM-05, PMM-06 — cannot resolve a canonical ID until this table exists, so
PMM-02's schedule is on their critical path (§10).

### 3.3 Principal lifecycle — the "unvalidated free text" failure (AC-03)

AC-03's stated failure is "rows survive their principal's deletion and later resolve to a
different or absent identity". No principal kind gives a database FK here: `users` is
same-database but `TenantMixin` carries no FK either (§5.6), the S2 agent registry is
DynamoDB, and the S3 Cognito client has no *per-principal* ADP-side record — the org-level
approved-client list exists but names no individual principal and is unread at authentication
(§3.2.1). Two requirements follow, and they must be explicit because no constraint enforces them:

1. **Write time:** refuse unless the principal resolves to a real, in-tenant identity
   (§7.3). This is what AC-03 tests.
2. **Read time:** resolution is by `(org_id, principal_kind, principal_id, persona_key)` where
   `principal_id` is the canonical ID (§4.2). The synthesis's immutability requirement changes this
   analysis for the better, but does not make it disappear — it **relocates** it to the alias
   table. `users.id` and `service_accounts.id` are UUIDs never reissued; but an S2 `agent_name`
   and an S3 `client_id` are **reusable names**, so the risk is now precisely: *does
   re-registering a reused alias name point at the same canonical principal, or a new one?*
   - Pointing at the **same** canonical ID means a new operator registering a recycled agent
     name silently inherits the previous principal's model preferences.
   - Pointing at a **new** canonical ID means the old preference rows become orphans, which is
     inert and safe — they govern nothing, because no alias resolves to that canonical ID any
     more.

   The second is the fail-closed reading and this note recommends it, but the alias contract
   must state which it is, because the two differ in exactly the #4511 direction: governing
   somebody who was never meant to be governed. A cleanup job for orphaned rows is **not** in
   scope; orphans are inert under the recommended reading. This is §12, decision 2.

### 3.4 What `principal_kind` may be

Exactly two values, `human` and `service_account`, enforced by a CHECK constraint (§4.2).
Note the value is **not** the same string as `TokenContext.account_type` (`"human"` /
`"service"`). The mapping is explicit in the service layer; `account_type` is a bare `str`
with no enum or validator (`shared/schemas/auth.py:54`), and out-of-contract values
(`"cognito"`, `"unknown"`) exist elsewhere in the tree, so the service must map known
values and refuse anything else rather than passing the token's string through.

`principal_kind` answers "human or machine". Under the settled alias contract it no longer has
to answer "which identity store", because the canonical ID is unambiguous without it
(§3.2.3). `principal_source` is retained as **provenance** — which alias path authenticated the
write — with its own closed set (`self` for a human, and `sa_registration` / `agent_registry` /
`oauth_client` for the three service paths). Keep both columns, and keep the division of labour
explicit: `principal_kind` is key material (§4.2), `principal_source` is evidence only and no
read ever consults it.

---

## 4. Storage

### 4.1 Table

Proposed `persona_model_preferences`, tenant-scoped via `TenantMixin`:

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `String(255)` PK | no | `default=new_uuid`. Matches 037's string-PK shape |
| `org_id` | `String(255)`, indexed | no | From `TenantMixin` (`base.py:15`). The **tenant partition**, not a scope rung (D1) |
| `principal_kind` | `String(16)` | no | `human` \| `service_account`, CHECK-constrained |
| `principal_source` | `String(32)` | no | **Provenance only** (§3.2.3 item 2) — which alias path authenticated the write: `self` \| `sa_registration` \| `agent_registry` \| `oauth_client`. CHECK-constrained. Derived server-side from the authenticated context, never from the request. **It must not participate in selection**: no read, resolution or precedence decision may branch on it (§4.2) |
| `principal_id` | `String(255)` | no | For a human, canonical `users.id` (§3.1). For a service principal, the **immutable canonical service principal ID** resolved from an alias (§3.2.3) — never the alias itself |
| `persona_key` | `String(64)` | no | Validated against PMM-03's catalogue (§7.1) |
| `canonical_model_id` | `String(255)` | no | Versioned identifier, never a family alias (§8.1) |
| `requested_alias` | `String(128)` | yes | Display only — what the caller typed. Never resolved from |
| `revision` | `Integer`, `server_default="1"` | no | Compare-and-set fence (§4.4) |
| `created_at` / `updated_at` | `DateTime(timezone=True)`, `server_default=now()` | no | 037's shape |
| `updated_by` | `String(255)` | no | The acting principal as a **canonical ID only** — canonical `users.id` for a human actor, or the `canonical_service_principal_id` for a service actor. **A raw service subject (an S1 row UUID, an S2 `agent_name`, an S3 `client_id`) or any presented alias must never be stored here.** Resolve the actor through the same alias contract as the subject (§3.2.3) before writing |
| `updated_by_source` | `String(32)` | no | **Provenance of the *actor*** — which alias path the caller who wrote this row authenticated through: `self` \| `sa_registration` \| `agent_registry` \| `oauth_client`. Same closed set and CHECK as `principal_source`, and the same prohibition: evidence only, never consulted by a read. Distinct from `principal_source`, which is the provenance of the row's *subject* — the two differ on every administrative write, where a human administrator (`self`) edits a service principal's row (`agent_registry`), and that difference is the only stored evidence separating a self-change from an administered one |

**No `active`/`disabled` column.** The issue lists it as "if soft retirement is required".
It is not: reset **removes the row** (D1/D2 — absence is the only path to the default), so a
disabled row would be a second, contradictory way to express absence. This mirrors 037's
reasoning for having no `platform` rung value: "A platform row would be a second,
contradictory way to express the fallback" (`037:87-89`). Retirement of a *model* is
PMM-03's concern and is reported at read time, not stored here.

**No harness column** — see §7.2.

### 4.2 Constraints

- `ck_persona_pref_principal_kind`: `principal_kind IN ('human','service_account')`.
  Closed-set columns get a CHECK here, as `scope_type` does in 037.
- `ck_persona_pref_principal_source`: `principal_source IN ('self','sa_registration',
  'agent_registry','oauth_client')`, plus the pairing rule that `human` implies `self` and
  `service_account` implies one of the other three. The pairing is a CHECK, not a service-layer
  convention, because it is what stops a human row from claiming a service provenance.
- `ck_persona_pref_updated_by_source`: the same closed set over `updated_by_source` (§4.1).
  If the actor's source is worth recording as audit evidence (§5.5) it belongs in a database
  constraint rather than a service-layer convention — otherwise one qualifier is enforced and
  the other is merely intended.
- **Provenance may not affect selection — stated as a prohibition, not left as an omission.**
  `principal_source` and `updated_by_source` are evidence columns. No query that resolves a
  preference may filter, order or branch on either: the lookup is by
  `(org_id, principal_kind, principal_id, persona_key)` and nothing else. Leaving this implicit is how a
  provenance column becomes a de facto key rung — a later "read the row that matches how this
  caller authenticated" filter would silently reintroduce the one-principal-two-rows failure the
  key exists to prevent (below). Worth an explicit test asserting a row written through one
  alias path is returned unchanged to the same canonical principal authenticating through
  another.
- `uq_persona_model_preference`: **UNIQUE** over
  `(org_id, principal_kind, principal_id, persona_key)` — one active row per principal per
  persona, per AC-06 and the issue's "must be enforced in the database, not only in the
  service". **`principal_kind` is in the binding key by review direction**; `principal_source`
  is **not**.

  The two exclusions have different reasons, and only one of them is free:

  **Provenance is excluded and that is unambiguously right.** Under the settled alias contract
  `principal_id` is a canonical, unambiguous ID (§3.2.3), so including `principal_source` would
  *weaken* the constraint in the exact way the contract exists to prevent: one canonical
  principal could hold two rows for one persona by authenticating along two alias paths, and
  which one governed would depend on the path taken at invocation time.

  **`principal_kind` is included, and the cost must be stated rather than assumed away.** Kind
  is *derivable* from the principal — a canonical ID is either a `users.id` or a
  `canonical_service_principal_id`, never both — so as key material it is redundant, and
  redundant key material widens what the database permits. Concretely: with kind in the key,
  the database no longer refuses two rows for one canonical ID and one persona **differing only
  in kind**. Such a pair is unreachable through the API (kind is derived server-side from the
  resolved context, §3.4, so a given principal always presents the same kind), which is why the
  inclusion is safe — but "unreachable through today's handlers" is a property of code, and the
  reason to put an invariant in the database is that it survives code changes. So:
  **the one-kind-per-canonical-ID invariant moves to the service layer and must be named,
  asserted and tested there** — a write whose derived kind disagrees with an existing row's kind
  for the same `(org_id, principal_id)` is refused, not inserted alongside. Without that
  explicit relocation, applying this direction silently deletes a guarantee instead of moving
  it. §11's AC-06 row records that the DB-level half is now narrower than it was.

  The upside of the inclusion is real and is why the direction is right on balance, and one part
  of it only became visible when the siblings were re-read at their current heads: **PMM-01, the
  epic note, has defined the key with kind in it all along** — "a mapping is `(tenant, principal
  kind, principal ID, persona key) -> canonical model ID`" (`5417:21`, restated at `:143` and
  `:397`). This note's earlier demotion of `principal_kind` put it out of step with its own parent,
  and PMM-06 flagged exactly that divergence (`5424:578-580`). Restoring kind to the key **closes
  that gap rather than opening a new one**: three of the four notes in this epic now describe one
  key. Beyond alignment, the key reads self-describingly in the schema, matches the shape PMM-04
  and PMM-06 name a principal by (`principal_kind` + canonical ID, §10.1), and means a kind-scoped
  query needs no join to work out which store a principal came from.

  One nuance PMM-06 is right about and this note preserves: kind is a **snapshot** field there
  because its §3.2 needs it for AC-03 regardless of the storage key (`5424:579-581`). That is
  independent of this constraint and unaffected by the change.

  Because there are no nullable scope columns (D1), this is a **plain composite unique
  constraint**, not 037's `COALESCE` expression index. That is strictly better: it needs no
  expression, and it is enforced identically on both dialects (§4.3).

### 4.3 What the test suite actually proves — and this time, it proves it

The issue requires the PR to "state which invariants the test suite actually proves",
warning that partial-unique invariants are often Postgres-only. Verified position:

- The gateway suite runs on **SQLite** (`tests/conftest.py:28`,
  `sqlite+aiosqlite:///:memory:`); Postgres is opt-in
  (`tests/migrations/conftest_postgres.py`).
- The `TeamMembership` docstring (`organization.py:168-181`) documents a genuinely
  Postgres-only invariant: `uq_team_memberships_one_primary` is a **partial** index behind a
  dialect guard in migration 040, so "the SQLite suite does NOT enforce one-primary at the
  DB level".
- **That does not apply here.** This design's uniqueness is a plain composite constraint
  with no `WHERE` clause and no dialect guard. SQLite enforces it. Migration 037's
  `COALESCE` expression index is likewise unguarded and SQLite-supported (§1.2).

**Therefore AC-01 and AC-06 are provable in CI**, and the PR should say so affirmatively
rather than issuing the usual Postgres-only caveat. The caveat that *does* remain: no FK
enforces the principal reference on either dialect (§3.3), so AC-03 tests service-layer
validation, not a database constraint.

### 4.4 Optimistic concurrency (AC-06)

There is no ETag or `If-Match` support anywhere in the gateway — confirmed by grep across
`src/`, and the frontend client sends only Content-Type and Authorization
(`frontend/src/services/api.ts:56-70`). So the fence travels in the request body.

**Reuse the orchestration precedent** rather than inventing a mechanism: a hand-rolled
integer compare-and-set inside the transaction, exactly as
`orchestration/execution_store.py:963` (`if row.revision != advance.expected_revision:`)
with the increment at `:1011` and the fence documented at `:921-923`. (Those line numbers moved
between this branch's merge base and `c4809bb1` — grep for the predicate rather than trusting the
number, which is the same discipline §6.1 asks for on the migration head.) Migration 052's comment
states the property: "A caller
presenting a revision the row has passed is stale by construction, which is what stops a
lost update." Do **not** use SQLAlchemy `version_id_col` — no gateway model uses it, and
introducing it here would be a new convention for a solved problem.

Contract:

- `set` carries `expected_revision`. Absent means "create only" — if a row exists, refuse.
  This is what makes a blind CLI save safe rather than accidentally last-write-wins.
- Mismatch → **HTTP 409** carrying the **full current row** in the same shape a `GET` returns —
  not merely the current revision and `canonical_model_id`. 409 is already the gateway's conflict
  code (`bedrock_routing/routes.py:554`, `:612`). PMM-04 (#5422) requires the full shape so the
  page can re-render the conflicting state without a second round trip; returning two fields would
  force every client into a follow-up `GET` on the unhappy path. Adopted here (§10.1).
- Success increments `revision`. PMM-06 (D5) binds this value as the mapping revision in
  its signed claims.

### 4.5 Read model — saved versus effective (AC-02)

`list` returns **one row per persona in the catalogue**, not one per stored row, so a
principal with no preferences still sees the full picture. Each entry carries the saved
value (or null), the effective value, and a `source` of `principal-mapping` or
`system-default`. This is the `/effective` explainer pattern from #4692 (§1.2 of that
note), and it is what makes "why is this agent on this model" answerable without reading
the database.

### 4.6 The canonical service principal and its aliases — two tables, owned by PMM-02 (ruling 1)

Ruling 1 assigns this store to this story: "PMM-02 (#5419) owns the enabling identity slice: an
opaque immutable ADP `canonical_service_principal_id`, the alias registry, canonical resolution in
authentication, and the manageable-service-principals endpoint."

**This is two tables, not one.** A single alias table can say "this name resolves to that ID" but
has nowhere to record anything about the principal *itself* — no display name, no status, no
lifecycle. Three consequences make the separation necessary rather than tidy:

- **The discovery endpoint has nothing to return.** PMM-04 requires `display_name` and a
  `manageable` flag per principal (§5.3.1, its `:353`/`:356`). With aliases only, a principal
  with three aliases yields three rows with no name — the endpoint would have to synthesise a
  label from an `agent_name`, which is exactly the alias-as-identity coupling ruling 1 forbids.
- **A principal's own status is not a property of any one alias.** Disabling a principal must
  disable it however it authenticates. Expressed only on aliases, that becomes "revoke every
  alias and hope none is missed" — and a later-registered alias silently revives it.
- **Immutability needs a row to be immutable *in*.** An ID that exists only as a repeated
  foreign value in alias rows has no birth record, no `created_at`, and no authority trail for
  who approved the principal as distinct from who attached each name.

**Table 1 — `service_principals` (the entity).** Tenant-scoped via `TenantMixin`.

| Column | Notes |
|---|---|
| `canonical_service_principal_id` | PK. The **opaque, immutable** ADP-minted ID (`String(255)`, `default=new_uuid`, matching `service_accounts.id`'s shape at `shared/models/organization.py:212`). Opaque means it encodes nothing about its aliases — deriving it from an `agent_name` or ARN would make it change when the alias does, defeating immutability |
| `org_id` | Tenant anchor (`TenantMixin`, `shared/models/base.py:15`). The principal belongs to exactly one tenant, and this is what every preference query filters on. Note `TenantMixin` provides **no foreign key** to `organizations`, so tenant membership is an explicit service-layer check here as everywhere else (§5.4) |
| `display_name` | Human-readable label for the administration UI (PMM-04 `:353`). Set at registration, editable without touching identity — which is the point of separating it from the alias |
| `status` | `active` \| `disabled`, CHECK-constrained. Disabling stops **all** resolution for the principal regardless of alias |
| `created_at` / `approved_by` | `approved_by` is the canonical `users.id` of the human who created the principal. Never a raw subject (§4.1's `updated_by` rule applies identically) |

**Table 2 — `service_principal_aliases` (the names).** Tenant-scoped.

| Column | Notes |
|---|---|
| `id` | PK, `default=new_uuid` |
| `canonical_service_principal_id` | FK to table 1. Non-unique index for reverse lookup — one principal legitimately holds several aliases, which is the entire point of the contract |
| `org_id` | Tenant anchor, and part of the active-alias key. A registered alias carries a tenant even when its upstream store does not — which is what makes an S3 client id usable at all (§3.2.1) |
| `alias_source` | `sa_registration` \| `agent_registry` \| `oauth_client`, CHECK-constrained |
| `alias_id` | The subject form the authentication path produces (§3.2.1) |
| `is_active` | Whether this alias currently resolves. Revocation sets it false and **keeps the row**, so history survives and the name becomes re-registrable (below) |
| `registered_at` / `registered_by` / `revoked_at` | `registered_by` is the canonical `users.id` of the approving human, per "approved service identities" |

#### 4.6.1 Active-alias uniqueness, revocation and re-registration — and the dialect limit

**The rule: `(org_id, alias_source, alias_id)` unique among *active* rows** — tenant-scoped and
source-qualified, never a global alias name.

- **Global keying breaks tenancy.** `agent_name` and `client_id` are not globally unique
  namespaces. Two tenants independently registering the same agent name is ordinary, and a
  table-global constraint makes the second registration fail with a conflict caused by a tenant it
  cannot see — a cross-tenant coupling in a table whose purpose is tenant-safe identity.
- **Dropping the source would be worse.** Two different alias namespaces can produce the same
  string; without `alias_source` an `agent_name` could resolve a row registered for a client id.
  The pair is what makes resolution deterministic.
- **"Among active rows" is what makes revoke-then-re-register work**, which ruling 1 requires
  ("re-registration creates a new canonical ID by default"). A *plain* unique constraint over the
  triple would refuse the second registration forever, because the revoked row still occupies the
  name. The sequence must be: revoke (`is_active=false`, row retained) → register the same name
  again → **new** `service_principals` row, new canonical ID → the old preference rows become
  inert orphans (§3.3), governing nothing because no active alias resolves to their principal.

**The honest limit, and it is a real one: this invariant cannot be enforced in the database on
both dialects.** Verified in this tree, and the note states it rather than claiming CI proves it:

- A partial unique index is **PostgreSQL-only**. `alembic/versions/040_team_memberships.py:94-96`
  creates `uq_team_memberships_one_primary` with a raw
  `CREATE UNIQUE INDEX … WHERE is_primary` behind `if bind.dialect.name == "postgresql":`.
  `021_tenant_memberships.py:53-54` does the same for its one-active-per-user rule, and
  `027_installation_tenant_uniqueness.py:53-65` returns early on any non-Postgres dialect.
- **Declaring it on the model would break the feature**, and this is documented in the tree as
  a real past failure, not a theory. `TeamMembership`'s docstring
  (`shared/models/organization.py:168-183`) records that SQLAlchemy emits `postgresql_where`
  only on PostgreSQL, so `create_all()` on the SQLite test suite renders it as a *plain* unique
  index — "declaring it here made `test_second_non_primary_team_is_accepted` fail with
  `UNIQUE constraint failed`". For this table the identical mistake would make **re-registration
  after revocation fail in CI while passing in production**, or vice versa.
- **The portable alternative exists and should be preferred if it fits.** 036 and 037 use a
  `COALESCE` expression unique index instead of a partial one precisely to keep the invariant on
  both dialects (`036_person_budget_defaults.py:130-135`, with the reason stated at `:127-129`;
  `037_bedrock_account_routing.py:209-219`). 036's comment names the exact reason a
  `UniqueConstraint` will not do: "Postgres treats NULLs as distinct inside one, which would let
  two platform defaults coexist" (`:127-129`) — the same trap here, where a nullable `revoked_at`
  inside a plain UNIQUE would permit unlimited simultaneously-active duplicates. If revocation is
  modelled as a nullable `revoked_at` rather than a boolean, then a unique index over
  `(org_id, alias_source, alias_id, COALESCE(revoked_at, ''))` is **portable, needs no dialect
  guard, and enforces the invariant in CI** — a revoked row's non-null timestamp takes it out of
  contention with the new active row, and two simultaneously-active rows still collide. This is
  the recommended shape; the boolean-plus-partial-index form is the fallback.
- **Either way the invariant also lives in the service layer**, as 040's docstring requires for
  its own rule ("the invariant ALSO lives in the application layer"). If the fallback is taken,
  §11's AC-01 must disclose that DB-level enforcement is Postgres-only and the migration test
  asserts the real DDL from source, as `tests/migrations/test_040_team_memberships.py` does.

**One caveat to carry into implementation:** `service_accounts.iam_role_arn` is `unique=True`
**table-globally, not per-org** (`shared/models/organization.py:217`). That is the existing
table's choice, not this one's, and the two now differ deliberately — a reader comparing them
should not conclude the alias registry is under-constrained.

**What makes this heavier than a settings table, and why §6.1 re-scopes AC-01.** This is an
*identity* store. Its write path is a registration and approval flow whose authorisation model is
"who may declare that this IAM role is that principal" — a stronger question than "who may pick a
model" — and ruling 1 additionally places canonical resolution **inside authentication** (§5.2.1),
so the change is not confined to new tables. AC-01's migration test will be asserting identity
invariants. That is the assigned scope; it should be priced, not discovered.

---

## 5. API surfaces

### 5.1 Package and registration

Proposed `modules/gateway/src/admin/persona_models/`, mirroring `bedrock_routing/`'s
verified five-file layout (`__init__.py`, `schemas.py`, `service.py`, `routes.py`,
`self_routes.py`).

Register by appending **two** dotted paths to `UNIT_MODULES` in `src/app.py` — one per
router, as `bedrock_routing` does at `:112` and `:122`. The loader includes any module
exposing `router` (`app.py:366-377`). A third entry importing the model module for metadata
registration is needed too (`app.py:168` is the precedent).

No `/api` and no `/admin` prefix on the self surface: CloudFront strips the first `/api`
before the origin (#4330, guarded by `tests/test_route_prefix_convention.py`).

**Canonical surfaces (ruling 6 — binding, one contract for all callers).** These are fixed, and
no others exist:

| Route | Audience |
|---|---|
| `GET /me/persona-models` | Self — list |
| `GET /me/persona-models/catalog?persona_key=...` | Self — selectable set |
| `GET /me/persona-models/explain/{persona_key}` | Self — effective-value explainer |
| `PUT` \| `DELETE /me/persona-models/{persona_key}` | Self — set / reset |
| `GET /me/persona-models/manageable-service-principals` | Authorized human administration discovery (§5.3.1) |
| `GET` \| `PUT` \| `DELETE /service-principals/{canonical_id}/persona-models[/{persona_key}]` | Human org-admin only, tenant-checked (§5.3) |

All callers share **one** request/response schema and **one** refusal vocabulary (§7.4). Two
consequences for the file layout above: the `explain` route means the read model of §4.5 is
exposed at two granularities (whole list and single persona) from one service function, and the
`{persona_key}` path parameter on the self surface is **not** a target parameter — it names a
persona, never a principal, which §5.2 has to state explicitly because the structural test
forbids path parameters outright.

### 5.2 The self surface — authorization *is* the path shape

`APIRouter(prefix="/me/persona-models")`, mirroring `self_routes.py:105`. The security
property is structural: **no parameter that names a principal, at any position**, so a request
naming somebody else "cannot be *formed*. There is no check to forget"
(`test_self_selection.py` docstring at `:141-152`).

**One wording precision the route inventory forces.** The existing structural test asserts
`"{" not in route.path` — it forbids path parameters *outright*
(`tests/admin/bedrock_routing/test_self_selection.py:155`). Ruling 6's self surface has
`PUT|DELETE /me/persona-models/{persona_key}` and `GET …/explain/{persona_key}`, so a
copy-pasted assertion would fail on a correct implementation. The property that actually matters
is that no parameter names a *principal*; `persona_key` names a persona. So reuse the test's
**forbidden-name** half verbatim — `("user_id", "person", "anchor", "scope", "target",
"credential_id")` plus `principal_id`, `canonical_principal_id`, `service_account_id` and
`agent_name` — and replace the blanket `"{"` check with an allowlist of exactly `{persona_key}`.
Stated here because silently dropping the path assertion is the plausible wrong fix, and it would
remove the only structural guard on this surface.

- Caller anchor derived server-side via §3.1.
- Dependencies: `Annotated[TokenContext, Depends(get_current_user)]` +
  `Annotated[AsyncSession, Depends(get_db)]`.
- The write body names a persona and a model and **nothing that names a principal** —
  the `test_s1b` property (`:161`), asserted on `model_fields`.
- **The anchor is a resolved canonical ID, not a presented one.** The self surface derives the
  canonical principal ID from the authenticated context by alias resolution (§3.2.3), and the
  synthesis states this directly: "The self API derives it from authentication." "No target
  parameter" is therefore necessary but **not sufficient** — a self route that resolved no
  alias and stored the raw subject would pass every no-target-parameter test and still write a
  row keyed on an alias rather than a principal. The AC-04 test must assert that the stored
  `principal_id` is the *canonical* ID, not the token's subject; the two differ for S2 and S3
  and that difference is invisible to a structural signature test.
- **One router, both caller kinds.** Ruling 6:
  "The FastAPI self routes exist once at `/me/persona-models`. Human JWT calls use that path;
  service SigV4 calls use external `/agent/me/persona-models`… do not duplicate the backend
  router." So there is **no** `require_human_user` / `require_service_account` split on the self
  surface. Both dependencies exist (`auth/middleware.py:193` and `:173`) but gating the self
  routes with either would make the surface single-audience, and mounting the router twice to
  compensate is what the ruling prohibits. `principal_kind` is derived from the resolved context
  (§3.4), not from which router was hit. §5.2.1 explains why one handler can serve both.
- AC-11: the platform default is not reachable here. There is no route on this surface that
  can write it, which is the same structural argument — not a runtime check.

#### 5.2.1 How one handler serves both a browser and a signed agent

This is the mechanism ruling 6 depends on, verified, because "one router, two audiences" is only
safe if the identity of each is resolved server-side and neither can present the other's.

**The edge strips the prefix.** `/agent/{proxy+}` is an `AWS_IAM`-authenticated API Gateway route
whose integration URI is `http://${var.internal_alb_dns}/{proxy}`
(`modules/gateway/infra/modules/api-gateway/main.tf:270-298`). The `/agent` segment is consumed by
the match and not forwarded, so an external `PUT /agent/me/persona-models/reviewer` arrives at the
pod as `PUT /me/persona-models/reviewer` — the same path the browser calls. API Gateway validates
the SigV4 signature itself and injects the caller's IAM ARN as `X-Caller-Identity` from
`context.identity.userArn` (`:294`). Contrast `/internal/{proxy+}`, which deliberately *preserves*
its prefix (`:316-324`) because those routes are registered with it — so prefix-stripping is
per-route behaviour, not a platform-wide rule, and this design depends on the `/agent` route's
specific choice.

**One dependency already resolves both.** `get_current_user` (`src/auth/dependencies.py`) tries the
IAM path first: when `X-Caller-Identity` is present it parses the assumed-role ARN, looks the role
up in the agent registry, and returns a service `TokenContext` (`:143-190`); otherwise it falls
through to Bearer JWT validation. This is the dependency `bedrock_routing/self_routes.py` already
uses (`:84`, `:238`), so a self surface built on it inherits dual-audience support rather than
adding it. Two properties of that code matter here and should be cited in the implementation:

- **Header presence is terminal** (`:145-158`, #3985). A request carrying `X-Caller-Identity` is
  either resolved to a registered agent or **rejected** — it never falls through to the JWT
  branch. That is what stops a caller from presenting a bogus ARN to reach the human path, and it
  is why the shared handler cannot be tricked into mis-typing a principal.
- **An unregistered role is refused**, not given an empty-tenant context (`:178-188`). The prior
  behaviour minted a `service` context with `org_id=""` for any parseable ARN; that was the
  vulnerability #3985 closed. This story's registry (§4.6) adds a *second* registration layer on
  top, so a caller must be both registry-known and alias-registered.

#### 5.2.2 How the canonical ID travels — an additive field on `TokenContext`

Ruling 1 places canonical resolution *inside* authentication, so the resolved ID has to reach the
handler somehow. No sibling note specifies this and neither did earlier revisions of this one —
it is the seam between "resolution happens in auth" and "the handler stores a canonical ID", and
leaving it unstated is how an implementer either re-resolves per handler or quietly widens an
existing field.

**The mechanism: one new optional field, `canonical_principal_id: str = ""`, on `TokenContext`
(`shared/schemas/auth.py:33`).** Three verified properties make this safe:

- **Additive, so nothing breaks.** `TokenContext` is a pydantic `BaseModel` whose existing
  optional fields already carry defaults in this exact style — `is_admin: bool = False` (`:55`),
  `auth_source: str = "jwt"` (`:57`), `attributed_org_id: str = ""` (`:76`). All seven
  construction sites in `src/` (`auth/token_manager.py:183`, `auth/dependencies.py:91`,
  `auth/auth_service.py:316`, `auth/agent_registry.py:255`, `auth/middleware.py:682`,
  `budget/middleware.py:128`, `orchestration/adapters/github_comments.py:363`) pass keywords, so
  a defaulted field changes none of them, and none of the ~223 test constructions either.
- **`user_id` keeps its current meaning and value, unchanged.** This is the load-bearing
  constraint: `.user_id` is read in **340 places** across `src/`, and its value is
  path-dependent by design (a Cognito sub for a human, an `agent_name` for S2, a `client_id` for
  S3 — §3.2.1). Repointing it at a canonical ID would silently change behaviour in every one of
  those readers. The new field sits *beside* it; nothing migrates onto it.
- **Empty means "not resolved", never "resolve later from `user_id`".** A handler that needs a
  canonical ID and finds the field empty must refuse, not fall back to the token subject —
  falling back is precisely how an alias becomes a preference owner after §5.2 closed that door
  at the route layer. The field is populated only on paths that actually performed resolution;
  on a human JWT call it stays empty and the human anchor comes from §3.1's `users.id`
  resolution instead.

One convention worth following rather than inventing: values on `TokenContext` that must not be
forgeable are `PrivateAttr`s, not fields (`:122-163`). A canonical ID does not need that
treatment — it is server-derived and carries no authority by itself (§9's "a preference is not an
entitlement") — but if a later story binds authority to it, the private-attribute pattern is the
established way and should be preferred to trusting a public field.

**The setting that would otherwise make every machine call fail, silently.** The IAM branch is
gated by `settings.trust_apigw_headers`, which **defaults to `False`** (`src/shared/config.py:66`)
and corresponds to `BG_TRUST_APIGW_HEADERS`. With it off, a signed agent's `X-Caller-Identity` is
ignored and the request is rejected as unauthenticated — the route would exist, pass every test
that uses a JWT, and serve no machine caller in a default deployment. That is the inert-config
class (#4511) reached through configuration rather than schema, so: the deployment step must
confirm the flag is enabled in the target environment (§6.2), and AC-09's test must exercise the
service path **through the route with the flag on**, not by calling the service layer with a
hand-built service context.

### 5.3 The administration surface — authority settled (was B2)

`GET|PUT|DELETE /service-principals/{canonical_id}/persona-models[/{persona_key}]` (ruling 6),
in `routes.py` as a separate module per `bedrock_routing`'s split-by-shape convention, with
the authorization check as the **first executable statement** of every handler — a property
asserted by source inspection in `test_authz.py:210` and `:226` ("the gate is the first
statement in every handler"). Reuse that test shape.

Note the path takes a **canonical ID only**, never an alias or a role ARN. Ruling 6: "administered
handlers accept only canonical IDs from the server discovery surface" (§5.3.1). A handler that
accepted an `agent_name` here would be re-admitting the alias-as-owner failure at the admin layer
after §5.2 closed it at the self layer.

The remaining question was *which* gate, and it is now answered. The verified constraints that
shaped the answer:

- `service_accounts` has **no owner column** at all — no `owner_id`, `created_by` or
  `user_id` (`organization.py:209-218`). Ownership is only implied by
  `org_id`/`department_id`/`team_id`, and those are bare strings, not FKs.
- `Permission` (`admin/config.py:21-80`) has **no** service-account member. The nearest is
  `AGENT_REGISTER` ("agent:register"), which gates registry writes. Admin level is an
  `AdminRole`, not a `Permission`.
- The closest existing precedent is how service accounts are *themselves* administered:
  `admin/routes.py:1204-1260` gates create/delete on `Permission.ORG_UPDATE` and list on
  `ORG_READ`, both with `target_org_id=org_id`. So "org admin may administer the tenant's
  service accounts" is an established pattern, and `ORG_UPDATE` is a defensible alternative
  to platform-admin that stays inside the existing permission vocabulary. It is still not
  *ownership* — it is org-wide authority — so it does not make AC-10's "as a service-account
  owner" literally true.
- The existing standing-delegation precedent, `agentauth/service_authority.py:226-228`,
  requires `account_type == "human"` **and** `auth_source == "jwt"` **and**
  `Permission.PLAN_APPROVE`. That is approval authority for plans, not service-account
  administration; borrowing it would overload a permission with a second meaning.

**The `is_admin` finding below is the reason the chosen gate needs an extra conjunction**, so it is
kept even though the authority question is settled. `require_platform_admin`
(`admin/access_control.py:502-515`, gating on `context.is_admin` at `:512`) is *not* a human gate.

**`is_admin` alone is not a human gate.** `require_platform_admin` tests only
`context.is_admin`, and two authentication paths set that flag on a context whose
`account_type` is `service`:

- **S1 (SigV4 service account):** `tenant_resolver.py:304` sets
  `is_admin=self._check_admin_privileges(organization, role_arn)`, which is true whenever the
  IAM role name appears in `organization.role_mappings["admin_roles"]` (`:503-519`).
- **S3 (Cognito `client_credentials`):** `auth_service.py:301-306` derives `is_admin` from
  `claims.role` / `claims.cognito_groups` **without consulting `account_type`**, which is set
  independently at `:298`.

Only the S2 agent-registry path hardcodes `is_admin=False` (`agent_registry.py:261`). So an
`is_admin`- or permission-only gate would let a *machine* principal administer another principal's
preference. The blast radius is bounded — §9's "a preference is not an entitlement" holds, so
this retargets model *selection* inside an already-permitted set rather than granting access —
but it is not the "delegated administration by a person" that AC-10 describes.

The verified in-repo precedent for making administration a human act is
`agentauth/service_authority.py:226-228` (`approving_human`): `account_type == "human"` **and**
`auth_source == "jwt"` **and** a permission check, all three in one condition. The handler must
carry that conjunction in addition to the permission test, with a test asserting a `service`
context is refused. Note this is one more reason the §3.2 contract matters: `auth_source`
alone cannot distinguish S1 from S3 (both `jwt`), so the `account_type == "human"` half of the
conjunction is doing the real work.

**Ruling 4 reinforces this from the epic side**, and is worth citing in the handler's docstring:
"`service_policy` is owned by the canonical service principal; the approving human is audit
attribution only." The human is the *actor*, never the owner — which is exactly the §4.1
`updated_by` / `principal_id` distinction, arrived at independently.

**The settled authority (ruling 1 and the administration ruling) is a three-way split:**

| Actor | May administer | Mechanism |
|---|---|---|
| Org administrator | canonical service principals **in their own tenant** | `Permission.ORG_UPDATE` + explicit target-tenant check + the human conjunction below |
| Service principal | **only itself** | Self surface (§5.2); no administration route |
| Platform administrator | platform default/policy settings only (§8) | `require_platform_admin`, fully audited |

So org-admin scope is chosen for service-principal administration; a platform-admin-only gate
would make delegated tenant administration impossible. **AC-10 must still be reworded**: "as a
service-account owner" remains unimplementable because no owner column exists
(`organization.py:209-218`, verified) — the correct wording is "as an organization
administrator, within the caller's tenant". The authority is org-wide, not ownership, and the
acceptance criterion should say what the code can enforce.

The `account_type == "human"` / `auth_source == "jwt"` conjunction from the finding above
**still applies and is now more important, not less**: `ORG_UPDATE` is held by `ORG_ADMIN` and
`PLATFORM_ADMIN` (`admin/config.py:88`, `:108`), and nothing in a permission check consults
`account_type`. Without the conjunction, a service principal holding an admin-mapped IAM role
(`tenant_resolver.py:304`) or an admin Cognito group (`auth_service.py:301-306`) would satisfy
"org administrator" and could administer *another* principal's preference — which directly
contradicts the synthesis's "service principals may manage only themselves". The conjunction is
what makes that sentence enforceable, so it is a requirement rather than defence-in-depth.

#### 5.3.1 The manageable-principals endpoint — owned here, with one honest limit

`GET /me/persona-models/manageable-service-principals` (ruling 6). Ruling 1 assigns it to PMM-02,
and ruling 6 requires that "administered handlers accept only canonical IDs from the server
discovery surface" — which makes the admin surface's authority enumerable rather than guessable,
the same property §5.2 gets structurally.

**PMM-04 (#5422) depends on this endpoint and has specified the shape it needs**, so the contract
is not PMM-02's to choose unilaterally. Re-read at `5fc0fb4f`, it requires
`canonical_principal_id`, `display_name`, `tenant_label`, `source` and `manageable`
(`5422:351-356`), with the browser explicitly forbidden from relating an entry to a role ARN, an
`agent_name`, a `client_id` or a `service_accounts.id` (`:364`). That is the right division — the
server owns namespace knowledge, the client stays namespace-agnostic — and this note adopts it.
Three details must be pinned rather than left to integration, because each is a place where two
notes agree on substance and differ on a string:

- **`principal_kind` is returned, not optional.** PMM-04's JSON block shows it (`:352`) while its
  prose calls the contract five fields and says "PMM-02 may omit it" (`:360`). It is **not**
  optional now that it is in the binding preference key (§4.2): a picker that omitted it would
  force the client to infer the kind it must send back on the administered write, which is the
  namespace inference `:364` forbids. Six fields.
- **`tenant_label`, not `tenant`** (`:354`, and `:395` confirms it is display-only). Display-only
  matters: the page sends no tenant identifier on any request, so this field must never be the
  thing a server-side tenant check reads.
- **`source` values are hyphenated display labels**, not this note's column values. PMM-04 lists
  `"agent-registry" | "service-accounts" | "cognito-client"` (`:355`); §4.6's `alias_source` CHECK
  is `agent_registry` | `sa_registration` | `oauth_client`. These are deliberately different — one
  is a UI label, one is stored vocabulary — but the mapping must live in one place server-side, and
  the response must not be built by string-munging the column. PMM-04 `:365` requires the page
  never branch on `source` at all, which is what makes a display-only spelling safe.

It does not exist today, and the gap is not merely missing code:

- **Verified absent.** Nothing in `src/` returns "service accounts this caller may manage".
- **The schema cannot express per-caller manageability.** `service_accounts` has no owner
  column, and `department_id`/`team_id` are bare strings with no FK
  (`organization.py:213-214`). The existing org-scoped list
  (`admin/routes.py:1222-1242`, gated `Permission.ORG_READ` at `:1232`) filters on
  `org_id` alone (`admin/service.py:2010`, `:2015`) — no caller predicate is *possible*.
- **Therefore "principals you may manage" can only mean "every service principal in your
  tenant"** under the chosen authority. That is consistent with org-admin scope, so the
  endpoint is buildable — but it should be named and documented as tenant enumeration, not as
  per-caller entitlement, or it will read as a stronger guarantee than it makes.
- **Do not build it on the existing unguarded endpoint.** `auth/routes.py:346-370`
  (`list_service_accounts`) has **no authorization check at all** beyond authentication — it
  scopes to `token_context.org_id` (`:363`) but any authenticated org member, human or machine,
  can enumerate every service account in the tenant. Its sibling `create_service_account`
  (`:321`) does gate, at `:327`. This is a pre-existing gap, **not** this story's to fix and
  not a claim about live exploitation; it is recorded because reusing that handler as the
  picker would inherit the missing gate. The correct precedent to copy is
  `admin/routes.py:1439-1480` (`list_platform_users`), whose docstring explains why a picker
  endpoint needs an explicit gate as its first statement (`:1470`).

**Consequence for this story:** PMM-02 builds the picker. That is scope the issue does not
mention, and §6.1 re-prices AC-01 accordingly. The one thing that must not be lost in
implementation is the naming honesty above: with no owner column, this endpoint enumerates the
tenant, and the `manageable` flag PMM-04 expects is computed from the *caller's* org-admin
authority over the whole tenant — not from a per-principal relationship. Documenting it as the
latter would overstate the guarantee.

**One correction PMM-04 should take.** Its `:371` still records this note's gate as "platform-admin
plus a tenant check", so it concludes `manageable` is true "only for platform admins" and the
selector is "empty for everyone else". That is no longer the authority: B2 settled on a **human
org administrator in-tenant** (§5.3, §12), which makes the selector non-empty for org admins.
PMM-04's own conclusion — that the acceptance evidence must state which gate was in force or AC-06
reads as proving delegated ownership when it proved admin access — is correct and unaffected, and
§11's AC-10 adopts it.

### 5.4 Response states

Per the issue and D2, every entry distinguishes **not-configured, configured, unavailable,
disallowed, stale**. `unavailable`/`disallowed`/`stale` come from PMM-03's validator at read
time (§7.1) — they are *reported*, never *repaired*: D1 and D2 both forbid substituting
another model, so a disallowed saved row reads as disallowed and the invocation fails later
(PMM-07's job), rather than this surface quietly showing the default.

### 5.5 Audit (AC-08)

Reuse `security_audit_logs` via `AuditLog` (`shared/models/audit.py:23-43`). Its shape is
`id`, `org_id` (NOT NULL), `event_type`, `actor_id` (nullable), `details` JSON,
`created_at`. It has **no target and no before/after columns** (§1.3), so those go in
`details`: `principal_kind`, `principal_id`, `persona_key`, `previous_model`, `new_model`,
`revision`, `principal_source` and `updated_by_source` as **provenance**, and an `actor_kind`
distinguishing a human self-change from a service-principal self-change from an administrative
change — which is what AC-10's "audited distinctly" requires.

**The audit record and the key must describe the same key.** The audited *subject* is
`(org_id, principal_kind, principal_id, persona_key)` — exactly the key of §4.2, with
`principal_id` the canonical ID. `principal_source` and `updated_by_source` are recorded
**beside** it as evidence of how the subject and the actor authenticated, never as part of what
identifies the row; a reader who treats either as identifying will conclude two rows exist where
one does. The trail is unambiguous because the canonical ID is unambiguous, which is the property
the alias contract buys.

**`actor_id` carries a canonical ID only.** `AuditLog.actor_id` is a bare nullable
`String(255)` (`shared/models/audit.py:40`) with no constraint, so nothing stops a raw service
subject being written there. Per the review's rule, resolve the actor to a canonical ID first
(§4.1) — a trail whose actor column mixes canonical IDs with `agent_name`s cannot be queried
for "everything this principal did", which is the one question an audit trail exists to answer.

Do **not** use `admin/models.py:59`'s `AuditLog`. It has the nicer columns but **no
migration creates the `audit_logs` table** and it has no writer; it exists as metadata only,
created by `create_all` in tests. (Precisely: `audit_logs` appears in `alembic/` only in
`008_magic_link.py`'s docstring, recording that `security_audit_logs` was once named
`audit_logs` and collided with admin's table — there is no `op.create_table("audit_logs")`
anywhere.) Adopting it would mean a table that exists in CI and not in production — an inert
audit trail, which is worse than none.

**Reuse both existing writer helpers**, whose split is the reason AC-08 is satisfiable:

- `write_audit` (`bedrock_routing/service.py:752`) — flushes on the caller's transaction,
  for a mutation that succeeded.
- `write_refusal_audit` (`:779`) — **commits on its own transaction and swallows its own
  failures**, because a refusal writes no mapping and so has no caller transaction to ride.
  This is what lets a refused write still be recorded.

AC-08's "the audit history of both the original save and the reset survives" follows
directly: the audit rows are in a different table from the preference, so deleting the
preference row cannot cascade to them — there is no FK between them, deliberately.

### 5.6 Tenant isolation (AC-05)

`TenantMixin.org_id` is `nullable=False` with **no FK** and no composite constraint
(`base.py:15`). Isolation is therefore an **explicit check**, not a free property of a
query — #4692 §4.2 reached the same conclusion for routing mappings.

Requirements: every read and every write filters on the caller's `org_id`; the
administration surface checks the *target's* tenant equals the caller's before acting;
cross-tenant attempts create **no row and no audit entry in the victim tenant** (AC-05's
exact wording) — a refusal is audited in the *caller's* tenant, which is where the
suspicious act happened.

**State the refusal-audit argument rule explicitly.** `write_refusal_audit`
(`bedrock_routing/service.py:779-786`) takes `org_id` as a keyword *parameter* and commits on
its own transaction, so whether the victim tenant stays clean depends entirely on the call
site passing the **caller's** `org_id` and never the target's. Passing the target's would
write a row into tenant B while still returning 403 — a mistake that a status-code-only test
cannot catch. AC-05's assertion (§11) must therefore check both halves: the 403 *and* the
absence of any `security_audit_logs` row in the target tenant.

One caveat to record: `service_accounts.iam_role_arn` is `unique=True` **table-globally,
not per-org** (`organization.py:217`). It is not this design's key, but any future code
resolving a service account by role ARN must not assume the uniqueness is tenant-scoped.

---

## 6. Migration, deployment, rollback

### 6.1 Migration

**The head moves faster than this note, which is why the developer must re-derive it rather
than copy it.** This note was first written against `052_orchestration_executions` and has been
wrong about the tip twice since. As re-verified at `c4809bb1`, `origin/main` carries
`054_execution_tenant_guards`
(`alembic/versions/054_execution_tenant_guards.py:11-12`, `down_revision = "053_flow_slug_unique"`),
so the new migration is **`055_persona_model_preferences.py`** with
`down_revision = "054_execution_tenant_guards"`. Keep the revision identifier ≤32 characters
(#4123): `055_persona_model_prefs` is 23. Two migrations in three days is the normal rate on this
branch, so **the developer must re-run the single-head check at implementation time and not trust
this number either**; a migration test should assert linearity, as
`test_052_orchestration_executions.py` does. Chaining onto a stale head creates a second head and
`alembic upgrade head` then fails outright — the failure mode `029_orchestration_graph.py:34-40`
documents from experience.

**No longer one table, and "alters nothing, backfills nothing" is now only half true.** Ruling 1
assigns the identity slice here, so this migration creates **four**: the preference table (§4.1),
the service-principal entity and its alias table (§4.6), and the platform settings record (§8.3),
plus a structural seed for the Claude compatibility class (§8.4). The three tenant-scoped tables
are the first three. It still **alters** no existing table.

**Existing machine identities need a defined path onto the scheme — this is the part that is not
purely additive.** Every service caller that authenticates today (§3.2.1) has a subject but no
canonical principal, so on the day this ships none of them can own a preference. Two options,
and the choice belongs in the issue rather than being discovered in implementation:

- **Registration-only (recommended, fail-closed).** The migration seeds nothing. Each machine
  principal is registered explicitly, by a human, through the §4.6 write path. Consequence to
  state in the acceptance record: until a given caller is registered, its self-write is refused
  with the §7.4 vocabulary — the feature is unavailable rather than silently wrong. This is
  consistent with D2 and with ruling 1's "fails closed if unregistered", and it means the
  approval trail (`approved_by`) is real for every principal rather than backdated.
- **Migration backfill.** Mint a principal and an alias per existing `service_accounts` row.
  Cheap for S1 because the rows are in the same database, but it **cannot be done for S2 or S3**:
  the agent registry is DynamoDB and Cognito clients have no enumerable ADP-side per-principal
  record (§3.2.1), so a backfill would cover one of three paths and leave the other two
  fail-closed anyway — an uneven state that is harder to reason about than none. It would also
  have to invent an `approved_by` value for principals no human approved.

If a backfill is nonetheless chosen, follow `042_user_identity_primary.py:141-143`'s ordering rule:
backfill **before** creating the uniqueness index, or the index creation fails on data the
backfill itself produced.

**Beyond the four tables, this story now changes the authentication path.** Ruling 1 places
canonical alias resolution *inside* authentication (§3.2.3, §5.2.1), so the change set is not
only additive DDL: `get_current_user`'s service branch must resolve a registered role to a
canonical principal ID. That is a change to a file every authenticated request traverses, which
is a different risk class from a new table and should be reviewed as such.

**AC-01 must be re-scoped before implementation — this is the one piece of scope arithmetic the
rulings do not remove.** It was written as "migration up/down for one table". The real change is
**four** tables with three different authorisation stories, plus a change to the authentication
path. The criterion should name all four tables and the auth-path change explicitly, and should
carry §4.6.1's dialect disclosure — a green CI run on the fallback index shape proves less than the
criterion claims. Discovering any of this during implementation is how a story silently
quadruples; re-pricing it first costs one edit.

**Migration/model parity must be asserted.** 052's docstring names the exact hazard: the
migration is what runs in the deployed database and the models are what tests use, so drift
"passes every test and raises `UndefinedColumn` in dev". Copy that test's parity assertions.

### 6.2 Deployment

Gateway module, dev first. `gateway-deploy.yml` fires on merge for gateway source;
`gateway-infra-apply.yml` is manual by design (CLAUDE.md). Confirm against
`docs/adp-platform-deployment/deployment-manifest.md` at implementation time.

**The deployment step must confirm `trust_apigw_headers` is on in the target environment.** It
defaults to `False` (`src/shared/config.py:66`; env `BG_TRUST_APIGW_HEADERS`), and with it off the
self routes serve browsers correctly while **every signed service-principal call is rejected as
unauthenticated** (§5.2.1). Nothing in the schema or the test suite detects that state, so it is a
deployment-time check with a named expected value, not an assumption. If the flag cannot be
enabled in an environment, AC-09 is not satisfiable there and the acceptance record must say so
rather than reporting the human path's pass.

**Merging this story does not deploy it.** Deployment is an operator action and live
acceptance belongs to PMM-09 (#5427).

### 6.3 Rollback

Down-migration drops the tables it created. The rollback claim is checkable precisely because
nothing resolves models from Postgres yet — PMM-07 (#5425) is what connects this store to
dispatch — so dropping them **cannot change any run's effective model**. The PR must state this
explicitly, as the issue requires. Audit rows in `security_audit_logs` are unaffected by the
drop (§5.5), so the rollback is not self-erasing.

**One rollback caveat the alias table introduces**, and ruling 1 makes it this story's to carry:
once PMM-04 or PMM-05 resolves canonical IDs through the registry — and once `get_current_user`
resolves through it — dropping it stops being inert. Those surfaces lose principal resolution
entirely, and an auth-path dependency means a partial rollback can break authentication for
service principals rather than merely removing a feature. So the "dormant until its consumers
exist" property holds for the preference table but **expires for the alias table the moment the
auth change ships**. Practical consequence for the down-migration: it must be ordered so the
auth-path code is reverted before the registry table is dropped, and the PR must say so.

This is the "dormant until its consumers exist" property 052 relied on for the same reason.

---

## 7. The PMM-03 validation interface (AC-07)

### 7.1 Why this is an interface and not a local check

D3 makes the allowlist a real admission gate whose effective set is the intersection of
catalogue, tenant allowlist, harness compatibility, service-account restrictions and
**freshly proven invocability**. PMM-03 (#5420) owns all of that, including the bounded
invocation probe. This story must not reimplement any of it — and cannot: the persona
catalogue itself is not in the gateway (§1.6).

**Contract this story depends on** — a single function PMM-03 exposes, which PMM-02's save
path and PMM-06's snapshot builder both call, so exactly one definition of "selectable"
exists. **This is no longer a proposal: PMM-03's current head publishes it**
(`5420:348-351` @ `25ece717`) and states it adopted the form from this note:

```
validate_selection(db, *, org_id, principal_kind, canonical_principal_id,
                   persona_key, model: str) -> Selection | Rejection
```

On success it returns the canonical versioned model ID, the compatibility class it was validated
for, and the **evidence row** that justified it; on refusal, a stable reason code in the
`422 {reason, message}` shape. This story stores **only** what the validator resolved and
approved (§8.1).

**Three properties of this signature are load-bearing, and all three now hold on both sides.**

1. **No provenance parameter.** `principal_source` is deliberately absent. Passing it would let
   `validate_selection` return different answers for one principal depending on how it
   authenticated — the same selection-affecting use of provenance §4.2 prohibits in the store.
   PMM-03 reaches the same conclusion from R1 (`5420:353`: provenance "is retained by PMM-02 for
   audit, not as part of this key").
2. **The identifier is the canonical ID, never an alias.** PMM-03 `:353` states it "**receives**
   it and never resolves, guesses or accepts an alias". That is only satisfiable because §5.2.2
   puts the resolved canonical ID on `TokenContext` — a self handler has nothing else to pass.
3. **`principal_kind` is a parameter, not a derivation.** It is present for the same reason it is
   in the key (§4.2): a service-account restriction turns on human-vs-machine, and PMM-03 must not
   have to re-derive from the ID what the caller already knows.

**One naming delta worth settling now rather than at integration.** PMM-03 names the parameter
`canonical_principal_id`; this note's column is `principal_id` holding a canonical value (§4.1).
Both refer to the same thing and the mismatch is cosmetic at a keyword-only call site — but it is
exactly the #4744 shape where two names for one value drift into two meanings. **Recommendation:
adopt PMM-03's `canonical_principal_id` as the parameter name**, keep `principal_id` as the column
name, and say so in one comment at the call site. The parameter is the more dangerous of the two to
leave ambiguous, because passing a raw subject to it is a silent wrong answer rather than an error.

**The evidence row must survive the return.** PMM-06 binds harness/contract revision into its
snapshot, so a validator returning only a model ID forces a second lookup to reconstruct why the
selection was allowed. PMM-03 `:355` returns it; this story must not discard it on the way to
§5.5's audit `details`.

**If PMM-03 has not merged**, gate the write behind this interface with a minimal in-repo
implementation and say so in the PR. The issue is explicit that mocked validation does not
establish AC-07, and §11 marks it accordingly. Sequencing after PMM-03 is preferable.

### 7.2 Harness compatibility (D6) is validated, not stored

D6 makes a model selectable for a persona only when that persona's harness has a validated
compatibility contract for it. Two design consequences:

- Compatibility is checked **at write time and again at resolution** (D6 says the service
  "validates compatibility both when a mapping is written and again when an invocation is
  resolved"). Write-time here; resolution-time in PMM-07.
- **No harness column in this table.** The harness belongs to the persona's runtime, and
  D6 requires the *snapshot* (PMM-06) to carry the harness identifier — not the preference
  row. Storing it here would create a second source of truth that drifts when the harness
  is upgraded, silently invalidating stored rows nobody re-validated.

In practice, under D6 the initially selectable set is compatible **Anthropic Claude** models
only, because all direct persona execution runs on the pinned Claude Agent SDK harness.

### 7.3 Principal validation at write time (AC-03)

Before any write: resolve the caller's alias to a canonical principal ID and confirm that
principal exists in the caller's tenant — never by guessing from the identifier's shape:

| Alias path | Validated against | Tenant check available? |
|---|---|---|
| `self` (human) | `users` row, same database (§3.1) | Yes — `users.org_id` |
| `sa_registration` (S1) | `service_accounts` row | Yes — `service_accounts.org_id` |
| `agent_registry` (S2) | DynamoDB agent-registry entry | Yes — the entry carries `org_id` |
| `oauth_client` (S3) | The alias registry. The org-level approved-client list (`Organization.cognito_client_ids`) may be checked as a **registration precondition**, but is unread by authentication and names no individual principal (§3.2.1) | **Only via a registered alias** (§4.6) |

An unknown persona key, an invalid principal kind, an unresolvable alias, a rejected model, or
a stale revision each refuse with a stable reason code and write **nothing** — no partial
write, per the issue.

**One invariant lives here and only here, and it must be written down or it will be lost.**
Because `principal_kind` is key material (§4.2), the UNIQUE constraint no longer refuses two rows
for one canonical ID and one persona **differing only in kind**. The database is therefore no
longer the thing that guarantees a principal has one kind. The service layer is:

> **A canonical principal has exactly one kind.** The kind written to a preference row is the
> kind of the resolved principal — a human canonical user ID is always `human`, a canonical
> service-principal ID is always `service_account`. The kind is **derived from which registry the
> resolution succeeded against, never accepted from the request body or inferred from the
> identifier's shape.** A caller-supplied `principal_kind` that disagrees with the resolved
> principal is a refusal, not a correction.

Two consequences worth stating, because both are easy to get wrong and neither is caught by a
constraint any more:

1. **Derive, then compare — do not trust, then store.** §5.3.1's administered write is the risky
   one: the caller names a target principal, so `principal_kind` arrives from outside. Resolving
   the target and comparing is the whole of the check; skipping it is how one canonical ID acquires
   two rows for one persona and a read picks whichever the query happens to order first.
2. **This needs its own test** (§11, AC-06). Write a preference for a canonical ID as
   `service_account`, then attempt the same ID and persona as `human`, and assert a refusal rather
   than a second row. A duplicate-row `IntegrityError` test still passes and no longer covers this.

**Where the spelling trap bites.** §3.4 already records that this column's service value
(`service_account`) is **not** the runtime `TokenContext.account_type` value (`"service"`). That
mismatch stopped being merely untidy when kind became key material: a `principal_kind =
context.account_type` assignment passes type checking, writes `"service"`, and is refused by the
§4.2 CHECK — which is the good outcome. The bad outcome is someone widening the CHECK to accept
both spellings to make the error go away, because that reintroduces two kinds for one principal
and makes the invariant above unenforceable at the only layer still holding it. Map explicitly at
the single derivation point, and refuse unknown `account_type` values rather than passing them
through (§3.4).

**Once the alias table (§4.6) exists, it becomes the tenant anchor for every service path,
which is what closes the S3 gap** — a registered alias row carries `org_id` even though a
Cognito `client_id` has no upstream record that does. That is a genuine improvement from the
synthesis's choice of the alias mechanism over the source-qualified pair, and it is worth
stating explicitly: S3 was an unresolved hole in every earlier reading of this problem, and the
alias table is what fills it.

Until that table exists, an S3 write **cannot** satisfy AC-03's in-tenant requirement and must
be refused rather than stored unvalidated. Refusing is fail-closed and consistent with D2;
storing would be the inert-config class again.

### 7.4 Refusal vocabulary

Reuse the verified shape: HTTP **422** with `{"reason", "message"}`, raised from a
service-layer `MappingRejectedError`-equivalent carrying a stable code
(`bedrock_routing/self_routes.py:108-115`, `service.py:57-71`). Concurrency conflict is the
exception at **409** (§4.4), because it is retryable and the others are not.

One vocabulary across both surfaces: `self_routes._rejected`'s docstring states the reason —
"the client branches on `reason`, and one surface answering `{reason, message}` while the
other answered a bare string would mean two error parsers for one vocabulary". The future UI
(#5422) and CLI (#5423) depend on this.

---

## 8. The platform model-policy settings record (D4, AC-11, #5433)

### 8.1 Canonical identifiers only

`canonical_model_id` stores a **versioned** identifier, never a family alias. The epic's
own requirement: "the saved effective value must not drift silently when a provider changes
a 'latest' alias". `requested_alias` preserves what the caller typed, for display only.

### 8.2 D4's identifier does not exist in the gateway yet

D4 locks the canonical default to `us.anthropic.claude-sonnet-4-6`, preferring versioned
`us.` inference profiles over `global.` for account portability. The synthesis qualifies this
further: Sonnet is the **Claude-class candidate pending live proof**, not a settled value for
all classes. Two verified facts the implementer must not trip over:

- The gateway's alias map pins **`global.`** profiles: `"sonnet46":
  "global.anthropic.claude-sonnet-4-6"` (`proxy/model_resolver.py:28`), and the worker
  default is `global.anthropic.claude-opus-5` (`agent-worker-image/entrypoint.py:1578`).
  `us.anthropic.claude-sonnet-4-6` appears in the gateway only in pricing artifacts and
  fixtures — pricing coverage is not entitlement and not invocability, which is the whole
  lesson of #2300.
- `platform/scripts/enable-bedrock-models.sh:41-42` enables
  `anthropic.claude-opus-4-6-v1` and `anthropic.claude-sonnet-4-6` — bare model ids.

**Reconciling those divergent defaults is explicitly PMM-09's** (D4: "PMM-09 then
removes/consolidates every remaining divergent hard-coded default"). This story must not
change them. What it must do is read its default from **one** place.

### 8.3 Where the default lives — Postgres, per the synthesis

The synthesis directs: "Postgres remains authoritative for preference rows and for a versioned
platform model-policy settings record." This note argued for configuration instead until the
objection behind that preference was checked and found false. The objection is recorded here
because it is the kind that recurs: a platform row "would need a tenant", because
`TenantMixin.org_id` is `nullable=False` (`base.py:15`) and `bedrock_routing` needed the
`PLATFORM_AUDIT_ORG = "__platform__"` sentinel (`service.py:54`) to audit platform events. That
inference does not hold, and the counter-evidence is in the tree: **a model simply need not
mix in `TenantMixin`.** `PersonBudgetDefault` is declared `class PersonBudgetDefault(Base)`
(`shared/models/budget.py:128`) with no tenant mixin, created by migration
`036_person_budget_defaults.py`, and it stores exactly this kind of thing — a platform-scope
default for a policy that is otherwise tenant-scoped, with `scope_type` recording the rung and
a nullable `scope_id_org`. `ModelPricing` (`shared/models/usage.py:85`) is the same shape.

So a non-tenant-scoped platform settings table is **established precedent, needs no sentinel,
and is the better choice** for the reason the synthesis gives: a table gives the record a
`revision` and an audit trail, which a configuration value does not, and D5 requires a revision
that PMM-06 can bind into signed claims.

**Shape**, following `PersonBudgetDefault`'s precedent:

| Column | Notes |
|---|---|
| `harness_compatibility_class` | **The key** — `claude-agent-sdk` \| `codex-sdk`, unversioned, CHECK-constrained (§8.4). No cross-class fallback, so a lookup miss is an error, never a substitution |
| `harness_contract_revision` | The versioned compatibility-contract revision, carried **separately** from the class key per ruling 2, and part of evidence/snapshot keys |
| `canonical_default_model_id` | Versioned identifier (§8.1). Claude class: D4's `us.anthropic.claude-sonnet-4-6`, a **candidate pending live proof** (ruling 2), not a proven default |
| `revision` | Monotonic. What PMM-06 binds into signed claims (D5); compare-and-set as §4.4 |
| `enforcement_posture` | Report-only vs enforcing, per D3's staged rollout and PMM-09's flip |
| `updated_by` / `updated_at` | Canonical `users.id` of the platform admin, per the #4647 audit-column contract |

UNIQUE on `harness_compatibility_class` — one authoritative record per class, the same
"one rule per rung" property `uq_person_budget_default` enforces.

**Authority:** platform-admin only and fully audited, per the synthesis. AC-11 remains
satisfied structurally — no self route can write this table (§5.2), and it is a different table
from the preference rows, so the self surface cannot reach it even by accident.

**Live proof is required before the Claude-class value is authoritative.** D4 demands a bounded
real invocation in the actual runtime request shape, and §8.2 records that
`us.anthropic.claude-sonnet-4-6` appears in the gateway today only in pricing artifacts and
fixtures — pricing coverage is neither entitlement nor invocability (#2300). Seeding the record
with an unproven identifier would be the inert-config class at platform scale, so the migration
should seed the *structure* and leave the value's proof to the deployment step that can perform
it (PMM-09, #5427).

### 8.4 The harness compatibility class — vocabulary settled by ruling 2

This was the last genuinely blocking item in the note — the settings record's key had no defined
value set, so the migration could not be written. **Ruling 2 defines it**, in the direction this
note recommended:

> "Class IDs are stable and unversioned (`claude-agent-sdk`, `codex-sdk`); harness/contract
> revision is a separate versioned field and part of evidence/snapshot keys. PMM-02 owns the
> versioned Postgres default/posture records keyed by class."

So: the legal values are `claude-agent-sdk` and `codex-sdk`; the key is **unversioned**; the
compatibility-contract revision is a separate attribute; and **PMM-03 (#5420) owns the
persona→class registry** while PMM-02 owns the default/posture records keyed by it.

**Why unversioned is the right call, recorded because it prevents a specific outage.** Had the
class been versioned (`claude-agent-sdk@1`), a harness upgrade would mint a *new* class with no
seeded default — and under "no cross-class fallback" that is a hard failure for **every** persona
on the upgraded harness, triggered by a routine dependency bump. An unversioned key with the
revision carried separately means an upgrade changes evidence, not identity.

**The values do not exist in the tree yet, and that is now a seeding task rather than a blocker.**
Verified absent as class identifiers: no `harness_id`, no `compatibility_class`, no
`PERSONA_TO_RUNTIME` constant anywhere in `modules/` or `docs/`. `claude-agent-sdk` occurs only as
the npm package name (`modules/agent-factory/agent/package-lock.json:11`, pinned `0.3.220`), and
`codex-sdk` occurs nowhere at all. Two implementation consequences:

- **The CHECK constraint is now writable** — `harness_compatibility_class IN ('claude-agent-sdk',
  'codex-sdk')` — but it will need amending for every future class. Prefer the CHECK anyway,
  consistent with §4.2's closed-set rule; an unconstrained free-text key is how a typo becomes a
  silently defaulted class.
- **The class strings deliberately match the SDK package names.** That is legible but it invites
  the wrong inference: the class is a *compatibility grouping*, not a dependency reference, and it
  must not be derived from an installed package version at runtime. Worth a comment on the column.

**The preference table still stores no class.** It is derivable from `persona_key` through PMM-03's
registry, so storing it on a preference row would drift on harness upgrade in exactly the way §7.2
warns about. §7.2 is unchanged and reinforced.

**PMM-02 may ship with the Claude class seeded only**, and this remains the recommendation: under
D6 all current direct persona execution runs on the pinned Claude Agent SDK harness, so a
`codex-sdk` row would have no personas to serve until #5433 ships, and #5433 seeds its own class
as part of its own rollout (ruling 2: "#5433 registers `gpt-*` personas and a separately proven
Codex/GPT default"). The consequence to implement deliberately: a `gpt-*` invocation before #5433
lands must fail with an **actionable platform-readiness error naming the absent class**, not a
generic lookup miss. "No cross-class fallback" makes the missing row fatal by design, so the error
message is the only thing standing between an operator and an unexplained dispatch failure.

---

## 9. Security boundaries — summary

| Boundary | Mechanism | Evidence |
|---|---|---|
| Self surface cannot name another principal | No target parameter at any position; structural, asserted by signature+path inspection | `test_self_selection.py:141-158` |
| Cross-tenant read/write | Explicit `org_id` filter on every query; target-tenant check before administrative action; refusal audited in caller's tenant only | §5.6; #4692 §4.2 |
| Administrative surface | Authorization as first statement of every handler, asserted by source inspection | `test_authz.py:210,226` |
| Administration is a **human** act | `account_type == "human"` **and** `auth_source == "jwt"` **and** `ORG_UPDATE`, because `is_admin`/permission checks alone admit a service context | §5.3; `service_authority.py:226-228`; `tenant_resolver.py:304`; `auth_service.py:301-306` |
| A service principal cannot administer another | Self surface only; no administration route reachable with a service context | §5.2, §5.3 |
| An alias cannot become an owner | Canonical ID resolved server-side; a presented alias is an input, never a key | §3.2.3 |
| A signed caller cannot reach the human branch of the shared handler | `X-Caller-Identity` presence is **terminal**: resolved to a registered agent or rejected, never falling through to JWT (#3985) | §5.2.1; `auth/dependencies.py:145-158` |
| An unregistered role gets no context at all | Refused, not given `org_id=""` — the #3985 fix — with this story's alias registry as a second required layer | §5.2.1, §4.6; `auth/dependencies.py:178-188` |
| Provenance cannot alter a selection | No query resolving a preference may filter, order or branch on `principal_source` / `updated_by_source`; lookup key is `(org_id, principal_kind, principal_id, persona_key)` only | §4.2 |
| One canonical principal has one kind | **Service layer, not the database** — with `principal_kind` in the key (§4.2) the UNIQUE constraint no longer refuses two kinds for one canonical ID and persona. Kind is derived from which registry resolved, never accepted from the body; a disagreement is a refusal | §7.3; §11 AC-06 |
| Human vs service self — **not** used as a gate | Both dependencies exist but neither gates the self surface: one router serves both audiences (ruling 6), and `principal_kind` is derived from the resolved context | §5.2; `auth/middleware.py:193`, `:173` (no production callers) |
| Platform default | Separate non-tenant table, platform-admin only, unreachable from any self route | §8.3 |
| Preference confers no access | A preference selects within the permitted set and can never widen it | D1, D3 |
| Lost update | Integer compare-and-set inside the transaction; 409 on mismatch | §4.4; `execution_store.py:963` |

**A preference is not an entitlement.** Writing a row grants no model access, no AWS
destination and no budget — D1 and D3 both say so, and it is why save-time validation
*intersects* rather than *authorizes*.

---

## 10. Dependencies and parallelism

**Every decision this note previously escalated is settled** (§12). What remains is a **scope
re-pricing** (§6.1, AC-01) and the cross-story reconciliations in §10.1.

**The canonical ID binds four stories**, and it is now a shared *dependency* rather than a shared
*unknown* — with PMM-02 owning the mechanism, so PMM-02's schedule is on three other stories'
critical path:

| Story | What it needs from the canonical contract |
|---|---|
| PMM-02 (this) | The stored principal key and its write-time validation |
| PMM-04 (#5422) UI | The principal it displays and administers must be the same ID, and it needs the manageable-principals endpoint (§5.3.1) |
| PMM-05 (#5423) CLI | Self-authentication must resolve to the same ID server-side |
| PMM-06 (#5424) snapshot | The signed claim must name the same principal, or a snapshot authorizes a different one |

If these four disagree, a preference saved through one surface is invisible to another —
the same inert-configuration failure in a new shape.

**Blocked on another story:** AC-07's real-catalogue proof needs PMM-03 (#5420). The
interface (§7.1) lets the rest be built and tested first, and PMM-03's head now **publishes**
that signature (`5420:348-351`), so the mock this story tests against can be shaped against a
real contract rather than a guess. PMM-03 also owns the compatibility class (§8.4) and its
persona→class registry (`5420:92-108`), which makes it the strongest single dependency here —
though not a blocking one, since §8.4 needs the derivation only for error text.

**New dependency: #5433.** The settings record's key exists because #5433 requires a Codex/GPT
class with no Claude fallback. PMM-02 need not wait for #5433 to ship — it may seed the Claude
class only (§8.4 item 3) — but the *key's vocabulary* must be agreed with #5433 before the
migration is written, or the two stories will mint incompatible class identifiers.

**Can run in parallel with this story:**

- PMM-03 (#5420) — independent; this story consumes its function.
- PMM-04 (#5422) UI and PMM-05 (#5423) CLI — can design against §5's contract, but should
  not merge before the endpoints exist, and both inherit the canonical principal ID.
- PMM-06 (#5424) snapshot — needs the `revision` contract (§4.4) **and** the canonical ID.

**Must follow:** PMM-07 (#5425) resolver, then PMM-09 (#5427) live acceptance. Nothing
resolves from this table until PMM-07, which is what makes §6.3's rollback claim true.

### 10.1 Cross-story reconciliations — read against the siblings' *current* heads

Re-read at the heads named below, which have **all five advanced** since the previous revision of
this note (`e2c7d099`) quoted them.
That movement is the finding, not a footnote: **three of the five conflicts this note previously
reported have since been fixed by the sibling itself**, so continuing to report them would have
been the same staleness this section exists to prevent. Those rows are rewritten to record the
closure rather than left asserting a conflict that no longer exists. What remains is two genuine
open items and one inconsistency internal to a sibling.

| Sibling head read | State at that head | What must change, and where |
|---|---|---|
| `agent/issue-5423` @ `b9e8e96e` (PMM-05 CLI) | **Both conflicts resolved by PMM-05 itself.** It now explicitly repudiates keying on `service_accounts.id` (`:43-50`, `:255-265`: "Raw `service_accounts.id`, `agent_name`, `client_id` and role ARN **never** own a preference") and formally withdraws the second `/agent` mount (`:1216-1221`, on the ground this note gave — the edge already strips the prefix) | **Nothing. Closed, and recorded as closed** so a later reader does not reopen it from revision 5's text |
| `agent/issue-5425` @ `82bc735f` (PMM-07 resolver) | **Resolved.** `codex-gpt` no longer appears as vocabulary — it survives only as PMM-07's own correction of it (`:980-981`: "The previous revision wrote `codex-gpt`, which this note invented"). Class IDs are `claude-agent-sdk` / `codex-sdk` | **Nothing. Closed** |
| `agent/issue-5420` @ `25ece717` (PMM-03 catalogue) | **The persona→class gap this note previously reported as unowned is now owned.** New §2.4 (`:92-108`) defines the persona→compatibility-class registry, puts `compatibility_class` on every persona row (`:102`), assigns all twelve current personas to `claude-agent-sdk` (`:100`) and reserves `codex-sdk` with no personas (`:103`). It also publishes the `validate_selection` signature (`:348-351`) — **and it matches §7.1's**, including `principal_kind` and excluding provenance | **Two corrections to this note, both applied:** §7.1 no longer says PMM-03 "does not yet publish a signature", and §8.4/§11 no longer report the derivation as unowned. One residual: PMM-03 `:110-114` flags whether its registry *is* #5433's input or a projection of it as an open cross-epic question. That is PMM-03's to route and **does not block PMM-02**, which needs the derivation only for error text (§8.4) |
| `agent/issue-5424` @ `f9f0ec68` (PMM-06 snapshot) | Its field table now carries the canonical ID correctly (`:610`, "never `service_accounts.id`, `agent_name`, `client_id`, an ARN or any caller-supplied" value), and C1/C7 are marked settled (`:1626`, `:1633`). **But two sentences inside it still contradict that:** `:708` maps a `service_policy` authority's `principal_id` to "the registered service-account ID (`actor.user_id`)", and `:744` still reads "**Operator decision required (§9, C1)**" for a condition its own §9 marks settled on PR #5442 | **PMM-06 to fix `:708` and `:744`.** `:708` is the one that matters — an authority-mapping table naming a raw subject is what a developer implements from, and it would key a snapshot on an alias after the field table forbade it. Not a design disagreement: PMM-06's own settled C1 agrees with §3.2.3. `:744` is lower risk but re-opens a settled gate in a reader's mind |
| `agent/issue-5424` @ `f9f0ec68` (PMM-06 mapping key) | `:578-580` reports this note as having "demoted `principal_kind`" and flags the divergence from PMM-01 for the synthesis | **Closed by this revision, in PMM-06's favour.** §4.2 puts `principal_kind` back in the key, which is where PMM-01 has had it all along (`5417:21`, `:143`, `:397` @ `b8045dbf`). PMM-06 should retire the flag. Its own decision to keep kind as a **snapshot** field regardless (`:579-581`) is correct and independent of the storage key |
| `agent/issue-5424` @ `f9f0ec68` (PMM-06 `policy_revision`) | `:614` still defines `policy_revision` as one value detecting "the mid-flight edit (AC-04) and a tampered revision (AC-05)", without saying which of this story's revisions it is | **Unchanged and still open.** This note exposes **two** monotonic revisions and they are not interchangeable: the **per-row** `revision` (§4.4) and the **settings record's** `revision` (§8.3). A single scalar over the tenant would make an unrelated principal's edit look like tampering; a settings-record revision would miss a preference edit entirely. **Recommended:** `policy_revision` is the pair — the per-row revision of each frozen mapping plus the settings-record revision PMM-06 already carries as `allowlist_policy_revision` (`:615`). **PMM-06 owns the field; this note owes it both inputs and supplies them** |
| `agent/issue-5422` @ `5fc0fb4f` (PMM-04 UI) | Requires the full-row 409, the discovery endpoint's fields (`:351-356`), and correctly insists the browser never joins aliases (`:364`) or branches on `source` (`:365`) | **Substance adopted** (§4.4, §5.3.1); **three strings pinned there** because the two notes agree on meaning and differ on spelling: `principal_kind` is returned and **not** optional (PMM-04 shows it at `:352` but calls it omittable at `:360` — it cannot be, now that it is in the binding key §4.2); the field is `tenant_label` not `tenant` (`:354`, display-only per `:395`); and `source` carries PMM-04's hyphenated display labels (`:355`), which are deliberately **not** §4.6's stored `alias_source` values, so the mapping must live server-side in one place |
| `agent/issue-5422` @ `5fc0fb4f` (PMM-04 authority) | `:371` records this note's gate as "platform-admin plus a tenant check", and concludes `manageable` is true "only for platform admins" with the selector "empty for everyone else" | **Stale — PMM-04 to correct.** B2 settled on a **human org administrator, in-tenant** (§5.3, §12), so the selector is non-empty for org admins and the UI's empty-list path is not the common case it assumes. PMM-04's actual conclusion is untouched and is adopted at §11's AC-10: the acceptance record must state which gate was in force, or AC-06 reads as proving delegated ownership when it proved admin access |

**One cross-cutting gap this note is closing on everyone's behalf.** Read together, *none* of the
five heads describes a service-principal **entity** distinct from its aliases, and none says how
the resolved canonical ID reaches a handler — PMM-03 (`:388`) and PMM-05 (`:1226`) both state that
resolution happens "in authentication" without naming a carrier. Both gaps are PMM-02's by ruling
1, and both are now specified (§4.6, §5.2.2). They are recorded here because four stories consume
the result and would otherwise each invent one.


**Why this section exists at all.** Four of the five rows are the *same* failure in different
places: two stories naming a principal, a class or a revision differently, so a value written
by one is unfindable or misread by another. That is #4744's identifier-mismatch class, and it
is the reason this epic exists. None of it is caught by a passing unit test in either story —
only by reading both heads together, which is what this section records having done.

---

## 11. Acceptance coverage

| AC | Mechanism | Deterministic in CI? |
|---|---|---|
| AC-01 migration up/down, linear head | Migration test + parity assertions per `test_052_*` | **Yes, if §4.6.1's portable form is chosen.** The preference table's key is a plain composite unique with no dialect guard (§4.3), and the alias table's active-row uniqueness is provable in CI **only** as the `COALESCE(revoked_at, '')` expression index. If the implementer instead takes the `postgresql_where` partial index, the invariant is **not** proven by the SQLite test suite — SQLAlchemy renders it as a plain unique index there, which would forbid the revoke-then-re-register this design requires. That divergence must be **disclosed in the acceptance record**, not left for a reader to infer from a green run (§4.6.1). Also **re-scope first**: four tables plus an auth-path change, not one table (§6.1) |
| AC-02 saved vs effective, two sources | Read model returns a row per catalogue persona with `source` (§4.5) | Yes, with a stubbed catalogue |
| AC-03 bad persona / kind / principal refused | Alias resolution + in-tenant validation (§7.3); no FK exists on any path, so this tests code not constraints | Yes for `self`/`sa_registration` (same database); S2 needs a registry stub; S3 once the alias registry exists — which is now in scope here (§4.6), so no longer blocked on another story |
| AC-04 self endpoint ignores injected target | Three assertions, not one: the **forbidden-name** half of `test_self_selection.py:141-158` with the blanket `"{"` check replaced by an allowlist of exactly `{persona_key}` (§5.2 — copying it verbatim fails a correct implementation of ruling 6's routes); the other principal's row unchanged, through the real FastAPI route; and the stored `principal_id` equal to the **canonical** ID, not the token subject (§5.2) | **Yes** |
| AC-05 cross-tenant read/write fails closed | Explicit tenant checks; assert no row **and** no audit row in tenant B | Yes |
| AC-06 concurrent save conflicts | Compare-and-set; second save 409s carrying current revision | **Yes for the concurrency half** (§4.3). But the **uniqueness half is now narrower at the database level than it was**, and the acceptance record must say so: with `principal_kind` in the key (§4.2) the UNIQUE constraint no longer refuses two rows for one canonical ID and one persona **differing only in kind**. A test that writes one row and asserts an `IntegrityError` on the duplicate still passes, and still proves less than it did. The one-kind-per-canonical-ID invariant is a **service-layer** assertion (§7.3) and needs its own test — write a preference for a canonical ID as `service_account`, then attempt the same ID and persona as `human`, and assert a refusal rather than a second row (§7.3 states the invariant; §3.4 the one-word spelling difference that would defeat it). `principal_source` remains out of the key entirely (§4.2) |
| AC-07 unusable model refused | `validate_selection` (§7.1) | **No** if PMM-03 has not merged — a mock establishes that this note calls the interface, not that the interface refuses the right models, and that limit must be disclosed. The second disclosure earlier revisions carried is **now withdrawn**: PMM-03's current head owns the persona→class registry and publishes a `validate_selection` signature matching §7.1's (`5420:92-108`, `:348-351`), so the derivation is no longer unowned. What remains is sequencing, not ownership (§10.1) |
| AC-08 reset removes row, audit survives | Separate table, no FK (§5.5) | Yes |
| AC-09 service-account self-management | §5.2 + §3.2.1 + §5.2.1 | **Yes, with one non-obvious condition.** A service principal manages itself through the *same* route as a human, and "its own mapping" is well-defined because all three alias forms resolve to one canonical ID. The test must go **through the route with `trust_apigw_headers` enabled** (§5.2.1) — a service context hand-built in the test bypasses the exact default-off setting that would make every real machine call fail |
| AC-10 administration in-tenant only | §5.3, §5.3.1 | **Reworded, not open.** "As a service-account owner" is unimplementable — `service_accounts` has no owner column — so the criterion reads "**as an organization administrator, within the caller's tenant**". Provable once the manageable-principals endpoint exists, which ruling 1 puts in this story. The acceptance record must state which gate was in force, or it will read as proving delegated ownership when it proved admin access (PMM-04 makes the same point at `5422:371`) |
| AC-11 default not writable via self API | Structural — separate non-tenant table, no self route can reach it (§8.3) | Yes |

**Both AC-04 and AC-05 must be traced through the real FastAPI route with a token context**,
not by calling the service layer, because the property under test is the absence of a target
parameter on the route — which a service-level test cannot observe. The plausible wrong
result the issue names, a test asserting 200 while the other principal's row was in fact
modified, is caught only by asserting on the *other* row.

Checks for the implementing PR: `cd modules/gateway && ruff check src/ tests/ && ruff format
--check src/ tests/ && python3 -m pytest tests/ -q`.

---

## 12. Operator decisions — all settled

**Nothing in this section is a request.** Every decision this note previously escalated has an
answer, and each answer is recorded below with where it is applied and what it cost. Kept as a
record rather than deleted so a reader of the PR can see *which* question each design choice
answers — a settled decision with no trace is how the same question gets re-litigated in the
next story.

| # | Question this note asked | Settled answer | Source | Applied in |
|---|---|---|---|---|
| B1 | Which service-account subject form is the stored key | Neither raw form: an **opaque immutable canonical ID** produced by alias resolution | Unified rulings, ruling 1 | §3.2.3, §4.1, §4.6 |
| B2 | Who may administer another principal's preference | A **human** org administrator, in-tenant, with `account_type == "human"` **and** `auth_source == "jwt"` **and** an org-update permission — `is_admin` alone is insufficient because two paths set it on a service context | Unified rulings + §5.3's verification | §5.3, §9 |
| 1 | Who owns the alias registry, the canonical resolution and the manageable-principals endpoint | **PMM-02 owns all three**, including canonical resolution *inside authentication* | Ruling 1 | §4.6, §5.2.1, §5.3.1 — and §6.1's re-pricing is the cost |
| 2 | Alias re-registration semantics | **A re-registered alias mints a new canonical principal by default.** Old preference rows become inert orphans rather than being inherited by whoever recycled the name | Ruling 1 (adopts this note's recommendation) | §3.3, §4.6 |
| 3 | The harness compatibility-class vocabulary and its owner | `claude-agent-sdk` \| `codex-sdk`, **unversioned**; contract revision a **separate** versioned field; **PMM-03 owns the persona→class registry**, PMM-02 owns the class-keyed default/posture records. PMM-03's head has since **built** that registry (`5420:92-108`), so this is discharged rather than merely assigned | Ruling 2 (adopts this note's recommendation) | §8.3, §8.4 |
| 4 | Sequencing against PMM-03; may the Claude class ship alone | **Yes — seed the Claude class only**, with two disclosures that must appear in the acceptance record, not be quietly dropped: AC-07 is unproven against a real catalogue until PMM-03 merges, and D4's `us.anthropic.claude-sonnet-4-6` is a **candidate pending live invocation proof**, not a verified default | Ruling 2 + D4 | §8.2, §8.3, §11 (AC-07) |
| 5 | One API contract or two self surfaces | **One.** The FastAPI self routes exist once at `/me/persona-models`; machine callers arrive at external `/agent/me/persona-models` and reach the same handler via the edge prefix strip. A `require_human_user`/`require_service_account` split would duplicate the backend router and is prohibited | Ruling 6 | §5.1, §5.2, §5.2.1 |
| 6 | Is `principal_kind` key material or derived metadata | **Key material.** The binding key is `(org_id, principal_kind, principal_id, persona_key)`; `principal_source` stays out. The cost — a DB constraint that permits one canonical ID to hold two kinds for one persona — is named, not assumed away, and the lost invariant moves to the service layer | Focused review 2026-09-18T18:09:53Z | §4.2, §7.3, §11 (AC-06) |
| 7 | Where does a service principal's name, status and lifecycle live | A **canonical entity table** (`service_principals`) distinct from the alias rows, because PMM-04's endpoint has no `display_name` to return otherwise, status is not a property of any one alias, and immutability needs a row to be immutable in | Focused review 2026-09-18T18:09:53Z | §4.6 |
| 8 | How does the canonical ID reach a handler | **Additively** — one new optional `canonical_principal_id` on `TokenContext`. `user_id` keeps its meaning and value (340 read sites), and empty means "not resolved", never a silent fall back to the token subject | Focused review 2026-09-18T18:09:53Z | §5.2.2 |

**What is left, and it is not an operator decision.** Two things:

1. **AC-01's re-scope** (§6.1) — four tables, an auth-path change, and §4.6.1's dialect
   disclosure. This is a wording fix on
   the issue's acceptance criteria that follows mechanically from ruling 1; it needs doing before
   implementation is scheduled, not deciding.
2. **Two cross-story items in §10.1**, down from five — the other three were fixed by the
   siblings themselves and are recorded as closed. What is open: PMM-06's `:708`/`:744`, which
   contradict PMM-06's own settled §9 rather than disagreeing with this note, and
   `policy_revision`'s derivation, which PMM-06 owns and this note now supplies both inputs for.
3. **One item that reads like a new conflict and is in fact a closed one.** The review directs
   `principal_kind` back into the binding key (§4.2), and PMM-06 `:578-580` reports this note's
   earlier demotion of kind as a divergence from PMM-01. Re-read at `b8045dbf`, **PMM-01 has
   defined the key with kind in it throughout** (`5417:21`, `:143`, `:397`). So the review restores
   alignment with the epic note rather than creating a disagreement, and PMM-06's flag can be
   retired. What §4.2 and §7.3 add is the honest accounting: the constraint got weaker, and the
   invariant it used to carry now lives in the service layer where it must be tested.

**One dependency that has moved since the last revision.** "Which compatibility class is this
persona in" was recorded here as answered by nothing in the platform and owned by nobody. The
first half is still true of the *running* platform; the second is not — PMM-03's head defines the
registry (`5420:92-108`) and assigns all twelve current personas to `claude-agent-sdk`. PMM-03
itself flags at `:110-114` whether that registry *is* #5433's input or a projection of it. PMM-02
needs the derivation only for the text of an absent-class error (§8.4) and never for a stored
value (§7.2), so this story is **not blocked** either way. PMM-07 consumes it directly and is.
