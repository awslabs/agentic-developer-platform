# Bedrock account routing eval (capability + regression)

End-to-end evaluation of **Bedrock per-principal account routing** — Issue #4761
(R7), arc parent #4692 / EPIC #4324.

The routing arc (R1 #4742 … R6 #4747) ships with per-story unit tests, but
nothing proved the **capability** holds end to end against a live environment,
nor that the surfaces routing touches — budgets, metering, the core proxy — still
behave. This suite is both halves in a single run.

| | |
|---|---|
| Workflow | [`.github/workflows/eval-bedrock-routing.yml`](../../../.github/workflows/eval-bedrock-routing.yml) — `workflow_dispatch` only |
| Script | [`run-eval.sh`](run-eval.sh) — one function per phase |
| Harness tests | [`tests/test-run-eval-dry-run.sh`](tests/test-run-eval-dry-run.sh) — 17 groups, no AWS/cluster/network needed |
| Shared harness | [`../lib/`](../lib/) — shared with the budget/ratelimit and CLI-onboarding evals |

```bash
# Full run against dev
./platform/evals/bedrock-routing/run-eval.sh

# A subset, e.g. just the precheck and the authz matrix
./platform/evals/bedrock-routing/run-eval.sh --phases 0,1,8

# Prove the suite can go red (perturbs the world; real assertions must catch it)
./platform/evals/bedrock-routing/run-eval.sh --dry-run --inject-failure wrong-account

# Sweep anything a killed run left behind
./platform/evals/bedrock-routing/run-eval.sh --cleanup-only
```

Exit code is `0` only when **zero assertions failed**. Skips and findings are not
failures.

---

## The honesty contract

Read this before adding a case. Two failure modes are equally bad, and the phase
layout exists to avoid both:

1. **Reporting GREEN when a capability was never exercised.** A case that cannot
   run must SKIP **with its precise blocking reason**. It must never pass by
   default. A gate that degrades into a pass is the worst outcome for an eval,
   because it retires the question — someone reads green and stops asking.
2. **Reporting RED for something routing did not cause.** Fixture drift and an
   unwired environment are not routing regressions. A suite that cries wolf gets
   ignored, which costs the same as having no suite.

So the rule is: **fixture drift is a LOUD FAIL** (phase 0), while a
correctly-reported not-yet-wired environment is a **SKIP that carries its
reason**. That distinction is what makes a green run mean something.

The harness tests enforce this directly — test 4 asserts the gates skip rather
than pass and that no skip is a bare label, test 5 asserts drift fails loudly and
is labelled `FIXTURE DRIFT` so it is never mistaken for a routing regression, and
test 9 asserts a fatal setup error cannot print a green banner.

---

## Routing is active by default; inference acceptance still needs a runner

Saved, verified rules are always enforced. The former environment and
organization rollout flags are retired, including existing false values. There
is no SSM or organization opt-in step. See the current
[routing runbook](../../../docs/runbook-bedrock-routing.md).

Phases 5 and 6 still report **SKIP** because they do not yet execute a
fixture-owned inference runner, a controlled destination denial, or AWS account
landing evidence. Enabling an old flag cannot turn an unimplemented case into
validation. Complete those runners before treating this suite as proof of
cross-account invocation or fail-closed behavior.

### R5 and R6 are merged, and their cases run for real

They were unmerged when this suite was first drafted; both have since landed
(R5 #4746 as `169fe17`, R6 #4747 as `6ff19d8`) and phase 7 asserts them live.
Two things about their real shape differ from how they were described in advance,
and the assertions follow the **code**:

- **The pinned-row refusal is a `422`, not a `409`.** There is no 409 anywhere on
  this feature. `self_routes._rejected` deliberately mirrors R4's `routes._rejected`
  so both halves of the surface answer one error vocabulary
  (`{"detail":{"reason","message"}}`), because the client branches on `reason`.
  The reason code is **`pinned_by_platform_admin`**, and the guard applies to
  **PUT and DELETE alike** — an unguarded DELETE would make the PUT refusal
  bypassable by delete-then-reselect. This is the authority-escalation case: without
  it a user could silently un-pin an admin's mapping.
- **The self write names a `credential_id`, not a `destination_id`.** A person owns
  connections, not registry rows, and has no way to learn a destination id.

The self surface is exercised **as an ordinary member**, not as the admin: its
authz is the *shape* of the path (no target parameter at any position, anchor
derived from the token), so exercising it as a platform admin would prove nothing
about the member-callability that distinguishes it from R4's admin-only router.

---

## Fixture ownership contract

The suite depends on **standing fixtures it does not own and must not create**.
If one drifts, the suite fails loudly in phase 0 rather than reporting a false
routing regression.

| Fixture | Owner | If it drifts |
|---|---|---|
| The destination IAM role + its ExternalId condition | Platform operator | Phase 0 FAILs with `FIXTURE DRIFT` |
| Partial model enablement on the destination | Platform operator | Phase 0 FAILs (model EOL is detected explicitly) |
| Sandbox `938500344975` trusting the gateway account | **Human, via quick-create** | Phase 0 reports a finding; phase 6 skips |
| The designated test org + its member | Dev seed data | Phase 1 FAILs — mapping cases cannot author |

The sandbox trust relationship is a **setup precheck, never a skip that reads
green**. Per the #4748 ruling a human must re-quick-create the destination role
there with `GatewayAccountId=879318057152`.

---

## Verified live findings that shaped this file

Each is **re-derived at runtime by phase 0**, never trusted from this document.
They are recorded here because each one invalidates an assertion someone would
otherwise write in good faith.

1. **The original rollout gate is retired.** Saved routing rules are now active
   without an environment or organization opt-in. Phases 5/6 still need the
   inference and denial runners described above.
2. **The sandbox account is not reachable.** #4761 names `938500344975` as the
   routed destination with a "proven" role. Probed live: three candidate roles
   all return `AccessDenied` from the gateway account. Until a human re-creates
   it there is **no second account to land in**, so the "the bill really moved"
   evidence is *unavailable*, not merely unwritten.
3. **The real destination fixture lives in the platform account** —
   `arn:aws:iam::879318057152:role/ADP-Agent-dev-routing-validate`. Proven green:
   assume **with** ExternalId succeeds, **without** is denied (the condition is
   live), and a real `bedrock:InvokeModel` succeeds.
4. **Shadow mode populates, with a cutover to scope around.** For
   `usage_logs.bedrock_account_id` the last NULL row is `16:43:26Z` and the first
   filled row `16:46:18Z` on 2026-09-07; every hour since is 100% filled. All
   NULLs predate the R2 deploy, so a naive "never NULL" assertion would fail on
   pre-deploy history — the #4743 lesson. The suite asserts NULL-freedom only for
   rows **after** the observed cutover.
5. **Do not hardcode a model id.** `claude-3-5-haiku-20241022` and
   `claude-3-haiku-20240307` now return `ResourceNotFoundException` ("end of its
   life"). Phase 0 proves the model still invokes and **fails loudly if it EOLs**,
   so a dead model never reads as a routing regression.
6. **A presence probe with a wrong path manufactures a permanent false skip.** The
   first revision of this suite probed `GET /api/bedrock-routing/self` to decide
   whether R5 had shipped. That path has never existed at any commit — the real
   route is `/api/me/bedrock-routing/selection` — so the probe always 404'd,
   `R5_PRESENT` could only ever be false, and phase 7 skipped forever while
   announcing "R5 is not merged" about merged code. A skip that lies is the exact
   failure mode this suite exists to prevent, and it is *more* dangerous than a
   red run because it reads as diligence. The harness tests now assert the real
   path is used and that no skip claims R5/R6 are unmerged.
7. **Detect a retirement by its guard, not by the absence of a string.** R6 keeps
   `ADP_BEDROCK_VIA` (it still honours `gateway` and `direct`) and retires only the
   `user` *value*, so "no reference anywhere under `modules/agent-factory`" was
   never the post-merge state and reported a merged R6 as unmerged. The suite greps
   for the `RETIRED_BEDROCK_VIA` table and the `raise` that consumes it.
8. **`jq`'s `//` operator swallows `false`.** The shared `jqr` helper is
   `.field // empty`, so a field that is legitimately `false` is indistinguishable
   from one that is absent. Three of phase 7's assertions are about *presence* of
   booleans whose most interesting value is `false`
   (`own_selection_active`, `overrides_self_selection`, `pinned_by_platform_admin`),
   so they use `jq -e 'has("field")'`. Using `jqr` there would have failed a correct
   server on exactly the common state, and — worse — made "not pinned" and "the pin
   is no longer disclosed" look identical.
9. **`REPO_ROOT` is three levels up, not two.** This file lives at
   `platform/evals/bedrock-routing/`, so `../..` is `platform/`. The two-level form
   made every repo-tree lookup miss *silently* (`[ -f "$REPO_ROOT/modules/..." ]`
   is just false), which is how finding 7's detection failed even once its grep was
   correct. A path resolving to a real-but-wrong directory fails quietly.

---

## The phases

### Capability

| # | Phase | Asserts |
|---|---|---|
| 0 | Precheck | Caller account, gateway health, live configmap (shadow / platform account), the destination fixture triple-check (assume **with** ExternalId, **denied without**, real `InvokeModel` with EOL detection), sandbox trust, R5/R6 presence. **Fixture drift FAILs here.** |
| 1 | Resolve the world | The designated test org, a member principal, a destination owned by that org, and a **foreign-org** destination as the tenant-isolation fixture |
| 2 | Shadow baseline | An unmapped call is attributed to the **platform** account; no NULL `bedrock_account_id` after the R2 cutover |
| 3 | Destination registry | The gateway itself proves the destination is `verified` and `routing_capable` |
| 4 | Mapping ladder | Authoring at the org rung makes effective resolution **name that rung**; removing it falls back to platform (**rollback works**) |
| 5 | Enforcement success | *Not yet implemented:* fixture-owned invocation and destination-account evidence |
| 6 | Cross-account landing | *Not yet implemented:* controlled denial and proof that the invocation lands in the second account |
| 7 | Self-service (R5) + R6 | The self surface is **member-callable**, states `own_selection_active` and `pinned_by_platform_admin`, the effective read discloses `overrides_self_selection`, a pinned member's PUT **and** DELETE are refused **422 `pinned_by_platform_admin`** (the authority-escalation case), an injected `user_id` is not honoured, and `ADP_BEDROCK_VIA=user` fails loudly while `=gateway` survives |
| 8 | Authz + tenant isolation | The strongest runnable cases — see below |

### Regression (runs every time)

| # | Phase | Asserts |
|---|---|---|
| 9 | Core proxy | `/api/health` 200, a real model call, streaming SSE frames, and the **GitHub login canary** (`/auth/github` — the broker route, *not* under `/api`) |
| 10 | Metering + attribution parity | Routed and platform rows carry the identical field set; an `attributed_org_id` header **cannot** change the resolved rung or the enforced budget rung |
| 11 | Budget | The person-default surface still reads, and the `/budget` surface stays **read-only** — no self-write route is reachable |
| 12 | Latency guardrail | Control-endpoint p50 within tolerance of the in-AWS baseline, measured from a **stable vantage, not a laptop** (the #4743 lesson) |
| 13 | Gate-script orchestration | Runs the two proven gate scripts and folds their verdicts in: `bedrock-routing-validate.sh --check destination` and `--check authz`, plus `validate-bedrock-routing-shadow.sh --baseline-p50`. **Reuse, not re-implementation** — see below |

### Why phase 13 runs the gate scripts instead of re-implementing them

`platform/scripts/bedrock-routing-validate.sh` (R4's ops gate) and
`platform/scripts/validate-bedrock-routing-shadow.sh` (R2's shadow gate) already
encode checks this arc depends on, and both are maintained against real dev runs.
Copying their logic here would create exactly the **drift** that
`bedrock-routing-validate.sh`'s own header warns about: two copies of one decision
table, where a fix to either is invisible to the other.

So this phase *invokes* them and folds their exit codes into this suite's tally.
That has a second benefit — the eval becomes a regression test **for the gates
themselves**. If a future change breaks one, this suite goes red rather than
quietly ceasing to cover what the gate used to check. Their stdout is captured to
the run workdir (`gate-destination.log`, `gate-authz.log`, `gate-shadow.log`)
rather than dumped, because each prints a full report and interleaving them would
bury the tally.

Passing `--baseline-p50` is what turns the shadow gate's latency section from a
report into an assertion; it already implements the #4743 cutoff and vantage
lessons directly.

### Why phase 8 is the strongest evidence today

With the inference phases still unimplemented, the authz matrix is this suite's
strongest live evidence. It is built to avoid a specific trap.

**A plain member is denied by *any* authz check, so a member-only 403 proves
nothing.** The load-bearing assertion uses a **real `org_admin`** (minted from the
dev pentest-actor Lambda, because a hand-rolled token carrying
`custom:role=org_admin` *is a member* — authority is DB-resolved from
`tenant_memberships`) and qualifies it with a **positive control**: that identity
must first get a 2xx on its **own** org. Only then does its 403 on the routing
route count. This mistake shipped once (#4794 fixed it); the harness tests assert
the control **precedes** the denial so it cannot regress.

The four 422 refusals — foreign-org destination, malformed scope, nonexistent
scope, unknown destination — each check the status *and* the **load-bearing
half: that nothing was stored**, by re-reading effective resolution afterwards.
The refusal message must name the **scope's own org**, never the destination's
tenant, or it becomes a cross-tenant enumeration oracle.

---

## Safety: the scope guard

Every write is guarded by `assert_test_scope`, which **dies** unless the scope
names the designated test tenant. A Bedrock routing rule decides **whose AWS bill
pays** for inference, so an unguarded write against shared dev would silently
redirect a real tenant's spend.

**Why an allowlist and not a run tag.** The budget/ratelimit eval creates its own
throwaway orgs, so it can require every write to carry `eval-bgt-<run_id>`.
Routing rules only bind to orgs/teams/users that **already exist** — authoring
against a nonexistent scope returns 422 `scope_not_found` — so this suite must
write against a **pre-existing** designated test org. A tag-substring check could
never match and would reject every legitimate write.

The guard runs **before** the mutating call, and the created scope is recorded in
state **before** the write, so cleanup finds the rule even if the process dies
mid-write. The harness tests assert both orderings structurally.

## Idempotence and cleanup

The only rows this suite creates are routing mappings. Cleanup runs from an EXIT
trap, **verifies zero mappings remain** on the test org rather than assuming it,
and is available standalone via `--cleanup-only`. Re-authoring the same rule is
idempotent server-side (the PUT replaces in place) and DELETE is 204 whether or
not a row existed, so re-running is safe. The workflow also runs an
**unconditional** `--cleanup-only` sweep, because a cancelled job never fires its
trap and a leaked rule would keep rerouting spend long after the run ended.

## Non-goals (hard)

Saved rules affect routing automatically; inference cases must use
the designated test org only. **An eval must never widen a production flag to
make itself pass.** No load testing, no prod runs, no UI/visual assertions — this
suite asserts at the API level.

## Secrets

The destination ExternalId is read from SSM SecureString at runtime and is never
echoed, never passed on argv, never written to a file. Bearer tokens reach `curl`
through a **config file**, because argv is readable via `/proc` on a shared host.
Harness test 14 asserts no secret material reaches stdout or the job summary.

## Traps worth knowing

- **`aws cloudtrail lookup-events` is unusable here** — it caps around 50 events
  per page, throttles, and dev KEDA volume pushes the events of interest out of
  the window. Read trail objects from **S3** instead.
- **On a DENIED AssumeRole, `requestParameters` is null and `roleArn` is `None`.**
  Assert on app-side structured fields, not on trail request params.
- **`api_status` and `api_body` fire one request each.** Call `api()` once and
  read `API_STATUS`/`API_BODY` when you need the pair — otherwise a mutating PUT
  is attempted twice and a status can be paired with a different response's body.
- **An unreadable read is inconclusive, not evidence.** Phase 8 distinguishes
  "the effective read failed" from "a rejected rule persisted"; conflating them
  sends someone hunting a persistence bug that does not exist.
