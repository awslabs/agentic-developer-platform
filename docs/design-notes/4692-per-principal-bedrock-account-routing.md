# Design Note: Per-Team / Per-User Bedrock Account Routing (Issue #4692)

> **Status**: Design-review (spike output) — **three open questions need a human ruling** (§8.1, §8.2, §8.3)
> **Author**: @agent-architect
> **Date**: 2026-09-07
> **Issue**: #4692 — route Bedrock calls to a chosen AWS account per team or per user
> **Mode**: Per-issue spike (EPIC #4324)
> **Verdict**: ⚠️ Design-complete **with three operator rulings outstanding and two prerequisite blockers** (§4.1, §5.0). The mechanics are grounded in code; the fail-closed recommendation is stated with its tradeoff rather than decided unilaterally.
> **Related**: #4690 (defaults ladder — precedence + authoring), #4132 (attribution vs. authorization), #4689 (fused person envelope, hot-path discipline), #4300 (root-human attribution), #440 (credential scope relaxation — the ladder this reuses), #481 / #562 (aws_role assume delivery + AWS-connect), #4620 (cross-org person budgets)

---

## 0. Executive summary

The issue asks for a resolution ladder over "the existing linked-account/assume-role
machinery." The grounding read produced **five findings that materially change the
shape of the work** relative to the issue text. All five are cited from code, not
assumed.

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
   **This issue is ~80% "point the proxy at machinery that already exists," not
   "build a mapping ladder."** A new sibling table mirroring #4690 would be a
   *second* user/team/org ladder over the same credential data — the duplicate-
   implementation failure CLAUDE.md's reuse-table rule exists to prevent.

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

The resulting recommendation: **reuse `user_credentials` + `CredentialResolver` as the
mapping and the ladder; introduce no new mapping table; make the routing decision a
resolved-target argument to `IPoolService.get_client()`; cache STS credentials keyed
on the full identity tuple; fail closed; and thread `bedrock_account_id` into the
existing usage parameter so shadow mode ships before enforcement.**

Metering, pricing and attribution are **structurally untouched** by routing, and §5
proves why rather than asserting it.

---

## 1. Question 1 — Resolution model

### 1.1 Recommendation: reuse the credential ladder; do not build a sibling table

The issue proposes "likely a sibling table" to #4690's `person_budget_defaults`. **I
recommend against it**, on reuse grounds.

| What's needed | Where it already lives | Verdict |
|---|---|---|
| user → team → org precedence walk | `CredentialResolver.resolve()`, `src/shared/services/credential_resolver.py:142-156`; order at `:49` | **Reuse** |
| Per-scope storage of an AWS role target | `user_credentials` owner columns + CHECK, `src/shared/models/vault.py:114-146` | **Reuse** |
| The routing target payload (account_id, role_arn, external_id, region) | `aws_role` credential `scopes` JSON + SM secret, `src/auth/aws_connect_routes.py:145-183` | **Reuse** |
| Authoring UI + validation that the role really exists | AWS-connect start/verify, `src/auth/aws_connect_routes.py:118`, `:208` | **Reuse** |
| Assume with ExternalId + session tags | `src/internal/assume_role_routes.py:220-240` | **Reuse** |
| A *platform-default* rung (today's behavior) | — | **New**, but §1.3 shows it needs no table |

A sibling table would duplicate rungs 1–5 and create two disagreeing answers to "which
account for this person," which is the #4511 inert-config class the #4690 review already
flagged.

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
1. user      — user_credentials.user_id = <canonical users.id>
2. team      — user_credentials.team_id = <users.team_id>
3. org       — user_credentials: all three owner columns NULL (org-scoped)
4. platform  — no row matches → ambient IRSA (today's behavior, unchanged)
```

Rungs 1–3 are `CredentialResolver.resolve(org_id=…, service="aws",
label=<routing label>, user_id=…, team_id=…)`. Rung 4 is the
`CredentialNotFoundError` branch — which is exactly why the default is
today's behavior with zero configuration (§7.1).

**A dedicated label is required, not optional.** `service="aws"` credentials already
exist for other purposes (the whole #481 agent-assume path). Routing must not
hijack an arbitrary AWS credential a user connected for something else. Recommend a
reserved label — `bedrock-routing` — and resolve with `label=` set, which takes the
`_find` fast path (`credential_resolver.py:250-252`, single-row `LIMIT 1`) rather
than the multi-row ranking branch. This also gives operators a way to connect an
account *without* routing traffic to it.

**`team_id` is available on the hot path already**: `TokenContext.team_id` is a
required field (`src/shared/schemas/auth.py:48`), so the team rung costs no extra
lookup. Note the semantics: `users.team_id` is a plain non-null `String(255)`
(`src/shared/models/organization.py:123`) with a `Team` table alongside it — there
*is* a team entity, so #4690's `scope_team_id` and this ladder's team rung agree.

### 1.3 Who may author each rung

The issue's own instinct is right and the code supports it. **Authoring is
org-admin-within-their-org, unlike #4690's platform-admin-only person limits** —
because the destination account is org-linked and the thing being authored is
"spend our own money," not "bound someone else's spend."

| Rung | Author | Enforcement point |
|---|---|---|
| user | the user themselves **or** an org admin | AWS-connect already scopes to the caller: `db_user_id` from the token, `effective_org_id` resolved server-side (`aws_connect_routes.py:134-138`) |
| team | org admin | new authz check; must verify the admin's `org_id` owns the team |
| org | org admin | same |
| platform | nobody — it is the absence of a row | n/a |

The critical property: **a user authoring their own rung is not a privilege
escalation**, because they can only point at an account they can prove control of —
`connect_verify` performs a real STS AssumeRole against the ExternalId before the
credential is usable (`aws_connect_routes.py:208-261`). Contrast with #4690, where
self-authoring *was* an escalation (raise your own cap) and was removed by operator
ruling. **The two issues reach opposite authoring answers for a principled reason,
and this note records that asymmetry deliberately** so a future reader doesn't
"harmonize" them into a bug.

`ScopeEscalationError` (`credential_resolver.py:56-62`, raised at `:178-182`) and the
`strict` flag (`:168`) are already the guardrails for "don't let a user-scoped
request silently acquire an org-wide credential." Routing should pass **no**
`scope_hint`, because fallback up the ladder is the entire feature — but it must
honor `strict`, which it gets for free.

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

1. **Bounded query count.** With `label=` set, each rung is one indexed single-row
   lookup. `uq_user_credentials_user_service_label` and
   `..._team_service_label` are unique indexes on exactly
   `(owner, service, label)` (`src/shared/models/vault.py:119-120`), and the org rung
   is covered by `ix_user_credentials_org_id_service` (`:122`). Worst case **3 indexed
   lookups**, best case 1.
2. **An existence gate, mirroring the person-cap one.** The pattern exists and is
   proven: `_any_person_caps_exist`
   (`src/budget/enforcement_service.py:1353-1372`) is a process-local, unlocked
   `SELECT id … LIMIT 1` behind a 60s TTL (`_PERSON_CAPS_EXISTENCE_TTL_SECONDS`,
   `:97`). Routing should ride the same shape: a short-TTL cached boolean "does this
   org have *any* `bedrock-routing` credential?"
   Orgs with zero mappings — which is **every org on day one** (§7.1) — pay **zero
   queries**, not three. This is the single most important latency decision in the
   design and it is the reason the feature can ship default-on-safe.
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

### 2.5 Fail-closed vs. fallback-to-platform

**Recommendation: fail closed.** With the tradeoff stated for the operator, per the
instruction on this issue.

| | Fail closed (recommended) | Fallback to platform |
|---|---|---|
| Wrong-bill risk | **Eliminated structurally.** A resolved mapping is honored or the call fails. | **Present and silent.** The mapping's entire purpose voids itself precisely when it matters, with a 200 response. |
| Availability | **Worse.** A broken role link (customer deletes the role, rotates ExternalId, hits an SCP) takes that team's model access down until fixed. | Better — calls keep working. |
| Detectability | Immediate, loud, attributable — a 5xx naming the account link. | **Undetectable without the audit trail** the feature doesn't have yet. |
| Reversibility of the harm | Downtime is recoverable. | **A misdirected bill is not** — the money is spent on someone else's account. |

The asymmetry is decisive: fail-open trades an *unrecoverable, silent* accounting harm
for a *recoverable, loud* availability harm. The issue's own blast-radius table already
grades wrong-account as "the worst possible spend bug." Silent fallback is that bug
with extra steps.

**Operator ruling requested (§8.1)** — because the cost is real: an org that routes at
the org rung and whose role link breaks loses *all* model access, not some. Two
mitigations make fail-closed operationally survivable, and I recommend both:

- **Shadow mode first** (§7.2) — no enforcement until the audit trail shows the
  resolved account is the intended one for real traffic.
- **An explicit, per-mapping, admin-set opt-out** if the operator wants fallback
  available at all. It must be an authored decision on the mapping row with an
  audit event, never a global default and never an implicit catch — "the operator
  chose availability over billing accuracy for this mapping" is defensible;
  "the system quietly chose it for everyone" is not.

**Error shape.** A 5xx (502 — upstream credential acquisition failed; the client's
request was well-formed) whose body names *the account link*, never the credential:
`{"error": "bedrock_account_unavailable", "account_id": "…", "credential_id": "…",
"scope": "team"}`. Precedent for the redaction discipline is already in the repo —
`assume_role_routes.py:241` writes `role_arn` to the audit row but explicitly keeps
it out of the user-facing error ("do NOT include role_arn in user-facing error",
`:241`; audit-only note at `:298-299`). Follow it exactly.

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

- the **audit trail** that makes shadow mode possible (§7.2),
- the **forensic record** that answers "whose account did this actually go to,"
- **not** read by any budget or enforcement path — grep confirms the only readers are
  usage read/filter surfaces.

It stays NULL for unrouted calls, which correctly means "not captured" rather than
"platform account" — consistent with the repo's established null-discipline (the
`client_tool` comment at `service.py:394-396` and the cache-token comment at
`:449-461` both insist on exactly this distinction).

---

## 4. Question 5 — Tenant isolation of mappings

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
credential row's own provenance, which is stronger than a config list anyway:

- A routing mapping is a `user_credentials` row, and `user_credentials` carries
  `org_id` via `TenantMixin` (`src/shared/models/vault.py:91`).
- `CredentialResolver` filters **every** rung on `org_id` (`credential_resolver.py:206`)
  — cross-org resolution is unrepresentable, not merely checked.
- The account's linkage is **proven, not asserted**: `connect_verify` performs a real
  STS AssumeRole with the per-credential ExternalId before the row becomes usable
  (`aws_connect_routes.py:208-261`, `external_id = str(uuid.uuid4())` at `:142`,
  described as confused-deputy protection).

So "an org may never route onto another org's linked account" is enforced by the
`org_id` predicate on the resolver plus the ExternalId proof — no reliance on the
broken column. The `aws_accounts` shape collision should still be **filed separately**
as a defect (§9, child E); it is not this issue's to fix, but any design that leaned on
it would be built on sand.

### 4.2 Additional isolation requirements

- **Only `status == "verified"` rows may route.** `connect_start` writes
  `status: "pending"` into `scopes` (`aws_connect_routes.py:172-176`); the resolver's
  ranking function reads that status but only as a *tie-break preference*, and will
  still return a pending row if it is the only match (`credential_resolver.py:239-248`).
  For routing, pending must be **excluded**, not deprioritized — otherwise
  `connect_start` alone (before the customer ever creates the role) silently
  reroutes a principal's traffic to an account that will fail every assume. With
  fail-closed, that is a self-inflicted outage triggered by merely *starting* a
  connect flow.
- **Cross-tenant cache key**, per §2.3.
- **Audit every resolution that routes**, reusing the `_write_audit` pattern
  (`assume_role_routes.py:123`, `:290-299`) — role ARN server-side only.

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

### 5.2 Recommendation: error, naming the account — agreeing with the issue

Do **not** fall back to the platform account. Fallback here is the §2.5 fail-open
argument in a narrower disguise, and worse: it would fire *per model*, so a team would
be routed correctly for most traffic and silently billed to the platform for whichever
models they hadn't enabled — the hardest possible version of the bug to notice.

Requirements:

1. **Distinguish the error classes.** `_invoke_bedrock`'s bare-`Exception` catch must
   discriminate `AccessDeniedException` / `ValidationException` from transport failures
   so the model-not-enabled case can carry its own error code. This is a small,
   contained change but it is a **prerequisite**, not a nicety — without it the
   feature's most common failure mode is indistinguishable from an outage.
2. **The error names the account and the model, never the credential**, per the §2.5
   redaction rule: `{"error": "model_not_enabled_in_account", "account_id": "…",
   "model_id": "…"}` with remediation text pointing at Bedrock model access in that
   account.
3. **Surface it at authoring time too, as a warning not a gate.** When an admin
   authors a mapping, list which of the org's allowed models are enabled in the target
   account. A gate is wrong — enablement changes after authoring, so a
   validate-once check would give false confidence.
4. **`get_available_models` becomes account-dependent in principle**
   (`model_resolver.py:211-225`). Out of scope for the spike; flagged as a known
   follow-on (§9, child D) because a model list that doesn't match what the
   destination account will actually serve is a support-load generator.

---

## 6. Coverage gap: the worker-side signing path

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

### 6.1 🟠 A second, pre-existing customer-billed Bedrock path already exists — and bypasses the gateway

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

**Recommendation: deprecate `ADP_BEDROCK_VIA=user` as part of this arc** — once
gateway-side routing lands, the legitimate use case ("operations persona on
customer-billed Bedrock") is served by a routing mapping, *with* metering and budget
enforcement intact. Raised as ruling §8.3, because deprecating it is a behavior change
for the operations persona and is not mine to decide. Until then, child A must
document it as a known-uncovered path rather than leaving it to be rediscovered.

### 6.2 Other non-covered paths

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

## 7. Question 6 — Migration and rollout

### 7.1 Default is today's behavior, and it costs nothing

With zero `bedrock-routing` credentials:

- Every rung misses → `CredentialNotFoundError` → rung 4 → ambient IRSA →
  `SimplePoolService` exactly as today (`src/app.py:141`).
- With the §2.2 existence gate, an org with no mappings performs **zero** extra
  queries. Not "a cheap query" — zero.

The change is therefore inert until an admin authors a mapping. No backfill, no data
migration, no behavior change on deploy.

### 7.2 Shadow mode — strongly recommended, and nearly free

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

Note one honest limitation: shadow mode validates *resolution*, not *assumption*. It
cannot prove the target role is assumable, since it never assumes it. Phase 2's
per-org opt-in is what covers that, and it is why phase 2 exists rather than going
straight from shadow to default.

### 7.3 Rollback

| Change | Rollback |
|---|---|
| Routing code | Revert the PR — code-only, and inert with zero mappings. |
| Enforcement flag | Flip to shadow; traffic returns to the platform account immediately. |
| A single bad mapping | Delete/unverify the credential row; next request falls through to the next rung. Requires cache eviction on revocation (§2.3) — **without it, rollback is delayed by up to the credential TTV (up to 3600s)**, which is unacceptable for a mis-billing incident. Eviction-on-revoke is a requirement, not an optimization. |
| Schema | **None needed** — no new table, no migration. |

The zero-migration property is worth stating plainly: it is the direct consequence of
reusing `user_credentials` instead of adding a sibling table, and it is why rollback is
a flag flip rather than a down-migration.

---

## 8. Operator rulings requested

### 8.1 Fail closed, or allow an opt-in fallback? (§2.5)

**Recommendation: fail closed, with shadow mode first and — only if the operator wants
it — an explicit per-mapping, audited fallback opt-out.** Never a global default,
never implicit.

The tradeoff, stated plainly: fail-closed means **a broken role link takes that
principal's model access down**, and at the org rung that is the whole org. In
exchange, a misdirected bill becomes impossible rather than merely unlikely. Given the
issue grades wrong-account as "the worst possible spend bug," and given downtime is
recoverable while spent money is not, I recommend accepting the availability cost.

### 8.2 May a user author their own user-rung mapping? (§1.3)

**Recommendation: yes** — because a user can only point at an account they proved
control of via `connect_verify`'s real STS assume (`aws_connect_routes.py:208-261`),
so self-authoring grants no privilege and spends no one else's money.

This **deliberately differs from #4690**, which removed self-authoring for person
limits by operator ruling. The asymmetry is principled — raising your own cap spends
the org's money, while choosing your own account spends your own — but the operator
should confirm it rather than have two ladders quietly disagree on authoring.

### 8.3 Deprecate `ADP_BEDROCK_VIA=user`? (§6.1)

**Recommendation: yes, once routing lands** — it is a second, unmetered answer to
"whose account pays for Bedrock" that bypasses the gateway, so it silently escapes
budget enforcement and the audit trail.

The tradeoff: it is the *current* mechanism for the operations persona on
customer-billed Bedrock, so deprecating it is a behavior change for that persona and
requires their routing mapping to be in place first. Sequencing, not principle, is
what needs the operator's call.

### 8.4 Noted, not a ruling

Cross-account and cross-*region* are entangled: the credential payload carries
`default_region` (`aws_connect_routes.py:142-152`, read at
`assume_role_routes.py:220`). A routed account may not have the caller's model in the
caller's region. Related to the open region-agnosticism spike (#1324). Recommend
routing carries the region from the credential and the §5 error path covers
region-unavailability with the same error class.

---

## 9. Proposed child issues

**Proposed only — not filed**, per the instruction on this issue. Sequenced; A is a
prerequisite for the rest.

| # | Title | Scope | Depends on |
|---|---|---|---|
| **A** | Bedrock-reaching path audit + `bedrock_account_id` capture | Enumerate every path that reaches `bedrock-runtime` (gateway proxy, mantle, worker, Lambdas); classify routed vs. knowingly-out-of-scope (§6). Thread the resolved account into the existing `log_request(bedrock_account_id=…)` parameter (§3.5). No migration. Delivers the audit trail shadow mode needs. | — |
| **B** | Routing target resolution + shadow mode | `BedrockTarget` resolution via `CredentialResolver` with the `bedrock-routing` label (§1.2); existence gate (§2.2); verified-only filter (§4.2); reads `org_id` **not** `attributed_org_id` (§3.4); shadow-mode flag. **No signing change** — resolve and log only. | A |
| **C** | Cross-account signing: `get_client(target)` + isolated credential cache | Interface change + 8 call sites (§2.1); `AsyncBedrockClient` accepts explicit credentials preserving both `Config` timeouts (§2.4); STS cache keyed on the full identity tuple, LRU-bounded, evict-on-revoke (§2.3); fail-closed 502 naming the account (§2.5); per-org enforcement opt-in. | B, ruling §8.1 |
| **G** | **Blocker:** connected role cannot invoke Bedrock — `aws_role_v2` template | The AWS-connect CFN role attaches only `ReadOnlyAccess` (§5.0), so every routed call would fail. New template version with resource-scoped `bedrock:InvokeModel*`; a **separate** routing role rather than widening the shared read-only one; capability probe in `connect_verify`; re-run-CFN prerequisite surfaced in the UI. | — (parallel with A/B) |
| **D** | Bedrock error-class discrimination + model-enablement UX | Split `AccessDeniedException` / `ValidationException` from transport errors in `_invoke_bedrock` (§5.1) — prerequisite for a usable failure mode; `model_not_enabled_in_account` error (§5.2); authoring-time enablement warning; note `get_available_models` account-dependence as follow-on. | C |
| **E** | **Defect (independent):** `organizations.aws_accounts` shape collision | Two admin APIs write incompatible shapes into one JSON column; the tenancy reader's membership test is always-false for the object shape (§4.1). Pre-existing, not introduced here, and **not** on this feature's critical path since §4.1 sidesteps it — but a live tenancy-resolution bug. File independently. | — |
| **F** | Mapping authoring API + admin UI | Org-admin CRUD for team/org rungs with authz (§1.3); reuses AWS-connect for the account link; audit events. | B, G |
| **H** | Retire `ADP_BEDROCK_VIA=user` | Deprecate the gateway-bypassing customer-billed Bedrock path (§6.1) once routing serves the operations persona with metering intact. | C, ruling §8.3 |

**Not proposed** (out of scope per the issue's non-goals or this note): header-driven
account selection (#4132 class); routing the mantle passthrough (§6 — documented
out of scope); reconciling ADP list pricing against real AWS invoices (§3.5).

---

## 10. Design coverage audit

| Design question (from the issue) | Answered in | Confidence |
|---|---|---|
| 1. Resolution model + authoring | §1 | **High** — ladder already exists in code; authoring asymmetry vs. #4690 explained and raised as a ruling |
| 2. Mechanics: hook, credentials, cache, fail-closed | §2 | **High** — seam identified at 8 call sites; two latent bugs found (cache key, client shape) |
| 3. Spend/budget interplay | §3 | **High** — proved by non-participation with citations; the `attributed_org_id` prohibition is the key finding |
| 4. Model access | §5 | **High on diagnosis, blocked on prerequisite** — §5.0 found the connected role has no Bedrock permission at all (`ReadOnlyAccess` only); per-model recommendation clear; `get_available_models` deferred |
| 5. Tenant isolation | §4 | **High**, with a prerequisite defect found and sidestepped |
| 6. Migration/rollout + shadow mode | §7 | **High** — zero-migration, inert-by-default, shadow mode nearly free |
| Wrong-account row: structural prevention? | §7.2 (shadow mode) + §2.3 (cache key) + §4.1 (`org_id` predicate) | **Structural, not test-only** ✅ |
| Fail-open row: structural prevention? | §2.5 (fail closed; fallback only as an audited per-mapping opt-out) | **Structural**, pending ruling §8.1 ⚠️ |
| Cross-principal credential-cache row | §2.3 — cache key is the full identity tuple, making cross-tenant reuse unrepresentable | **Structural** ✅ |
| Hot-path latency row | §2.2 — existence gate ⇒ zero queries for unmapped orgs; ≤3 indexed lookups otherwise | **Structural** ✅ |
| Coverage completeness (is all Bedrock traffic actually routed?) | §6, §6.1 — child A audit; `ADP_BEDROCK_VIA=user` and mantle documented as uncovered | **Documented, not yet closed** ⚠️ |

---

## 11. Verdict

⚠️ **Ready with caveats.** The mechanics are grounded in code and the reuse path is
clear. The spike's six questions are answered. Before implementation starts:

1. **Child G is a hard blocker** (§5.0). The role the AWS-connect flow creates attaches
   only `ReadOnlyAccess`, which excludes `bedrock:InvokeModel`. Routing to it fails
   100% of calls. No amount of correct resolution fixes this; the template must gain a
   scoped Bedrock grant (in a *separate* routing role, not by widening the read-only
   one) before enforcement can be switched on for anyone.
2. **Ruling on §8.1** (fail closed vs. opt-in fallback) — gates child C.
3. **Ruling on §8.2** (user self-authoring) — gates child F.
4. **Ruling on §8.3** (deprecate `ADP_BEDROCK_VIA=user`) — gates child H.
5. **Child A must land first.** Without the path audit, routing coverage could be
   silently partial (§6.1 shows one bypass already exists), and without
   `bedrock_account_id` capture there is no shadow mode — the only structural
   prevention for the wrong-account bug.

Three findings should change how the operator reads the original issue:

- **The mapping ladder already exists** and should be reused, not rebuilt (§1.1). The
  issue's "likely a sibling table" would create a second, disagreeing answer to
  "which account for this person." Reuse also means **zero migrations** and a flag-flip
  rollback (§7.3).
- **The "existing linked-account machinery" is two different things** (§0.2): a live,
  ExternalId-proven credential ladder (`user_credentials` + `CredentialResolver` —
  reuse) and a dead round-robin throughput pool (`PoolService` — harvest its STS cache,
  discard its selector; its IAM grant even targets a role-name convention
  (`*BedrockGateway-Pool*`) that no existing connected account matches).
- **The destination roles cannot invoke Bedrock today** (§5.0) — the single largest gap
  between the issue's premise and the code, and the reason the effort is larger than
  "wire up a ladder" even though the ladder is free.

The budget question the issue flagged as load-bearing is the *least* risky part: §3
shows metering, pricing and attribution are untouched **by non-participation** — the
routing decision and the settlement path share no state. The one discipline that must
be enforced by review is §3.4: routing keys off `org_id` (authenticated), never
`attributed_org_id` (caller-influenced), or #4132 returns as a credential-acquisition
bug rather than an accounting one.
