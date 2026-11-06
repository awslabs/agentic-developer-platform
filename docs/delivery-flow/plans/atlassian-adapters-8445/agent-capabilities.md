# Agent capabilities and autonomous execution

> **Current execution strategy (2026-10-25):** [Complete the remaining code, then qualify one release](batch-completion.md). It supersedes the per-story deployment sequence below. PR tests, review and CI remain required; all runtime acceptance moves to the combined final campaign. Earlier registration/status text below is historical.

This delivery revision implements the user's requirement: use the agents' actual
capabilities, deploy and verify every deliverable increment, and avoid routine
human stage decisions. It supersedes the two human evidence checkpoints in the
previous draft. It is a source-grounded design, not a claim of deployed capability.
Source inspected: `c8c42938888bcec577965d517acaeb7ff86d4914`.

## Who does the work

| Executor | Actual supported capability | Assignment and boundary |
|---|---|---|
| `agent-codex-developer` | Repository shell, implementation, relevant repository checks, commit/push and ready PR | Builds each existing story and its executable verification. Does not impersonate a deployment operator or merge without controller authority. |
| `agent-codex-reviewer` | Inspects the current PR, fixes in-scope defects, runs relevant checks and returns structured results | Existing controller publishes, waits for CI, verifies and merges. The reviewer does not perform historical acceptance or live deployment. No extra human source review after every successful repair. |
| Engine D2/D3 | Resolves reviewed source and registered target, observes/dispatches workflows, verifies runtime | Deploys changed components after merge; records actual images, assets, migrations and runtime observations. Passing Actions alone does not conclude deployment. |
| Engine `operations` story worker | `repair_tasks`, `repair_progress`, file read/write, repair PR publication/status/merge, Actions dispatch/read/logs/rerun/wait | Owns each explicit deployment through diagnosis, scoped repair, CI, merge, redeployment and runtime verification. Keeps the same issue and durable journal. Tests execute in CI/workflows; this worker has no general shell, browser or raw AWS/GitHub credentials. |
| Machine repository evidence reader | Checks declared merge/runtime receipts, workflow provenance and artifact predicates | Releases the next stage automatically after evidence passes. It neither runs browser/client tests nor repairs code. |
| Protected live workflow | Runs the installed client/browser/provider drivers implemented by the story | Produces the live observations. An independently implemented reader validates provider effects, runtime identity and cleanup. |

The selected operations executor is the existing **`operations` engine path**,
which exposes `actions_wait` (up to 30 seconds without another model turn) and
has a 60-turn default. This is distinct from the general operations persona.
The native `agent-codex-operations` path has repair tools but its inspected host
and gateway session cap it at 20 turns, and its host tool list omits
`actions_wait`. Do not assume the other runtime's limits/tools apply to it.
Using the existing engine path avoids introducing a worker-runtime migration
just to deliver these connectors. Check the installed path before activation.

Evidence sources:

- [Developer](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/agent-factory/rules/personas/agent-codex-developer.md), [reviewer](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/agent-factory/rules/personas/agent-codex-reviewer.md), and [operations story](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/agent-factory/rules/personas/operations-story.md) instructions.
- [Operations tool definitions](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/agent-factory/agent/src/operations-story-tools.ts) and [worker entry/limits](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/agent-factory/agent/src/operations-story-entry.ts).
- [Native Codex tools](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/agent-factory/codex-harness/src/github-tools.ts), [host limits](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/agent-factory/codex-harness/src/github-entry.mjs), and [gateway session limits](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/gateway/src/agentauth/codex_github_session.py).
- [D2 workflow service](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/gateway/src/orchestration/deployment_workflows.py), [D3 runtime reader/controller](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/gateway/src/orchestration/deployment_controller.py), and [verified repair revision chain](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/gateway/src/orchestration/operations_repair.py).

## Test setup is implementation work

The [test-data strategy](test-data-strategy.md) records what the source actually
provides: private Jira/Slack fixture hooks and synthetic tests, not verified live
accounts or a prepared dataset. Agents build generators and cleanup as part of
the existing stories. Stages 1–7 use controlled integration and actual deployment
readback; provider-dependent activation/final clauses retain separate live proof.
Missing provider data does not stop independent implementation or require people
to prepare fixtures. Actual model/client access is also discovered, not assumed.

## Authority the agents actually need

Accept one scoped dev execution policy: develop, review, repair, merge, operate,
deploy and evaluate; no routine `human_gates`; every evaluation explicitly machine.
Registration still creates its one initial human acceptance gate. Require the
read-only preview to show **only that gate**; `ORCHESTRATION_AUTONOMY_GATE_EVERY_WAVE`
must not introduce later gates. Existing consent and external policy decisions
retain their real owners; agents cannot grant themselves provider consent.

The previous draft listed `repair` but omitted the policy's exact `repair_paths`
and `repair_required_checks`. That would refuse operations edits/merges. The
revised draft includes a bounded file list and the existing Tools CI `verify`
check. Before acceptance, reconcile that list against the final implementation
file layout, including new adapter files, and qualify CI trigger coverage for
**every allowed file**. The policy supports exact files, not directory grants.
#8446 qualifies Tools CI trigger coverage and initial repair scope before the first
operations deployment; #8447 extends package checks for the adapter surface; normal repository protections and other required checks still apply.
Never add an unrelated always-green check to satisfy this requirement.

The repair gateway also prohibits approval/credential/policy files and changes
to workflow triggers, permissions and credential-bearing inputs. Workflow repair
requires that workflow in the node's allowlist. Product authorization redesign
belongs to the developer/reviewer lifecycle; an operations prompt cannot bypass
these restrictions. Repair file reads/writes are bounded (32 KiB); a large launcher or configuration file is not automatically editable just because its path is listed. Reconcile the final file layout and qualify the actual repair call before admission; keep focused implementation helpers within the supported surface. Broad new scope remains an exception, not an automatic repair.

Private activation must supply both the registered environment connection and
`user_credentials` authority (credential IDs, exact role ARNs, authorized actions).
An AWS profile on the coordinator's box is not authority delegated to a hosted
agent. Verify `ADP_ENGINE_OPERATIONS_EXECUTOR`, protected authority and report
transport in gateway, scheduled engine and worker; check actual repository
installation permissions and branch/environment protections.

[Actions admission](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/gateway/src/agentauth/actions_routes.py)
already admits reviewed dev defaults for `gateway-deploy.yml` and
`gateway-frontend-deploy.yml` despite their optional approval input. Other
approval-bearing workflows remain restricted. Do not introduce repeated user
approval by treating the mere presence of that input as a dev requirement.
Custom worker workflow inputs are currently refused. The tools-live workflow
needs `gateway_revision` and an exact scenario selection, so simply allowlisting
its filename does **not** make it executable.

## Small missing integrations, with existing owners

| Gap found in the real path | Work and owner |
|---|---|
| No admitted tools-proof/stage invocation with bound nondefault inputs | #8446 builds the isolated Identity proof, initial repair/CI coverage and bounded controller dispatch/readback needed to execute it after its reviewed merge. Keep the three existing proof ACs; do not create a new generic test catalogue. |
| Adapter runtime component/reader is absent; foundation cannot wait for #8453 | #8447 brings forward minimum adapter release/receipt support and stage handoff. Explicit core bootstrap deployment installs it before the first adapter operation. #8453 retains coordinated migration/rollback ownership. |
| Stage execution and operations repairs need compatible dispatch/evidence/CI coverage | #8447 qualifies the existing operations repair tools, accepted file/check scope, exact revision continuation and stage workflow invocation. The later stories extend their existing tests and executable acceptance procedures. |
| Final operations could pass on runtime health while the live suite fails | #8453 keeps the final release owner active through full qualification; binds the workflow result to the release receipt. Existing repair tools and `repair_revisions` advance source after a checked repair. Do not conclude the owner and then invent a way to reopen passed nodes. |
| A live repair may affect more than the final routing component | #8453 integrates changed-component reconciliation through the existing release orchestration, under the accepted workflow/target set. Verify all affected runtimes at the repaired revision before rerunning tests. One component's receipt cannot cover the rest. |
| Runner/reader and long expiry/cleanup continuation are incomplete | #8454 implements the already specified installed-client/provider checks, complete AC mapping, durable continuation and cleanup in the existing tools workflow. |
| Machine evidence does not by itself close GitHub issues | #8454 supplies an idempotent completion consumer using existing orchestration/issue integration. Close only exact current requirements with complete evidence after final evaluation passes; retain closure progress and surface concurrent requirement changes. |

These integrations are planned implementation. Current schema validation does
not prove them deployed. A bootstrap source defect still follows reviewed repair;
there is no permission to bypass failed checks to install the bootstrap. Qualify
the controller/worker path before allowing it to own unattended dependent work.
Preserve the accepted verifier digest across upgrades. If a change requires a
new verifier contract, resolve that dependency before the autonomous run; a plan
that needs repeated human digest amendments does not meet this design.

## Per-stage handoff and recovery

Each stage names an existing story, its actual actor, affected deployable artifacts,
and the existing AC/scenario procedures due at that phase. A developer supplies
missing executable checks as part of that story. CI tests source behavior; D3
verifies the deployed artifact; the workflow executes the controlled phase
procedure and any separately eligible live procedure. Its report identifies which evidence it actually collected.
No arbitrary number of tests, invented scenario IDs or prose-only pass is accepted.

The bootstrap builds (#8446/#8447) have explicit operations successors. All later
development nodes omit `code_only`: the existing policy-driven lifecycle must
complete deployment and runtime verification before their stage evaluation.
Unchanged components retain independently verified baseline receipts. Source
selection/lease checks prevent concurrent releases from invalidating another
stage's evidence. New behavior runs through isolated candidate runtimes with
generated data; active connector routing moves together only after the complete harness is ready.

The [parallel execution contract](README.md#parallel-execution) removes the
reads/UI join and splits writes/client evidence. The graph has a maximum of three
independent story actions. The source's
[`_running_count`](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/gateway/src/orchestration/policy_admission.py)
counts running nodes and excludes engine machine evaluations;
[`review_cycle_dispatch`](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/modules/gateway/src/orchestration/review_cycle_dispatch.py)
reauthorizes review without counting the same running node again. These facts
justify a proposed limit of three; they do not establish available worker capacity.

Existing deploy workflow concurrency groups differ across components. They do
not by themselves hold a target-wide lease through deployment and phase readback.
#8447 owns the minimum cross-workflow lease, isolated phase resources and automatic
queued handoff before the feature fork. Qualification must run competing release
requests, demonstrate no overlap in the shared critical section, and recover a
lost owner using fenced lease generations. Keep actual workflow runs serialized
without dropping pending phase work; GitHub Actions concurrency alone is not a
durable queue. #8453 extends the same mechanism for group routing and rollback.
#8454 adds capability-aware concurrent checks with independent fixture ownership;
operations still dispatches the protected workflow, not its own shell or browser.

#8453 and #8454 have both construction and live obligations. Their early stage
reports mark only the construction/deployment obligations due then as passed;
remaining AC clauses stay pending until the coordinated release/full live run.
Each stage report carries the exact issue/AC/scenario keys, requirement revision,
phase, executed command, actor, installed artifact, expected/observed behavior,
and evidence reference. `phase_status: pass` never means a whole AC is complete.
The final report requires all clauses of all original ACs and EC-01–06.

Operations starts from `repair_tasks`, reads current logs and receipts, repairs
within accepted files, publishes, waits for real CI, merges the exact checked head,
observes matching automatic deployment before dispatch, then re-verifies runtime.
An unchanged deterministic failure requires diagnosis/fix. Lost acknowledgement
requires reconciliation. Unknown provider effects retain their operation IDs and
must be settled before another mutation. The final owner repeats this same loop
for in-scope live failures; it does not ask the user to accept a failed stage.

Before unattended execution, demonstrate this **actual actor path** with a bounded
in-scope defect: accepted assignment -> operations tools -> repair PR -> required
CI -> verified merge -> changed source pins -> deployed runtime -> live workflow
result -> automatic successor. Derive the checks from #8453 AC-02/04 and #8454
AC-04/05/06. Repeat observation after worker restart to verify retained tasks.
This qualification establishes capability; another report field cannot substitute.

Stop the affected action for revoked/missing authority, an unsupported product
decision, an out-of-scope repair, explicit user stop or exhausted accepted bounds.
Missing provider consent/data holds only dependent live checks/activation; record
it once and continue independent work. Do not ask people to create test datasets.
Retain all progress. Queued CI, normal deployment,
recoverable failure and final evidence assembly are agent work, not user gates.
