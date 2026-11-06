# Source audit and release map

## Evidence boundary and reproducibility

Audit date: 2026-10-16 UTC. Source revision:
`c958000f0829b1462c2f4ce23bfb743676b97306` (`main` when fetched).
GitHub source, issue/PR metadata and workflow results were inspected read-only.
The repository clone is shallow; history search is not represented as a complete
historical security audit. No AWS identity, live deployment, private scan ledger,
customer workflow or external plugin installation was inspected.

The inventory is **repository-wide, not a representative sample**:

| Artifact | Coverage at the pinned source |
|---|---|
| [tracked-artifacts.tsv](tracked-artifacts.tsv) | All 10,472 tracked paths with blob ID, component, disposition, content kind and entrypoint classification; zero unclassified paths. |
| [entrypoints.tsv](entrypoints.tsv) | All 2,259 statically discovered workflow, action, Docker/build, package, Terraform, manifest, script and build/deploy-reference candidates, including inactive templates and unrelated components. Each maps to a component and explicit disposition. |
| [influence-sources.tsv](influence-sources.tsv) | All 285 files matching the documented comment-reader/control/approval search, with every matching source line number. Tests, records, generated assets and unrelated API uses remain visible rather than disappearing from the count. |
| [authority-sources.tsv](authority-sources.tsv) | All 343 authority, lineage, credential-depth, control-authorization and prompt-fence candidates, similarly classified. |
| [inventory-summary.txt](inventory-summary.txt) | Reconciled component counts. |
| [audit.py](audit.py) | Reproducible source inventory and document-link checker; stdlib only, pinned Git tree, no network or production actions. |

Discovery includes hidden tracked paths, all modules, `platform`, `codebuild`,
`scripts`, `contracts`, root launchers, workflows, workflow templates and copied
client workflows. It scans every text blob at the pinned revision, including
code-generated command examples. Non-text/symlink paths remain inventoried but
are not decoded as executable source. Search expressions are visible in the
script; candidates are **not** synonymous with vulnerabilities. Path-based
component attribution is exhaustive; manual behavior review concentrates on
comment-influence and release boundaries listed below. An unexecuted unrelated
script is not claimed to have been semantically proven safe.

## Comment-channel audit

The rows account for production comment consumers and their adapters found in
the search. Files without authority effects are explicitly excluded from code
changes, not assumed absent. Source line details for each path are in the TSVs.

| Component and tracked artifacts | Actual use / disposition | Implementation decision |
|---|---|---|
| `agent/src/monitoring.ts`; `agent/src/agent-pm.ts` | Parser → built-in switch → instruction/query callbacks; startup `/monitor`; reassessment polling. PM source workflow calls this executable. | Mandatory gate before parser output; gate startup; preserve reassessment guard. Fix cursor, verb normalization and paused control polling as required compatibility work. |
| `agent/src/pm-health-monitor.ts` | Standalone CLI (`require.main`) → `runHealthCheck` → proposal wait → cleanup/retry/custom effects. Six command verbs accepted without permission. | In scope even though no primary hosted dispatch selects it. Bind exact health proposal and author before any effect. |
| `agent/src/agent-superpower.ts`; `utils/superpower-summary-prompt.ts` | Source workflow calls prototype. Existing comment scan, polling, guidance and previous-summary harvesting; feedback can continue implementation. Summary helper already treats supplied material as data. | Replace approval producer and fence all guidance/context; retain safe summary behavior. |
| `agent/src/utils/comment-authority.ts`; `components/GitHubClient.ts`; `services/ApprovalService.ts`; `skill-agent.ts` | Existing reusable permission/grammar/client/wait implementations. Service regex is less strict than the anchored helper; its machine test also treats `GITHUB_ACTOR` as self. Skill poll reads first default REST page then slices, risking missed recent replies. | Consolidate decisions, retain outcomes, add pagination/metadata and correct self identity. Preserve cross-repository permission check. |
| `agent/src/index.ts`; `services/AgentService.ts`; `components/AgentOrchestrator.ts`; `components/FixOrchestrator.ts`; `components/PlanningAgent.ts`; `components/MCPOnboardPlanningAgent.ts`; `types/index.ts` | Legacy `AGENT_ENTRYPOINT=github` path constructs approval service. Retry comments can override the concurrency lock and feed planning. Planning components consume that context; they are not independent approval authorities. | Gate retry once before both uses; keep bounded host approval. Planning prompts must receive fenced guidance, not raw authority. Update types/callers together. |
| `agent/src/mcp-onboard.ts` | Separate executable supplies a model-visible shell approval poll that accepts arbitrary recent approval text. Not the same path as `MCPOnboardPlanningAgent`. | Replace with host-owned shared approval; a helper import or prompt instruction alone does not close this channel. |
| `agent/src/agent-worker.ts`; `aidlc-presence.ts` | Hosted entrypoint reads comments as fenced task context, but separately derives a synthetic `HUMAN_TURN` from non-bot history when an AIDLC gate is pending. | Preserve fenced history; change the presence producer to require exact authorized trigger/request evidence. Conditional on AIDLC state, but not excluded as prototype code. |
| `agent/src/reassessment.ts` | Gathers comments and summarizes association-based approval words; PM consumes the analysis. | Context remains fenced and explicitly non-authoritative. Do not expose `userApprovals` as evidence capable of releasing a gate. |
| `agent/src/aidlc-gate-enforcer.ts`; `.github/workflows/aidlc-gate-nudge.yml` | Marker search avoids duplicate request/reminder posts; nudge checks whether any non-bot replied. Neither grants approval. | Reporting-only exclusion for this story; retain nudge/marker behavior. Suppression by a forged marker is not represented as an implementation approval. |
| `agent/src/github-comments.ts`; `utils/ghPost.ts`; `model-policy-feedback.ts`; `.github/scripts/post_security_pr_comment.py` | Comment publication, retries, fallback reporting and prior-notice dedup. Model-policy dedup additionally uses provider-attested authorship. | Reporting-only; no permission gate on posting the agent's own report. Preserve authorship checks and avoid reply storms. |
| `codex-harness/src/github-entry.mjs`; `codex-reviewer/src/github.ts`, `developer.ts`, `task-board.ts`, `engine-review.ts` | Planning context includes issue comments/previous structured artifact; task checklist retrieval explicitly says it is not verified acceptance. Effects pass through admitted operations/control and protected review contracts. | Context-only exclusion from new approval-service wiring. Keep structured historical artifacts non-authoritative; do not treat a prior JSON comment or checklist as accepted plan evidence. Native controls remain independently authenticated. |
| `agent-worker-image/entrypoint.py`; `lib/pr_binding.py`; `adp_review/client.py` | Hosted executable selection; review comments/metadata as part of mediated review and bound PR recovery. | Integration/regression boundary. A reviewer comment is not a generic Superpower approval. No alternate bot approval path is introduced. |
| `webhook-ingress/lambda/github/handler.py`, `intent_parser.py`; `common/identity_resolver.py`, `skip_reasons.py`, `spawn_persona.py` | Signed webhook → identity → floor → correlation → intent → persona publisher or signed engine-command row. Dispatch families enumerated in design. | Mandatory admission and exhaustive lineage adapters. Reuse identity, metrics and publisher guards. |
| `webhook-ingress/lambda/github/agent_trigger.py`; `lambda/eventbridge/handler.py`; `lambda/task_api/`; `common/task_dispatch.py`, `service_authority.py`, `agent_authority.py` | Authenticated agent-trigger, scheduled service and Task API paths share packaging/publisher support but do not derive a GitHub actor from a webhook. | Explicit GitHub-floor exclusion, with regression tests for service identity, child depth and protected authority. Never fabricate a GitHub human to fit the new gate. |
| `gateway/lambdas/ingest/github_dispatch.py` | WebSocket task bridge posts comments via GitHub rather than consuming issue history. | Producer boundary: resulting GitHub events must pass admission. Do not special-case generated text as authority. |
| Gateway `src/orchestration/engine_commands.py`, `controls.py`, `pending_amendments.py`, `merge_evidence.py`, `work_claims.py`; `src/activity/control_service.py` | Signed engine command verification plus platform permission; hosted tenant/owner controls; protected plan/evidence and merge authorization. | Reuse, do not replace. GitHub comment admission is additional, never a substitute for `PLAN_APPROVE` or owner checks. Merge evidence must stay bound to current PR/review identity. |
| Gateway `src/admin/identity_index.py`, `service.py`, `connections/service.py`, `connections/github_client.py`; `src/activity/external_provider.py`; frontend App import guide | Installation policy projection; existing App API/token/membership patterns; activity observation and advertised webhook events. | Reuse settings/permission patterns. Activity/display readers do not authorize commands. Advertised review-comment events are not necessarily dispatching events. |
| `.github` infrastructure-plan workflows; Cyber infrastructure-plan action; App-registration/onboarding scripts; `platform/scripts/verify-github-wiring.sh` and rollout/evaluation helpers | Post/find status comments, subscribe App events, generate client workflows or observe staging response. | Release/producer/test inventory, not direct in-run approval consumers. Generated client workflow bypass risk remains explicit below. |
| Other matches in Superplane API approval/retry routes, CLI sign-in/onboarding routes, contracts/golden files, lockfiles, pricing paths, release manifests, and historical documents | Generic `/approve` or `/retry` strings, package names, schema/data or separate authenticated APIs. | Explicit exclusion: not a GitHub issue-comment control/approval channel. Component attribution and match lines remain in the inventory. |

Paths beginning `agent/`, `codex-*`, `webhook-ingress/`, `gateway/` and
`agent-worker-image/` in this table are relative to `modules/agent-factory/`.
“Gateway `src/...`” means `modules/gateway/`, not the Agent Factory WebSocket gateway.

### Reachability evidence

- [PM workflow](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/.github/workflows/agent-pm.yml#L139-L179)
  runs `npm ci`, build and `npx ts-node src/agent-pm.ts` from a separate source checkout.
- [Superpower workflow](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/.github/workflows/agent-pt-superpower.yml#L195-L228)
  similarly runs `agent-superpower.ts`.
- [Hosted executable selection](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent-worker-image/entrypoint.py#L89-L96)
  points to `agent-worker.js` and native Codex entrypoints, not the prototype.
- [Legacy startup selector](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent/startup.sh#L1-L49)
  and [retry lock override](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent/src/services/AgentService.ts#L41-L69)
  establish a real alternate entry path, not proof it is currently deployed.
- [PM health command wait](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent/src/pm-health-monitor.ts#L859-L905),
  [hosted synthetic presence call](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent/src/agent-worker.ts#L2358-L2387)
  and [MCP poll prompt](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/agent/src/mcp-onboard.ts#L45-L66)
  are confirmed sibling influence paths.

## Existing fixes and concurrent work

| Work inspected | Relation and integration instruction |
|---|---|
| [Bounded approval #4181](https://github.com/aws-e/adp/issues/4181), closed | Source confirms bounded fail-closed service and caller outcome handling. Preserve this fix; closed status alone is not deployment proof. |
| [Installation policy #3134](https://github.com/aws-e/adp/issues/3134) | Preserve optional floor, tenancy settings and no-new-synchronous-gateway-call constraint. Extend actor evidence locally in ingress, not via a new gateway round trip. |
| [Bot inception #3245](https://github.com/aws-e/adp/issues/3245) | Existing label exception explains why bot `issues.opened` reaches the bad fallback. A label is not human approval. Preserve authenticated bot work only with valid provenance/service authority. |
| [Protected agent authority #5365](https://github.com/aws-e/adp/issues/5365), [lineage binding #5663](https://github.com/aws-e/adp/issues/5663) | Shared publisher/identity files; preserve verified human envelopes, credential horizon, source/tenant binding and flag compatibility. This design does not claim those issues' live rollout is complete. |
| [SSRF reconciliation PR #7838](https://github.com/aws-e/adp/pull/7838), merged | Added a reviewed annotation to the permission helper; request behavior was unchanged. It does not fix this story. |
| [Security PR #6395](https://github.com/aws-e/adp/pull/6395), open when checked | Touches `agent-superpower.ts` command construction. Coordinate shared-file edits and preserve argument-array hardening; do not treat an unmerged PR as current main. |
| [Evaluation approval PR #7613](https://github.com/aws-e/adp/pull/7613), open when checked | Separate engine gate compatibility. Do not redirect its platform approval policy into the GitHub comment helper or import its unfinished changes. |

No PR referencing #7845 existed at the initial check. Open PR search was bounded
to the first 100 open PRs plus an explicit #7845 search; it is not an assertion
that no other concurrent branch touches these files. The integration owner must
rebase/recheck shared-file changes before implementation review.

## Build and deployment entry points

The complete entrypoint inventory is linked above. This table maps the relevant
release paths and exclusions; it does not instruct this design worker to deploy.

| Entry point(s) | What ships / ownership / trigger |
|---|---|
| `.github/workflows/agent-pm.yml`, `agent-pt-superpower.yml`, `skill-agent.yml` | Label/workflow-call source execution on ARC. Operator verifies the source checkout revision contains the fix; changing a worker image tag alone does not update this code path. |
| Other `.github/workflows/agent-{architect,developer,operations,product,reviewer}.yml`; `modules/agent-factory/client-workflows/`; `rules/workflows/`; `runner-infra/scripts/full-onboard-repo.sh` | Alternate/copyable dispatch producers. Enumerated, not assumed hosted. Tests must ensure generated comment-start workflows cannot bypass the required admission contract; installed customer copies are outside the tracked inventory. Changes needed to executable generated bypasses belong to the sibling work package. |
| `modules/agent-factory/agent/package.json`, `tsconfig.json`, `Dockerfile`, `startup.sh`; hosted `agent-worker-image/Dockerfile` | TypeScript compilation and legacy/hosted packaging. Hosted image copies/compiles the whole agent source plus native workers; legacy image has its own startup selector. No claim a source workflow consumes either image. |
| `.github/workflows/agent-worker-image.yml`; `codebuild/bs-agent-runtime.yml`; `platform/scripts/publish-shared-image.sh`; `platform/scripts/rollout-worker-image.py` | PR validation; main-path build followed by protected-environment immutable-digest rollout for new jobs. Running jobs retain old image and authority. Release operator verifies build **and deploy job**, admitted digest, ScaledJob image and canary execution. |
| `platform/scripts/publish-local-image.sh`, `reuse-release-images.py`, `upgrade-workers.py`, `maintain-shared-runtime.py`; `.github/workflows/shared-runtime-maintenance.yml`; `platform/automation-infra/worker-deployment.tf` | Other image publication/reuse/maintenance routes. Same source/digest verification required; never infer “fixed” from mutable `latest`. Included in repository-wide build inventory. |
| `modules/agent-factory/webhook-ingress/scripts/package-lambdas.sh`; `lambda/github/`, `lambda/common/`, `lambda/eventbridge/`, `lambda/task_api/` | Package standalone handlers plus shared and in-process modules. Common publisher changes affect more than GitHub; installed-zip smoke and service-route regression required. New JWT dependencies must be deliberately packaged. |
| `.github/workflows/webhook-ingress-ci.yml` | Unit tests/lint/package verification; deployment-safety checks and Terraform tests are distinct from runtime acceptance. |
| `.github/workflows/webhook-ingress-deploy.yml`; `.github/scripts/detect-webhook-deploy-changes.sh`; `.github/deployment-holds/webhook-infra.md` | Main code-only pushes can package/update Lambda. Infrastructure, mixed code+infra and manual full dispatch are held while the hold file exists. Do not remove/bypass the hold for this fix. |
| `.github/workflows/webhook-code-deploy.yml`; `platform/automation-infra/deploy-webhook-code.py`, `prepare-webhook-source.py`, `webhook-code-manifest.json` | Separate manual, main-only, protected-environment code publisher. Committed manifest is unbound; not a ready substitute for the normal code-only path. |
| `modules/agent-factory/webhook-ingress/scripts/deploy-webhook-ingress.sh`; `infra/lambdas.tf`, `scaledjob.tf`, `iam.tf`, `agent-authority-boundary.tf`; `scripts/plan-shared-worker-rollout.py` | Bootstrap/full-stack and image/admission definitions. No infrastructure change is required by this design. Never run a full apply to deliver a code-only patch. |
| Root `deploy.sh`; `platform/scripts/deploy-all.sh`; `platform/infra/modules/codebuild/`; platform release/publish/upgrade scripts and workflows | Broader bootstrap/release entry points discovered and attributed. Use canonical deployment guide under separate authorization; no new deployment procedure here. |
| `.github/workflows/agent-factory-infra-{plan,apply,destroy}.yml`; `arc-runner-{build,image-deploy}.yml`; Agent Factory infra/runner infra | Legacy ARC infrastructure/build. Manual infrastructure apply is not a precondition for changing source-executed controls; operator verifies any retained legacy consumer separately. |
| `.github/workflows/agent-control-ci.yml`, `credential-path-tests.yml`, `codex-harness-tests.yml`, `provenance-contract-tests.yml`, `orchestration-review-contract-tests.yml`, `adp-ci.yml` | Adjacent validation. Preserve native controls, delegated authority and review evidence. Add changed-channel selectors to existing CI rather than assuming generic CI discovers them. |
| Gateway deployments, chat-worker deployment, Agent Context, MCP Hub, Cyber, Superplane, other domain/tools builds, database migrations, unrelated release scripts | Explicitly excluded as release targets unless implementation changes their code. They are fully enumerated/attributed in TSV, not a requested platform redeployment. Gateway connection APIs supply a pattern; this design does not require shipping a new gateway service. |

Key release controls are source-backed:
[worker triggers](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/.github/workflows/agent-worker-image.yml#L14-L64),
[protected worker rollout](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/.github/workflows/agent-worker-image.yml#L301-L372),
[webhook change classifier](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/.github/scripts/detect-webhook-deploy-changes.sh#L1-L49),
[hold](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/.github/deployment-holds/webhook-infra.md#L1-L18),
[Lambda packaging](https://github.com/aws-e/adp/blob/c958000f0829b1462c2f4ce23bfb743676b97306/modules/agent-factory/webhook-ingress/scripts/package-lambdas.sh#L1-L78).

### Rollout and rollback contract

Implementation owner supplies reviewed source, passing tests, dependency/package
lock and release notes. The story coordinator names the release operator and
staging repository before live work. That operator, using existing authorized
connections and protected environments, owns:

1. Source-runtime/worker release with immutable evidence; verify new jobs use the
   fixed revision, and cancel/drain/restart affected old jobs with existing owner
   authority. A new image does not patch already-running processes.
2. Code-only Lambda update after runtime compatibility checks. Validate actor
   permission lookups in a staging installation whose App permissions are known;
   no fail-open toggle if older App permissions are incomplete.
3. Positive and negative live control/approval tests plus all relevant event
   families; inspect per-delivery outcome, queue nonpublication, lineage and the
   owner-bound credential decision, not just workflow success.
4. Roll back each component to its recorded prior immutable artifact only under
   operator authority. Runtime and ingress can roll back independently; reverting
   ingress does not undo a fixed runtime gate but **does reopen the old ingress
   admission/lineage risk**. Prefer stopping affected dispatch over re-enabling
   unsafe behavior. Reverting source does not revert running jobs or Lambda code.
   No data down-migration is needed. Dependency package and runtime artifact must
   roll back together.

Observed GitHub workflow results: worker build workflow
[2026-10-16 run](https://github.com/aws-e/adp/actions/runs/38830780032) and webhook
workflow [2026-10-15 run](https://github.com/aws-e/adp/actions/runs/38731703304)
reported success. The two latest inspected PM/Superpower runs were skipped.
These observations establish workflow metadata only, **not** which vulnerable
entrypoint is live, the effective IAM mode, service health or security acceptance.
No deployment or workflow dispatch was performed during this design task.

## Coverage gaps and design coverage audit

| Area | Result / owner / remaining uncertainty |
|---|---|
| Description | Full three-finding scope retained, plus sibling channels required by the story's uniform-gate promise. Design-only publication boundary distinct from security-story completion. |
| Impact | Unauthorized effects and conditional vault exposure explained; intentional grammar/staleness/bot compatibility changes explicit. API latency/quota increase is unmeasured, not asserted sub-second. |
| Design | Current callers, reuse, proposed interface/error/cache rules, actor mismatch, request/run binding, prompt fencing, lineage, package requirements and shared-file integration specified. No unresolved architecture decision blocks coding. |
| Deployment | Source-built versus hosted paths separated; code-only Lambda deployment and infrastructure hold checked. Release operator/staging target selection remains coordinator-owned and unverified; blocks live work, not code review. |
| Validation | Stable acceptance in issue, independently asserted scenarios in backlog, pre-merge versus live proof separated. This PR's static checks do not count as future implementation-test passes. |
| Tracked inventory | 10,472 paths reconciled, all mapped; all matching source/build candidates retained. Binary contents, computed runtime imports and generated model actions are outside semantic static proof. |
| External artifacts | Customer-copied workflows, private configuration, installed plugins, generated/untracked artifacts and current cloud resources cannot be enumerated from Git. Operator must inventory installed consumers before claiming fleet-wide remediation. No repository inventory work is deferred to that step. |
| Private scanner evidence | Not accessed; coordinator retains it privately. No exploit payloads or deployment identities reproduced. The three finding IDs remain in the issue. |
| Live account permissions | Older Apps may not have accepted existing `members:read`; ingress/App binding and lookup usability need staging evidence. No alternate credentials or IAM expansion is authorized by this document. |
| Current source drift | Recheck default-branch diff and open shared-file PRs at implementation integration. The audit remains pinned to the reviewed SHA rather than pretending to follow moving main. |
