# Design Note: Cross-Org Person-Scoped Cloud-Agent Budgets (Issue #4620)

> **Status**: Design-review (spike output) — one open question needs a human ruling (§5.7)
> **Author**: @agent-architect
> **Date**: 2026-09-01
> **Issue**: #4620 — person-scoped budgets are org-partitioned in storage
> **Mode**: Per-issue spike
> **Verdict**: Design-complete for the read + isolation model; **the deny question is presented as a ruling, not decided unilaterally** (§5.7)
> **Related**: #4300 (root-human attribution), #4536 (authoring per-person caps), #4396 (fused envelope), #4487 (rollup narrowing), #2982 / #3068 / #3074 (tenancy direction), #4132 (attribution partition)

---

## 0. Executive summary

The issue's premise is **confirmed by code, not assumed**. Both budget tables are
uniquely keyed with `org_id` first (`modules/gateway/src/shared/models/budget.py:22`,
`:42`), the usage tracker writes *every* entity row — including `root_user` — into the
one `org_id` off the chat log (`lambda/budget-usage-tracker/handler.py:388`, `:499-509`),
and `/api/me/budget` reads exactly one partition
(`src/budget/me_routes.py:707`). A person whose runs execute outside the partition
their cap was authored in gets a cap that caps nothing and a page that shows nothing.

Three findings change the shape of the fix relative to the issue text:

1. **The identifier already fuses across orgs; only the partition splits.** The
   `root_user` ledger key is a canonical `users.id` UUID
   (`src/budget/run_binding.py:215-217`), and the webhook identity resolver keys the
   person by `provider_user_id` with **no org in the lookup key**
   (`webhook-ingress/lambda/common/identity_resolver.py:229-239`, `:563-568`) — so a
   run executing in `aws-e` writes the *home-tenant* `users.id`. `/me/budget` resolves
   the same UUID org-free from `cognito_sub` (`src/shared/identity/resolver.py:81`).
   **The keys match; only `org_id` differs.** Aggregation is therefore a widened
   `org_id` predicate, not an identity-stitching project. (Caveat in §3.3.)
2. **"Home org" is really "active session tenant."** `attributed_org_id` defaults to
   the token's `org_id` (`src/shared/schemas/auth.py:116-127`). The read partition
   follows the switcher, which materially changes the #2982 question (§6).
3. **A cross-org *denial* is constrained by infrastructure, not just policy.** The
   in-flight reservation key embeds `{org_id}` as a Redis Cluster hash tag so the
   multi-key atomic Lua stays single-slot (`src/budget/reservations.py:226-241`). A
   person key cannot share that slot. This is the real cost of Option A and it is
   absent from the issue's framing (§5.5).

**Recommendation:** a two-layer model where **org-authored caps never cross a tenant
boundary** and a **person-level cap is authored by the person themselves (or platform
admin), stored partition-free, and *may* deny anywhere** — because the person is the
one party common to every org involved, so a ceiling on their own agents is
self-restraint, not authority inversion. The read model is per-org lines plus one
labelled aggregate, visible **only to the person and platform admin** — never to a
home-org admin. Full rationale §5.6; the ruling being asked for is §5.7.

---

## 1. Verified current state

| Fact | Evidence |
|---|---|
| `budget_configs` unique key is `(org_id, entity_type, entity_id, period_type)` | `src/shared/models/budget.py:22` |
| `budget_usage` unique key is `(org_id, entity_type, entity_id, period_start, period_type)` | `src/shared/models/budget.py:42` |
| Tracker writes all entities into one `org_id` | `lambda/budget-usage-tracker/handler.py:388`, `:499-509`; upsert key `:298` |
| That `org_id` is the **execution/attributed** tenant | chat logs write `org_id=context.attributed_org_id` — `src/proxy/routes.py:410`, `:446`, `:701`, `:783` (#4132) |
| `root_user` row is a *third* row, not a second debit | `handler.py` comment at `:445-470`; gated on presence and `!= user_id` (`:495`) |
| Enforcement's org level uses `attributed_org_id` | `src/budget/enforcement_service.py:442-443`; header path `:1439`, `:1460` |
| `root_user` ids: humans **bare**, services `service:`-prefixed | `src/budget/enforcement_service.py:90-93`, `:118-123` (#4344) |
| `/me/budget` reads exactly one partition | `src/budget/me_routes.py:707`; `_read_cap` `:546-555`, `_read_settled_spend` `:518-530` |
| Caller's canonical id resolves **org-free** | `src/shared/identity/resolver.py:81` (global partial unique index on `cognito_sub`, `src/shared/models/organization.py:114-119`) |
| Cap authoring is confined to the authorized target org | `src/admin/routes.py:311-324`; scope check `src/admin/access_control.py:275-287`; write `src/admin/service.py:2000-2001` |
| …except **platform admin**, which skips the scope block | `src/admin/access_control.py:276` |
| Cap `entity_id` is resolved *within* the target org | `src/shared/identity/resolver.py:244`, `:249`, `:284`, `:295` — "never walks sideways out of the target org" |
| `tenant_memberships` is deliberately cross-partition (no `TenantMixin`) | migration `021_tenant_memberships.py:42`, `:52-54`; model `src/shared/models/onboarding.py:53-86` |
| `TenantMixin` is a **column only** — no query filter, no RLS | `src/shared/models/base.py:12-15` |
| Reservation key embeds `{org_id}` as a cluster hash tag | `src/budget/reservations.py:226-241` |
| Fan-out across member tenants is an established pattern | `src/admin/connections/routes.py:296-306`; sanctioned by `docs/design-notes/3074-…md` §1.4 |

### 1.1 One correction to the issue body

The issue says `/api/me/budget` "reads only the caller's **home-org** partition." It
reads `current_user.attributed_org_id`, which for a browser JWT defaults to the token's
`org_id` — the **active session tenant** (`src/shared/schemas/auth.py:116-127`). Today
those coincide for the operator, so the symptom is identical; but the distinction is
load-bearing for §6, because the switcher can already change what the page shows.

---

## 2. The operator's scenario, mechanically

Person: home tenant `pranavsharma1000`; runs execute in `aws-e`.

| Row | `org_id` | `entity_type` | `entity_id` | Who reads it |
|---|---|---|---|---|
| The authored $5,000 cap | `pranavsharma1000` | `root_user` | canonical `users.id` | nothing — no runs execute in this partition |
| The real settled spend | `aws-e` | `root_user` | **same** canonical `users.id` | enforcement in `aws-e`, if a cap existed there |
| What `/me/budget` queries | `pranavsharma1000` | `root_user` | same id | finds the cap, finds **no usage row** → `$0` |

The live evidence in the issue (`Including root-human entity: 650f093f…` in `aws-e`,
`handler.py:497`) is the tracker writing row 2 correctly. Nothing is broken in
attribution; the cap and the spend are simply in different partitions, and **the
`entity_id` is the same string in both** — which is what makes every option below
tractable.

---

## 3. Layer 1: per-(org, person) caps — keep exactly as-is

Unchanged from #4536. Each org authors caps in its own partition, enforcement reads
that partition, and this is what protects an org's own budget. This layer is correct
and load-bearing; nothing in this note weakens it.

### 3.1 Why it must stay the denying layer
It is the only layer whose authority is unambiguous: the org that pays for the spend
sets the ceiling on spend executing inside it. Row 2 of the issue's isolation table
("foreign-org cap allowed to deny") is a hazard **only** for a layer other than this
one.

### 3.2 Its gap
It cannot answer "how much can this person's agents spend in total?" — that question
spans partitions by construction.

### 3.3 The multi-`users`-row caveat (must be handled by any aggregation)
A person is normally one `users` row, and the webhook resolver's org-free lookup means
one `root_user` key across all orgs (§0.1). But `users` carries `TenantMixin`, and a
person independently onboarded into two orgs **can** have two `users` rows and thus two
`root_user` keys (see `tests/shared/test_resolve_root_user_entity_id.py:302-320`, where
one GitHub account has distinct ids per org). Any aggregate must therefore resolve the
person set through `user_identities.provider_user_id` (the GitHub numeric id —
`src/shared/identity/resolver.py:267-286`) and sum over **all** their `users.id`
values, not `GROUP BY entity_id` alone. A bare `GROUP BY entity_id` would under-report
for exactly the multi-org population this issue is about.

**Anchor recommendation:** the person-level anchor is the **GitHub numeric id**
(`provider_user_id`), consistent with CLAUDE.md's anchor-stable guidance. `users.id`
stays the ledger key; the anchor is the cross-org join key.

---

## 4. Layer 2: the person-level cap — storage and authoring

### 4.1 Storage: a new partition-free table, not a sentinel `org_id`

```
person_budget_configs
  id              PK
  person_anchor   varchar   -- "github:<numeric_id>", NOT org-scoped
  period_type     varchar
  budget_amount_usd  numeric(10,2)
  enforcement_mode   varchar  -- soft | hard  (see §5.7)
  authored_by_user_id varchar
  created_at / updated_at
  UNIQUE (person_anchor, period_type)
```

**No `TenantMixin`** — deliberately, exactly like `tenant_memberships`
(`021_tenant_memberships.py`), which is the existing precedent for a legitimately
cross-partition table. Rejected alternatives:

| Alternative | Why rejected |
|---|---|
| Reuse `budget_configs` with a sentinel `org_id` (`"__person__"`) | Puts two id namespaces under one unique constraint — precisely the collision `EntityType` and #4344 exist to prevent (`src/shared/schemas/budget.py:36-60`). Every `org_id`-filtered query in the module would need a sentinel exclusion or would silently read it as a tenant's row. |
| Store it in the person's home-org partition | Recreates the bug: "home org" is mutable (switcher) and arbitrary; the cap's authority would appear to derive from one tenant. |
| `parent_tenant_id` org-linking (#2954) | Already exists (`src/admin/tenants/routes.py:182`, resolution at `src/admin/onboarding/handler.py:261`) but **fuses the tenants entirely** — one budget, one ledger, shared visibility — and is platform-admin-only (`admin/tenants/routes.py:100`), single-level (`:154`). Right tool for "these orgs are one company", wrong tool for "one person spans two unrelated companies." Discussed as Option D, §5.4. |

**No aggregate spend table.** Person-level spend is derived by summing existing
`root_user` rows across partitions. A second accumulator would be a
denormalized duplicate of the same dollars and the #4322 double-count family
(`alembic/versions/032_budget_usage_org_entity_type.py`) is the standing warning
against that.

### 4.2 Authoring authority

| Author | May set person-level cap? | Rationale |
|---|---|---|
| The person themselves | **Yes** | Self-restraint over their own agents. No cross-tenant authority is exercised. |
| Platform admin | **Yes** | Already holds cross-org authority by design (`access_control.py:276`). |
| An org admin (incl. home org) | **No** | This is the authority inversion of isolation-table row 2. An org admin's reach stops at their own partition's cap. |

This split is the crux of the recommendation, and it is what lets a person-level cap
deny without inverting authority (§5.6).

---

## 5. The hard question: may a person-level cap DENY in a foreign org?

Each option is graded against **all three rows** of the issue's isolation table, then
walked end-to-end for the operator.

### 5.1 Option A — person-level cap denies anywhere, authored by home org

The literal reading of "a person-level cap."

| Isolation row | Outcome |
|---|---|
| Cross-org read without authz | 🔴 Enforcement in `aws-e` must read a cap row governed by `pranavsharma1000`, and the denial message names it. |
| Foreign-org cap allowed to deny | 🔴 **Direct hit.** A `pranavsharma1000` admin halts workloads inside `aws-e`. `aws-e` cannot see, audit, or override the ceiling stopping its own work. |
| Aggregation double-counts | 🟠 Manageable — sum `root_user` rows only, never mixed with org/user rows. |

Plus the infrastructure cost of §5.5.

**Operator walkthrough.** Cap lives in `pranavsharma1000`; enforcement in `aws-e`
consults it and denies at $5,000; `/me/budget` shows one fused figure. Convenient — and
`aws-e`'s workloads are now stoppable by an admin of an org it has no relationship
with. **Rejected.**

### 5.2 Option B — person layer aggregates and alerts only; denial stays per-org

| Isolation row | Outcome |
|---|---|
| Cross-org read without authz | 🟠 Only the aggregate read needs authz — solvable (§7), and totals-only for the person themselves is the narrowest possible crossing. |
| Foreign-org cap allowed to deny | 🟢 **Structurally impossible.** No foreign cap ever denies. |
| Aggregation double-counts | 🟠 Same manageable risk as A. |

**Operator walkthrough.** Their $5,000 becomes an *alerting* threshold. Caps that
actually stop work must be authored per-org — so to bound `aws-e` spend they author a
cap in `aws-e` (which they can, as its admin). `/me/budget` shows per-org lines plus a
labelled cross-org total and an alert state. **Nothing enforces the $5,000.**

Honest cost: the issue's headline complaint is "a cap that doesn't cap," and Option B
does not fix that — it makes the *number* true while leaving the *ceiling* advisory.
For an operator who wants one number they cannot exceed, B is a documentation of the
limitation, not a remedy.

### 5.3 Option C — self-authored person cap may deny; org-authored caps never cross

The recommendation. Identical to B in storage and read model; differs only in who may
author the person-level cap and whether that layer is permitted to deny.

| Isolation row | Outcome |
|---|---|
| Cross-org read without authz | 🟢 The only cross-partition read is the person's own aggregate, plus enforcement reading a cap keyed to the person, not to any tenant. No tenant reads another tenant's data. |
| Foreign-org cap allowed to deny | 🟢 **Avoided by construction.** No *org* ever denies outside itself. The denying authority is the person over their own agents. `aws-e` is never subject to an admin of `pranavsharma1000` — it is subject to the person whose agents are doing the spending, who is an `aws-e` member acting on `aws-e` work. |
| Aggregation double-counts | 🟠 Same manageable risk; mitigation §7.3. |

**The asymmetry that makes this safe:** row 2's hazard is *whose* authority crosses the
boundary. An org admin's authority crossing is an inversion. A person's own ceiling on
their own agents is not a crossing at all — the person is the single party present in
every one of those orgs, and the spend being capped is spend they set in motion.

**Operator walkthrough.** They author a **personal** $5,000 monthly cap (Settings →
their own budget, not the org's Budget Management screen). It is stored in
`person_budget_configs` keyed `github:<their numeric id>` — no partition. Their agents
run in `aws-e`; the settled `root_user` row accrues there as today. Enforcement in
`aws-e` evaluates the per-org hierarchy exactly as now **and** the person layer, whose
denominator is the cross-org sum of their `root_user` rows. At $5,000 total across all
orgs, their agents stop — everywhere — with a denial that names the *person* scope, and
`aws-e`'s own org/team caps are untouched and still independently authoritative.
`/me/budget` shows: per-org lines (`aws-e $X`, `pranavsharma1000 $0`), the person-level
cap with its cross-org denominator, and headroom. Every symptom in the issue resolves.

Residual, and it must be stated: `aws-e` workloads *can* now be stopped by a ceiling
`aws-e` did not author and cannot raise. The mitigations are that only the person (or
platform admin) can author it, the denial reason names the person scope explicitly
(precedent: `DenyReason` / `scope="root_user"`, `enforcement_service.py:1021-1044`), and
the person-level layer defaults to `soft` (§5.7).

### 5.4 Option D — declare it out of scope; use `parent_tenant_id` fusion

Link `pranavsharma1000` under `aws-e` (`src/admin/tenants/routes.py:182`). One tenant,
one partition, bug gone with zero new storage.

Valid **only** when the orgs genuinely are one billing entity. It requires platform
admin, is single-level (`:154`), and fuses all visibility — an unacceptable answer for
a consultant in two client orgs. Worth documenting as the recommended path for the
"same company, several GitHub orgs" case, which is otherwise likely to be
mis-solved by the new machinery. **Not a general answer.**

### 5.5 The infrastructure constraint on any denying person layer (A and C)

`ReservationTarget.key()` puts `org_id` in braces as a Redis Cluster hash tag so every
key one request touches lands in one slot, keeping the multi-key atomic Lua valid
(`src/budget/reservations.py:226-241`); that Lua checks every key's headroom before
incrementing any (`:78`). A person-level key cannot carry an `org_id` hash tag without
re-partitioning the very thing it exists to span. Consequences:

- The person key must be reserved in a **second** Lua call → the two checks are not
  mutually atomic. Bounded overshoot under concurrency, of the same family the
  existing `grace_window.py` / `test_budget_overshoot.py` already reason about.
- Or the person layer enforces on the **settled ledger only** — simpler, no cluster
  problem, but a lagged denominator (settlement is asynchronous; `me_routes` already
  surfaces this as `freshness.cost_backfill_lag`).

**Recommendation:** ship settled-ledger-only first (bounded, honest, no new atomicity
claims), and treat a person-level reservation key as a follow-up only if measured
overshoot justifies it. The implementation issue must state the overshoot bound rather
than implying a hard cap it does not deliver.

### 5.6 Recommendation

**Option C**, with the person-level layer defaulting to `soft` (alert-only) and `hard`
available as the person's explicit choice. That composition is deliberate: it makes
Option B the *default behaviour* and Option C's denial an opt-in the person performs on
themselves — so the platform never surprises a foreign org with a denial nobody there
chose, while the operator who wants a real ceiling can have one.

### 5.7 ⚠️ The ruling requested

> **May a person-level cap, authored by the person themselves, hard-deny spend
> executing in an org that is not the cap's home org?**

- **Recommended: yes, opt-in, default soft.** Tradeoff: the person gets a ceiling that
  genuinely bounds total spend; the price is that an org's workload can be stopped by a
  ceiling that org did not author and cannot raise. Bounded by: person-or-platform-admin
  authoring only, soft default, denial names the person scope, and the org's own caps
  remain independently authoritative.
- **The conservative alternative: no — Option B.** Denial stays strictly per-org and the
  person layer only aggregates and alerts. Tradeoff: perfect tenant hygiene, and "one
  cap that bounds a person's total agent spend" remains impossible; the operator's
  $5,000 stays advisory.

A human must pick. Everything else in this note is identical under either choice —
which is why the read model, isolation boundary and migration below are safe to
implement before the ruling lands.

---

## 6. Relationship to the org switcher (#2982) and tenant model (#3068)

`#3068` adopted **invisible tenancy** as the north star, specified in
`docs/design-notes/3074-invisible-tenancy-per-action-resolution.md`: tenancy is
"isolation and billing plumbing," authorization resolves per action (§1.2), list reads
fan out across member tenants (§1.4), and the switcher is demoted to a default-workspace
preference (§5.4).

Three consequences:

1. **A switcher does not make per-org views sufficient.** Under invisible tenancy the
   "active tenant" that `attributed_org_id` defaults to is being demoted precisely so
   users stop mode-switching (§5.4). A budget page that answers "how much have I spent?"
   only for the current mode contradicts that direction. **Aggregation is required.**
2. **The read shape is already sanctioned.** §1.4's membership fan-out plus §5.3's "show
   all workspaces" toggle is exactly the per-org-lines-plus-total model in §7 — so this
   is a reuse of a committed pattern, not a new convention.
3. **§6 of that note lists a "multi-tenant billing dashboard" as a non-goal and a
   "separate product decision."** This note *is* that decision, for the person-scoped
   slice only. It should be recorded as answering that deferral rather than
   contradicting it — and it does not disturb §367's "billing remains per-tenant":
   the per-tenant ledger stays the unit of record; the person view is a derived read.

Note also §8.1's invariant — membership is the gate. Every option here keeps it: the
person aggregating their own spend is a member of each org contributing to it.

---

## 7. The read model and the isolation boundary

### 7.1 `/api/me/budget`

Per-org lines **plus** one labelled cross-org aggregate. Not a single fused number with
no breakdown — the existing endpoint already refuses to present a sum as a governed
figure (`_combined_informational` carries no cap field so no progress bar can bind to
it, `src/budget/me_routes.py:440-458`), and that discipline extends here: the cross-org
total is a cap-bearing line **only** if the person-level cap exists and §5.7 rules yes;
otherwise it is informational, with the same no-denominator treatment.

Response shape (additive; every existing field keeps its meaning):

```
lines[]            # unchanged: the caller's direct + cloud lines for the ACTIVE partition
per_org[]          # NEW: {org_id, org_name, cloud_spend_usd, cap_usd|null}
person_envelope    # NEW: {anchor, spend_usd (cross-org sum), cap_usd|null,
                   #       enforcement_mode, headroom_usd|null}
freshness          # unchanged (#4477) — still a lower bound; settlement is async
```

`_read_cap`/`_read_settled_spend`'s 5-filter predicate stays exactly as-is for the
active partition (`me_routes.py:518-530`, `:546-555`); the cross-org read is a
**separate, explicitly-authorized** query, not a relaxation of those.

### 7.2 What crosses the tenant boundary, and what never does

| Consumer | Foreign-org **totals** | Foreign-org **run detail** |
|---|---|---|
| The person themselves | ✅ yes — their own spend, in orgs they are a member of | ❌ never from this surface |
| Platform admin | ✅ yes | via existing platform-admin surfaces only |
| The person's **home-org admin** | ❌ **no** | ❌ no |
| Any other org admin | ❌ no | ❌ no |

**The issue's question 3 answered explicitly: no.** A home-org admin must **not** see
foreign-org spend totals, not even aggregated. A dollar total is `aws-e`'s cost data;
disclosing it to an admin of an unrelated org because a shared person exists is a
cross-tenant disclosure with no membership basis — it fails §8.1's invariant. An org
admin sees, as today, their own partition: `managed_scope_routes` derives its
`target_org` from the DB and gates on `BUDGET_READ`
(`src/budget/managed_scope_routes.py:251-299`, `:435`), and that stays untouched. An
admin wanting a person's total across orgs is asking for other tenants' data; the answer
is no.

Run detail never crosses under any option. Only settled dollar totals do, and only to
the person and platform admin.

### 7.3 Authorization for the cross-org read, concretely

Two real constraints from the current code:

- `check_permission` accepts a **single** `target_org_id` (`access_control.py:287`) and
  `get_user_role` pins to the caller's **one `is_active`** membership
  (`:108-159`) — so it cannot express "authorized across N tenants" as-is.
- `TenantMixin` applies **no** query filter (`src/shared/models/base.py:12-15`), so a
  widened predicate is mechanically trivial and the *only* guard is the hand-written
  predicate. That is the IDOR risk the module already separates routers to manage
  (`src/app.py:40-55`).

Therefore: the aggregate must be a **self-scope-only** read on the `/me` router (the
router that structurally accepts no scope parameter — `me_routes.py:648-655`), with
the partition set derived **server-side** from `tenant_memberships` for the caller's
resolved `users.id` (pattern: `src/admin/connections/routes.py:296-306`), never from a
request parameter. Concretely `WHERE entity_type='root_user' AND entity_id IN (:person_user_ids)
AND org_id IN (:member_tenant_ids)` — both lists server-derived. Plus:

- **Shadow-user gap:** users auto-provisioned via `POST /resolve-user` have
  `users.org_id` but **no** membership row (`src/internal/provenance_routes.py:140-147`).
  The aggregate must union `users.org_id` as a fallback, exactly as
  `provenance_routes.py:180-190` does, or it will silently omit partitions.
- **Double-count guard:** sum `root_user` rows **only** — never mixed with `user`/`org`
  rows (which would re-count the same dollar, the #4322 family) — and exclude
  `service:`-prefixed principals, per the existing exclusion and its rationale
  (`me_routes.py:450-458`).

---

## 8. Migration

### 8.1 Existing single-org caps: leave them in place

They are enforcing (or not) exactly as they do today; moving a cap silently changes
what stops a workload. **No automatic re-partitioning, no backfill.**

### 8.2 Detection, not mutation

Ship an operator report — "authored `root_user` caps in a partition with zero settled
`root_user` spend this period" — which is precisely the mis-partitioned-cap signature:

```sql
SELECT c.org_id, c.entity_id, c.budget_amount_usd
FROM budget_configs c
WHERE c.entity_type = 'root_user'
  AND NOT EXISTS (
    SELECT 1 FROM budget_usage u
    WHERE u.org_id = c.org_id AND u.entity_type = c.entity_type
      AND u.entity_id = c.entity_id AND u.period_type = c.period_type
  );
```

A dormant cap is not proof of misconfiguration (a person may simply not have run yet),
which is exactly why this reports rather than migrates.

### 8.3 The operator's $5,000

Two steps, in order: (1) **now**, to bound `aws-e` spend, author a `root_user` cap in
the `aws-e` partition — which they can do as its admin, and which works with zero new
code; (2) **after** §5.7 is ruled and the person layer ships, author the personal cap
and delete the dormant `pranavsharma1000` one. Step 1 is available today and should not
wait for this design.

### 8.4 Rollback

- `person_budget_configs` is additive; no existing row changes meaning. Rollback = stop
  reading the table (feature flag), then drop it. A down-migration is trivial because
  nothing else references it.
- The `/me/budget` additions are additive fields; the flag off restores byte-identical
  responses.
- No change to the tracker, the ledger, or per-org enforcement → nothing to roll back on
  the write path. **This is the main reason to keep person-level spend derived rather
  than accumulated.**

---

## 9. Proposed child issues (for the operator to file — not filed by this spike)

1. **Read-only cross-org person view** — `per_org[]` + informational `person_envelope`
   on `/api/me/budget`; self-scope only; membership-derived partitions with the shadow
   fallback (§7.3). Ships value under *either* ruling; no ruling needed.
2. **Mis-partitioned-cap report** (§8.2) — operator-facing, read-only.
3. **`person_budget_configs` + self-service authoring** (§4) — soft mode only.
4. **Person-level enforcement** — *blocked on the §5.7 ruling*; settled-ledger
   denominator, documented overshoot bound (§5.5).
5. **Docs** — record this note as answering `3074` §6's deferred billing decision (§6).

Ordering matters: 1 and 2 are unblocked and independently useful; 3 and 4 wait on the
ruling. Sequencing filed work is the orchestration role's call, not this note's.

---

## 10. Design coverage audit of #4620

| Section | Assessment |
|---|---|
| Plain terms | ✅ Solid — accurate symptom, correctly avoids mechanism. |
| Description | ✅ Solid; one correction (§1.1): the read partition is the active session tenant, not immutably the home org. |
| Impact analysis | ✅ Strong — the three-row isolation table is the right frame and is used as the grading rubric throughout. |
| Design | 🟠 Asked the right five questions but **omitted the Redis hash-tag constraint** (§5.5), which is the binding feasibility limit on any denying person layer, and did not note that `parent_tenant_id` fusion already exists (§5.4). |
| Deployment | ✅ Correct for a spike ("no code, no deploys"). |
| Validation | 🟠 "Design note reviewed and approved" is right, but should name the ruling in §5.7 as the specific gate. |

---

## 11. Verdict

⚠️ **Ready with caveats** — the design is complete and implementable for items 1–3 of §9
under either ruling. Caveats:

1. **§5.7 needs a human ruling** before person-level *enforcement* (§9 item 4) can be
   specified. Recommendation: Option C, opt-in, soft by default.
2. **Any denying person layer must state its overshoot bound**, not imply a hard
   guarantee (§5.5).
3. **Cross-org aggregation must resolve the person through `provider_user_id`**, not
   `GROUP BY entity_id` alone (§3.3), and must union the shadow-user fallback (§7.3),
   or it will under-report for the exact population this issue concerns.
4. **A home-org admin gets no foreign-org figures, including totals** (§7.2). If the
   product wants otherwise, that is a separate decision with a real disclosure cost and
   needs its own ruling.
