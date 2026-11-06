# Product delivery BOLT contract

Requested scope: medium-sized implementation stories, deployment work where needed,
and automated verification through the actual UI and/or installed CLI after every
BOLT, supported by independent API/result readback. GitHub
issues own story acceptance; the BOLT index groups their native parent/child links.
A BOLT is a bounded delivery outcome, not a new execution engine or approval gate.

## Size and completion

Each implementation story owns one bounded behavior, normally 2–4 focused engineering
days including its pre-merge tests. This is a sizing estimate, not a delivery promise.
Split again if independent outcomes or more than one new runtime make it larger.
Implementation stories finish at reviewed merge; verification stories build missing
checks, arrange the actual deployment, run them and resolve in-scope findings.
Verification is build-and-run work, not an evaluation template that forbids fixes.

Every BOLT needs implementation plus live automated verification of a new user
capability through the served UI and/or installed CLI. Its EC-04 maps that checkpoint
to a verification story's acceptance criteria in the [filed index](README.md#what-can-be-demonstrated-after-each-bolt).
API-only, helper-only or pipeline-only evidence cannot close a BOLT. A separate
deployment story is needed only for an identified delivery/configuration gap or a
new deployable. Existing workflows remove redundant pipeline stories, never the
actual deployment obligation. Without a deployment child, the verification owner
selects the approved artifact, runs/observes the existing pipeline, proves the
served revision, and then runs acceptance. Use `Refs #N` until live acceptance;
merge, issue closure and green pipeline status alone do not complete a BOLT.

## Agent execution and human evidence review

Verification stories own three phases: build the checks, execute them against the
deployed increment, and publish evidence for human acceptance. Code review/repair
uses the engine lifecycle. Humans review what agents actually executed; rerunning
a UI/CLI journey is optional. Keep the issue open with `Refs` until its BOLT's
evidence decision is recorded. The operation node completes on execution/evidence;
it must not wait for the human decision of its successor evaluation node.

Each V story adds an evidence criterion (AC-04, or V13 AC-05) and corresponding
scenario without renumbering existing keys. The agent's packet must contain:

- Actions, independently specified expected results, actual results and AC →
  scenario/test → run/attempt links, including failures, skips and not-run cases.
- UI screenshots or video with trace where available; installed CLI commands,
  stdout/stderr and exit codes; independent API/result readback. Include the
  surfaces required by the story, with separate UI and CLI reports.
- Source/requirements revision, deployment receipt, schema head and relevant
  image/asset/CLI checksum identities, plus cleanup receipts.
- A concise outcome summary, authorized artifact links and optional rerun steps.
  Agents provide setup/cleanup if a reviewer elects to rerun; there is no mandatory
  reviewer-owned fixture or manual demonstration.

B01 combines V01 and V03 evidence in one decision; B09 combines V10 and V11.
All other BOLTs use their single V story. A packet with a required failure, missing
result, mismatched revision or failed cleanup cannot be accepted. Record the
human decision against the exact packet/plan revision and preserve rejected
attempts. Publish sanitized links on the existing stories/BOLT; retain private
execution evidence only in authorized artifacts. See the
[BOLT review procedure](../../delivery-flow/plans/product-onboarding-8141/human-checks.md).

The proposed flow has **11 BOLT evidence reviews plus one initial ADP flow
approval**. It has no design, peer-readiness, release-readiness or final module
human gate. Completion rolls up required accepted child outcomes and parent
coverage. The existing review/merge lifecycle still applies to implementation.

## Automatic delivery within the accepted dev scope

Once repository checks, compatible artifacts and target conditions pass, agents
deploy or observe the matching automatic deployment and run verification. No
per-BOLT release decision is requested. Initial acceptance binds the dev target,
workflows, authority and limits. Missing technical prerequisites block execution
with evidence; they are not human readiness tasks.

The exact gateway/frontend dev targets permit reviewed main without a per-release
assistant approval, as confirmed in `scripts/check-assistant-deploy-boundary.sh`
at `ecf16bb8de4132249c555df790721f654fcf9cf0`. D01 owns configuration and first-rollout
qualification, including an automatic release-to-verification handoff. The current
operate-node parser still requires literal revision pins; the bounded prerequisite
and testable handoff contract are explicit in
[automatic-deployment.md](../../delivery-flow/plans/product-onboarding-8141/automatic-deployment.md).
Qualify that wiring before activation; do not conceal repeated manual amendments.

The first deployment story extends existing release/configuration and test-selection
wiring, then deploys the first usable API and installed CLI slice. B01 includes
Product create/list/read/update/archive/restore CLI acceptance; B02 adds the
browser workspace. Later BOLTs reuse that path.
Catalog placement is a bounded contract checkpoint: reuse the gateway pipeline if
it is the selected runtime; if the owner chooses a new deployable, file its specific
pipeline/deployment child before enabling catalog implementation. Do not invent a
workflow for an unselected service.

B11 must expose its lineage/context projection through I23's proposed installed
`adp insights context show` command. I23 first binds the read-service schema,
authorization/errors, runtime and publish/health/recovery path. V13 AC-04 proves
the deployed read service and CLI artifacts alongside the emitter/adapter, then
checks dated membership, coverage, deduplication and revoked-access denial. If the
selected read runtime lacks deployment coverage, link a concrete deployment child
before live acceptance. An emitter or infrastructure receipt cannot prove this path.

## Current pipeline evidence

Inspected main: `ff122e12295aea61c4d791a91817c580c9457c83` (2026-10-19).
Design contract: `6c875c6b5a29f54bbd94a98dda9a37bc8e411db5`, PR #8155.
All proposed test files, suite names and Product/Insights commands in stories are deliverables,
not currently available operations. The prototype is not the production test target.

| Surface | Existing source at inspected main | Trigger and limitation |
|---|---|---|
| Gateway/API/CLI and migrations | [gateway-deploy](https://github.com/aws-e/adp/blob/ff122e12295aea61c4d791a91817c580c9457c83/.github/workflows/gateway-deploy.yml), [gateway-ci](https://github.com/aws-e/adp/blob/ff122e12295aea61c4d791a91817c580c9457c83/.github/workflows/gateway-ci.yml) | Main path changes or explicit dispatch; candidate-image migrations before serving; CLI is packaged in the gateway. Profile flags and live case selection still need wiring. |
| Frontend | [frontend-deploy](https://github.com/aws-e/adp/blob/ff122e12295aea61c4d791a91817c580c9457c83/.github/workflows/gateway-frontend-deploy.yml) | Separate main frontend-path publication or dispatch; gateway rollout does not prove matching served browser assets. |
| Installed CLI evaluation | [eval-cli-uplift](https://github.com/aws-e/adp/blob/ff122e12295aea61c4d791a91817c580c9457c83/.github/workflows/eval-cli-uplift.yml), `tests/e2e/cli_uplift/` | Dispatch/reusable suite, clean disposable host, expected revision and durable cleanup; new Profile group must be registered rather than passed as an unsupported selector. |
| Served browser evaluation | [e2e-new-ui-playwright](https://github.com/aws-e/adp/blob/ff122e12295aea61c4d791a91817c580c9457c83/.github/workflows/e2e-new-ui-playwright.yml), `tests/e2e/new_ui/` | Main-only live job via dispatch/coordinator; register Product cases and module selection in the existing inventory. |
| Insights | [insights-deploy](https://github.com/aws-e/adp/blob/ff122e12295aea61c4d791a91817c580c9457c83/.github/workflows/insights-deploy.yml) | Main push defaults to infrastructure. Emitter/distiller services require explicit stage and immutable image; an infrastructure pass is not adapter deployment. A read-service runtime/deployment path is not established by this evidence; I23 resolves it before dependent code. |
| Catalog/Context | [Context owner epic](https://github.com/aws-e/adp/issues/8149) | No Product Context runtime established by the Profile design. Resolve service/API/storage placement first; existing Knowledge Layer pipelines do not prove this service exists. |

## Shared fixtures and controls

Use two run-owned tenants, an owner, an authorized editor, a nonmember and a revoked
actor. The fixture owner supplies private references through existing protected
configuration. Do not publish account IDs, target URLs, tenant/source identities,
credentials or private evidence in issues or docs. Canonical runtime Project and
catalog module/feature bindings remain unresolved until the delivery coordinator
resolves them; the GitHub Roadmap project ID is a planning identity, not that ID.
PranavSharma1000 retains scope/acceptance ownership and assigns executors through
#5411 before dispatch. Filing this plan assigns no new platform authority.

Before deployment, the operator follows the [canonical deploy guide](../../adp-platform-deployment/deploy-with-agent.md),
verifies the active target identity and obtains any target approval required there.
The current work authors stories; it is not a request to deploy unimplemented code.
Use protected environment references and existing OIDC/delegation, not copied keys.
Keep feature admissions off until compatible schema/API/client versions are present.

Pre-merge tests pin provider stubs, source revisions, clocks and actor roles. Live
checks use actual authorized sources and bounded invariants, separately classified
from stubbed tests. Compare sets or stable IDs, not incidental pagination order.
Use a fixed clock for expiry/period boundaries; live fixtures supply explicit dated
inputs and an as-of time, never a test whose expected value changes at midnight.
Model checks assert schema, allowed effects and metering, not exact model prose.

For async state, poll its authoritative operation/status API with a recorded deadline
(default 120 seconds, 2-second maximum cadence); no arbitrary sleep is the oracle.
A timeout is failed/inconclusive, not success. An independently owned provider
operation may declare a longer bounded deadline in its story before execution.
At most 10 bindings, 100 records/page, 5 pages and 30 seconds/attempt apply to Profile
discovery; continue only through the explicit cursor contract. Model guidance is
at most one metered call and 10 proposals per turn. No automatic paid retry.

## Live verification and recovery

1. Record requirements/source SHA, workflow definition revision, API image digest,
   schema head, installed CLI checksums and served UI manifest as applicable. Reject
   stale or mismatched artifacts before reporting a functional pass.
2. Invoke the real public entry point. Every BOLT's new user capability must be
   exercised in the served browser and/or installed CLI. Browser actions are checked
   by independent authorized API readback; CLI assertions include exit code/JSON plus API readback;
   API assertions check durable state in a new session and authoritative provider
   observation for any external effect. Do not derive expected values with the
   implementation under test. A mocked service pass is not live proof.
3. Run the BOLT's positive, denied and recovery cases plus affected prior BOLT
   regressions. Select explicit case IDs; missing/unknown selections must fail.
   Separate UI and CLI reports where both are exercised. Their setup can share a
   fixture contract, never a mutable hidden fixture or a duplicated result count.
4. Save an attempt-aware AC → scenario/test → run → outcome → evidence map. Preserve
   failed attempts, skips and blocked fixtures. Private result artifacts retain
   actual target/source observations; public issues contain authorized references
   and sanitized outcome summaries only.
5. Disable new admissions before rollback; retain additive schema, accepted values,
   history, revoked-access tombstones and operation receipts. Reconcile effects
   whose acknowledgement was lost; never retry an unbounded provider create blindly.
6. Cleanup only run-owned resources using the durable fixture manifest and existing
   recovery sweep. Reused connections/providers survive. Verify absence with the
   owning service/provider, preserve audit evidence, and keep cleanup failure red.

No compulsory Portfolio, Initiative, Contribution, Release, repository, team or
budget field is added to first save. Product is the user-facing identity; Projects
remain delivery/access/evidence scopes. Raw Insights events retain original IDs and
same-Project references. Business context joins neither grant access nor multiply
work/cost. Release implementation remains with #7628; broad Flow/Task authorization
remains #8148, including reuse of the existing flow-Project story #7298.
