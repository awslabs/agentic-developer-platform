# Autonomous Security delivery waves

Delivery plan for [Security epic #8682](https://github.com/aws-e/adp/issues/8682)
and its existing 16 stories. The [architecture](architecture.md) remains the
behavioral contract; the [backlog](backlog.md) supplies each story's detailed
scope. This plan groups implementation and verification. It does not record an
activated flow, completed implementation or live acceptance. The
[executable flow bundle](flow/README.md) defines registration, machine evidence
and the SA15/SA16 release handoff.

Each story record in [the wave map](delivery-waves.json) includes grouped
`file_groups`, exact repository-relative paths and its published test selectors.
The [story file plan](story-files.md) is the readable view. Distinguish new files,
existing-file edits and edits to files created by a predecessor; keep shared
router/model/migration/package changes under the named integration owner.

The requested operating model has **no human wave checkpoints or final human
evaluation**. Agents implement, review, repair, merge, release and evaluate within
the configured authority. Normal gateway and frontend push deployments continue.
Development progresses on code evidence without waiting for those deployments.
There is **one planned coordinated L5 infrastructure/worker release**, followed
by live qualification. A failed qualification can require a targeted repair
release; an unconditional one-deployment guarantee would hide that requirement.

## Waves

An arrow means a required dependency; parallel work starts only when its inputs
are available. SA15 and SA16 have ordered construction and execution phases, not
duplicate requirements owners. Phase completion does not close their issues.

| Wave | Existing stories and ordering | Deliverable | Automatic exit evidence | Deployment behavior |
|---|---|---|---|---|
| **1. Shared foundation** | SA01 → SA02 → SA03 → SA05 → SA06 | Shared contracts, delegated Product reads, saved configuration, durable run admission and recoverable Lambda execution code. Reuse the merged design; finish SA01's remaining contracts, mappings and integration checks. | Reviewed merges; contract, authorization, database/concurrency, source-version and queue/provider fault tests through the implemented entry points. | Normal gateway/frontend auto-deploys may run. No L5 infrastructure apply or worker release. |
| **2. Security lifecycle and release preparation** | **SA04, SA09, SA10 and SA15 construction in parallel** | Persistent Security bindings, one-off/scheduled/event triggers, findings/triage/comparison, and package/migration/infrastructure/release tooling. | Reviewed merges; binding ownership/cleanup, event deduplication, result visibility and package/install/migration checks. Infrastructure plans are evaluated without applying them. | Normal auto-deploys continue. Build the L5 worker artifacts and release path; defer the L5 apply/publication until wave 4. |
| **3. Assessments and clients** | **SA07, SA08, SA12, SA13 and SA14 in parallel**; then **SA11** after SA07/SA08 and wave 2's SA10 | Threat modeling, static code review, Product UI, installed CLI, MCP and durable publication into Product Context. | Reviewed merges; both adapter suites, exact-source coverage, cross-client configuration/run/result parity, publication retry/revocation and combined offline integration. | Normal auto-deploys continue. No separate deployment barrier for each client or adapter. |
| **4. Assemble and release once** | SA16 test-harness preparation after all construction dependencies; **SA15 release execution** | One pinned release manifest, complete live drivers, L5 infrastructure, compatible schema and same-wheel worker/runtime artifacts. | Combined-candidate checks; current scoped target access; migration/plan checks; actual gateway/frontend/CLI/worker artifact readback; queue, identity and recovery smoke evidence. | Reuse matching successful auto-deploys. Apply the accumulated L5 infrastructure changes and publish the final workers as one coordinated release. |
| **5. Live qualification and completion** | **SA16**, with SA15 retaining release/recovery ownership | Real threat-model and static-code-review journeys; all clients; one-off and recurring runs; failure recovery; later agent reuse of a real Context contribution; cleanup. | Machine verification of every existing required story AC/scenario and EC-01–09 at compatible revisions, including independent provider/source observations and cleanup. | No planned fresh rollout. A source defect produces a reviewed fix and only the affected component release, followed by the affected live checks. |

The [wave map](delivery-waves.json) preserves every dependency in the epic's
story table. The wave-3 SA11 join is mandatory: publication cannot be accepted
using only a guessed producer contract. Profile/Context implementation and native
source eligibility are tracked as capability prerequisites; fixtures permit
independent construction, but do not satisfy the real integration criteria.

## Keep development independent of deployment

Use the existing [code-only node completion contract](../delivery-flow/build-completion.md)
for development nodes in waves 1–3 and SA16's harness construction. Each node
requires its reviewed, independently verified merge. Each engine wave has one machine
evaluation over the code criteria due in that phase. Live clauses remain pending.
Passing code evidence makes successor development eligible immediately.

`delivery_mode: "code_only"` does not suppress GitHub push workflows. Keep
ordinary main-branch PRs and the existing gateway/frontend deployment triggers.
Successor development uses code evidence; deployment observers reuse already
running automatic releases. This separation is
supported by the [merge controller](../../modules/gateway/src/orchestration/merge_controller.py)
and [deployment workflow observer](../../modules/gateway/src/orchestration/deployment_workflows.py).

Intermediate main revisions must remain deployable. L5 admission, triggers and
provider setup stay disabled until the final readiness checks pass. Ship additive,
compatible migrations with the code that needs them; test startup and the disabled
service path when workers or provider bindings do not yet exist. Gateway imports
must not require the worker-only Security SDK. Saving configuration must not start
provider jobs. UI availability must reflect actual server readiness.

Auto-deployment failures are repaired by their owner while unrelated development
continues. They block final runtime qualification, not every later code story.
The source defect still blocks any dependent code or compatibility check it breaks.

SA15 implements and qualifies worker publication as an explicit grouped release
for this initial delivery. Building an artifact or extending backend path selection
must not accidentally publish workers on every intervening story merge. This is a
release-tooling deliverable, not a claim that the current workflows already do it.

## One coordinated L5 release

Wave 4 owns one deployment phase; it can invoke several component workflows.
Automatic gateway/frontend deployments are additional allowed background activity,
so this is not a claim that the repository runs only one deployment job in total.

1. Resolve a source revision containing all verified construction merges. Record
   the requirements, wheel digest, dependency locks, migration heads, workflow
   definitions and component artifacts in one release manifest. Do not silently
   replace this candidate with a moving `main` during verification.
2. Read the actual gateway/frontend/CLI deployments. Reuse matching verified
   artifacts or wait for their existing automatic jobs. Dispatch only a missing
   or incompatible component. A green Actions run alone is not runtime evidence.
3. Apply the accumulated L5 infrastructure plan through the scoped deployment
   path and bring the schema to the required additive heads. An already applied
   migration is verified rather than rerun as a new change. Preserve the ordering
   and rollback guarantees in the [deployment architecture](architecture.md#deployment-and-compatibility).
4. Publish the final L5 workers/relay with the same wheel digest consumed by the
   gateway and dispatch disabled. Verify imports, identity, queue permissions,
   database/API access, immediate dispatch, delayed polling and repair behavior.
5. Verify Product delegation, exact source access, provider readiness, Context
   access, fixture ownership and current limits. Enable only the qualification
   fixture and continue directly into wave 5.

SA15 owns the target release lease through qualification and recovery. Do not
mark the release owner finished and then invent a way to reopen it after SA16
finds a defect. Record wave-4 readiness separately from final release acceptance.
Waves 4 and 5 are two logical phases of one final engine operations wave: SA16's
harness build precedes SA15's release-and-qualification operation, which invokes
the SA16 live procedures and is followed by one machine evaluation. Readiness is
a progress receipt inside that operation, not a completed-node dependency that
would prevent qualification from starting. This avoids a circular wait between
release completion and live acceptance.
Repairs reconcile the actual release first, batch compatible fixes, and reuse
unchanged artifacts. An L5 wheel change may affect both gateway and workers;
component selection follows that dependency rather than just the edited path.

## Autonomous progression and recovery

- Use agent review and required CI on each current PR head. Merge automatically
  when those checks and repository rules are satisfied. Shared model/migration,
  router/catalogue and packaging changes have one integration owner; parallel
  authors serialize conflicting edits through that owner.
- Propose at most three independent development actions and one release owner per
  target. Use separate fixtures/worktrees and durable attempt records. Resume
  interrupted work from receipts; reconcile an uncertain provider effect before
  retrying it. Repeated deterministic failures require a fix, not blind reruns.
- Every authored wave evaluation is explicitly `machine`, with an installed
  verifier and a bound evidence specification. Do not add per-wave gates or a
  final human evaluation. The existing engine creates an initial acceptance
  record; the coordinator must bind the user's authorization through the supported
  attributed admission path. A prose plan does not activate that record.
- Resolve registered repository/environment connections, target scopes,
  runtime capabilities and spend/time/attempt limits before
  the corresponding action. Discover and use existing authorized configuration;
  do not invent grants or copy expired authority from earlier spikes. Missing
  prerequisites are explicit blockers for affected actions while independent
  construction continues. This is not a scheduled human approval checkpoint.
- SA15 must qualify automated infrastructure dispatch, worker/runtime receipts
  and the protected live-workflow invocation. The current infra workflow is
  `workflow_dispatch`, not an automatic apply on merge. Agent dispatch must
  retain its revision/plan checks and existing target protections. Use the
  [deployment guide](../adp-platform-deployment/deploy-with-agent.md) for actual
  commands and authority; a new account/environment is not implied by this plan.
- SA16 owns executable live drivers, independent evidence reading, generated
  fixture setup and cleanup. Qualify any missing automatic acceptance/completion
  integration before activation. Existing source support is not proof that the
  installed engine can execute every required operation unattended.
- Persist long-running Security jobs and qualification attempts. Poll at the
  configured ten-minute default; do not occupy an agent or workflow with a
  hours-long sleep loop. Resume bounded observations and keep monitoring health,
  suspected stalls, native failures and local expiry separate.
- After complete live evidence, accept the matching criterion revisions and
  close eligible stories/epic through the authorized idempotent completion path.
  A merge, simulated provider result or successful deployment alone does not
  accept the epic. Private runtime evidence stays in the evidence store; public
  plans and sanitized summaries remain under `docs/l5-services/`.

## Implementation handoff

The JSON wave map is planning data, not an executable engine proposal. Construct
the actual proposal using the [delivery-flow contract](../delivery-flow/README.md),
with existing issues, ordered phases for SA15/SA16, machine evaluations and scoped
operations assignments. Map the three code waves and the combined final
release/qualification wave to four engine waves, each with one machine evaluation.
Preserve all stable issue/AC/scenario identities. Bind
future release selection to verified predecessor receipts; do not fill in invented
future commit SHAs, targets or verifier hashes.

Copy each story's `file_groups` into its proposal node so the graph displays the
grouped Create/Modify plan inside an expandable **Planned files** panel. Carry
the immutable file-map anchor into supported node context. Other planning-map
fields (`file_map`, `planned_test_references`, root `file_plan`) are not proposal
fields. These file groups are optional human-facing explanations, not an exhaustive
scope or an execution-policy repair allowlist. Agents may change unlisted files
without amending the map first; no node fails because its diff differs from it.
Repository/action authority remains separate. The flow bundle records the
existing operations-repair limitation and its qualification owner.

At preflight, verify that the effective graph has no recurring human gates, that
code nodes finish on verified merges, and that neither wave exit nor an implicit
continuation waits for each gateway/frontend auto-deploy. The only planned
runtime barrier is wave 4, followed by wave 5's actual live proof.
