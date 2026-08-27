# Budget + rate-limit eval (clean room)

Deep, end-to-end evaluation of **budget enforcement** and **rate limiting** —
Issue #4163.

It seeds a throwaway org structure in dev, defines budget and rate-limit configs
at **every entity level** (user / team / department / org) **through the real
admin APIs**, then drives traffic from a clean-room "laptop" pod through **both
wire paths** and asserts enforcement, error shapes, accounting and tenant
isolation.

| | |
|---|---|
| Workflow | [`.github/workflows/eval-budget-ratelimit.yml`](../../../.github/workflows/eval-budget-ratelimit.yml) — `workflow_dispatch` only |
| Script | [`run-eval.sh`](run-eval.sh) — one function per case |
| Harness tests | [`tests/test-run-eval-dry-run.sh`](tests/test-run-eval-dry-run.sh) — 19 groups, no AWS needed |
| Shared harness | [`../lib/`](../lib/) — the clean-room boundary, shared with the CLI-onboarding eval |

The two wire paths are the ones real users are on:

- **Claude Code** — `POST /v1/messages`, Anthropic message format
- **Codex** — `POST /openai/v1/responses`

Every enforcement case drives both. A limit that holds on one wire and leaks on
the other is exactly the kind of gap this eval exists to catch, so a case that
stopped driving a wire is itself a test failure (harness test 15).

## Why "clean room" is in the name

The eval must not run in the agent-worker image, and this is a stronger
requirement here than it is for the CLI-onboarding eval.

That image bakes in a sigv4-proxy on `127.0.0.1:9090` and `ANTHROPIC_*` env. A
request that leaves through it authenticates as an **agent**, not as the seeded
human — so it is enforced against a **different budget entity entirely**. Every
cascading-cap case would return 200, the eval would report green, and it would
have proven nothing at all. False green is worse than no eval.

So the run splits in two:

- **HARNESS** — the eval process, on the ARC runner. Holds IRSA and kubeconfig.
  Resolves SSM, seeds Cognito, writes the admin-API config, reads Postgres and
  DynamoDB, and makes every assertion.
- **LAPTOP** — a pod created from stock `node:20-bookworm` with
  `automountServiceAccountToken: false`, `enableServiceLinks: false` and no
  environment, reached **only** by `kubectl exec`. All traffic under test
  originates there.

Three mechanisms enforce the boundary, and none of them is a promise in a doc:

1. **There is no credential to steal.** No service-account token, no env, no
   volumes.
2. **`--assert-clean-room` is the first command exec'd in the pod** — and it runs
   *before the first admin write*, so a contaminated run aborts in seconds having
   changed nothing in dev.
3. **`laptop()` is the only wrapper that can reach the pod**, and it can only
   reach the pod. The functions that see credentials (`h_aws`, `h_kubectl`,
   `h_psql`) have no path into it.

Secrets travel to the pod on **stdin only, never argv** — anything in an exec'd
command line is visible in the exec API and in the runner's own process table.

The boundary lives in [`../lib/pod.sh`](../lib/pod.sh) and is **shared** with the
CLI-onboarding eval rather than copied, so a hardening fix lands in both at once.
Harness test 19 asserts this eval does not redefine `laptop()`, `h_kubectl()` or
`assert_clean_room()` locally.

## The seeded world

All throwaway, all name-tagged `eval-bgt-<run_id>`:

```
Org A ─ department D1 ─ team T1 ─ u1, u2
      │                └ team T2 ─ u3
      └ org-admin a1                      (performs every admin-API write)
Org B ─ x1                                (isolation control)
```

Two details are load-bearing:

- **Org ids need no `organizations` row.** `budget_configs`,
  `rate_limit_configs` and `budget_usage` carry `org_id` as an indexed column
  with **no foreign key**, so a synthetic org can hold config without touching
  the tenant tables.
- **The admin identity does need real DB rows.** Authority is DB-resolved, not
  claim-derived (#3987): a caller with no `tenant_memberships` row defaults to
  `MEMBER` and has neither `budget:update` nor `ratelimit:update`. So `a1` gets
  an `organizations` row, a `users` row and an active `org_admin`
  `tenant_memberships` row.
- **Department is claim-only.** `users` has no `department_id` column at all, so
  the Cognito `custom:department_id` claim (copied into the *access* token by the
  pre-token-generation Lambda) is the only way to exercise case 3.

`a1`'s token stays on the **harness** side. The admin writes are harness actions,
and an org-admin token inside the clean room would let the emulated laptop raise
its own cap — which harness test 9 asserts never happens.

## The numbers that make it deterministic and nearly free

Two pieces of arithmetic decide the whole design.

**Budgets deny on an estimate, before spending anything.** Enforcement compares
`current_spend + $0.05` (a flat `_DEFAULT_ESTIMATE_USD`) against the cap. So a
cap of **$0.01 denies on the first request, at zero spend** — no token burn, no
waiting on the async S3→Lambda ledger. That is what makes cases 1–5 instant and
deterministic, and it confines the ledger to case 7, where the ledger *is* the
subject. `TRIP_CAP` must stay below `$0.05` for this to hold.

**Rate limits trip at burst capacity, not at the nominal limit.**

```
max_tokens = max(1, int(rpm × burst_multiplier(1.5) / 60 × refill_buffer(10)))
```

At the default 60 rpm that is **15** tokens — so "the 61st request 429s" is
simply false. `rpm=1` gives capacity 1, the only setting that makes a 429
reachable in a bounded number of requests.

**And there are eight buckets, not one.** Dev runs the in-memory limiter backend
with `replicas: 2 × --workers 4`, behind an ALB. Each worker has its own bucket
and its own clock. The eval therefore asserts **"at least one 429 within N
requests"** and never "request number K is the one that 429s" — the ordinal is
not a property of the system. `N` defaults to 40, comfortably above 8 × capacity.

**Config changes are invisible for up to 60s.** The limiter reloads from the DB
at most once per `_DB_RELOAD_INTERVAL`, per worker. There is no endpoint that
reports the *enforcing* worker's view, so polling cannot help: the eval waits the
interval out (`RL_RELOAD_WAIT`, default 70s). This is why the workflow's timeout
is 60 minutes rather than 45.

## What it covers

### Budget cases — assert `HTTP 402` and `details.entity_type`

| # | Case | Asserts |
|---|------|---------|
| 1 | user cap | 402, `entity_type == "user"` |
| 2 | team cap | 402, `entity_type == "team"` |
| 3 | department cap | 402, `entity_type == "department"` |
| 4 | org cap + Org-B isolation | 402, `entity_type == "org"`; Org B's user is unaffected — plus a **finding** |
| 5 | precedence | with every level over cap, the **most specific** exceeded level is the one reported |
| 6 | no-config baseline | an unconfigured hierarchy returns 200 — enforcement must not deny by default |
| 7 | accounting integrity | after a billable request, `budget_usage` has rows keyed to the right entity, for all three period types, and Org A's spend does not appear under Org B |

Cases 2–5 give the levels *not* under test an **open** cap ($1000) rather than
leaving them unset. That matters: it proves the cascade *reaches* the level being
tested having already found a satisfied one above it, instead of only proving
that an unset level is skipped.

### Rate-limit cases — assert `HTTP 429` and the documented body

```json
{"error": "rate_limited",
 "details": {"limit_type": "...", "limit": N, "remaining": N, "reset_seconds": N}}
```

Note `reset_seconds`, **not** `retry_after_seconds` — the other spelling lives in
a `ratelimit/middleware.py` that is never mounted. All four `details` keys are
checked for **presence**, not truthiness, because `remaining: 0` is the normal
value in a 429 and must not read as missing.

| # | Case | Asserts |
|---|------|---------|
| 8 | user RPM | 429, `limit_type == "rpm"`, full documented shape |
| 9 | user TPM | **skipped, with a finding** — not reachable end-to-end |
| 10 | concurrent = 1 | 429, `limit_type == "concurrent"`, from genuinely overlapping in-flight requests |
| 11 | org RPM shared bucket | **finding** — an org-level limit can never match; passes loudly if fixed |
| 12 | defaults | with no config at any level, the defaults (60 rpm / 100000 tpm / 10 concurrent) admit normal traffic |

Case 10 backgrounds its requests. Sequential calls each release their slot in the
middleware's `finally`, so they can never collide — a sequential "concurrency
test" would be testing nothing.

Case 12 asserts only that defaults do **not** deny normal traffic. Proving a
default eventually 429s would need 120+ real inference requests; that is a load
test, and load-testing the limiter backends is an explicit non-goal.

### Phase H — the headline question

> Does human-triggered agent spend land under the triggering human's budget?

Answered **observationally**, from lineage that already exists in dev: H1 finds a
human-rooted agent run in the `webhook-events` table, H2 joins it to `usage_logs`
via `agent_run_id` (`event_id == ADP_MESSAGE_ID == x-agent-runid ==
usage_logs.agent_run_id`), H3 reads who was actually billed, and H4 checks
whether the human's ledger moved. No agent is dispatched and no attribution is
fabricated.

**The answer today: it does not.** Not even partially. See the findings below.

H3 is written so that if attribution is ever implemented, it reports that as the
notable event it would be rather than silently continuing to pass.

## The four findings — read this before reading the results

Four things in this area **cannot honestly pass**, and were verified in the code
before the eval was written. Each is reported as a `FINDING` — a pinned
observation of current behaviour — rather than a fake pass or a flaky red. A
finding does **not** fail the run, and the summary renders them in their own
block so nobody mistakes one for a passing assertion.

**1. Org budgets enforce on the estimate but never on accumulated spend.**
The tracker Lambda writes `budget_usage` rows with `entity_type='organization'`;
enforcement reads `EntityType.ORGANIZATION`, whose value is `'org'`. Org usage
therefore never joins its own config. Case 4's *estimate* half passes; its
accumulated-spend half cannot.

**2. TPM rate limiting is effectively unenforceable.**
`consume_rate_limit(context, tokens=1)` is the only call site, and it never
passes a real token count — so the TPM bucket is debited **one token per
request**, regardless of how many tokens the request actually used. A TPM limit
behaves as a second, much larger RPM limit. Setting `tpm=1` to "prove" a 429
would be dishonest: it would 429 because one request debits one token, not
because token accounting works. Case 9 skips and says so.

**3. An org-level rate limit can never be enforced.**
`POST /admin/organizations/{org}/ratelimits` accepts `entity_type='org'` and
stores it verbatim; the limiter loads rows by that raw string but looks them up
as `EntityType.ORGANIZATION == 'organization'`. The keys never match, so an org
that has been given a rate limit silently keeps the default 60 rpm. Case 11
reports the finding — and passes loudly if the spelling is ever fixed.

**4. Human-triggered agent spend does not land under the triggering human's
budget.** An agent request is billed as `('user', <agent registry name>)`:
`auth/agent_registry.py` builds the agent's `TokenContext` with `user_id` set to
the agent's registry name and `account_type='service'`. `root_human_id` is
captured in the `webhook-events` item, the `root-human-index` GSI, the worker's
`ADP_ROOT_HUMAN_ID` env and the write-only `action_provenance` table — but is
absent from `chat_logging/**`, `usage/service.py`, the tracker Lambda and
`budget/**`, and no migration through 027 adds it to `usage_logs` or
`budget_usage`.

**Consequence: a per-user budget places no bound on what that user's agents can
spend.** Nothing in the codebase claims it *should*, so the follow-up is a
feature request, not a bug. Implementation is out of scope for #4163.

Phase H also pins two adjacent defects: the `('agent', agent_id)` branch in the
tracker Lambda is dead code (it reads a `ChatLog` field that does not exist), and
agent budget *enforcement* checks `service_account` while agent budget
*accounting* writes `user` — so an agent's accumulated usage is never compared
against a `service_account` budget config.

## Running it

The live run is `workflow_dispatch` only. Unlike the CLI-onboarding eval there is
**no cron**: this eval writes spend controls in a shared account, and a nightly
that mutates them unattended is the wrong default. Harness test 5 asserts no
`schedule:` reappears.

```bash
# Full run (needs IRSA + kubeconfig for the target env)
gh workflow run eval-budget-ratelimit.yml -f environment=dev

# A subset of cases
gh workflow run eval-budget-ratelimit.yml -f phases=1,2,3,4,5

# Prove the eval still fails when the thing it tests is broken
gh workflow run eval-budget-ratelimit.yml -f phases=1,8 -f inject_failure=wrong-entity
```

Locally, against a real dev account:

```bash
./platform/evals/budget-ratelimit/run-eval.sh --environment dev
./platform/evals/budget-ratelimit/run-eval.sh --phases 1,2,3
./platform/evals/budget-ratelimit/run-eval.sh --cleanup-only
```

With no AWS, no cluster and no network:

```bash
./platform/evals/budget-ratelimit/tests/test-run-eval-dry-run.sh
```

`--inject-failure wrong-entity` is the **acceptance check on the eval itself**:
it flips cases 1 and 8 to assert the wrong `entity_type`/`limit_type`, and the
run must go red. The dry-run stubs make this meaningful — they *derive* each
verdict by re-implementing the real cascade and the real bucket arithmetic from
the config the eval wrote, rather than being handed the expected answer per case.
A stub told the answer could not fail, and the acceptance check would be
worthless.

## What it changes in the target environment, and how it cleans up

Writes, all tagged `eval-bgt-<run_id>`:

- 5 Cognito users (`u1`, `u2`, `u3`, `a1`, `x1`) with tenant claims
- 1 `organizations` row, 1 `users` row, 1 `tenant_memberships` row (for `a1`)
- budget configs and rate-limit configs at user / team / department / org level
- `budget_usage` rows, from case 7's one billable request
- 1 clean-room pod in `adp-gateway`

**Every admin write asserts the tag first.** `assert_tagged()` runs immediately
before each one and **dies** rather than recording a failure and continuing — an
untagged write would mutate a real tenant's spend controls in a shared account,
so there is no "continue and report it" option. This is the property harness test
2 exercises most directly, and it is the most dangerous thing in the file to get
wrong.

Cleanup runs from an `EXIT` trap, so a crashed run and a deliberate teardown take
the same path. It deletes configs **before** identities (the DELETEs authenticate
as `a1`, so removing that identity first would strand everything it created),
then sweeps tag-scoped rows from `budget_usage`, `budget_configs`,
`rate_limit_configs` and `organizations`, then deletes the pod **by label** — so
a pod orphaned by a crashed run is swept by a later run that never learned the
old run id.

### If a run is killed mid-flight

The trap does not fire if the runner is killed. The workflow's `if: always()`
sweep covers that, but to do it by hand:

```bash
./platform/evals/budget-ratelimit/run-eval.sh --environment dev --cleanup-only
```

It is standalone and idempotent — safe to run twice, and safe to run when there
is nothing to clean up. Leaked clean-room pods (`--cleanup-only` already does
this):

```bash
kubectl delete pod -n adp-gateway -l app=eval-bgt
```

## Triaging a failure

The job summary renders one row per assertion, plus a separate findings block.

- **A case fails with `expected 402, got 200`** — enforcement is not seeing the
  config. For a *budget* case, check the cap is below `$0.05` (the flat estimate)
  and that the entity id matches what the token actually carries. For a
  *rate-limit* case, the config-reload interval is the usual cause; the eval
  waits 70s, but a worker that just restarted starts its own clock.
- **A case fails with the wrong `entity_type`** — the cascade resolved a
  different level than expected. Usually the token is missing a claim: department
  is claim-only, so a missing `custom:department_id` makes case 3 resolve to org.
- **`no 429 within 40 requests at rpm=1`** — enforcement is not applying the
  config at all. With 8 buckets of capacity 1, 40 requests is ample.
- **Setup dies at `--assert-clean-room`** — the pod is contaminated. Read the
  violations: this is the check working, not a bug. Nothing was written to dev.
- **Setup dies minting an RDS IAM auth token** — the workflow preflights this
  precisely so you find out *before* anything is seeded. Check `rds-db:connect`
  on the runner boundary.
- **A finding appears where a pass used to be** — read it. It is a pinned
  statement of current behaviour with a code citation, and it may be telling you
  something regressed into one of the four known shapes above.

## Known operational prerequisites

- **The gateway RDS master user is IAM-auth-only** (`BG_RDS_IAM_AUTH=true`,
  password auth disabled). The harness authenticates with
  `aws rds generate-db-auth-token` + `PGSSLMODE=require`, and the runner boundary
  must allow `rds-db:connect`. The master-password pattern is gone; do not
  resurrect it.
- **The runner needs `pods create/delete` and `pods/exec create`** in
  `adp-gateway`, which its existing RBAC already grants.
- **The Cognito app client must allow `USER_PASSWORD_AUTH`** — that is how
  credential-free identities authenticate from inside the clean room
  (`InitiateAuth` is an unsigned API, which is exactly why a pod with no
  credentials can call it).

## Non-goals

Per #4163, and deliberate:

- **Fixing any gap this finds.** Each becomes its own issue. This eval's job is
  to state what is true today, precisely, with citations.
- **Period rollover / reset simulation.** Would need either clock control or a
  multi-day run.
- **Load or performance testing of the limiter backends.** Case 12 stops at "the
  defaults admit normal traffic" for exactly this reason.
- **Production runs.** Dev only.
