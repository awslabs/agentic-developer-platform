# Design Note: `member_org_ids` write-through and the membership-eligibility read (Issue #4849)

> **Status**: Implemented (T0b) — the read ships in SHADOW MODE, no sign-in behavior change
> **Author**: @agent-developer
> **Date**: 2026-09-10
> **Issue**: #4849 — T0b: membership-eligibility read for the two auth Lambdas
> **Enables**: #4844 (T5) — the caller that makes this verdict authoritative
> **Related**: #3134 (the projection + its first fail-closed reader), #537 (dual-table identity index), #3986 (tri-state allowlist + fail-closed broker), #4006 / #4018 / #4019 (the membership write paths), #4848 (T0a — pre-signup duplicate-file cleanup)

---

## 0. What this note is for

The projection has one writer contract and one reader contract, and both are easy
to get subtly wrong in ways that no test fails on. This note is the record of:

- **§2** what the projection means, and what a reader may and may not infer
- **§3** the failure semantics: what a failed write means, what a missing
  attribute means, and why the reader is deliberately stricter than the other
  reader of the same attribute
- **§4** the staleness bound
- **§5** the reconciliation path
- **§6** why the read is DynamoDB and not a gateway call (a standing platform
  rule, not a preference)
- **§7** what T5 must do before it can enforce this

---

## 1. Shape

`member_org_ids` is a list-of-string attribute on the **identity-index rows** for a
GitHub account:

| Table | Key | Written by |
|-------|-----|------------|
| `adp-<env>-identity-index` (legacy) | `identity_type="github_user"`, `identity_value=<github numeric id>` | `IdentityIndexWriter.update_user_membership_orgs` |
| `adp-<env>-user-identity-index` (v2, #537) | `provider="github"`, `provider_user_id=<github numeric id>` | same, behind `USER_IDENTITY_INDEX_V2_WRITE` |

Keyed on the **numeric GitHub account id**, never the login: logins are
renameable, and a projection keyed on a renameable value silently detaches from
its user on rename. This is also why the projection deliberately does *not* reuse
`signup_allowlist`'s `username` key shape.

Source of truth is Postgres `tenant_memberships`. The DDB attribute is a
read-optimized copy for readers that cannot reach Postgres.

---

## 2. Meaning, and what a reader may infer

`member_org_ids` is **the complete set of `tenant_memberships.tenant_id` values
for the user, as of the last successful write-through**.

Two inferences are valid:

- a non-empty list ⇒ the user held at least one membership at write time
- membership in a *specific* org ⇒ that org id appears in the list

One inference is **not** valid, and it is the trap:

> an identity row existing ⇒ the user holds a membership

Identity rows are written by identity-creation paths; memberships are written by
membership paths. A user can have an identity row and no `TenantMembership` at
all. §3.2 is the consequence.

### 2.1 `is_active` is not a filter

`tenant_memberships.is_active` marks which *single* membership is the user's
currently-selected workspace — at most one row per user carries it. It does not
mark whether the membership is real. Filtering the projection on it would emit
one org for a multi-org user and strip the rest; to a fail-closed reader that is
a denial. The projection therefore takes **every** membership row.

Corollary: the `switch_tenant` paths, which only flip this flag, do not change
the set of tenant ids and correctly do not project.

### 2.2 One user, several identity rows

`user_identities` is uniquely indexed on `(provider, provider_user_id, org_id)` —
per-org since migration 021 (#2961). One GitHub account joining two orgs
legitimately has two rows. The write-through fans out over the distinct
`provider_user_id` values rather than assuming one; the four inline copies it
replaced all used `scalar_one_or_none()`, which raises `MultipleResultsFound` on
exactly this shape, so the projection silently never happened for the multi-org
users who most need it.

---

## 3. Failure semantics

### 3.1 A failed projection write

`project_member_org_ids` (`src/admin/memberships.py`) **never raises** and returns
`False` on failure. The membership write it follows has already committed;
Postgres is authoritative and durable. Turning a DDB fault into an error response
would report failure for a change that in fact succeeded, and would invite the
caller to "roll back" a committed transaction.

The cost is a projection that can silently drift. That is only acceptable because
§5 exists — reconciliation is the designed repair, not an afterthought.

**Write ordering: after commit, never before.** The two choke-point helpers
(`upsert_tenant_membership`, `set_membership_role`) deliberately flush without
committing so the caller owns the transaction. Projecting from inside them would
publish memberships that a caller's later rollback erases, with no way for a
reader to detect it. So the projection is a separate call each caller makes
*after* its own `commit()`. Consolidation of the *implementation* was the goal;
inverting the transaction ordering was not.

### 3.2 A missing attribute, to a reader

**Fail closed: missing row, missing attribute, and empty list all mean
NOT eligible.**

This is deliberately **stricter** than the other reader of the same attribute.
`webhook-ingress/lambda/common/identity_resolver.py` defaults a missing
`member_org_ids` to `[the row's own org_id]`. That is correct for *its* question
("may this user trigger work in org X?"), where the identity row's home org is
itself evidence of one membership.

For *our* question ("does this identity hold **any** platform membership?") the
same fallback is fail-**open**: per §2, an identity row can exist for a user with
no membership, so inferring "has a membership" from "has a row" answers yes for a
user who has none. Copying the other reader here would be a security regression,
which is why `tests/lambda/test_membership_eligibility.py` pins it as a named
regression guard.

The cost of the strictness is that a user whose membership predates consistent
write-through reads as ineligible. §7 is how that is handled.

### 3.3 Tri-state, not a boolean

The reader returns `ELIGIBLE` / `NOT_ELIGIBLE` / `UNAVAILABLE`.

`UNAVAILABLE` (DDB error, table not configured) is reported distinctly from
`NOT_ELIGIBLE` (the read succeeded; there is no membership). Callers deny on
both — the fail-closed direction is identical — but collapsing them makes a
misconfigured table indistinguishable from a legitimate denial. That exact
ambiguity is what #3986 was filed to fix, and it mirrors the
`ALLOWED`/`DENIED`/`UNVERIFIED` shape of the broker's `allowlist.py`.

The v2 read is the one exception to "an error is UNAVAILABLE": a v2 fault falls
back to the legacy table, because legacy remains the authoritative read until the
#537 cutover completes. A fault in *both* is `UNAVAILABLE`.

---

## 4. Staleness bound

The projection is **eventually consistent with a bound of one membership
mutation**, not a time bound:

- **Normal path:** written immediately after the mutating request's commit, so a
  reader observes it within one DDB write of the Postgres change — hundreds of
  milliseconds. There is no queue, no batch, and no scheduled sweep in the happy
  path.
- **On write failure (§3.1):** the projection is stale **indefinitely** until
  either the *next* successful membership mutation for that user (which rewrites
  the full list, healing it) or a reconciliation run. There is no automatic
  retry and no TTL. This is the honest bound, and it is why §5 is part of the
  design rather than an operational extra.
- **Direction of staleness:** stale-stale means a revoked membership can still
  read as eligible until the next write. A revocation path that commits and then
  fails to project leaves the user readable as a member. Any caller for which
  that matters must not rely on this projection alone.

---

## 5. Reconciliation

`modules/gateway/scripts/backfill_member_org_ids.py` rebuilds the projection from
Postgres truth. Idempotent; safe to re-run.

```bash
# One user — the targeted repair after a failed write-through
python backfill_member_org_ids.py --provider-user-id 20402445
python backfill_member_org_ids.py --user-id <users.id UUID>

# Everyone — post-incident sweep, or the pre-enforcement gate (§7)
python backfill_member_org_ids.py --dry-run
python backfill_member_org_ids.py
```

Both flows recompute the full org list per GitHub id and overwrite; neither
merges, so a membership deleted in Postgres is also removed from the projection.
A single-user run that matches nothing exits **non-zero** — an operator repairing
one user must not read "complete" and assume the row is now correct.

Note the one case it cannot repair: a user whose *last* membership was revoked has
no rows to aggregate, so the script reports no memberships and writes nothing.
Clearing the attribute is the revoking path's job (it projects the empty list).
Since a missing/empty attribute is already NOT eligible (§3.2), the fail-closed
direction holds either way.

---

## 6. Why DynamoDB and not the gateway

Standing platform rule from the 2026-07-05 dispatch outage: **a non-VPC Lambda
must never synchronously call the internal gateway.** Neither auth Lambda has a
`vpc_config`, a `GATEWAY_API_URL`, or an admin-token ARN, and the nearest
precedent is deliberately switched off — `2951-github-org-to-adp-tenant.md:414`:
*"GATEWAY_API_URL intentionally stays "" — the Lambda must never call the internal
ALB."*

Reinforcing constraints:

- Cognito's synchronous trigger budget is **5s, non-configurable and
  non-retryable**, and already partly spent on a GitHub call. A VPC round-trip
  plus ENI cold start inside that window turns a cold-start blip into a denied
  sign-in.
- VPC-attaching either Lambda is out of scope and an `execute-api` VPC endpoint is
  **forbidden** — its private DNS hijacks resolution for *all* `*.execute-api`
  names in the VPC.
- `/internal/v1/resolve-user` is not an option even if the network path existed:
  on a miss it auto-provisions a shadow user and returns 201, so using it as a
  read would *create* the thing being tested for.

Reading DynamoDB from the pre-signup trigger is already the shipped pattern (its
explicit-allowlist check does exactly that).

### 6.1 IAM and the Terraform dependency cycle

Both Lambdas get `dynamodb:GetItem` on the two identity-index tables — read only;
the gateway API remains the sole writer.

The table names and ARNs arrive as **module variables**, not cross-module
references. `modules/gateway/infra/main.tf:723-736` documents a real cycle,
`cloudfront → api_gateway → github_auth_broker → cloudfront`, and the broker
module sits on it: a reference from inside the child back into the root is exactly
what closes the loop. The root module referencing `aws_dynamodb_table.identity_index`
to *pass into* the child is root → child and safe. Every new variable defaults
inert (`[]` / `""` / `"false"`), so an apply that has not wired them yet is a
no-op rather than a malformed policy — and an empty IAM `Resource` list is a
malformed policy, not an empty grant, hence the `concat(..., cond ? [...] : [])`
guard on both policies.

### 6.2 One shared reader, packaged twice

`modules/gateway/lambda/shared/membership_eligibility.py` is the single
implementation, packaged flat beside each handler by:

- `.github/workflows/github-auth-broker-deploy.yml` (CI) **and**
  `modules/gateway/scripts/deploy-broker.sh` (the manual equivalent) — these two
  must stay in lockstep; the script is documented as replicating the workflow
  exactly, so a file added to one and not the other ships different code by
  deploy path
- `infra/modules/cognito/pre_signup.tf`, via a multi-`source` `archive_file`
  (precedent: budget-lambda, #4391)

A per-Lambda copy was the alternative and is how the write side ended up with four
divergent implementations of the same 30 lines. One file, two packagers.

---

## 7. What T5 (#4844) must do before enforcing

The read ships **inert**. Both Lambdas call it, log
`verdict` / `live_outcome` / `would_agree`, and discard the answer; the wrapper
swallows every exception, because the broker is the sole enforcement point for
GitHub sign-in and an exception in a path with no opinion yet would be a total
login outage — the #3999 code-before-config shape. `ALLOW_OPEN_SIGNUP` (CLAUDE.md)
is the cautionary precedent for shipping code and config out of step on this
surface.

Before any caller lets this verdict decide a sign-in:

1. **Run reconciliation for all users** (§5) and confirm zero drift. Every user
   whose membership predates consistent write-through reads as NOT eligible until
   this runs (§3.2) — enforcing first is a mass lockout.
2. **Read the shadow logs.** `would_agree=false` on a real sign-in is the signal
   the projection is not yet trustworthy; the shadow period exists to measure that
   against production traffic, not to be skipped.
3. **Decide the `UNAVAILABLE` policy explicitly.** Denying on it means a DDB blip
   is a login outage; allowing on it means the check is bypassable by breaking the
   table. T0b takes no position — it only guarantees the two are distinguishable.
4. **Gate it behind a new `ALLOWLIST_MODE`,** so enabling and rolling back are one
   env var, and existing modes are untouched.
