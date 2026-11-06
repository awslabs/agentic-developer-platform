# Implementation backlog and acceptance map

This is the actionable backlog for [the proposed design](design.md), not a new
issue tracker or a dispatch instruction. Existing issue bodies own acceptance.
The table preserves every existing child AC/SCN and epic AC-01/SCN-01; additions
are explicitly **proposed** for approval, never replacements for existing IDs.
All implementation and live checks below are **not run** by this design PR.

## Work packages and dependencies

| Existing work item | Concrete implementation package | Owner and release boundary | Dependency / first runnable checkpoint |
|---|---|---|---|
| [7559](https://github.com/aws-e/adp/issues/7559) | Attested startup classification; action-based review attempt exemption; durable backoff/streak/block; resume reset; progress projection | Engine developer, worker owner only for new report categories; gateway then optional worker | Independent. First checkpoint: real action history counts a verified bootstrap failure as exempt while an ambiguous failure still counts. |
| [7563](https://github.com/aws-e/adp/issues/7563) | Early protected startup deadline; conditional old-attempt fence; trusted stall decision; one bounded developer retry; visible repeated-stall block | Engine developer; gateway/tick | Independent of 7559. First checkpoint: concurrent late startup cannot acquire authority after fence. |
| [7571](https://github.com/aws-e/adp/issues/7571) | Receipt-before-terminal-response barrier across model routes; drain registry; idempotent accounting replay; shared-proxy budget wait with cancellation and authority expiry | Gateway and worker developers; gateway first, worker second | Reuse 7451/7567 already in source. First checkpoint: forcibly terminated proxy cannot produce successful completion without a durable receipt. |
| [7382](https://github.com/aws-e/adp/issues/7382) | Narrow supplement lineage; durable disposition snapshot; amendment preview/response; CLI/UI warning; four supplement readers and concurrency tests | Engine developer plus UI/CLI owner; gateway/tick then consumers | Confirm carry rule in design review. First checkpoint: content-only amendment preserves independently accepted budget and retry values. |
| [7381](https://github.com/aws-e/adp/issues/7381) | Reproduce uncovered human/default cases; gate-first routing; evidence-bound approval; eval refusal projection and UI | Engine/UI developer; gateway/tick and frontend | Owner confirms gate-first. First checkpoint: default human eval reaches a gate in one tick with no worker dispatch. |
| [7385](https://github.com/aws-e/adp/issues/7385) | Correlated annotations/job lookup; token-safe bounded downloader; redaction; visible unavailable reason; evidence-token permission check | Engine developer; connection owner for missing permission; gateway | Independent; helps 7457 repair but not required for its unit development. First checkpoint: non-Python failing job yields usable redacted detail. |
| [7358](https://github.com/aws-e/adp/issues/7358) | Add archive column; serialized mutation and admission guard; list/count parity; graph/list UI and CLI; migration/rollback documentation | Engine/UI/CLI developer; migration before gateway/tick and frontend | Independent; integrate all retry/deploy/eval admissions before release. First checkpoint: concurrent archive/dispatch admits no hidden work. |
| [7457](https://github.com/aws-e/adp/issues/7457) | Dual manifest reader; repository-at-merge loading; audited component mappings; explicit connection authority; derived revision snapshots; exact target lease; developer repair fallback | Engine developer, platform owner approves permission model; gateway/tick then v2 proposals/activation | Permission decisions before implementation; live tools case after 7513. First checkpoint: v2 reviewed fixture binds exact merge/connection without dispatch on an empty legacy grant. |
| [7513](https://github.com/aws-e/adp/issues/7513) | Bootstrap/release state ownership split; narrow release IAM/RBAC; interceptor foundation; missing trust admission; environment setup runbook; phased release verification | Platform developer; platform/automation/environment owners apply only when authorized | Direct push path independent of 7457. First checkpoint: Terraform/mock policy and negative RBAC tests prove no IAM policy mutation or other-workload access. |
| [7572](https://github.com/aws-e/adp/issues/7572) | Integrate all child evidence; versioned live-window procedure and intervention ledger | Delivery coordinator and operator; no engine behavior added by epic | All child live criteria before starting final 24-hour window. |

Do not split 7457 into an unbounded replacement deployment engine. Its existing
leases, action scheduler, workflow correlation, verification adapters and
credential resolver remain the integration interfaces. Inventory enumeration is
already delivered in this PR; implementation consumes it and closes mapping
defects, rather than being assigned the audit anew.

### Shared-file integration order

- 7559 changes review-stage accounting; 7457 repair fallback must consume that
  same counter. Merge the accounting change first or rebase the fallback before
  release. 7563 does not refund the first developer attempt.
- 7382 receipt carry must be exercised with 7559/7563/7457 authority readers;
  version comparison remains the common lineage helper. It must not turn a
  content amendment into new deployment permission.
- 7358 guards the flow lock around admissions and unresolved actions. Rebase its
  tests against all new recovery paths; no partial rollout may let an archived
  flow dispatch through an older consumer.
- Schema changes have one migration owner at merge. Enumerate existing parents
  from `gateway-migrations.tsv`, rebase on current main and verify one head. Do
  not assign a guessed numeric migration filename now.
- 7513 platform bootstrap precedes its three live pipelines. 7457 may be coded
  and tested against synthetic verified connections while infrastructure setup
  is pending; those fixtures do not satisfy live acceptance.

## Deterministic test contract

Use isolated real PostgreSQL transactions for locks/uniqueness/lineage/races and
real Redis for reservation Lua semantics. SQLite/helper-only tests cannot prove
these claims. Give each test a synthetic tenant, flow, node, request, invocation
and operation identity; clean up its rows/keys in fixture teardown. Use frozen
UTC clocks and seeded jitter, fixed plan/PR hashes and controlled provider
responses. Drive actual routes/tick/worker transport, then assert persisted
decisions, exact dispatch counts, independently queried receipts and observed
states—not only a helper's returned object. No paid model/provider call is needed
for pre-review tests. Secrets are fake sentinels; verify they never reach response,
artifact or captured logs.

Fault barriers control “before reservation,” “after fence,” “after dispatch but
before acknowledgement,” “after provider terminal but before receipt commit” and
“after commit but before completion delivery.” Restart consumers against the same
durable fixture. Include duplicate delivery, lock contention, tenant mismatch,
plan amendment, expired/revoked authority, exhausted budget/attempts and stale PR
heads. A test failure must identify an observable wrong outcome. Do not accept a
skipped database, worker-image or infrastructure test as PASS.

## Existing acceptance and scenario mapping

Paths below are repository-relative. Existing files are test extension points;
new selectors/files are marked planned. Live artifacts stay private with a safe
summary and revision/PR link in the issue. Each AC's state is **not-run** here.

| Issue / stable IDs | Independent action and assertion | Test reference / owner / phase |
|---|---|---|
| 7559 AC-01 / SCN-01 | Observe a trusted bootstrap-only reviewer failure; independently count charged review actions and node attempts before/after; no charge change, exactly one due retry | Extend existing `modules/gateway/tests/orchestration/test_review_cycle.py`; planned bootstrap/dispatch selectors (the issue's `test_review_cycle_dispatch.py` is not present); developer, pre-review |
| 7559 AC-02 / SCN-02 | Process three startup failures under a frozen clock; fourth queue send count stays zero, durable block includes sanitized cause | Same file plus review-cycle tests; developer, pre-review |
| 7559 AC-03 / SCN-03 | Resume-continuation twice using the accepted snapshot; one reset decision and one new dispatch, old model-executed charges unchanged | Same file plus recovery-route integration; developer, pre-review |
| 7563 AC-01 / SCN-01 | Stall exact unbound assignment, fence it, execute recovery/tick; node becomes ready, next actual dispatch increments attempt once and records stall_retry | `modules/gateway/tests/orchestration/test_developer_recovery.py`, planned selector; developer, pre-review |
| 7563 AC-02 / SCN-02 | Stall replacement too; no third dispatch, typed operator block says worker vanished twice | Same file; developer, pre-review |
| 7563 AC-03 / SCN-03 | At 1199s no early failure; at configured 1200s eligible unacknowledged attempt is fenced/failed, without waiting normal threshold | `modules/gateway/tests/orchestration/test_stall.py`, planned selector; developer, pre-review |
| 7571 AC-01 / SCN-01 | Finish provider stream, trigger lifespan shutdown while settlement awaits; client success is ordered after durable receipt, exit drains or withholds success | Planned `modules/gateway/tests/proxy/test_stream_settlement_on_shutdown.py`; gateway developer, pre-review |
| 7571 AC-02 / SCN-02 | Installed worker receives exact budget 429, then success; clock-controlled retries remain alive within cap, no second model invocation before admission | Extend `modules/agent-factory/agent/src/sigv4-proxy.lifecycle.test.ts` and worker integration; worker developer, pre-review |
| 7382 AC-01 / SCN-01 | Accept fictional run cap 75 and attempts 5, amend node content; independent budget/retry previews at new version still show 75 and 5 | `modules/gateway/tests/orchestration/test_shared_budget.py`, `test_shared_retry.py`, `test_amend.py`; planned selectors; developer, pre-review |
| 7382 AC-02 / SCN-02 | Lower a base limit or change repository scope; inspect persisted amendment decision and response: same dropped receipt, original value, new effective value and cause | `modules/gateway/tests/orchestration/test_amend.py`; developer, pre-review |
| 7382 AC-03 / SCN-03 | Authorized live compatible/incompatible amendments; response, readback and CLI/UI agree with accepted supplements/dispositions | [Live procedure](#live-acceptance-procedure), flow owner, post-deploy |
| 7381 AC-01, AC-02 / SCN-01 | Ready human evaluation with satisfied declared prerequisites: one tick gives awaiting_gate and zero worker submissions; authorized approval gives passed and releases successors once | `modules/gateway/tests/orchestration/test_evaluation_controller.py`, tick and gate-route tests; developer, pre-review |
| 7381 AC-03 / SCN-02 | Force typed eval admission refusal; graph API and NodeChip show it rather than queued | `modules/gateway/tests/orchestration/test_delivery_progress.py`, `modules/gateway/frontend/src/__tests__/components/orchestration/NodeChip.test.tsx`; developer, pre-review |
| 7381 AC-04 / SCN-03 | Human evaluation on authorized fixture reaches gate within one observed tick interval; capture revision/evidence links and UI state | Live procedure, flow owner, post-deploy |
| 7385 AC-01 / SCN-01 | Empty check output plus annotation/failed step/arbitrary non-Python error: reviewer details contain that evidence bound to exact head/job | `modules/gateway/tests/orchestration/test_reviewer_checks.py`, planned selector; developer, pre-review |
| 7385 AC-02 / SCN-02 | Inject credential sentinels and large logs; output <=4000 characters/failure, <=4 failures, redacted; redirect receives no auth header | Same file plus bounded downloader tests; developer, pre-review |
| 7385 AC-03 / SCN-03 | Live failed-shard PR review identifies actual failing test using supplied evidence, without operator log copying | Live procedure, flow owner, post-deploy |
| 7358 AC-01, AC-07 / SCN-01 | Archive inactive/rejected flow, then unarchive; two transition decisions total, timestamp/status round-trip and pause remains unchanged | Planned `modules/gateway/tests/orchestration/test_flow_archive.py`; developer, pre-review |
| 7358 AC-02 / SCN-02 | Mixed archived/active fixture; default/include/status-only queries and pagination yield exact row sets; attention/stall chips exclude archives | Same file and existing display/list tests; developer, pre-review |
| 7358 AC-03, AC-04 / SCN-03 | Active-work archive returns 409 without writes; USAGE_READ-only mutation returns 403 while list remains readable | Same file, route-level PostgreSQL fixture; developer, pre-review |
| 7358 AC-05 / SCN-04 | Snapshot usage/cost/lineage, archive and read detail/cost; all evidence/amounts retained, slug still unavailable | Same file plus registration regression; developer, pre-review |
| 7358 AC-06 / SCN-05 | Authorized owner archives private inactive fixture through graph UI; default list/CLI omit, archive filter/include flag restores visibility | Live procedure, flow owner, post-deploy; use authorized fixture, not copied historical IDs |
| 7457 AC-01 / proposed SCN-01 | Reviewed PR adds workflow+manifest and code; accepted target authority; engine after merge dispatches exact reviewed revision and records independent runtime proof with no SHA edit | End-to-end fixture, platform owner, post-deploy; pre-review provider fixture is not this pass |
| 7457 AC-02 / proposed SCN-02 | Change unmapped path; no dispatch, typed component-unmapped block; reviewed mapping amendment resolves it without Python edits | `modules/gateway/tests/orchestration/test_deployment_component_paths.py`, deployment workflow tests; developer, pre-review |
| 7457 AC-03 / proposed SCN-03 | Reviewer repair leaves red checks; with remaining repair allowance exactly one developer repair dispatch; exhaustion blocks and changed head invalidates prior review | `modules/gateway/tests/orchestration/test_review_cycle.py`, dispatch tests; developer, pre-review |
| 7457 AC-04 / proposed SCN-04 | Accepted deploy human gate remains unanswered; zero dispatch until matching authorized approval | Deployment controller/policy tests; developer, pre-review |
| 7513 AC-01 / proposed SCN-01 | Opt in synthetic tools deployment Terraform; exact OIDC trust, no IAM mutation/other resources, dedicated RBAC refuses other workloads | `platform/automation-infra/tests/` (new tools permission tests planned); developer, pre-review |
| 7513 AC-02 / proposed SCN-02 | Trust inventory validation admits all three tools workflows and rejects unlisted workflow/incorrect environment subject | `platform/automation-infra/tests/test_trust_contract.py`; developer, pre-review |
| 7513 AC-03 / proposed SCN-03 | Reviewed façade release runs under selected identity; observe ready named workload at expected immutable digest and valid deployment context | Live procedure, platform owner, after bootstrap/apply |
| 7513 AC-04 / proposed SCN-04 | Platform apply readback matches reviewed gateway/targets; interceptor numbered version/hash and live alias match receipt | Live procedure, platform owner, after bootstrap/apply |
| 7513 AC-05 / proposed SCN-05 | Existing tools client-spike story 7253 exercises actual façade; retain its own criteria and evidence, not merely a green deploy workflow | Existing 7253 owner, after deployment |
| 7572 AC-01 / SCN-01 | All children accepted; three active flows progress for full 24 hours; only gate approvals, none of the four prohibited run-failure codes | Live procedure, operator with coordinator review, post-deploy |

The observed bootstrap/stall/rollout live checks required in 7559, 7563 and 7571
remain mandatory even though their current tables only assign pre-review IDs.
Capture them as post-deploy evidence under the same child, not as a substitute
for the corresponding deterministic test.

## Proposed additional acceptance cases

These are the next unused issue-local IDs found in the fetched bodies. Review
against the issue's latest body before applying them; do not renumber someone
else's intervening amendment. All states are **planned/not-run**.

| Child / proposed IDs | Given / when / observable then | Verification and controls |
|---|---|---|
| 7559 AC-04 / SCN-04 | Missing receipt but issued credential or malformed refusal; duplicate observers/restart; no exemption or duplicate retry; cause rotation cannot evade streak cap | PostgreSQL + conditional authority fixture, frozen clock, queue call counter |
| 7563 AC-04 / SCN-04 | Late worker starts concurrently with fence; one side wins; revoked old attempt cannot bind or call model and replacement dispatch is unique | Conditional store integration + real SQL locks, synchronized barriers; unreadable EXEC refuses |
| 7571 AC-03 / SCN-03 | Kill before/after settlement commit and lose commit acknowledgement; client never sees unbacked success, debit at most once, Redis reconciles to receipt or conservative estimate | Process fault injection, real receipt database and Redis, provider fake, fixed request ID |
| 7571 AC-04 / SCN-04 | Provider 429/402/503, client abort, policy expiry or wait cap reached; no special retry outside exact budget denial, no unauthorized extension; cap produces budget_wait_exhausted | Installed transport integration, virtual time, seeded jitter, distinguish pre/post-stream boundary |
| 7382 AC-04 / SCN-04 | Window/concurrency supplements across multiple compatible hops plus amendment/increase race; values and original expiry preserved exactly or explicit disposition, never widened by missing evidence | Real SQL serialization; tampered hash/tenant/repository/action negative fixtures |
| 7381 AC-05 / SCN-04 | Stale gate snapshot/unauthorized user or missing declared deployment evidence; no pass; repeated ticks do not duplicate gates and machine-mode acceptance unchanged | Real route/tick integration and graph UI, independent dispatch count |
| 7385 AC-04 / SCN-04 | Redirect to unapproved host, missing Actions permission, stale head/job attempt or oversized log; no credential leak, safe unavailable reason or existing conflict, check stays failed | Controlled HTTPS responder with header capture; fixed IDs and secret sentinels |
| 7358 AC-08 / SCN-06 | Passed node has pending deploy or concurrent retry; archive refuses or prevents reservation under shared lock; archived cost reconciliation still succeeds | Real PostgreSQL race and action ledger, independent dispatch/receipt assertions |
| 7457 AC-05 / SCN-05 | Legacy empty grant, ambiguous connection, revoked/foreign role, changed workflow blob or unverified creation scope; no dispatch or lease takeover | Provider identity fixtures + real lease races; preserve v1 outstanding action reconciliation |
| 7457 AC-06 / SCN-06 | Shared runtime policy/build dependency changes or workflow acknowledgement lost; required components selected, one correlated run reconciled, no unsafe docs-only bypass | Full audited path fixtures, duplicate tick/provider timeout, record source/definition digest |
| 7513 AC-06 / SCN-06 | Ordinary release attempts IAM/KMS policy change, other workload update or unverified placeholder activation; denied; rollback restores known alias/digest without destroying state | Policy/RBAC negative tests before review; authorized live denial and rollback after setup |

## Test entry points and release documentation

Commands are intended **after implementation**, from the stated directory. They
are not evidence of tests run in this design PR. Use the repository's locked
dependency installation and dedicated PostgreSQL/Redis test fixtures, never live
service URLs. New test files do not exist until the implementation story adds them.

From `modules/gateway` for existing recovery/lineage suites:

```bash
uv run pytest tests/orchestration/test_review_cycle.py tests/orchestration/test_developer_recovery.py tests/orchestration/test_stall.py tests/orchestration/test_amend.py tests/orchestration/test_shared_budget.py tests/orchestration/test_shared_retry.py -q
```

From `modules/gateway` for existing evaluation/evidence/deployment suites:

```bash
uv run pytest tests/orchestration/test_evaluation_controller.py tests/orchestration/test_delivery_progress.py tests/orchestration/test_reviewer_checks.py tests/orchestration/test_deployment_component_paths.py tests/orchestration/test_deployment_controller.py -q
```

From `modules/gateway/frontend`, after installing its lockfile dependencies:

```bash
npm test -- --run src/__tests__/pages/FlowsList.test.tsx src/__tests__/components/orchestration/NodeChip.test.tsx
```

From repository root for trust inventory:

```bash
python3 -m pytest platform/automation-infra/tests/test_trust_contract.py -q
```

Worker tests follow `modules/agent-factory/agent/package.json` and the hosted
worker-image workflow, with the new budget-wait integration exercising the image
that will actually be promoted. Add new settlement/archive and migration tests
to Gateway CI; run Terraform mock tests with synthetic inputs under the existing
automation-infra test configuration. Schema and test commands must be updated in
the implementing PR when new paths land. Do not claim root Terraform tests prove
live RBAC or selected-role identity.

Each child PR includes a safe release note, rollback/forward-compatible reader
behavior and AC report at its actual tested commit. Permission-model changes
need platform-owner review. Existing required CI/review gates remain unchanged;
ready PRs only after pre-review checks, and no merge is performed by this design
assignment.

## Live acceptance procedure

This versioned procedure supplies the current stories' otherwise unspecified
manual SCN references. It is a runbook contract, not permission to execute it.

1. **Prepare an authorized fixture.** Operator records private flow/tenant/target
   selection, accepted policy, exact gateway/tick image, worker image, frontend
   revision and relevant infrastructure/workflow commits. Verify selected account
   **and exact role** through the approved credential broker, following the
   [deployment guide](../../adp-platform-deployment/deploy-with-agent.md). Never
   substitute ambient credentials or publish the target identity. Confirm the
   three final-window flows have dependency-ready work and enough accepted bounds.
2. **Exercise each child once before the window.** Use controlled disposable
   fixtures for bootstrap failure and fenced disappearance; a rollout during a
   completed-stream settlement; compatible/incompatible supplement amendments;
   human evaluation and approval; a known CI failure with redacted reviewer
   detail; archive/unarchive inactive work; reviewed deployment workflow change;
   tools façade/platform/interceptor and the existing 7253 spike. Restore fixtures
   and verify no orphan claims, leases or unsettled unknown-cost records. Use the
   independent assertions in the table, not just successful request status.
3. **Start the 24-hour window only after child proof.** Coordinator records UTC
   start/end, three private flow identities and immutable version evidence.
   Export decisions with complete pagination and stable cursors/watermarks at
   start/end, plus protected run terminal outcomes and API/control audit records.
   Decisions alone may miss external resumes/manual actions; reconcile with the
   intervention journal and deploy/worker events. A missing audit interval is
   inconclusive, not zero interventions.
4. **Observe without repairing.** Normal human gate approvals are allowed and
   recorded. Any resume, resume-continuation, retry/budget increase, manual worker
   restart, database mutation, manual merge/repair or other recovery intervention
   fails the window. Do not hide failures by archiving test flows. Each flow must
   demonstrate actual scheduled progress, not merely absence of logged errors.
5. **Evaluate independent results.** Count operator control actions other than
   gates: expected zero. Query run failures for `attempts_exhausted`,
   `spend_unknown`, `failure_requires_operator`, `budget_reservations_pending`:
   expected zero. A transient handled budget wait is not a failed run, but must
   be distinguishable in evidence. Account for every child live criterion and
   all three flows for the full window. A failed/interrupted window is restarted
   after the relevant fix; it is never combined from cherry-picked intervals.
6. **Report and retain.** Map every child to merged PR, deployed revision and
   live observation; map epic AC-01/SCN-01 to pass/fail/blocked/not-run. Coordinator
   closes only after mandatory proof passes. Retain raw exports privately and
   publish sanitized counts, revision links and a private evidence reference.
   Unknown infrastructure/non-model cost remains unknown, not zero.

## Design coverage audit

| Required issue section | Coverage in this design | Remaining decision/evidence |
|---|---|---|
| Description | Nine bounded outcomes and existing owners; no new product scope; code/live boundaries separated | Owner approval of proposed changes to child bodies not implied |
| Impact analysis | Attempt exhaustion, unknown spend, authority widening, hidden work, log disclosure and excessive deployment privilege have concrete negative tests | Live frequency/cost savings not measured |
| Design | Verified reuse, contracts, retry semantics, compatibility, tenancy/storage and proposed interfaces per child | 7457 explicit selection/creation authority and 7513 bootstrap split require platform decision |
| Deployment | Actual paths/triggers, manual infrastructure, ordering, mixed-version and rollback behavior | Target identity, environment settings, deployed image/RBAC health remain unverified |
| Validation | All 34 child ACs, existing scenarios and epic AC-01/SCN-01 mapped; proposed extra cases and live procedure | No implementation/live acceptance tests run in this documentation-only assignment |

Current delivery checklist: design and static enumeration complete in this PR;
document validation and publication are reported in [verification.md](verification.md).
All implementation, controller-owned CI/merge and live acceptance stay pending
with their existing owners.
