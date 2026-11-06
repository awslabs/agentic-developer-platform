# Implementation backlog and scenario contract

This is a design backlog for [issue #7845](https://github.com/aws-e/adp/issues/7845),
not newly filed or dispatched work. The story's issue body owns the only acceptance
table; rows below map implementation packages and scenarios to its stable IDs.
The implementation owner reconciles review findings and supplies pre-merge evidence.
The story coordinator names a release operator for post-merge/live acceptance.
No named operator identity or live connection is inferred from this repository.

## Work packages and order

| Package | Outcome and concrete change boundary | Dependencies / verification / owner |
|---|---|---|
| W1 — Shared runtime authority | Extend `agent/src/utils/comment-authority.ts`; normalize comment/actor metadata in `components/GitHubClient.ts`; add bounded decision cache, refreshed-token adapter, precise reasons, exact approval parser and bounded notices. Preserve boolean wrapper and current request safety. | First runtime package. Developer owns interface and tests for AC-01/02/03. No production effect can accept a raw parsed comment instead of a decision. |
| W2 — Run-bound approvals | Consolidate `services/ApprovalService.ts`, `skill-agent.ts`, `agent-superpower.ts`; adapt `AgentOrchestrator.ts` and `FixOrchestrator.ts` posting/wait boundary; bind posted plan/run/request, fetch all reply pages, require fresh writer and human metadata, remove feedback-as-approval, fence both prompts. | After W1. One owner edits these shared files; coordinate open security PR #6395 before merging Superpower edits. AC-03/04 and positive approval compatibility. |
| W3 — Monitoring and sibling channels | Update `monitoring.ts`, `agent-pm.ts`, `pm-health-monitor.ts`, `services/AgentService.ts`, `mcp-onboard.ts`, `aidlc-presence.ts`, `agent-worker.ts`, relevant reassessment/planning prompt adapters. Gate parser/startup/retry-lock/health/presence effects; correct cursor, paused polling and query case; replace MCP model-owned approval. | After W1; use W2 request contract for proposal gates. AC-01/02/04/05. Do not parallel-edit W2's approval files; integration owner serializes shared files. |
| W4 — Actor-bound GitHub admission | Refactor `lambda/github/handler.py`, `intent_parser.py`; add proposed `lambda/common/github_actor_authority.py`, package requirements/lock and installed-zip checks. One event registry and admission call, sender/object binding, writer requirement for human comments, optional association lower-bound evaluation, errors/dedup/metrics. | Independent of W1 internally; shared handler ownership with W5 requires serial integration. AC-06. Reuse current settings; no new gateway endpoint, infrastructure or credential path. |
| W5 — Lineage and publisher invariants | Extend correlation adapters in `handler.py`; strengthen `common/spawn_persona.py` input and audit defaults; retain `agent_authority.py`, `agent_trigger.py`, EventBridge/Task API contracts. Reject unresolved continuation without root/depth reset; preserve credential horizon and service roots. | After W4's normalized candidate contract. AC-07/08. Use end-to-end handler → envelope/blocked outcome assertions, not just helper tests. |
| W6 — Regression guard and distribution | Add proposed consumer registry/source guard and table-driven tests, register selectors in existing CI. Update source-built/copyable workflows or templates if they can start comment-driven work without the mandatory gate. Verify all relevant package/image copies and dependency installation. Add helper architectural note and operator compatibility notes. | After W2–W5. AC-09; integration owner verifies the audit's entire channel table. No “inventory later” subtask: inventory is already delivered. |
| W7 — Release and live acceptance | Reviewed code PR uses `Refs #7845`; record fixed runtime/source revision and Lambda archive hash; new-job canary, old-job drain/cancel policy, staging attempts by read/triage/writer/bot identities, actor-mismatched events, truthful lineage and no credential authority on denied roots. | After pre-merge ACs and existing deployment authorization. Release operator owns AC-10; coordinator assigns operator/fixture. Remains pending until live observations exist. |

Recommended integration: W1 → W2/W3 with non-overlapping assigned edits;
W4 → W5; W6 joins both tracks; W7 follows reviewed/merged implementation.
These are sequencing recommendations, not agent dispatch or permission to merge.
Do not use a partial implementation PR to claim the uniform gate is fixed.

## Controlled scenario matrix

All selectors below are **planned** unless named as an existing suite in the
reproduction section. The design PR has not implemented these tests. The test
oracle is the expected external effect (callback count, state transition, emitted
envelope, credential owner, no queue publish), independently declared from the
production command/event registry. Tests must not merely iterate the same registry
and assert that its own authorization flag is true.

Common controls: fixed fake UTC/monotonic clocks; deterministic request/run IDs;
synthetic GitHub numeric IDs/logins and repository fixtures; separate control/effect
repositories; injected REST responses and tokens; isolated cache per test;
no live GitHub/AWS calls; deterministic comment pagination/order; fresh imported
Lambda module/environment per case; isolated fake stores/queue; reset call counts
and environment after each case. Stub secret/JWT/token exchange with fixture keys
only; packaging smoke must execute from the built zip, not the source tree.

| Scenario | AC mapping | Given / action / independently expected outcome | Planned verification |
|---|---|---|---|
| SCN-01 | AC-01 | Each of nine monitoring verbs; admin/maintain/write versus read/triage/none/unknown; process through real loop. Allowed writer causes exactly the intended callback or state change; every denied row produces zero state/effect calls, including built-ins before callbacks. | Automatic integration; new monitoring authority suite, fake callbacks and clock. |
| SCN-02 | AC-02 | Malformed author/type, deleted user, non-OK API, 429, timeout, redirect and JSON error; warm cache before/after 60/15/5-second expiry; token generation changes. Deny with reason; no raw error/token/text in notice; cached success expires; approval forces fresh lookup; notices remain bounded. | Automatic unit + integration; shared helper and caller spies. |
| SCN-03 | AC-01/02/05 | Opaque IDs, two comments with equal timestamp, >100 comments, duplicate pages, partial page failure, old edits, retry after denial, two active runs. Commands reach only their current session once; no history replay; failed batch does not skip unseen commands. Pause then authorized resume/stop/extend is still observed; `/queryPM` works case-insensitively. | Automatic integration; monitoring and comment-adapter fixture pages, independent state oracle. |
| SCN-04 | AC-03 | Current posted design/run; explicit matching approval from each writer tier; source and effect repository permissions differ; writer equals workflow `GITHUB_ACTOR`. Only both-scope writer allows once; source-only writer denies; triggering human is not mistaken for the agent. | Automatic integration; shared wait and Superpower/skill adapters, implementation-phase spy. |
| SCN-05 | AC-03 | Old request/run/plan digest, future or malformed timestamp, reply between posting and poll, post failure, edited plan/comment, feedback, bare/quoted/prefix/extra-text commands, bot/agent, expiry and API outage. No invalid row starts implementation; fast valid reply does; timeout/rejection/unavailable remain distinct; revised plan requires new ID. | Automatic integration; initial scan and polling exercised separately, fake timers and posting response. |
| SCN-06 | AC-04 | Authorized and unauthorized guidance/retry/custom/rejection/old summaries, including harmless fence-like strings. Both rendered Superpower prompts contain only selected authorized guidance as fenced data; no privileged “skip gate” block; host gate remains closed regardless of text. Legitimate task refinement still reaches authorized callback as bounded data. | Automatic prompt-shape assertions plus host-effect test, not substring-only test. |
| SCN-07 | AC-05 | PM startup/reassessment, all six health verbs, legacy retry lock and guidance, MCP approval, hosted AIDLC pending stage. Unauthorized, stale, wrong-stage/run and bot comments cannot switch mode, override lock, clean up, deploy through MCP or mint human presence. Authorized current actions retain behavior. | Automatic integration per concrete entrypoint; synthetic presence audit shard and effect spies independently inspected. |
| SCN-08 | AC-06 | Every dispatching event/action, issue-comment on PR, no-op lifecycle/review events; sender differs from issue/PR author; each configured threshold, missing/invalid setting, missing installation, stale cache, API failure, absent App permission, mismatched numeric ID or App/repo binding. No borrowed author association; only sufficiently proved actor dispatches; denial/unavailable has no queue/authority/engine-row effect. Recovered retry can run once. | Automatic handler integration; independently listed event fixtures, captured HTTP outcome, fake queue and stores. |
| SCN-09 | AC-07 | Human, registered bot, missing/unsigned/foreign marker, failed pointer lookup, cross-tenant/install/repo pointer, bot inception label, opt-in auto PR review on/off. Eligible proven human gets real root identity; valid continuation preserves server root/parent; unresolved GitHub origin is non-human and not published; no default empty-correlation human root. | Automatic handler → publisher contract, correlation fixtures with distinct identities. |
| SCN-10 | AC-08 | Both authority modes; known depths 0, cap−1, cap; invalid/negative/bool depth; credential horizon >= spawn depth; valid service root; repeated no-op events. Non-human yields empty credential owner even with populated human hints; invalid/missing lineage cannot reset cap; one actual child increments once; no-op increments zero. Signed envelope/human-sender guard unchanged. | Automatic integration across handler, spawn, protected-authority and service adapters. |
| SCN-11 | AC-09 | Add a synthetic new comment consumer/verb/event family without a classified gate; copy legacy workflow/prompt template; build and import packaged handler. Coverage guard fails on missing classification/adapter; independent negative assertions fail on bypass; fixed zip imports JWT/crypto without source-path leakage. Existing native controls, skill approval and service dispatch still pass. | Automatic source/AST guard, CI contract, installed-artifact fixture and existing regression suites. |
| SCN-12 | AC-10 | Authorized staging fixtures: read and triage collaborators, maintainer, registered bot, distinct issue author/labeler; fixed new worker and Lambda, plus known old run. Attempt normal commands/approval/dispatch; maintainers succeed only for current request, others have no effect; denial is visible; admitted lineage identifies actual actor; old run is not falsely declared patched. | Manual versioned procedure below; private receipts tied to tested revision/digests. |

No synthetic string test is claimed to prove live GitHub permission behavior,
credential isolation or fleet-wide artifact coverage. A failing new guard is not
fixed by marking a command excluded when it can still cause an effect.

## Existing pre-merge checks to extend

Existing agent package commands are `npm test` and `npm run build`. After installing
its locked dependencies, run the focused existing suites first, then the newly
implemented channel suites, then the broader runtime suite:

```bash
cd modules/agent-factory/agent
npm ci
npm test -- --runInBand src/utils/comment-authority.test.ts src/services/ApprovalService.test.ts
npm run build
```

Existing webhook unit-test paths are below. Use an isolated Python environment
with the module's dev dependencies; existing CI additionally installs its test
plugins. These commands do not perform deployment. Use an offline/mocked test
environment, not shell-injected production credentials.

```bash
cd modules/agent-factory/webhook-ingress
python3 -m pytest lambda/github/tests/test_handler_author_association.py lambda/common/tests/test_spawn_authorized_user.py -q
python3 -m pytest lambda/github/tests/test_handler_correlation_1696.py lambda/github/tests/test_chain_depth_generations_4268.py lambda/github/tests/test_handler_lineage_context_5663.py -q
```

Proposed implementation must add tests for W1–W6; these existing suites alone do
not cover Superpower, sibling gates or the new actor-evidence adapter. Follow
`.github/workflows/webhook-ingress-ci.yml` for full Lambda unit/package checks and
`agent-worker-image.yml` for installed worker checks. Do not run Terraform apply,
image publication, deployed smoke or live test markers as part of offline proof.

## Live procedure — SCN-12, version 1

Prerequisites owned by the coordinator/release operator: an explicitly authorized
staging installation/repository; permission-confirmed human fixtures; approved
credential connection; current App permissions; known fixed runtime source/digest
and Lambda code hash; access to delivery/command logs, queue/invocation observations
and credential-authority decision records. Fixtures and resource identities stay
in private configuration. Missing prerequisites mark AC-10 blocked, not passed.

1. Record test revision and installed artifacts. Start a test run through an
   authorized path, record run/session ID and host-posted request ID. Confirm the
   observed worker is new, rather than an old pod surviving image rollout.
2. Submit normal control and approval commands as read/triage fixtures. Observe
   unchanged execution/approval state and a denial reason tied to actor/comment.
   Check that a denied request did not enqueue work or trigger an effect.
3. Repeat as a writer. Exercise pause→resume, instruct/query, extend and stop on
   disposable runs. Approve the current design explicitly; verify exactly one
   phase transition. A previous request must not authorize a replacement run.
4. Exercise issue open, label, comment and enabled PR dispatch actions with the
   actor differing from content author where applicable. Below-floor or
   unresolvable actor is refused; eligible human records their own root identity.
   Confirm disabled auto-review remains disabled. Registered bot without valid
   provenance gets neither dispatch nor vault authority; valid authenticated
   continuation preserves its existing chain bounds.
5. Use an operator-controlled staging failure fixture to exercise permission API
   outage and recovery. Do not disrupt shared GitHub/network services. Verify
   fail-closed logs, bounded notices, new-comment retry and idempotent redelivery.
6. Cancel/stop fixture runs through existing authorized control, remove disposable
   branches/issues under fixture policy, clear test-only settings, and preserve
   sanitized outcomes plus private receipts. Do not persist tokens or private
   deployment identities in public docs. Map each AC to pass/fail/blocked/not-run;
   coordinator closes the security story only after the required evidence exists.
