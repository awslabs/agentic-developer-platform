# Design Note: Per-Team / Per-User Bedrock Account Routing (Issue #4692)

> **Status**: **Rev-2 — access model SETTLED.** All rulings previously open (§9) are decided; §9 is now a record of what was decided and why, not a request.
> **Author**: @agent-architect
> **Date**: 2026-09-07 (rev-1), revised 2026-09-07 (rev-2)
> **Issue**: #4692 — route Bedrock calls to a chosen AWS account per team or per user
> **Rev-2 issue**: #4734 — fold in the operator's settled rulings
> **Mode**: Per-issue spike (EPIC #4324)
> **Verdict**: ⚠️ Design-complete **with three prerequisite blockers** (§4.1, §5.0, §5.0b) — all technical, none awaiting a ruling. See §12.
> **Related**: #4690 (defaults ladder — precedence + authoring), #4691 (admin defaults panel — the surface pattern §6.3 mirrors), #4132 (attribution vs. authorization), #4689 (fused person envelope, hot-path discipline), #4300 (root-human attribution), #440 (credential scope relaxation — the ladder this reuses), #481 / #562 (aws_role assume delivery + AWS-connect), #4511 (inert-config class), #4620 (cross-org person budgets)

---

## Rev-2 changelog — what the rulings changed

The operator ruled on 2026-09-07 (recorded in full on #4692 under "Settled rulings"). Six rulings; the two that were *not* among rev-1's three open questions are the ones that moved the design most.

| Ruling | Effect on this note |
|---|---|
| 1. Fail closed always, with an actionable error | §2.5 and §5.2 become decisions. The per-mapping fallback opt-out rev-1 floated is **overruled and removed**. Error must name account + reason + fix (§2.6). |
| 2. Self-service allowed for one's own mapping | §1.3 settled; UI is the credentials screen (§6.4). Asymmetry with #4690 confirmed deliberate. |
| 3. `ADP_BEDROCK_VIA=user` retires | §7.1 / §9.3 settled on a shadow → enforce → remove sequence. |
| 4. Admin authoring is **platform-admin only** | §1.3 **reversed** — rev-1 recommended org-admin. No org-admin rung for now. |
| 4b. Surface is the **admin console**, not the credentials page | New §6. Forces a **platform-scoped** registry, which is what breaks rev-1's central reuse claim — see below. |
| 4a/5. Mapping targets are **connections**, never bare account numbers | §1.1 restructured: mapping and connection are now two layers, not one row. |
| 6. Team/org mappings reference org-linked or admin-registered connections, never an individual's personal credential | §4.3 — and §5.0b shows the *code* already forces this, harder than the ruling does. |

**Two consequences of the rulings that rev-1 did not contemplate**, both found by re-reading code against the settled model. Neither contradicts a ruling; both are things the children must respect:

- **Rev-1's "zero new tables, zero migrations" claim does not survive ruling 4b** (§1.1b). `user_credentials` cannot hold a platform-scoped row — `TenantMixin.org_id` is `nullable=False` (`src/shared/models/base.py:15`) — and `CredentialResolver` filters every rung on `org_id` (`src/shared/services/credential_resolver.py:206`), which is precisely the property rev-1 §4.1 used *as* its tenant-isolation proof. A platform-scoped destination registry is therefore a new table, and isolation needs a new mechanism (§4.2).
- **A second hard blocker on the connect role** (§5.0b): the CFN trust policy pins `aws:RequestTag/adp:user_id` to one baked-in user id, so **every currently connected account is assumable by exactly one person**. Ruling 4a's "dropdown of already-connected accounts" and ruling 6's team/org mappings cannot use those roles at all until a v2 template ships. This is a structural reinforcement of ruling 6, not a challenge to it.

**Explicitly unchanged from rev-1**, per #4734's instruction and its validation criterion: **§3 (spend/budget interplay) in full** — routing changes whose AWS bill pays, never whose ADP budget is charged — and the two rev-1 prerequisites (§4.1 `organizations.aws_accounts` dual-shape defect; §5.0 connect role lacks `bedrock:InvokeModel`).

---

## 0. Executive summary

**The settled access model, in five lines** (rulings in full on #4692):

- A **mapping** names a scope (user / team / org) and points at a **connection** — an
  account id plus a role ARN the platform can assume — never a bare account number.
- **Platform admins author** org/team/user mappings, in an admin-console "Bedrock
  Account Routing" panel beside the Budget Management defaults panel (#4691). No
  org-admin rung.
- **One exception**: a user may select, on their own credentials screen, which of
  *their own* credentialed accounts serves their calls.
- **Save performs a real test assume-role.** A mapping that cannot be assumed is
  rejected, never stored inert (#4511 class).
- **Fail closed, always**, with an error naming the account, the reason, and the fix.
  No silent fallback to the platform account, ever.

Precedence is unchanged and is first-match: **user > team > org > platform default**.

The issue asks for a resolution ladder over "the existing linked-account/assume-role
machinery." The grounding read produced **five findings that materially change the
shape of the work** relative to the issue text. All five are cited from code, not
assumed. Rev-2 adds a sixth (§5.0b, the session-tag pin) and corrects finding 1's
scope in light of ruling 4b.

**Finding 0 (the blocker, stated first because it resizes the effort):** the role ADP's
AWS-connect flow creates in the customer account attaches **only**
`arn:aws:iam::aws:policy/ReadOnlyAccess` (`src/auth/cfn_templates/aws_role_v1.yaml:56-57`),
which does not include `bedrock:InvokeModel`. Routing to any currently-connected
account would fail **every** call. The ladder is free; the destination permission is
not. See §5.0.

1. **The ladder already exists and already stores AWS roles at user/team/org scope.**
   `CredentialResolver.resolve()` walks user → team → org → domain_app
   (`src/shared/services/credential_resolver.py:49`, `:142-156`) over
   `user_credentials`, which has exactly those owner columns with a one-owner CHECK
   (`src/shared/models/vault.py:114-146`). The AWS-connect flow already writes
   `credential_type="aws_role"` rows carrying `role_arn` + `external_id` +
   `account_id` + `default_region` (`src/auth/aws_connect_routes.py:145-183`), and
   `POST /internal/v1/.../assume-role` already resolves through that ladder and
   assumes with ExternalId + session tags (`src/internal/assume_role_routes.py:110-240`).
   **The connection layer is therefore ~80% "point the proxy at machinery that already
   exists."**

   **Rev-2 correction, and it is the most important structural change in this
   revision.** Rev-1 concluded from this finding that the *mapping* needs no table
   either — reuse the credential row as both the connection and the mapping. **Ruling
   4b makes that impossible.** A platform-admin-authored, platform-scoped destination
   registry cannot live in `user_credentials`: `TenantMixin.org_id` is
   `nullable=False` (`src/shared/models/base.py:15`), so a row with no tenant is
   unrepresentable. The two layers must separate (§1.1b): a small mapping rule table
   (the migration-036 `person_budget_defaults` shape, which omits `TenantMixin` for
   exactly this reason — "the platform rung has no tenant at all",
   `alembic/versions/036_person_budget_defaults.py:25-29`) pointing at connection
   records that remain `user_credentials` `aws_role` rows. Reuse survives where it was
   real; the "zero migrations" corollary does not.

2. **The single-account proxy option is not what the issue assumes it is.** The live
   wiring is `SimplePoolService` (`src/app.py:141`), which builds two ambient-IRSA
   `bedrock-runtime` clients from the pod role (`src/pool/simple_pool.py:57-58`) —
   no STS, no account choice, no account identity at all. The cross-account
   `PoolService` + `STSClient` (`src/pool/service.py`, `src/pool/sts_client.py`)
   **exists but is dead code**: constructed nowhere in `src/` (only in
   `tests/pool/conftest.py:169`), and its Terraform enabler `pool_account_arns`
   defaults `[]` (`modules/gateway/infra/variables.tf:137`) and is set in no
   environment. Its selection policy is round-robin **for throughput**
   (`src/pool/selector.py`), which is the *opposite* of deterministic per-principal
   routing. Reviving it as-is would be wrong; harvesting its STS cache is right.

3. **`usage_logs.bedrock_account_id` already exists and is never written.** The
   column (`src/shared/models/usage.py:26`), the service parameter
   (`src/usage/service.py:52`, `:101`), the interface (`src/shared/interfaces/usage.py:19`),
   the response schema (`src/usage/schemas.py:22`) and a query filter
   (`src/usage/service.py:622-623`) are all present since `001_initial_schema.py:171`.
   `ProxyService._log_usage` never passes it (`src/proxy/service.py:428-443`), so it
   is NULL on every row. **Shadow mode and post-hoc audit need no migration** — they
   need one argument threaded through an existing parameter.

4. **The org→AWS-account store the issue points at is broken by a shape collision.**
   `organizations.aws_accounts` is a single JSON column
   (`src/shared/models/organization.py:30`) written by two different admin APIs with
   **incompatible shapes**: `list[str]` of bare account IDs
   (`src/admin/schemas.py:19`, `:31`) vs. `list[AwsAccountEntry]` objects carrying
   `account_id`/`role_arn`/`external_id` (`src/admin/identity/schemas.py:32-37`,
   written at `src/admin/identity/organizations_service.py:260-261`). The tenancy
   reader does `if aws_account_id in aws_accounts`
   (`src/auth/tenant_resolver.py:250-252`) — correct for the string shape,
   **silently always-false for the object shape**. This is a pre-existing defect that
   any design keying routing validation off that column would inherit. See §4.1.

**Finding 6 (rev-2, and the second blocker):** the connect template's trust policy pins
`aws:RequestTag/adp:user_id` to a single `UserSessionTag` value baked in at
stack-create time (`src/auth/cfn_templates/aws_role_v1.yaml:38-53`). **Every
platform-connected account today is assumable by exactly one user.** So ruling 4a's
"admin dropdown of accounts already connected to the platform" is, against the
existing role population, a dropdown of **per-user-shaped** credentials that will
`AccessDenied` for every member except the one who ran the CloudFormation stack. See
§5.0b — this makes ruling 6 (team/org mappings must not reference an individual's
personal credential) enforced by IAM, not merely by policy.

The resulting shape, under the settled rulings: **separate the mapping rule from the
connection record; reuse `user_credentials` + `CredentialResolver` for connections and
for the self-service rung; add a small platform-scoped mapping table + destination
registry for admin authoring; validate with a real test assume-role at save time; make
the routing decision a resolved-target argument to `IPoolService.get_client()`; cache
STS credentials keyed on the full identity tuple; fail closed with an actionable error;
and thread `bedrock_account_id` into the existing usage parameter so shadow mode ships
before enforcement.**

Metering, pricing and attribution are **structurally untouched** by routing, and §3
proves why rather than asserting it. That section is unchanged from rev-1 by design.

---

## 1. Question 1 — Resolution model

### 1.1 Two layers: the mapping rule, and the connection it points at

**Ruling 4a is the organizing decision of this section: a mapping's target is a
CONNECTION, never a bare account number.** An account id alone is unusable — the
platform needs an assumable role in the destination account. So there are two distinct
things, and rev-1 collapsed them into one:

| Layer | What it holds | Storage |
|---|---|---|
| **Connection** | account id + role ARN + ExternalId + region; proof it is assumable | **Reuse** `user_credentials` `aws_role` rows (§1.1a) |
| **Mapping** | "this scope's Bedrock calls go to that connection" | **New** rule table (§1.1b) — forced by ruling 4b |

Keeping them separate is what makes ruling 6 expressible: a mapping is a *reference*, so
"this team's mapping must not point at one person's personal credential" is a constraint
on the reference, checkable at authoring time.

### 1.1a Connections: reuse, as rev-1 found

The connection layer needs nothing new. Everything it requires already exists:

| What's needed | Where it already lives | Verdict |
|---|---|---|
| user → team → org precedence walk (for the self-service rung) | `CredentialResolver.resolve()`, `src/shared/services/credential_resolver.py:142-156`; order at `:49` | **Reuse** |
| Per-scope storage of an AWS role target | `user_credentials` owner columns + CHECK, `src/shared/models/vault.py:114-146` | **Reuse** |
| The routing target payload (account_id, role_arn, external_id, region) | `aws_role` credential `scopes` JSON + SM secret, `src/auth/aws_connect_routes.py:145-183` | **Reuse** |
| The role-ARN input form + quick-create flow | `ConnectAws` page + `connect_start`, `frontend/src/pages/settings/ConnectAws.tsx`, `src/auth/aws_connect_routes.py:118` | **Reuse** — ruling 4b requires the *same* component (§6.6) |
| Test-assume validation before use | `connect_verify`, `src/auth/aws_connect_routes.py:208-261` | **Reuse** — ruling 4a's save-time validation (§6.7) |
| Assume with ExternalId + session tags | `src/internal/assume_role_routes.py:220-240` | **Reuse** |

### 1.1b Mappings: a new rule table, because ruling 4b requires platform scope

Rev-1 argued a mapping table would be a *second* user/team/org ladder over the same
credential data — the duplicate-implementation failure CLAUDE.md's reuse-table rule
exists to prevent. **That argument is sound only while every mapping is tenant-scoped.
Ruling 4b breaks the premise**, on two hard code facts:

1. **A platform-scoped row is unrepresentable in `user_credentials`.** The table carries
   `TenantMixin`, whose `org_id` is `String(255), nullable=False`
   (`src/shared/models/base.py:15`). The platform registry ruling 4b calls for — the
   pool of destinations an admin picks from, and the platform-default rung — has no
   tenant. There is no value to put in that column that is not a lie.
2. **`CredentialResolver` cannot read across tenants, by construction.** Every rung
   filters `UserCredential.org_id == org_id`
   (`src/shared/services/credential_resolver.py:206`). A platform admin authoring a
   mapping for org B while authenticated in org A cannot reach B's connections through
   it — and *should not*, which is why the resolver is right and the reuse is wrong.

So the mapping is a rule table. **The precedent to copy is `person_budget_defaults`
(migration 036), not `user_credentials`** — and it is a close fit precisely because
#4690 hit the same platform-rung problem:

- **No `org_id`, no `TenantMixin`** — 036's docstring gives the reason verbatim: "the
  platform rung has no tenant at all. The org/team rungs name their tenant in
  `scope_id_org` explicitly, which is a scope the row *declares* rather than a partition
  it *lives in*" (`alembic/versions/036_person_budget_defaults.py:25-29`).
- **`scope_type` stored, not inferred** from which columns are NULL (`:30-33`), so a
  future department rung is a new value rather than a re-reading of existing rows.
- **Two scope columns, not one packed id** (`:34-37`) — a `teams.id` is unique only
  inside its org (`Team` carries `TenantMixin`, `src/shared/models/organization.py:95`),
  so the team rung needs both; a packed `"org:team"` string is the #4344 collision class.
- **Uniqueness as a UNIQUE EXPRESSION INDEX over `COALESCE(col, '')`, not a
  `UniqueConstraint`** (`:40-50`). This one is load-bearing and easy to get wrong: in
  Postgres NULLs compare *distinct* inside a unique constraint, so the obvious
  `UNIQUE (scope_type, scope_id_org, scope_id_team)` accepts **two** platform defaults.
  For a *budget* that means two conflicting numbers; for **routing it means two
  destination accounts for one scope, and which one bills depends on row order** — the
  wrong-account bug, installed at the schema level. Copy 036's `Index(...)` +
  `text("COALESCE(...)")` shape (`src/shared/models/budget.py:208-216`) exactly.
- **`CheckConstraint` pinning shape per rung** (`src/shared/models/budget.py:220-226`) —
  without it an `org` row with a NULL `scope_id_org` is a rule matching every tenant.
- **`authored_by_user_id` is the canonical `users.id`**, not `TokenContext.user_id`
  (a Cognito sub on the ordinary JWT path) — the #4647 audit-column contract.

The mapping row's *target* is a reference to a connection/registry record, not an
inlined account id — that is ruling 4a in DDL. Column shape and whether the registry is
a separate table or a platform-scoped row class is child F/I's call; the constraints
above are not.

**What this costs, stated plainly since rev-1 sold the opposite:** rev-1's headline
"zero migrations, flag-flip rollback" (§8.3) is **no longer accurate**. Routing now
ships a migration. Rollback is correspondingly the 036 shape — "stop reading it, then
drop it" — which is still cheap, but it is not nothing. §8.3 is corrected accordingly.

### 1.2 The ladder

Four rungs, narrowest first. A precision note the issue's framing invites getting
wrong: the budget hierarchy is **not** a precedence ladder. `_get_entity_hierarchy`
(`src/budget/enforcement_service.py:369-453`) enumerates user/team/dept/root_user/org
and `_check_entity_budget` evaluates **every** rung independently, with a missing config
row meaning *allow* for that rung (`:1190-1192`) — it is a conjunction of ceilings, not
a first-match walk. #4690 adds a genuine fallback ladder on top of that; the existing
first-match precedents in the repo are `_resolve_scope_cap` (`:455-522`,
`min(configured, platform_default)`) and — the one that matters here —
`CredentialResolver`.

So routing is **first-match** (exactly one account must be chosen), which makes
`CredentialResolver` the right structural precedent and the budget hierarchy the wrong
one. Same precedence *philosophy* as #4690, and the *same implementation* as #440:

```
1. user      — mapping row scope_type='user' (admin-authored)
                OR the user's own self-service selection (ruling 2)
2. team      — mapping row scope_type='team' (scope_id_org + scope_id_team)
3. org       — mapping row scope_type='org' (scope_id_org)
4. platform  — no mapping row matches → ambient IRSA (today's behavior, unchanged)
```

Each rung that matches yields a **connection reference**, which is then dereferenced to
the account id + role ARN + ExternalId (§1.1a) and assumed. Rung 4 is the no-match
branch — which is exactly why the default is today's behavior with zero configuration
(§8.1).

**Rev-2 note on the user rung: it has two authors, one meaning.** Ruling 4 gives
platform admins authority over all three rungs; ruling 2 additionally lets a user select
their own. Both write the *same* user-rung mapping, so the design must decide precedence
between them — see §1.4, which is a question the rulings create and do not answer.

**A dedicated label is still required for the self-service rung**, which is the one
place a mapping is expressed by resolving a credential rather than by reading a mapping
row. `service="aws"` credentials already exist for other purposes (the whole #481
agent-assume path). Routing must not hijack an arbitrary AWS credential a user connected
for something else. Use a reserved label — `bedrock-routing` — and resolve with `label=`
set, which takes the `_find` fast path (`credential_resolver.py:250-252`, single-row
`LIMIT 1`) rather than the multi-row ranking branch. This also gives users a way to
connect an account *without* routing traffic to it, which under fail-closed (§2.5)
matters: connecting an account should never be the act that redirects your traffic.

Under ruling 4a the admin-authored rungs do not need the label at all — they name a
connection explicitly, which is strictly better than matching on a string.

**`team_id` is available on the hot path already**: `TokenContext.team_id` is a
required field (`src/shared/schemas/auth.py:48`), so the team rung costs no extra
lookup. Note the semantics: `users.team_id` is a plain non-null `String(255)`
(`src/shared/models/organization.py:123`) with a `Team` table alongside it — there
*is* a team entity, so #4690's `scope_team_id` and this ladder's team rung agree.

### 1.3 Who may author each rung — SETTLED: platform admin only, plus self-service

**Rev-1 recommended org-admin-within-their-org. That is overruled.** Ruling 4:
*"All org/team/user mappings are authored by platform admins — no org-admin authoring
rung for now (may be delegated later)."* Ruling 2 carves out the one exception.

| Rung | Author (settled) | Enforcement point |
|---|---|---|
| user | **platform admin** — or **the user themselves**, for their own mapping only (ruling 2) | `AccessControlService.require_platform_admin` (`src/admin/access_control.py:525`) for the admin path; the self path is scoped to the caller by construction (§6.4) |
| team | **platform admin only** | `require_platform_admin`, plus the §4.2 scope check that the connection belongs to the org being mapped |
| org | **platform admin only** | same |
| platform default | nobody — it is the absence of a mapping | n/a |

**Why the rev-1 recommendation was wrong, in the operator's terms.** Rev-1 reasoned that
the destination is org-linked, so authoring is "spend our own money." Ruling 4b names the
flaw: the destination pool is now **platform-wide**, so an org admin choosing from it
would be picking a destination the platform registered, not one their org owns. Routing
governance is platform governance. Rev-1 also underweighted a simpler argument — a
team/org mapping decides *whose AWS account absorbs a whole group's model spend*, which
is authority over an account the authoring admin may not own.

Note the implementation detail that makes "platform admin only" cheap and
"org admin later" a genuine extension rather than a rewrite: `require_platform_admin`
checks `context.is_admin` (`src/admin/access_control.py:536-539`), while
`is_org_admin(context, org_id)` already exists alongside it (`:553`). Delegating a rung
later is adding a branch, not restructuring authz. Keep the authoring API's authz check
in one function so that later delegation has one edit site.

#### The two asymmetries, both deliberate — do not "harmonize" them

A future reader will notice this design disagrees with #4690 in one direction and with
itself in another. Both are intentional; recording them is the point of this subsection.

| | This design | #4690 person limits | Why they differ |
|---|---|---|---|
| **Self-service** | **Allowed** (ruling 2) | **Removed** by operator ruling | Raising your own cap spends **the org's** money. Pointing your own calls at **your own** account spends your own. One is an escalation; the other is not. |
| **Org-admin rung** | **Not present** (ruling 4) | n/a — platform-admin-only there too | Routing governance is platform-wide because the destination pool is (ruling 4b). |

So this design is *more* permissive than #4690 at the user rung and *equally* restrictive
at the org/team rungs. That combination looks inconsistent from a distance and is
principled up close: **the discriminator is whose money moves, not which rung it is.**

The property that makes ruling 2 safe: **a user pointing their own calls at their own
account is not a privilege escalation**, because they can only point at an account they
proved control of — `connect_verify` performs a real STS AssumeRole against the
per-credential ExternalId before the credential is usable
(`aws_connect_routes.py:208-261`, `external_id = str(uuid.uuid4())` at `:142`). They
cannot select someone else's connection, and (§5.0b) the IAM trust policy would refuse
them if they tried.

### 1.4 🟠 Open design question the rulings create: admin vs. self at the user rung

Both ruling 2 and ruling 4 write the **user rung**. When both have written, one must win,
and neither ruling says which. This is not a ruling I should invent — it is flagged for
child F/I to settle explicitly, with the tradeoff:

- **Admin wins** — governance is authoritative; a user's own selection is a *default*
  the platform can override. Cost: a user's deliberate choice silently stops taking
  effect, and under fail-closed they may not notice until a bill lands elsewhere.
- **Self wins** — the user's own account is honored whenever they set one. Cost: a
  platform admin cannot pin a specific person's traffic, which is exactly the case
  "contractor's calls must land on the client's account" (the #4692 motivating example).

**Recommendation: admin wins, and the UI must say so.** The motivating use case needs it,
and the failure mode is manageable *if* the self-service screen shows "overridden by a
platform mapping → account …1234" rather than showing the user's stale selection as
though it were in effect. That display requirement is the #4511 inert-config lesson: a
setting shown as active while something else governs is the defect, not the override
itself. Whichever way this is settled, the effective-mapping display (§6.3) must show
the winning rung — which it does anyway for the admin panel.

`ScopeEscalationError` (`credential_resolver.py:56-62`, raised at `:178-182`) and the
`strict` flag (`:168`) are already the guardrails for "don't let a user-scoped
request silently acquire an org-wide credential." On the self-service rung, routing
should pass **no** `scope_hint` — but it must honor `strict`, which it gets for free.

Rev-2 scope note: under ruling 4a the admin rungs no longer fall back *through the
resolver* at all. The ladder walk is now over mapping rows (§1.2), and each row names its
connection outright. That is a simplification — the escalation class the resolver guards
against cannot arise where nothing is being resolved by scope.

---

## 2. Question 2 — Mechanics

### 2.1 Where resolution hooks in

**The seam is `IPoolService.get_client()`** — currently zero-argument
(`src/shared/interfaces/pool.py:7`), called at **eight sites** in
`src/proxy/service.py` (`:125`, `:218`, `:655`, `:702`, `:755`, `:802`, `:852`, `:897`).

Per-request routing means the resolved target must reach client construction. Two
options, and the choice matters for correctness:

| Option | Shape | Assessment |
|---|---|---|
| **A. Pass the target explicitly** | `get_client(target: BedrockTarget \| None)` | **Recommended.** Explicit, testable, no ambient state. Costs 8 call-site edits + interface change. |
| B. Read a contextvar inside the pool | pool reads `_current_bedrock_target` | **Rejected.** The proxy already carries three contextvars (`service.py:61`, request_id, agent_run_id) and `_log_usage` has a comment explaining a contextvar was needed *because a sync dependency lost async context* (`service.py:51-60`). Adding an ambient credential-selection input invites exactly the cross-principal leak of §2.3. A credential decision must be an argument. |

Resolution itself should happen **once per request in the budget/auth layer, not
eight times in the pool**, and be stamped onto the request state. The natural home is
alongside the existing per-request resolution that already runs there — the budget
middleware already resolves the person and writes `attributed_user_id` onto the
context (`src/shared/schemas/auth.py:68-74`, via `src/budget/run_binding.py`). Routing
resolution is the same shape of work at the same point in the lifecycle.

### 2.2 Latency — the #4689 lesson, applied

The issue's blast-radius table names "ladder resolution on hot path unbounded." Three
structural bounds:

1. **Bounded query count.** The mapping table (§1.1b) is tiny by construction — one row
   per scope — and its unique expression index leads with `scope_type`, so each rung is
   one indexed seek. All three rungs can in fact be fetched in **one** query
   (`WHERE (scope_type='user' AND …) OR (scope_type='team' AND …) OR (scope_type='org'
   AND …)`) and resolved most-specific-first in Python, which is the shape #4690's
   ladder uses. Worst case **1 query + 1 connection dereference**. The self-service rung
   adds at most one indexed credential lookup with `label=` set
   (`uq_user_credentials_user_service_label`, `src/shared/models/vault.py:119`).
2. **An existence gate, mirroring the person-cap one.** The pattern exists and is
   proven: `_any_person_caps_exist`
   (`src/budget/enforcement_service.py:1353-1372`) is a process-local, unlocked
   `SELECT id … LIMIT 1` behind a 60s TTL (`_PERSON_CAPS_EXISTENCE_TTL_SECONDS`,
   `:97`). Routing should ride the same shape: a short-TTL cached boolean "does **any**
   Bedrock routing mapping exist at all?"
   Installs with zero mappings — which is **every install on day one** (§8.1) — pay
   **zero queries**. This is the single most important latency decision in the design and
   it is the reason the feature can ship default-on-safe.
   Rev-2 note: because the mapping table has no `org_id` (§1.1b), the gate is naturally
   *global* rather than per-org — strictly cheaper than rev-1's per-org boolean, and it
   collapses to one cached flag for the whole process.
3. **Resolution result cached per principal** for a short TTL, keyed identically to
   §2.3.

### 2.3 Credential caching and isolation — the load-bearing security decision

The issue names "credential caching across principals: one team's calls signed with
another's account credentials" as a blast-radius row. **The existing dead-code cache
has exactly this bug in latent form.**

`STSClient` caches on `cache_key = account_config.role_arn`
(`src/pool/sts_client.py:52`, written `:84`). That is safe *only* because the dead
`PoolService` has one static account list and no principal dimension. The moment
routing makes the target principal-dependent, a `role_arn`-keyed cache is a
cross-tenant credential cache: two orgs whose admins connect the *same* role ARN
(entirely possible — a shared client account) with **different ExternalIds** would
share one cache entry, and the second org would be signed with credentials minted
under the first org's ExternalId and session tags.

**Requirement (structural, not test-only): the STS cache key must be the full
identity tuple, not the role ARN.**

```
cache_key = (org_id, role_arn, external_id, region, resolved_scope)
```

`org_id` first for the same reason the budget tables key `org_id`-first (#4620) — it
makes cross-tenant reuse *unrepresentable* rather than merely untested. `external_id`
because it changes the minted session's identity. `resolved_scope` so a
user-rung and org-rung hit that happen to name the same role don't collide.

**Rev-2 addition: any session tag sent must be in the key too.** §5.0b shows
`adp:user_id` is currently an *authorization* condition in the trust policy and will
remain an *audit* tag on the routing template. Either way it is part of the minted
session's identity, so two users sharing a cache entry keyed without it would each be
signed with the other's tag — wrong audit attribution at best, `AccessDenied` at worst.
The rule generalizes: **every input to the AssumeRole call belongs in the cache key.**
The corollary for a *shared* team/org destination is that credentials are only shareable
across principals if the assume inputs are genuinely identical for them — which is
another reason team/org destinations must not be per-user-tagged roles.

Two further requirements:

- **Refresh, don't expire-into-failure.** `AssumedRoleCredentials.is_expired(margin)`
  + `credential_refresh_margin_seconds` default 300 against a 3600s session
  (`src/pool/config.py:42-46`, checked `sts_client.py:57`) is a sound existing
  pattern — reuse it verbatim.
- **Bound the cache.** The existing cache is an unbounded dict (`sts_client.py:33`)
  with no eviction — fine for a static 2-account pool, a memory-growth and
  stale-entry problem when the key space is per-principal. Needs an LRU bound and
  eviction on credential revocation/re-verify.

### 2.4 Client construction — a real blocker the issue does not mention

The two pool implementations return **incompatible client shapes**, and the proxy is
written against only one of them:

- `SimplePoolService.get_client()` → `AsyncBedrockClient`, whose `invoke_model` /
  `invoke_model_with_response_stream` are `async` wrappers over
  `asyncio.to_thread` (`src/pool/simple_pool.py:60-64`), with **carefully tuned
  timeouts**: `read_timeout=3600` non-streaming, `300` streaming, documented at
  `:22-55` with an AWS-guidance citation.
- `PoolService.get_client()` → `PoolClient` dataclass wrapping a **plain sync**
  `boto3.client("bedrock-runtime", …)` (`src/pool/service.py:199-207`), constructed
  with **no `Config`** — so default 60s read timeout.

The proxy does `await client.invoke_model(...)` (`src/proxy/service.py:546`). Handing
it a `PoolClient` breaks on two counts: it is not the client (needs `.client`), and
the inner client is not awaitable. And even after fixing that, the default 60s read
timeout would time out large Opus/Sonnet calls that the 3600s setting exists to
survive.

**Requirement: the cross-account client must be built by the same
`AsyncBedrockClient` construction path, extended to accept explicit credentials.**
Concretely, `AsyncBedrockClient.__init__(region)` gains optional credentials and
passes them to both `boto3.client` calls, preserving both `Config` objects.
Anything else silently regresses timeouts on every routed call — a latency bug that
would present as random failures on long generations only for routed principals.

### 2.5 Fail closed — SETTLED (ruling 1)

**Decision: fail closed, always.** Ruling 1: *"If the mapped account cannot serve the
call (model not enabled there, assume-role failure, account unlinked), the call fails
with a clear reason naming the mapped account AND how to fix it. No silent fallback to
the platform account."*

**The per-mapping fallback opt-out rev-1 floated is overruled and removed from this
design.** Rev-1 offered it as a hedge ("only if the operator wants it"); the operator
does not. There is no fallback-to-platform code path to be reached, which is what makes
the wrong-bill class structurally impossible rather than merely defended against. Do not
reintroduce it as a config flag, an env var, or an exception branch — a single
`except: use_platform_account()` anywhere in the signing path voids the feature.

The reasoning below is retained because implementers need to know the availability cost
they are accepting, not to reopen it.

| | Fail closed (**settled**) | Fallback to platform (**rejected**) |
|---|---|---|
| Wrong-bill risk | **Eliminated structurally.** A resolved mapping is honored or the call fails. | **Present and silent.** The mapping's entire purpose voids itself precisely when it matters, with a 200 response. |
| Availability | **Worse.** A broken role link (customer deletes the role, rotates ExternalId, hits an SCP) takes that team's model access down until fixed. | Better — calls keep working. |
| Detectability | Immediate, loud, attributable — a 5xx naming the account link. | **Undetectable without the audit trail** the feature doesn't have yet. |
| Reversibility of the harm | Downtime is recoverable. | **A misdirected bill is not** — the money is spent on someone else's account. |

The asymmetry is decisive: fail-open trades an *unrecoverable, silent* accounting harm
for a *recoverable, loud* availability harm. The issue's own blast-radius table already
grades wrong-account as "the worst possible spend bug." Silent fallback is that bug
with extra steps.

**The accepted cost, stated so nobody is surprised by it:** an org that routes at the org
rung and whose role link breaks loses *all* model access, not some. Three mitigations
make fail-closed operationally survivable, and all three are requirements of this design
rather than options:

- **Shadow mode first** (§8.2) — no enforcement until the audit trail shows the
  resolved account is the intended one for real traffic.
- **Authoring-time test assume** (§6.7, ruling 4a) — the most common cause of a broken
  mapping is a mapping that never worked. Rejecting those at save time removes that
  entire class before it can cause an outage.
- **An actionable error** (§2.6, ruling 1) — the difference between a 5-minute fix and a
  support escalation is whether the error says what to do.

### 2.6 The actionable error — ruling 1's second half

Ruling 1 requires the error to name **the account, the reason, and the fix**. Rev-1
specified only the first. The ruling's own example sets the bar: *"model X is not enabled
in AWS account …1234 — enable it in the Bedrock console, or change/remove your account
mapping."*

A 5xx is right for the assume-failure class (502 — upstream credential acquisition
failed; the client's request was well-formed). Shape:

```json
{
  "error": "bedrock_account_unavailable",
  "message": "Cannot reach AWS account 1234 for this request: ADP could not assume the configured role. Ask a platform admin to re-validate the Bedrock account mapping for your team, or remove it to fall back to the platform account.",
  "account_id": "…1234",
  "reason": "assume_role_failed",
  "scope": "team",
  "remediation": "…"
}
```

Three requirements on it:

1. **`reason` is a stable machine code**, distinct per cause —
   `assume_role_failed` / `model_not_enabled` / `account_unlinked` / `role_missing_bedrock_permission`
   (the §5.0 class). Ruling 1 lists three causes that need different fixes; one generic
   error code cannot carry three different remediations. This is why §5.1's
   error-class discrimination is a prerequisite and not a nicety.
2. **The remediation is audience-correct.** A member hitting a *team* mapping cannot fix
   it — telling them to "check your account mapping" is a dead end, because under ruling
   4 they have no authority over that rung. Team/org-rung errors must direct them to a
   platform admin; user-rung self-service errors can direct them to their own credentials
   screen. The scope is already in the payload; use it to choose the text.
3. **Never leak the credential or role ARN.** Precedent is already in the repo —
   `assume_role_routes.py:241` writes `role_arn` to the audit row but explicitly keeps it
   out of the user-facing error ("do NOT include role_arn in user-facing error", `:241`;
   audit-only note at `:298-299`). The **account id is fine to show** (ruling 1 requires
   it); the role ARN and ExternalId are not. Follow that split exactly.

---

## 3. Question 3 — Spend/budget interplay (load-bearing)

The instruction on this issue is that the budget arc's invariants must be **provably**
untouched. The proof is structural: **the routing decision and the metering path share
no state.** Below, invariant by invariant, with citations.

### 3.1 The claim, stated precisely

> Account routing changes **whose AWS bill pays Bedrock**. It does not change
> **whose ADP budget is charged**, what is metered, or what is displayed.

### 3.2 Invariant: the metering paths are unchanged

An accuracy correction to the issue's framing first, because "one metering path" is a
simplification and designing against the simplification would be a mistake. There are
**three** cost-bearing paths, and routing must leave all three alone:

| Path | Where | What it settles |
|---|---|---|
| **Bedrock proxy** | `ProxyService._log_usage`, `src/proxy/service.py:368-448` | Reconciles the reservation (`:420-426`) and writes the `usage_logs` row (`:430-443`) — but with **`cost_usd=0.0` at every call site** (`:673`, `:721`, `:773`, `:820`, `:870`, `:915`, `:240`) |
| **Mantle passthrough** | `mantle_service.py:377-437` | Same two actions, but prices **inline** via `pricing_service.calculate_cost(...)` |
| **`budget_usage` ledger (the enforced denominator)** | `lambda/budget-usage-tracker/handler.py` | Prices asynchronously from the **S3 chat logs**, upserts `budget_usage` `ON CONFLICT (org_id, entity_type, entity_id, period_start, period_type)`, then bridges the cost back into `usage_logs.cost_usd` — `bridge_cost_to_usage_logs`, `:317-347`, `SET cost_usd = CASE WHEN cost_usd = 0 THEN %s ELSE cost_usd END` |

`_log_usage` is still the single **synchronous settlement point** for the Bedrock path,
and its docstring earns that description: every caller invokes it from a `finally`,
making it "the one point that runs on success AND on failure — which makes it both the
'charge the real cost' hook and the 'release what a failed request was holding' hook"
(`src/proxy/service.py:398-402`).

**None of the three takes an account or region parameter, and routing adds none that
affects charge:**

- Routing changes only which client object `_invoke_bedrock` was handed
  (`service.py:520-556`) — an argument already out of scope by the time `_log_usage`
  runs.
- Token counts come from the parsed provider response — `_cache_tokens_from_usage`
  (`service.py:449-461`) non-streaming, `_extract_usage_from_sse_chunk`
  (`:928-968`) for streaming, accumulating `message_start` (input + cache) and
  `message_delta` (output). Both are **byte-identical** whether the response came from
  the platform account or a routed one: same Bedrock API, same response schema.
- The ledger Lambda prices from the chat log, which carries model + tokens and **no
  account field**. This is worth naming explicitly because it *strengthens* the proof:
  the enforced denominator is computed in a process that cannot see the routing
  decision even in principle.

**Structural guard available:** the repo already uses "no `func.sum` in
`me_routes.py`" as an enforced test-guard (#4689). The equivalent here is a guard
asserting **no account/region parameter reaches `reconcile_budget_reservation`,
`log_request`'s charge fields, or the tracker Lambda's pricing call.** The one field
routing *does* add (§3.5) is a descriptive column with no reader in any budget path.

### 3.3 Invariant: displayed == enforced

#4689 achieved this structurally: `_check_person_budget` builds its denominator from
**the same** `_read_person_partition_spend` the endpoint uses. Routing does not touch
either function, does not touch `budget_usage`, and adds no new ledger. Since both
sides continue to read the identical unchanged function over the identical unchanged
rows, the equality is preserved **by non-participation** — the strongest available form.

### 3.4 Invariant: server-side attribution only

The charged entity is decided by `attributed_org_id` and `attributed_user_id`, both
documented as attribution-only and never authorization
(`src/shared/schemas/auth.py:32-84`). `usage_logs` rows are written with
`org_id=context.attributed_org_id` and an explicit comment "Never context.org_id
(authenticated-only, authorization's field)" (`src/usage/service.py:86-89`).
`attributed_user_id` is the stronger case: "WRITTEN BY THE BUDGET MIDDLEWARE, not by
token validation… NOT settable from any request header by any caller — there is
deliberately no header for it" (`schemas/auth.py:68-84`), sourced from the
server-resolved run row (`src/budget/run_binding.py`).

**This split is exactly why routing must read the other field.**

**Routing must be a strict consumer of these fields and never a producer.** Two
prohibitions follow, and they are the #4132 lesson applied:

- **Routing reads `org_id`, not `attributed_org_id`, for the mapping lookup.** The
  mapping lookup is an authorization-shaped decision (which account may this caller's
  traffic be signed into), and `attributed_org_id` is caller-influenced
  (`schemas/auth.py:37-40`). Keying routing off it would let an internal-plane caller
  that legitimately sets `X-Agent-OrgId` for attribution *also* redirect which AWS
  account gets billed — resurrecting #4132 in a worse form, since #4132 was
  "only" an accounting bug while this would be a real cross-account credential
  acquisition. **This is the single most important line in this section.**
- **Routing never writes any of the three fields.** It is a read-only consumer.

The issue's own non-goal ("per-request/header-driven account selection … is the #4132
class — mappings are server-side config only") is thereby enforced by construction:
the resolution inputs are `org_id` (authenticated), `users.id` (server-resolved) and
`team_id` (from the token, server-issued). No request header participates.

### 3.5 Pricing and usage capture across accounts

**Pricing is model-keyed only, with no account or region dimension.**
`get_model_pricing(model_id)` looks up `MODEL_PRICING[resolved_id]` and falls back to
`"default"` (`src/budget/pricing.py:325-342`); `calculate_cost` uses only that pricing
plus token counts (`:344-368`). No call site passes account or region. So a routed
call prices **identically** to a platform call.

This is a deliberate simplification worth stating rather than discovering later:
**ADP's ledger prices at list, so it is already independent of the destination
account's actual negotiated rate or commitments.** If the ML team's account has a
Bedrock commitment discount, their real AWS invoice will be lower than the ADP ledger
figure. That is *correct* for this design — the ADP budget is a governance envelope in
list-price terms, and the AWS bill is the AWS bill. **Any future attempt to reconcile
the two is a separate issue, not a hidden requirement here**, and it must not be done
by making pricing account-dependent (that would break displayed == enforced for any
principal whose account changes rung).

Usage capture is likewise identical: streaming metering runs through the same
`stream_handler` / eventstream decode path regardless of which client produced the
stream.

**One additive change, no migration:** thread the resolved account into the *existing*
`bedrock_account_id` parameter of `UsageService.log_request`
(`src/usage/service.py:52`, persisted `:101`, column
`src/shared/models/usage.py:26`, filterable `src/usage/service.py:622-623`). Today
`_log_usage` never passes it and it is NULL on every row. This is:

- the **audit trail** that makes shadow mode possible (§8.2),
- the **forensic record** that answers "whose account did this actually go to,"
- **not** read by any budget or enforcement path — grep confirms the only readers are
  usage read/filter surfaces.

It stays NULL for unrouted calls, which correctly means "not captured" rather than
"platform account" — consistent with the repo's established null-discipline (the
`client_tool` comment at `service.py:394-396` and the cache-token comment at
`:449-461` both insist on exactly this distinction).

---

## 4. Question 5 — Tenant isolation of mappings

> **Rev-2: this section changed materially.** Rev-1 derived isolation from
> `CredentialResolver`'s unconditional `org_id` filter — cross-org resolution was
> "unrepresentable, not merely checked." Ruling 4b's platform-scoped registry removes that
> guarantee: a platform admin authoring for org B is, by design, reaching outside their own
> tenant. Isolation must therefore become an **explicit authoring-time check** (§4.2)
> instead of a free consequence of the query. This is the honest cost of ruling 4b and the
> single place rev-2 is *weaker* structurally than rev-1 — so the check has to be real.

### 4.1 🔴 Prerequisite defect: `organizations.aws_accounts` cannot be trusted as the validator

The issue requires "mappings are org-scoped config referencing **org-linked accounts
only** — an org may never route onto another org's linked account; validation at
authoring time." The obvious validator is `organizations.aws_accounts`. **It is
currently broken.**

Two admin APIs write the same JSON column with incompatible shapes:

| Writer | Shape | Citation |
|---|---|---|
| `src/admin/service.py:279-280` | `list[str]` — bare account IDs | schema `src/admin/schemas.py:19`, `:31`, `:48` |
| `src/admin/identity/organizations_service.py:260-261` | `list[{account_id, role_arn, external_id}]` | schema `src/admin/identity/schemas.py:32-37`, `:53`, `:63` |

The only consumer tests membership with `if aws_account_id in aws_accounts`
(`src/auth/tenant_resolver.py:250-252`). Against the string shape that is correct.
Against the object shape it compares a string to dicts and is **always false** — so an
org onboarded through the identity API has, from the tenancy resolver's point of view,
no linked accounts at all. (Note also the reader loads **every** organization and
filters in Python, `:245-249` — a full-table scan per resolution, with the
database-agnosticism rationale in the comment.)

**Consequence for this design:** authoring-time validation must **not** be built on
that column until the shape collision is resolved, or the validation will pass or fail
arbitrarily depending on which admin API onboarded the org.

**Recommended resolution — sidestep it entirely.** The validator should be the
connection record's own provenance, which is stronger than a config list anyway:

- The account's linkage is **proven, not asserted**: `connect_verify` performs a real
  STS AssumeRole with the per-credential ExternalId before the row becomes usable
  (`aws_connect_routes.py:208-261`, `external_id = str(uuid.uuid4())` at `:142`,
  described as confused-deputy protection). Ruling 4a extends the same proof to mapping
  save time (§6.7).
- Connections themselves remain `user_credentials` rows carrying `org_id` via
  `TenantMixin` (`src/shared/models/vault.py:91`), so **a connection always knows which
  tenant linked it** — which is the fact §4.2's check reads.

The `aws_accounts` shape collision should still be **filed separately** as a defect
(§9, child E); it is not this issue's to fix, but any design that leaned on it would be
built on sand.

### 4.2 Isolation is now an explicit check, not a free query property

Ruling 4a scopes the admin dropdown: destinations are listed *"scoped to the org being
mapped."* With a platform-scoped registry, that scoping must be written down and tested,
because nothing in the query enforces it any more. Three requirements:

1. **On save, the authoring API must verify the chosen connection is legitimate for the
   target scope** — the org/team named in the mapping. For a connection sourced from a
   tenant's own linked accounts, that means comparing the connection's `org_id`
   (`vault.py:91`) to the mapping's `scope_id_org`. This is one predicate, and it is the
   whole of cross-tenant isolation for admin-authored mappings. **It must be enforced
   server-side in the API, not by the dropdown's contents** — a UI that only lists
   in-scope options is a usability feature; an API that only accepts them is the control.
2. **Admin-registered platform destinations are the deliberate exception** and must be
   marked as such. Ruling 4a explicitly allows registering a destination "for the case
   where nobody has linked the desired account yet," and §5.0b shows this is in fact the
   *only* workable path for team/org rungs today. Such a record has no owning tenant, so
   requirement 1 cannot apply to it — which means the registry needs an explicit
   "platform-registered, usable by any scope an admin names" flag rather than a NULL
   `org_id` that reads as an accident. A NULL that means "allowed everywhere" is how
   cross-tenant leaks get written by well-meaning code.
3. **Reject, never store inert** (ruling 4a, #4511 class) — see §6.7.

The property to preserve from rev-1, restated for the new shape: **an org's traffic may
only be signed into an account that a platform admin deliberately named for that org.**
Rev-1 got this from the resolver; rev-2 gets it from requirement 1 plus the ExternalId
proof. Rev-2's version depends on a code path being correct, so it needs the test rev-1
did not: an authoring-API test that a connection owned by org A cannot be attached to a
mapping scoped to org B.

### 4.3 Ruling 6: team/org mappings must not point at a personal credential

Ruling 6: *"a mapping for a TEAM/ORG should reference an org-linked or admin-registered
connection, not one individual's personal credential — one person's personal role
silently serving a whole team's traffic is an authority/audit problem."*

This is expressible as a constraint on the reference (which is why §1.1 separates the
layers): for `scope_type IN ('team','org')`, the referenced connection must **not** be a
user-owned row — i.e. `user_credentials.user_id IS NULL` (org-scoped, per the
all-owners-NULL convention at `src/shared/models/vault.py:91-107`) or the record must be
a platform-registered destination.

**§5.0b shows IAM enforces this more strictly than the ruling asks** — a user-owned
connection's trust policy will refuse to be assumed on behalf of anyone else, so a
team mapping pointing at one would fail every call rather than working-but-improperly.
That is the better failure, but it must not be the *discovery* mechanism: reject it at
save time with a clear reason, or an admin will read the runtime `AccessDenied` as a
platform bug.

### 4.4 Additional isolation requirements

- **Only `status == "verified"` rows may route.** `connect_start` writes
  `status: "pending"` into `scopes` (`aws_connect_routes.py:172-176`); the resolver's
  ranking function reads that status but only as a *tie-break preference*, and will
  still return a pending row if it is the only match (`credential_resolver.py:239-248`).
  For routing, pending must be **excluded**, not deprioritized — otherwise
  `connect_start` alone (before the customer ever creates the role) silently
  reroutes a principal's traffic to an account that will fail every assume. With
  fail-closed, that is a self-inflicted outage triggered by merely *starting* a
  connect flow.
  Rev-2: ruling 4a's save-time gate (§6.7) covers the admin path — a pending connection
  fails the test assume, so it cannot be saved as a mapping target at all. The
  verified-only filter is still required for the **self-service** path (§6.4), where the
  user is selecting from their own credential list rather than going through the mapping
  API, and as defence in depth on the resolution path.
- **Cross-tenant cache key**, per §2.3.
- **Audit every resolution that routes**, reusing the `_write_audit` pattern
  (`assume_role_routes.py:123`, `:290-299`) — role ARN server-side only. Rev-2: audit the
  **authoring** events too (who mapped which scope to which destination, and every
  save-time assume failure) — under ruling 4's platform-admin model this is the record of
  who decided whose bill pays, which is the question the whole feature exists to answer.

---

## 5. Question 4 — Model access on the resolved account

### 5.0 🔴 Blocker: the connected role cannot invoke Bedrock at all

Before per-model enablement matters, a blunter fact: **the role the AWS-connect flow
creates has no Bedrock permission whatsoever.** The CFN template attaches exactly one
managed policy:

```yaml
ManagedPolicyArns:
  - arn:aws:iam::aws:policy/ReadOnlyAccess
```

(`src/auth/cfn_templates/aws_role_v1.yaml:56-57`.) `ReadOnlyAccess` does not include
`bedrock:InvokeModel` or `bedrock:InvokeModelWithResponseStream` — those are mutating
actions. The template's own description is "cross-account role for agent delegation"
(`:2`), for `ReadOnlyAccess` shell/terraform inspection work (#562) — not for signing
model calls.

So every existing connected account, and every account connected by the unmodified
flow, would fail **100% of routed calls** with `AccessDeniedException`. Under
fail-closed (§2.5) that is total model-access loss for the routed principal.

**This is the largest single gap between the issue's premise and the code.** The issue
says routing should reuse "the existing linked-account/assume-role machinery"; the
machinery exists and resolves and assumes correctly, but the thing it assumes *into*
is deliberately read-only.

Implications, all of which belong to child C/G rather than being discoverable at
runtime:

1. **A new template version is required** — `aws_role_v2.yaml` adding a scoped inline
   policy granting `bedrock:InvokeModel` + `bedrock:InvokeModelWithResponseStream`,
   resource-scoped to foundation-model/inference-profile ARNs (never `Resource: "*"`,
   per the IAM rule in this repo's review standard). The template key is already
   env-configurable (`ADP_CFN_TEMPLATE_KEY`, `src/auth/cfn_template.py:28`), so
   versioning is cheap.
2. **Existing connected accounts must re-run CloudFormation** to gain the permission.
   This is a customer-side action, so it must be surfaced in the authoring UI as an
   explicit prerequisite — not a silent failure at first call.
3. **Routing must not offer an account whose role lacks Bedrock invoke.** The natural
   gate is at authoring time: extend the `connect_verify` probe (which already does a
   real assume, `aws_connect_routes.py:208-261`) to additionally confirm Bedrock
   invoke capability before a credential may be used *for routing*. A capability
   probe belongs there because that endpoint already owns "prove this link works."
4. **Do not widen the shared role.** Adding Bedrock invoke to the *existing*
   read-only role for all connected accounts would grant model-invoke to accounts
   connected purely for read-only inspection — a permission the customer never
   consented to. Recommend a **separate role/template for routing** (distinct
   nickname → distinct `ADP-Agent-*` role), which also gives the `bedrock-routing`
   label a natural 1:1 with a purpose-built role.

Note the IAM grant on ADP's side is already sufficient and needs no change:
`aws_iam_role_policy.gateway_sts` grants `sts:AssumeRole` + `sts:TagSession` on
`arn:aws:iam::*:role/ADP-Agent-*` unconditionally
(`platform/infra/modules/eks/main.tf:251-271`). The dormant pool's separate grant is
scoped to `*BedrockGateway-Pool*` (`modules/gateway/infra/main.tf:563`) — a naming
convention that **does not match** the `ADP-Agent-*` roles the connect flow creates,
which is further evidence the dormant pool is a different, abandoned design and should
not be revived (§0.2).

### 5.0b 🔴 Blocker (rev-2): connected roles are assumable by exactly ONE user

Reading the same template further surfaced a second structural problem, and it lands
directly on ruling 4a. The trust policy conditions **both** statements on a session tag
fixed at stack-create time:

```yaml
Condition:
  StringEquals:
    sts:ExternalId: !Ref ExternalId
    "aws:PrincipalArn": !Ref GatewayRolePrincipal
    "aws:RequestTag/adp:user_id": !Ref UserSessionTag
```

(`src/auth/cfn_templates/aws_role_v1.yaml:38-53` — the condition repeats on the
`sts:TagSession` statement at `:47-53`.) `UserSessionTag` is baked in by `connect_start`
as the **creating user's** canonical `users.id`, and the code comments why it must match
exactly: *"UserSessionTag must equal what the STS service sends at assume-role time…
Get it wrong and the trust policy's RequestTag condition will AccessDenied every call"*
(`src/auth/aws_connect_routes.py:183-190`). At assume time the tag is sent from the
resolved user (`src/internal/sts_assume_service.py:95`,
`{"Key": "adp:user_id", "Value": user_id}`).

**Consequence: every account connected by the existing flow can be assumed on behalf of
exactly one person.** Three implications for the settled model:

1. **Ruling 4a's "dropdown of accounts already connected to the platform" is, against
   today's role population, a dropdown of per-user credentials.** Selecting one for a
   *team* or *org* mapping produces `AccessDenied` for every member except the original
   connector. The mapping would look correctly saved and fail 100% of calls for almost
   everyone — and under fail-closed (§2.5) that is a hard outage for that scope.
2. **So ruling 4a's "register a NEW destination" path is not a convenience for the
   nobody-linked-it-yet case — it is the only viable path for team/org rungs** until a
   template version exists whose trust policy is not pinned to one user. Child G must
   own this, and the admin panel must not offer existing per-user connections as team/org
   destinations (§4.3).
3. **Ruling 6 is enforced by IAM, harder than by policy.** A personal credential
   *cannot* silently serve a whole team's traffic — it will refuse. This is a rare case
   where the safe behavior is the default; the work is making it fail at save time rather
   than at 3am.

**What a routing-capable destination role needs**, for child G: drop the per-user
`adp:user_id` condition in favour of conditions that hold for a *service* relationship —
keep `sts:ExternalId` (confused-deputy protection, the load-bearing one) and
`aws:PrincipalArn` pinned to the gateway role, and scope the granted Bedrock actions by
resource ARN. Session tags remain valuable for **audit** (who the call was for) and
should still be sent; they must simply stop being an authorization gate that only one
principal can pass. Note this is a *separate* template from the read-only agent-delegation
role for the §5.0 reason — do not widen the existing one.

One further code fact for whoever implements the signing path: **`src/pool/sts_client.py`
sends no `Tags` at all** (params built at `:65-74`, `ExternalId` only at `:72`). Against
these trust policies it would fail even for the correct user. Rev-1 §0.2 already
recommended harvesting that class's cache and discarding the rest; this is another reason
not to reuse it wholesale as the assume path.

### 5.1 The per-model gap

`check_model_access` is a **pattern match against the caller's allowed-model config**
(`src/proxy/model_resolver.py:186-198` → `is_model_allowed` → `_get_allowed_patterns`),
with `fnmatch` globs. It knows nothing about whether the *destination account* has that
model enabled. Bedrock model enablement is per-account — CLAUDE.md documents
`platform/scripts/enable-bedrock-models.sh` as the automation for exactly this on the
platform account, and it does not run on a customer's account.

So a routed principal can pass every ADP-side check and still get an
`AccessDeniedException` from Bedrock. Today that surfaces as an opaque wrapped error:
`_invoke_bedrock` catches bare `Exception`, logs, and raises
`BedrockInvocationError(str(e))` (`src/proxy/service.py:554-556`), losing the
distinction between "your account lacks this model," "your role can't be assumed," and
"Bedrock is down."

### 5.2 SETTLED (ruling 1): error naming the account, never fallback

Ruling 1 names "model not enabled there" as one of the three fail-closed causes
explicitly, and supplies the canonical message for it. Do **not** fall back to the
platform account. Fallback here is the §2.5 fail-open argument in a narrower disguise,
and worse: it would fire *per model*, so a team would be routed correctly for most
traffic and silently billed to the platform for whichever models they hadn't enabled —
the hardest possible version of the bug to notice.

Requirements:

1. **Distinguish the error classes.** `_invoke_bedrock`'s bare-`Exception` catch must
   discriminate `AccessDeniedException` / `ValidationException` from transport failures
   so the model-not-enabled case can carry its own error code. This is a small,
   contained change but it is a **prerequisite**, not a nicety — without it the
   feature's most common failure mode is indistinguishable from an outage.
2. **The error names the account, the model, and the fix**, per §2.6 and the redaction
   rule: `{"error": "bedrock_account_unavailable", "reason": "model_not_enabled",
   "account_id": "…", "model_id": "…", "message": "…"}`. Ruling 1's own wording is the
   template: *"model X is not enabled in AWS account …1234 — enable it in the Bedrock
   console, or change/remove your account mapping."* Note the audience rule from §2.6:
   for a team/org mapping the member cannot change the mapping, so their copy points at a
   platform admin.
3. **Surface it at authoring time too, as a warning not a gate.** When an admin
   authors a mapping, list which of the org's allowed models are enabled in the target
   account. A gate is wrong — enablement changes after authoring, so a
   validate-once check would give false confidence.
   **Rev-2 distinction, important because ruling 4a mandates the neighbouring behavior:**
   the *assume* check at save time **is** a gate (§6.7 — reject unassumable mappings), but
   the *model-enablement* check stays a warning. They differ because assumability is a
   property of the mapping itself (if it fails at save it will never work), while model
   enablement is a property of the destination account that changes independently of the
   mapping. Do not let ruling 4a's gate language pull enablement into the gate.
4. **`get_available_models` becomes account-dependent in principle**
   (`model_resolver.py:211-225`). Out of scope for the spike; flagged as a known
   follow-on (§9, child D) because a model list that doesn't match what the
   destination account will actually serve is a support-load generator.

---

## 6. Authoring surfaces (rev-2 — new section, rulings 2 / 4 / 4a / 4b / 5)

Rev-1 had no UI section; it had a one-line child issue ("mapping authoring API + admin
UI") built on the org-admin model that ruling 4 overturned. Rulings 4a and 4b specify the
surface in enough detail to write down, so it is written down here.

### 6.1 Two surfaces, deliberately separated

| Surface | Who | What they may do | Where |
|---|---|---|---|
| **Bedrock Account Routing panel** | platform admin | author user/team/org mappings; register new destinations | admin console, beside Budget Management defaults (#4691) |
| **Bedrock account selector** | any user | choose which of *their own* connected accounts serves *their own* calls | existing credentials screen |

Ruling 4b states the boundary and the reason: *"The personal credentials screen stays
scoped to 'accounts I connected for my work' — platform-wide routing governance does not
live there."* Rev-1 (and #4692's earlier ruling 2 text) had the self path on the
credentials screen and left the admin path unspecified; 4b resolves that the two paths
live in two places. **This is the settled reading: ruling 2's "UI home: the existing
credentials-mapping screen" governs the SELF path only; 4b governs the ADMIN path.** Both
statements are true of their own surface, which is worth spelling out because read
side-by-side they look contradictory.

### 6.2 Mockup status

A mockup is referenced as `docs/mockups/4692-bedrock-account-routing-admin.html`. **It is
not present in this repository** — `docs/mockups/` contains only
`budget-spend-dashboard-mockup.html`, and the path appears in no commit on any branch. The
surface description below is therefore derived from ruling 4b's text, **not** from the
mockup, and is **proposed, pending operator sign-off**. If the mockup exists outside the
repo it should be committed alongside the implementing PR so the description and the
artifact cannot drift; where they disagree, the mockup plus a fresh ruling wins over this
section.

### 6.3 The admin panel — three elements, per ruling 4b

Ruling 4b enumerates them:

1. **Mappings table: scope → destination connection, with effective-mapping and
   source-rung display.** The source-rung column is the #4511 discipline applied to a UI:
   showing a person's effective destination *without* saying which rung produced it
   invites the reader to "fix" the wrong row. #4690/#4691 reached the same conclusion for
   limits — the labeled-source requirement is in #4691's title.
2. **Destination dropdown sourced from all platform-connected accounts**, scoped to the
   org being mapped (§4.2 requirement 1 — the API enforces the scoping; the dropdown
   merely reflects it). Per §5.0b the panel must **exclude per-user connections from
   team/org destination lists**, or offer them only with an explicit warning that they
   will fail.
3. **"Register new destination"** — see §6.6.

**Placement:** beside the Budget Management defaults panel. The concrete anchor is
`frontend/src/pages/BudgetManagement.tsx` (route `/budgets`, `frontend/src/App.tsx:86`),
which is where #4691 is adding the defaults panel. Both surfaces answer adjacent
questions about the same spend decision — how much a principal may spend, and whose
account pays — so co-locating them is right. Follow whatever panel/tab structure #4691
lands rather than inventing a second one; if #4691 has not landed when routing's UI starts,
that ordering dependency is real (§9 sequencing).

**Authz:** `require_platform_admin` (`src/admin/access_control.py:525`) on every authoring
endpoint, and `AdminGuard` (`frontend/src/components/AdminGuard.tsx`, used at
`frontend/src/App.tsx:80`) on the route. Both — the guard is UX, the dependency is the
control.

### 6.4 The self-service selector — ruling 2

On the existing credentials screen (`frontend/src/pages/settings/SettingsCredentials.tsx`,
route `/settings/credentials`, `frontend/src/App.tsx:91`), which already lists the user's
AWS credentials separately from other services
(`awsCredentials = credentials.filter((c) => c.service === 'aws')`,
`SettingsCredentials.tsx:67`). The selector picks **one of those** as the Bedrock
destination — so it is a small addition to an existing list, not a new screen, which is
what ruling 2 asks for ("No new screen for the self path").

Three requirements:

- **Only the caller's own verified connections are selectable.** Scoping is by
  construction: the listing endpoint already resolves the caller (`db_user_id` from the
  token, `effective_org_id` server-side — `aws_connect_routes.py:134-138`). Per §4.4,
  `status == "verified"` rows only; a `pending` connection must not be selectable, or
  merely *starting* a connect flow could redirect the user's traffic to an account that
  fails every call.
- **Show the effective destination, including when it is overridden** by a platform
  mapping (§1.4). "Your calls currently go to …1234 (set by a platform admin)" is
  honest; showing the user's own stale pick as active is the inert-config defect.
- **Under fail-closed, say what selecting does.** The screen should state that if the
  chosen account cannot serve a call, the call fails rather than falling back — ruling 1's
  behavior is surprising if undisclosed, and this is the one surface where the person
  choosing is also the person who will be paged by it.

### 6.5 Deliberate non-goal: no org-admin surface

Ruling 4 removes the org-admin rung "for now (may be delegated later)." No org-admin
routing UI should be built, and no endpoint should accept an org-admin caller. §1.3 notes
how to keep later delegation to a one-site change.

### 6.6 "Register new destination" reuses the Connect-AWS component — ruling 4b

Ruling 4b: *"opens the SAME role-ARN form component the credentials page uses (identical
input + test-assume validation), saving into the platform-scoped registry rather than a
personal one."*

The component is `frontend/src/pages/settings/ConnectAws.tsx` — today a **page**, not a
reusable component: it owns its route, calls `useNavigate()`, and holds its own form state
(`ConnectAws.tsx:18-26`). Honoring "the SAME component" therefore means a small
refactor — extract the form + launch/verify flow into a shared component that both the
page and the admin panel render — rather than a copy. **Copying it is the failure mode to
avoid**: two divergent implementations of a CloudFormation quick-create + verify flow is
exactly the duplicate-implementation class CLAUDE.md's reuse-table rule targets, and the
two would drift the moment the template versions (§5.0, §5.0b both change it).

What differs between the two callers is only the **destination of the save**: personal
(`user_credentials` row owned by the caller, as today —
`aws_connect_routes.py:167-179`) vs. the platform-scoped registry (§1.1b, §4.2
requirement 2). That is a parameter, not a fork.

Per §5.0b, the admin registration path must use the **routing-capable template** (child G),
not `aws_role_v1.yaml` — otherwise every admin-registered destination is born pinned to
one user id and unusable for the team/org mappings it exists to serve.

### 6.7 Save performs a real test assume-role — ruling 4a, the #4511 gate

Ruling 4a: *"the platform performs a test assume-role before saving; a mapping that cannot
be assumed is rejected, never stored inert (#4511 class)."*

`connect_verify` already is this check — a real `sts:AssumeRole` with the credential's
ExternalId, writing `status` into `scopes` (`aws_connect_routes.py:208-261`). Requirements:

1. **Reuse it; do not write a second assume probe.** Two probes with different
   conditions is how "verified here, broken there" happens.
2. **Reject on failure with the reason surfaced** — and reuse §2.6's `reason` codes so the
   authoring-time failure and the runtime failure speak the same vocabulary. An admin who
   sees `assume_role_failed` at save and `assume_role_failed` at runtime can connect them;
   two different error taxonomies for one condition cannot be correlated.
3. **Probe Bedrock invoke capability too, not just assumability** (§5.0 implication 3). A
   role that assumes but lacks `bedrock:InvokeModel` is precisely an inert mapping —
   it passes the naive gate and fails every call. This is why §5.0 is a blocker for the
   *authoring* work and not only the signing work.
4. **A passing probe is not a permanent guarantee.** The customer can delete the role or
   rotate the ExternalId afterwards. The gate removes never-worked mappings; §2.5's
   fail-closed error handles worked-then-broke. Both are needed — do not let the gate's
   existence argue away the runtime error path, or vice versa.
5. **Re-validation should be available on demand** (a "test" action per mapping, like the
   admin-connections revalidate endpoints at `src/admin/connections/routes.py:17`), so an
   admin diagnosing a fail-closed outage can distinguish "mapping wrong" from "Bedrock
   down" without waiting for a user to retry.

---

## 7. Coverage gap: the worker-side signing path

`sigv4-proxy.ts` does **not** sign to Bedrock. It re-signs to an ADP target
(`--target` / `SIGV4_PROXY_TARGET`, `modules/agent-factory/agent/src/sigv4-proxy.ts:31`,
`:38-40`) using the runner's ambient credentials (`defaultProvider()`, `:23`, `:48`),
because "the Claude Code SDK signs with service=`bedrock` but API Gateway needs
[different]" (`:5-7`).

That is **good news for this design**: agent-worker traffic reaches Bedrock *through
the gateway*, so it inherits gateway-side routing automatically. No worker change is
needed and the worker fleet does not need cross-account IAM.

But it must be verified rather than assumed, because it determines whether routing
coverage has a hole: **if any worker path calls `bedrock-runtime` directly with ambient
runner credentials, that traffic bypasses the routing seam entirely and will keep
landing on the platform bill while the operator believes it is routed.** Silent
partial coverage is a spend bug of the same class as wrong-account. Recommend an
explicit verification task (§9, child A) enumerating every Bedrock-reaching path —
gateway proxy, mantle passthrough, worker, any Lambda — and classifying each as routed
or knowingly-out-of-scope.

### 7.1 🟠 A second, pre-existing customer-billed Bedrock path already exists — and bypasses the gateway

The worker entrypoint has an `ADP_BEDROCK_VIA` switch
(`modules/agent-factory/agent-worker-image/entrypoint.py:1536-1631`) with four values.
`"gateway"` (default) routes through the sigv4-proxy to the platform gateway and is
covered by this design. But `"user"` (`:1596-1603`) pops `AWS_ROLE_ARN`,
`AWS_WEB_IDENTITY_TOKEN_FILE` and `AWS_PROFILE` from the agent env so the customer's
assumed STS credentials serve Bedrock directly — **the pod calls Bedrock with customer
credentials, bypassing the gateway entirely.** The code labels it "legacy: operations
persona on customer-billed Bedrock" (`:1540`).

**This is already a customer-billed Bedrock mechanism, and it is invisible to
everything this design builds:** no gateway metering, no `usage_logs` row, no budget
reservation, no `bedrock_account_id` capture. It is reachable only for personas in
`PERSONAS_NEEDING_AWS = {"operations", "agent-operations"}` (`:49`) and only when
`ADP_BEDROCK_VIA=user` is explicitly set, so its blast radius is small today.

Why it matters to this issue: it is a **second answer to the same question** ("whose
account pays for Bedrock"), decided at a different layer, with different (absent)
metering. Two mechanisms that answer the same question and disagree is exactly the
condition that produces "the operator believes traffic is routed and metered when it
is not."

**SETTLED (ruling 3): `ADP_BEDROCK_VIA=user` retires** once the mapping mechanism is
proven — *"One source of truth for where a call lands."* Once gateway-side routing lands,
the legitimate use case ("operations persona on customer-billed Bedrock") is served by a
routing mapping, *with* metering and budget enforcement intact.

**The retirement sequence is the ruling's own — shadow → enforce → remove** (child H),
and it deliberately trails the rollout phases in §8.2 rather than running in parallel:

| Step | Action | Gate to advance |
|---|---|---|
| 1. Shadow | Routing resolves in shadow (§8.2 phase 1). `ADP_BEDROCK_VIA=user` still functions untouched. | Resolution verified correct for the operations persona's principals. |
| 2. Enforce | Routing enforced for those principals via a mapping (§8.2 phase 2), so the customer-billed path they need now exists *with* metering. `ADP_BEDROCK_VIA=user` still functions, but nothing needs it. | Operations persona confirmed working through the gateway on their own account; usage now appearing in `usage_logs` where before it appeared nowhere. |
| 3. Remove | Delete the `"user"` branch from the worker entrypoint (`modules/agent-factory/agent-worker-image/entrypoint.py:1596-1603`). | — |

Two notes for child H. **The order is not negotiable**: removing the env var before a
working mapping exists takes the operations persona's Bedrock access away, and under
fail-closed there is no fallback to soften it. And **step 2 is where the metering gap
closes** — that is the actual win of ruling 3, since traffic on this path is invisible to
`usage_logs`, budget reservations and `bedrock_account_id` alike. Until step 3, child A
must document it as a known-uncovered path rather than leaving it to be rediscovered.

### 7.2 Other non-covered paths

Called out now so nobody assumes otherwise:

- **The mantle passthrough** (`POST /openai/v1/responses`) signs with the pod's ambient
  IRSA chain via `SigV4MantleAuth` (`src/proxy/mantle_auth.py:77-93`,
  `session.get_credentials()` at `:84`) against a URL built from
  `settings.mantle_region` (`src/app.py:158`). It is metered (`pricing.py:198-202`
  carries mantle model pricing) but **not routable** by this design without a parallel
  change. Its auth object is constructed **once at app startup** (`app.py:159`), so
  per-request routing there is a larger refactor. Recommend explicitly out of scope,
  documented, not silently omitted.
- **IAM/service-account callers** whose `team_id` and `org_id` come from the agent
  registry — they resolve through the same ladder, which is correct, but their
  `users.id` may not exist, so the user rung simply won't match. Fine; worth a test.

---

## 8. Question 6 — Migration and rollout

### 8.1 Default is today's behavior, and it costs nothing

With zero mappings authored:

- Every rung misses → rung 4 → ambient IRSA → `SimplePoolService` exactly as today
  (`src/app.py:141`).
- With the §2.2 existence gate, an install with no mappings performs **zero** extra
  queries. Not "a cheap query" — zero.

The change is therefore inert until a platform admin authors a mapping (or a user makes a
self-service selection). No backfill and no behavior change on deploy.

**Rev-2 correction:** rev-1 said "no data migration" here. Ruling 4b's platform-scoped
registry means routing **does** ship a migration (§1.1b) — but it is a purely additive
empty table, so the *inertness* claim is unaffected: an install with no rows behaves
exactly as it does today, which is the 036 property ("Purely additive. Nothing is
backfilled and no existing row changes meaning",
`alembic/versions/036_person_budget_defaults.py:62-65`).

### 8.2 Shadow mode — required, and nearly free

Three phases:

| Phase | Behavior | Gate to advance |
|---|---|---|
| **1. Shadow** | Resolve the target, **do not use it**. Sign with the platform account as today. Write the *would-be* account to `usage_logs.bedrock_account_id` and to an audit event. | Operator reviews real traffic and confirms resolved == intended for every mapped principal. |
| **2. Enforce, per-org opt-in** | Routing live for orgs the operator enables. Fail closed. | Soak; no unexpected `bedrock_account_unavailable`. |
| **3. Default enforce** | Routing live wherever a mapping exists. | — |

Shadow mode is cheap **because the column already exists** (§3.5) — no migration, one
argument threaded through an existing parameter. It is the structural answer to the
issue's "wrong account resolved" row: the wrong-account bug becomes **detectable before
it can cost anyone money**, rather than being caught by a test that has to guess the
mapping wrong in the same way production will.

Rev-2: shadow mode is no longer merely recommended. It is load-bearing for **two**
settled rulings — it is the mitigation that makes ruling 1's fail-closed survivable
(§2.5), and it is step 1 of ruling 3's retirement sequence (§7.1). Both would be unsafe
without it.

Note one honest limitation: shadow mode validates *resolution*, not *assumption*. It
cannot prove the target role is assumable, since it never assumes it. Two things cover
that gap, and rev-2 adds the first: **ruling 4a's save-time test assume** (§6.7) proves
assumability at authoring time, and **phase 2's per-org opt-in** catches
worked-then-broke. That is why phase 2 exists rather than going straight from shadow to
default — the gate and the soak cover different failures (§5.2 requirement 4).

### 8.3 Rollback

| Change | Rollback |
|---|---|
| Routing code | Revert the PR — code-only, and inert with zero mappings. |
| Enforcement flag | Flip to shadow; traffic returns to the platform account immediately. **This is the fast lever** — under fail-closed it is also the outage remedy, so it must be operable without a deploy. |
| A single bad mapping | Delete the mapping row; next request falls through to the next rung. Requires cache eviction on mapping change (§2.3) — **without it, rollback is delayed by up to the credential TTL (up to 3600s)**, which is unacceptable for a mis-billing incident. Eviction-on-change is a requirement, not an optimization. |
| Schema (**rev-2: revised**) | A migration now exists (§1.1b). Rollback is the 036 shape: **stop reading the table, then drop it** — safe because the table is additive and has no readers outside routing. Not a data down-migration; nothing else joins it. |
| Admin panel / self-service selector | Feature-gate the surfaces (`FeatureGate`, `frontend/src/App.tsx:90-92`) — hides authoring without touching existing mappings. |

Rev-1 sold "zero migrations, so rollback is a flag flip." **That is corrected**: routing
ships one additive table. The practical rollback story is nearly as good — the *fast*
lever is still the enforcement flag, which needs no schema action at all, and the table
only has to be dropped in a full retreat. But implementers should not plan on a
migration-free change, and reviewers should expect a migration in the diff.

**One rollback ordering note that fail-closed makes sharp:** deleting a mapping is a
*safe* rollback (traffic falls to the next rung, ultimately the platform account), while
deleting a *connection* another mapping still references is **not** — it turns that
mapping into a fail-closed outage. Deleting a connection must therefore check for
referencing mappings first, or cascade to them. This is the ordinary FK-discipline
question, but under fail-closed the consequence of getting it wrong is an outage rather
than a dangling row.

---

## 9. Operator rulings — SETTLED (2026-09-07)

Rev-1 asked three questions here. All three are answered, and the operator added three
more decisions rev-1 had not asked about. **This section is now a record, not a request.**
The authoritative text lives on #4692 under "Settled rulings (operator, 2026-09-07)"; what
follows is each decision plus the consequence for implementers.

### 9.1 Fail closed, or allow an opt-in fallback? (§2.5) — ✅ RULED: fail closed

**Ruling 1: fail closed, always, with an actionable error. No silent fallback to the
platform account.** The per-mapping audited fallback opt-out rev-1 offered as a hedge was
**not** taken — there is no fallback mechanism in this design at any granularity.

Accepted cost, recorded so it is not relitigated at implementation time: **a broken role
link takes that principal's model access down**, and at the org rung that is the whole
org. In exchange a misdirected bill becomes structurally impossible. The issue grades
wrong-account as "the worst possible spend bug"; downtime is recoverable, spent money is
not.

Consequences: §2.6 (actionable error with `reason` codes), §6.7 (save-time gate removes
the never-worked class), §8.2 (shadow first), §8.3 (the enforcement flag is the outage
remedy and must not require a deploy).

### 9.2 May a user author their own user-rung mapping? (§1.3) — ✅ RULED: yes

**Ruling 2: self-service allowed for one's own mapping**, on the existing credentials
screen (§6.4). A user may select which of their own credentialed accounts serves their
calls.

Rev-1's rationale was accepted: a user can only point at an account they proved control of
via `connect_verify`'s real STS assume (`aws_connect_routes.py:208-261`), so self-selection
grants no privilege and spends no one else's money. §5.0b adds that IAM enforces this
independently — they *cannot* select someone else's connection and have it work.

**The asymmetry with #4690 is confirmed deliberate.** #4690 removed self-authoring for
person limits; this design allows it for routing. The discriminator is whose money moves
(§1.3). Do not harmonize them.

One question this ruling opens and does not close: when a platform admin *and* the user
have both written the user rung, which wins? See §1.4 — flagged for child F/I, with a
recommendation.

### 9.3 Deprecate `ADP_BEDROCK_VIA=user`? (§7.1) — ✅ RULED: yes, retire it

**Ruling 3: `ADP_BEDROCK_VIA=user` retires** after the mapping mechanism is proven, on a
**shadow → enforce → remove** sequence — *"One source of truth for where a call lands."*

Sequencing was the operator's call, and it was made: retirement trails proof. The step
table is in §7.1; the non-negotiable part is that a working mapping for the operations
persona exists *before* the env var branch is deleted, because under fail-closed there is
nothing to soften its removal.

### 9.4 Additional rulings rev-1 did not ask for

These three arrived unprompted and moved the design more than the three above.

| Ruling | Decision | Where |
|---|---|---|
| **4** | **Admin authoring is PLATFORM ADMIN ONLY** — no org-admin rung for now (may be delegated later). Reverses rev-1's org-admin recommendation. | §1.3 |
| **4b** | The admin surface is the **admin console** ("Bedrock Account Routing" panel beside Budget Management defaults), **not** the personal credentials page. Forces a platform-scoped registry. | §6.1, §6.3 |
| **4a / 5** | **Mapping targets are CONNECTIONS, never bare account numbers.** Dropdown from platform-connected accounts scoped to the org being mapped; "register new destination" reuses the Connect-AWS quick-create flow into a platform-scoped registry; **save performs a test assume-role and rejects unassumable mappings** (never stored inert, #4511). | §1.1, §6.6, §6.7 |
| **6** | **Team/org mappings must reference org-linked or admin-registered connections**, never one individual's personal credential. | §4.3, §5.0b |

Ruling 4b is the one with the largest structural consequence, and it is not obvious from
its text: a platform-scoped registry cannot live in `user_credentials` (§1.1b), which
retires rev-1's "zero new tables" claim and its "isolation is free from the resolver's
`org_id` filter" proof (§4.2). Both are corrected in place.

### 9.5 Noted, not a ruling

Cross-account and cross-*region* are entangled: the credential payload carries
`default_region` (`aws_connect_routes.py:142-152`, read at
`assume_role_routes.py:220`). A routed account may not have the caller's model in the
caller's region. Related to the open region-agnosticism spike (#1324). Recommend
routing carries the region from the credential and the §5 error path covers
region-unavailability with the same error class.

---

## 10. Proposed child issues

**Proposed only — not filed**, per the instruction on #4692 and #4734. **Rev-2 reworked
this table to match the rulings**: three children are new (I, J, K), two changed scope
materially (F, G), and the ruling-gated dependencies are gone because the rulings landed.

| # | Title | Scope | Depends on |
|---|---|---|---|
| **A** | Bedrock-reaching path audit + `bedrock_account_id` capture | Enumerate every path that reaches `bedrock-runtime` (gateway proxy, mantle, worker, Lambdas); classify routed vs. knowingly-out-of-scope (§7). Thread the resolved account into the existing `log_request(bedrock_account_id=…)` parameter (§3.5). No migration. Delivers the audit trail shadow mode needs. | — |
| **G** | **Blocker:** routing-capable destination role — `aws_role_v2` template | **Rev-2: scope grew.** Two defects, one template. (i) The connect CFN role attaches only `ReadOnlyAccess` (§5.0) — add resource-scoped `bedrock:InvokeModel*`. (ii) **NEW:** its trust policy pins `aws:RequestTag/adp:user_id` to one user, so every connected account is assumable by exactly one person (§5.0b) — the routing template must drop that condition while keeping `sts:ExternalId` + `aws:PrincipalArn`. A **separate** template from the read-only agent-delegation role, not a widening. Capability probe in `connect_verify`; re-run-CFN prerequisite surfaced in the UI. | — (parallel with A) |
| **I** | **NEW (ruling 4a/4b):** platform destination registry + mapping schema | The migration ruling 4b forces (§1.1b): mapping rule table on the migration-036 shape (`scope_type` stored; two scope columns; **UNIQUE expression index over `COALESCE`, not a `UniqueConstraint`** — the NULL-distinctness trap, §1.1b; `CheckConstraint` per rung; `authored_by_user_id` = canonical `users.id`), plus the platform-scoped destination registry with its explicit "platform-registered" marker (§4.2 req 2 — **not** a NULL `org_id` meaning "allowed everywhere"). Schema + model only. | — |
| **B** | Routing target resolution + shadow mode | Resolve `BedrockTarget` over the mapping ladder (§1.2) and dereference to a connection (§1.1a); global existence gate (§2.2); verified-only filter (§4.4); reads `org_id` **not** `attributed_org_id` (§3.4); shadow-mode flag. **No signing change** — resolve and log only. | A, I |
| **J** | **NEW (ruling 4a):** test-assume validation service | The save-time gate (§6.7): reuse `connect_verify`'s real STS assume — do **not** write a second probe — extended to also probe Bedrock invoke capability (§5.0 impl 3). Shared `reason` code vocabulary with the runtime error (§2.6). Reject-never-store-inert (#4511). Includes the on-demand re-validate action (§6.7 req 5). | G |
| **C** | Cross-account signing: `get_client(target)` + isolated credential cache | Interface change + 8 call sites (§2.1); `AsyncBedrockClient` accepts explicit credentials preserving both `Config` timeouts (§2.4); STS cache keyed on the full identity tuple, LRU-bounded, evict-on-change (§2.3); **fail-closed with the §2.6 actionable error** (no fallback path exists — ruling 1); per-org enforcement opt-in. | B, G, J |
| **F** | Mapping authoring API + admin panel | **Rev-2: rewritten.** **Platform-admin-only** CRUD via `require_platform_admin` (§1.3) — *not* the org-admin design rev-1 proposed. "Bedrock Account Routing" panel beside Budget Management defaults (§6.3): mappings table with **effective-mapping + source-rung display**, org-scoped destination dropdown excluding per-user connections for team/org rungs (§5.0b), "register new destination". Server-side scope check (§4.2 req 1) and the ruling-6 personal-credential rejection (§4.3). Settles §1.4 (admin vs. self precedence) explicitly. Audit events. | I, J; **UI pattern follows #4691** |
| **K** | **NEW (ruling 2):** self-service Bedrock account selector | On the existing credentials screen (§6.4) — an addition to the existing AWS-credential list, no new screen. Verified-only, own-connections-only, shows effective destination **including when overridden** by a platform mapping (§1.4), and discloses fail-closed behavior. Extracts the shared role-ARN form component that §6.6 needs so F can reuse it rather than copy it. | I, J |
| **D** | Bedrock error-class discrimination + model-enablement UX | Split `AccessDeniedException` / `ValidationException` from transport errors in `_invoke_bedrock` (§5.1) — **prerequisite for ruling 1's per-cause `reason` codes**, not a nicety (§2.6 req 1); model-not-enabled error (§5.2); authoring-time enablement **warning, not gate** (§5.2 req 3); note `get_available_models` account-dependence as follow-on. | C |
| **H** | Retire `ADP_BEDROCK_VIA=user` (ruling 3) | The shadow → enforce → remove sequence in §7.1. Step 3 deletes the `"user"` branch at `entrypoint.py:1596-1603`. **Ordering is not negotiable** — a working mapping for the operations persona must exist first, since fail-closed offers no softening. | C, F |
| **E** | **Defect (independent):** `organizations.aws_accounts` shape collision | Two admin APIs write incompatible shapes into one JSON column; the tenancy reader's membership test is always-false for the object shape (§4.1). Pre-existing, not introduced here, and **not** on this feature's critical path since §4.1 sidesteps it — but a live tenancy-resolution bug. File independently. | — |

**Sequencing summary.** Three can start immediately and in parallel: **A** (audit +
capture), **G** (the template blocker), **I** (schema). Then **J** (validation, needs G's
template) and **B** (resolution, needs A + I). Then **C** (signing) and the two surfaces
**F** / **K**. Then **D** and **H**. **E** is independent of everything.

**G is still the hard blocker** and rev-2 makes it harder, not easier: it now carries two
defects rather than one, and §5.0b means **no team or org mapping can work at all until it
ships**. If anything is pulled forward, pull G.

**Not proposed** (out of scope per the issue's non-goals or this note): header-driven
account selection (#4132 class); routing the mantle passthrough (§7 — documented
out of scope); reconciling ADP list pricing against real AWS invoices (§3.5); an
**org-admin authoring rung** (ruling 4 defers it explicitly — "may be delegated later";
§1.3 keeps it to a one-site change when it comes).

---

## 11. Design coverage audit

| Design question (from the issue) | Answered in | Confidence |
|---|---|---|
| 1. Resolution model + authoring | §1 | **High** — ladder mechanics grounded in code; authoring **settled** (platform-admin-only + self-service, ruling 2/4); both asymmetries recorded (§1.3). One derived question flagged open: admin-vs-self precedence at the user rung (§1.4) |
| 2. Mechanics: hook, credentials, cache, fail-closed | §2 | **High** — seam identified at 8 call sites; two latent bugs found (cache key, client shape); fail-closed **settled** with the actionable-error shape (§2.6) |
| 3. Spend/budget interplay | §3 | **High** — proved by non-participation with citations; the `attributed_org_id` prohibition is the key finding. **Unchanged in rev-2 by design** |
| 4. Model access | §5 | **High on diagnosis, blocked on two prerequisites** — §5.0 (role has no Bedrock permission) and §5.0b (role assumable by one user only); per-model behavior **settled** (error, ruling 1); `get_available_models` deferred |
| 5. Tenant isolation | §4 | **Medium-high** — rev-1's free query-level guarantee is **gone** under ruling 4b; replaced by an explicit authoring-time check (§4.2) that must be tested, plus the ruling-6 constraint (§4.3). Weaker than rev-1 and honestly labeled |
| 6. Migration/rollout + shadow mode | §8 | **High** — inert-by-default, shadow mode nearly free. **Corrected:** one additive migration now required (§1.1b), so "zero-migration" no longer holds |
| Authoring surfaces (rulings 2/4a/4b) | §6 | **Medium** — derived from ruling 4b's text; the referenced mockup is **absent from the repo** (§6.2), so the surface description is proposed pending sign-off |
| Wrong-account row: structural prevention? | §8.2 (shadow mode) + §2.3 (cache key) + §6.7 (save-time assume gate) + §4.2 (authoring scope check) | **Structural** ✅ — but note the isolation leg is now a code check, not a query property |
| Fail-open row: structural prevention? | §2.5 — fail closed, **settled**; no fallback path exists at any granularity | **Structural** ✅ (ruling 1 closed this) |
| Cross-principal credential-cache row | §2.3 — cache key is the full identity tuple, making cross-tenant reuse unrepresentable | **Structural** ✅ |
| Hot-path latency row | §2.2 — global existence gate ⇒ zero queries when no mapping exists; 1 query + 1 dereference otherwise | **Structural** ✅ (improved in rev-2 — the gate is global, not per-org) |
| Coverage completeness (is all Bedrock traffic actually routed?) | §7, §7.1 — child A audit; `ADP_BEDROCK_VIA=user` now **on a settled retirement path** (ruling 3); mantle documented as uncovered | **Improved, not yet closed** ⚠️ |
| Inert-config prevention (#4511) | §6.7 (reject unassumable at save), §4.2 req 3, §1.4 + §6.4 (never show a setting as active when something else governs) | **Structural** ✅ (ruling 4a closed the storage half) |

---

## 12. Verdict

⚠️ **Ready with caveats.** The access model is **settled** — no decision is waiting on
the operator. The mechanics are grounded in code and the spike's six questions are
answered. What remains is technical, and it is all in the children.

**Blockers (all technical; none is a ruling):**

1. **Child G is the hard blocker, and rev-2 doubled it.** (a) The role the AWS-connect
   flow creates attaches only `ReadOnlyAccess`, excluding `bedrock:InvokeModel` (§5.0) —
   routing to it fails 100% of calls. (b) Its trust policy pins
   `aws:RequestTag/adp:user_id` to a single user (§5.0b), so **no team or org mapping can
   work against any currently connected account.** Both need a *separate* routing template,
   not a widening of the read-only one. Nothing downstream can be validated end-to-end
   until this ships.
2. **Child A must land first** on the metering side. Without the path audit, routing
   coverage could be silently partial (§7.1 shows one bypass exists, now on a retirement
   path), and without `bedrock_account_id` capture there is no shadow mode — a
   structural prevention for the wrong-account bug.
3. **Child I is newly on the critical path** (§1.1b). The migration ruling 4b forces is
   small but it gates B, F, J and K, and its uniqueness index has a
   get-it-wrong-and-it's-a-wrong-account-bug trap (NULLs compare distinct in a Postgres
   `UniqueConstraint`).

**One design question the rulings created and did not answer** (§1.4): when a platform
admin and a user have both written the user rung, which wins? Recommended: admin wins,
with the self-service screen showing the override. Child F should settle it explicitly
rather than letting the first implementation decide by accident.

**One artifact gap:** the mockup at `docs/mockups/4692-bedrock-account-routing-admin.html`
is **not in the repo** (§6.2). §6's surface description is derived from ruling 4b's text
and is proposed pending sign-off.

**Four findings should change how the operator reads the original issue:**

- **The connection layer already exists** and should be reused, not rebuilt (§1.1a) — the
  credential ladder, the role-ARN form, the ExternalId-proven verify probe are all live
  code.
- **But the mapping needs its own table after all** (§1.1b). Rev-1 claimed zero
  migrations; ruling 4b's platform-scoped registry cannot live in `user_credentials`
  (`TenantMixin.org_id` is `nullable=False`). **This is the correction most likely to
  surprise a reader of rev-1**, and it also removes rev-1's free tenant-isolation proof
  (§4.2). Copy migration 036's shape, including its `COALESCE` unique index.
- **The "existing linked-account machinery" is two different things** (§0.2): a live,
  ExternalId-proven credential ladder (`user_credentials` + `CredentialResolver` —
  reuse) and a dead round-robin throughput pool (`PoolService` — harvest its STS cache,
  discard its selector; its IAM grant even targets a role-name convention
  (`*BedrockGateway-Pool*`) that no existing connected account matches, and it sends no
  session tags at all, §5.0b).
- **The destination roles can neither invoke Bedrock nor be assumed for anyone but their
  creator** (§5.0, §5.0b) — the largest gap between the issue's premise and the code, and
  the reason the effort is larger than "wire up a ladder" even though the ladder is cheap.

The budget question the issue flagged as load-bearing remains the *least* risky part: §3
shows metering, pricing and attribution are untouched **by non-participation** — the
routing decision and the settlement path share no state. That section is deliberately
unchanged in rev-2. The one discipline that must
be enforced by review is §3.4: routing keys off `org_id` (authenticated), never
`attributed_org_id` (caller-influenced), or #4132 returns as a credential-acquisition
bug rather than an accounting one.

The budget question the issue flagged as load-bearing is the *least* risky part: §3
shows metering, pricing and attribution are untouched **by non-participation** — the
routing decision and the settlement path share no state. The one discipline that must
be enforced by review is §3.4: routing keys off `org_id` (authenticated), never
`attributed_org_id` (caller-influenced), or #4132 returns as a credential-acquisition
bug rather than an accounting one.
