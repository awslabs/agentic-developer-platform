# Delivering an epic with the ADP delivery flow

One page for the whole path: from a set of GitHub stories to an accepted, running
engine flow, and what to do while it runs. It consolidates the CLI reference
([`modules/gateway/cli/flow.md`](../../modules/gateway/cli/flow.md)), the engine's
structural rules (`modules/gateway/src/orchestration/proposal.py`), the plan-document
convention under [`docs/delivery-flow/plans/`](plans/), the issue rules in
[`docs/orchestration-issue-guide.md`](../orchestration-issue-guide.md) and the AIDLC
skill (`modules/agent-factory/skills/aidlc-emit-issues/SKILL.md`). Those remain the
sources of truth; this page is the map. Worked example throughout: epic #7197, flow
`cos-runtime-7197` ([`docs/delivery-flow/plans/cos-runtime-7197/`](plans/cos-runtime-7197/)).

## 0. Two ways in

| Route | Use when | Command |
|---|---|---|
| **Author a plan document** | The stories already exist and you know the dependency graph | `adp flow create --file plan.json` |
| **Start from an outcome** | You have an intent or an epic issue and want the planning agent to refine it into a plan | `adp flow start --repo OWNER/NAME --issue N --plan` |

Both end at the same place: an **inert draft** whose acceptance gate you answer. Nothing
runs before that answer. The rest of this page follows the first route; the second
reaches step 4 with the plan the agent derived.

### Plan directory for skill-authored flows

When a planning skill builds the delivery plan, keep its reviewable artifacts
under `docs/delivery-flow/plans/<slug>/` in the delivery home repository.
Use `<home-repo-short>-<epic-number>` for the slug; append `-r2` for a second
registration rather than overwriting the first plan. A hand-authored plan may
still use any local JSON file with `adp flow create`.

| File | Written during | Purpose |
|---|---|---|
| `intent.md` | Intent confirmation | Records the agreed intent before issue creation. |
| `story-mapping.json` | Story creation | Records each story alias, repository, issue number, node id, URL, creation time and native-link status for resumable publication. |
| `README.md` | Plan composition | Explains waves, repositories, gates, submission and the proposed approval command. |
| `proposal.authored.json` | Plan composition | Holds the machine-readable Product-bound plan, repository-bound stories and proposed execution policy. |
| `policy-intent.json` | Plan composition | Records the proposed policy, evaluation issues and unresolved bindings (which must be empty); `status` is `proposed_not_authorized`. |
| `submission.json` | Inert registration | Records `flow_id`, `plan_version`, `plan_hash`, `acceptance_gate_id` and `status: registered_inert_not_accepted`. |

The authored proposal is a request for review, not execution authority. The
submission receipt confirms an inert draft, not approval or dispatch; the
owner's acceptance gate remains a separate decision. Hosted AIDLC Step 7e
also writes `aidlc/spaces/issue-<N>/construction/loop-proposal/proposal.json`
for worker registration; that artifact is not the skill's delivery-home plan
directory and existing committed copies are not rewritten.

## 1. Stories first

The engine delivers **issues**. Before any plan:

- One epic as a lean index, children as native sub-issues
  (`gh api graphql … addSubIssue`). Children follow the
  [developer issue template](../../modules/agent-factory/rules/templates/developer-issue.md)
  and [authoring guide](../../modules/agent-factory/rules/agents/issue-authoring.md):
  six sections, one acceptance table with stable IDs, ≤ 8 KB, verified file:line
  starting points, prerequisites separated into verified / decision / investigation.
- **Medium or smaller.** A story that bundles two deliverables with different owners or
  tests is two stories (the #1 failure in the issue guide). Example: #7200 was split into
  #7200 (proxy + switch) and #7208 (turn runner).
- Every story names its completion boundary (reviewed / merged / live-verified) and
  uses `Refs #N`, not closing keywords, when live acceptance belongs to the issue.

**Engineering Insights project binding is a target, not a current plan option.**
The [project registry (#5411 §11)](https://github.com/aws-e/adp/issues/5411)
will mint the stable `project_id` and bind each delivery flow to one registered
project (EI-S03); the current examples use `org_id` and do not establish that
binding. Planned typed node `lineage` replaces issue-body lineage comments
(EI-S02), while the emitter reads the resulting project-scoped records (EI-S04).
Do not add unsupported fields to a current proposal or infer project access
from an issue comment. The [Insights delivery architecture](../engineering-insights/architecture.md)
separates those planned steps from today's engine behavior.

## 2. Author the plan document

A JSON document (see `docs/delivery-flow/plans/cos-runtime-7197/proposal.authored.json`):

```jsonc
{
  "flow_slug": "cos-runtime-7197",            // first address segment
  "title": "…", "org_id": "aws-e", "intent_ref": "7197",
  "spec_revision": "<git sha the plan was authored against>",
  "description": "INERT DRAFT … proposed limits are not authorization …",
  "nodes": [
    { "address": "cos-runtime-7197/epic-7197/runtime/crew-pod-class",
      "kind": "story", "title": "…", "issue_ref": "7198",
      "lineage": { "story_id": "SAMPLE-42", "size": "M", "type": "feature",
                   "risk_class": "low", "team_id": "sample-team", "cohort": "pilot" },
      "labels": { "source": "tracker", "component": "runtime" },
      "executor": { "schema_version": 1, "kind": "agent", "role": "develop",
                    "persona": "agent-codex-developer" } },
    { "address": "cos-runtime-7197/epic-7197/runtime/runtime-evidence",
      "kind": "eval", "title": "…",
      "evaluation": { "schema_version": 1, "acceptance_mode": "human",
                      "evidence_schema": "orchestration-evaluation/v1",
                      "criteria": [{ "criterion_id": "EP-01", "required": true, "kind": "functional" }],
                      "max_duration_seconds": 7200, "max_age_seconds": 3600 } },
    { "address": "cos-runtime-7197/epic-7197/dashboard/live-acceptance",
      "kind": "gate", "title": "…" }
  ],
  "edges": [ { "from_address": "…/crew-pod-class", "to_address": "…/run-binding" } ],
  "epic_metadata": [ { "epic_ref": "epic-7197", "epic_id": "SAMPLE-4", "title": "…", "description": "…" } ],
  "wave_metadata": [ { "epic_ref": "epic-7197", "wave_ref": "runtime", "title": "…", "description": "…" } ],
  "proposed_execution_policy": {
    "schema_version": 1, "org_id": "aws-e", "team_ids": [], "repository_ids": ["aws-e/adp"],
    "environment_connection_ids": [], "allowed_actions": ["develop", "review", "repair", "merge"],
    "human_gates": [], "evaluation_acceptance": { "…/runtime-evidence": "human" },
    "expires_at": "2026-10-22T11:11:14Z",
    "limits": { "max_spend_usd": "1500", "max_wall_clock_seconds": 604800,
                "max_attempts_per_node": 3, "max_concurrent_actions": 2 }
  }
}
```

Rules the engine enforces (`proposal.py`; the CLI reports **all** violations at once):

| Rule | Meaning |
|---|---|
| `address_form`, `duplicate_address`, `flow_segment_mismatch` | Every address is `flow/epic/wave/node`, unique, first segment = `flow_slug` |
| `unknown_kind`, `container_as_node` | Node kinds are only `story`, `eval`, `gate`; epics and waves are address segments, not nodes |
| `lineage_invalid`, `labels_too_many`, `label_key_unknown`, `labels_invalid` | Optional metadata has typed, bounded lineage, at most 16 labels, and only controlled label keys |
| `dangling_edge`, `self_edge`, `cycle` | Edges resolve to declared nodes and form a DAG |
| `wave_eval_cardinality` | **Every wave that contains stories has exactly one `eval` node** |
| `unordered_same_issue` | Two nodes on one issue must be ordered |
| `missing_org_id`, `missing_spec_revision` | Both present and meaningful |
| `unknown_*_metadata`, `duplicate_*_metadata` | Display metadata once per epic/wave actually present |
| `evaluation_*` | Machine-accepted evals need an evidence specification bound to the installed verifier; human acceptance does not |

`lineage` on a node may contain only `story_id`, `size`, `type`, `risk_class`,
`fixes_story_id`, `team_id`, and `cohort`; each supplied value is a non-blank
string of at most 128 characters. `epic_metadata` may also contain `epic_id`.
`labels` is a map of up to 16 entries. Keys must come from
[`lineage_vocabulary.yaml`](../../modules/gateway/src/orchestration/lineage_vocabulary.yaml)
and be at most 32 characters; non-blank values are at most 128 characters.
All these fields are optional. The initial preview reports invalid bounds or
unknown keys as named violations; malformed lineage shapes instead produce a
schema error (`lineage_invalid`, HTTP 422). Draft revision preview and revise
use the same model and rules. Accepted values are stored on the node row and
returned on preview and in the nodes of `GET /orchestration/flows/{flow_id}`;
changing them changes the plan hash without replacing the acceptance gate.
Labels carry context only: engine policy and authorization do not consult them.
Node chips and worker-prompt projection are separate follow-up work, not part
of this metadata contract.

Story proposals may also include optional `file_groups` for the graph's expandable
**Planned files** panel. Each group has `group`, optional `purpose`, and arrays
`create`, `modify`, and `modify_after` (entries with `path` and `created_by`). Use
literal repository-relative file paths. Group names and paths must be unique
within a node; the limits are 16 groups, 128 files per array and 256 files total.
These groups are stored in the versioned plan and returned in draft previews and
graph reads. Omitting them preserves existing plan hashes and card layouts.
They describe intended changes, confer no execution/repair authority, and may be
amended without resetting completed work. See the
[schema](../../modules/gateway/src/orchestration/node_files.py) for exact bounds.

Do **not** declare the plan-acceptance gate: the engine inserts it at
`<flow>/<epic>/<first wave>/accept` during registration, and an explicit one collides
(`duplicate_address` + `self_edge`). Declare only your own gates (e.g. live acceptance).

Executors: code stories may propose `agent-codex-developer` with role `develop`;
review, repair and merge use the existing controller. With
`ADP_ENGINE_OPERATIONS_EXECUTOR=true`, an operational **story** may instead use
`{ "kind": "agent", "role": "operate", "persona": "operations" }` (or persona
`agent-codex-operations`). Give that node `evidence_expectations`, for example
`["runtime_verified:tools-facade"]`, and name each permitted workflow in
`required_inputs` as `deployment_workflow:<filename>.yml`. Pin the exact source
and workflow-definition commit SHAs and dispatch branch in the accepted node as
`deployment_source_revision:<40 lowercase hex SHA>`,
`deployment_definition_revision:<40 lowercase hex SHA>` and
`deployment_branch:<branch>`. The accepted policy must allow `operate`; the
protected worker authority and shared run reporting must be enabled. Otherwise
an operations dispatch holds rather than acquiring workflow write permission.
Set webhook-ingress `engine_operations_executor_enabled = true` to propagate the
flag to the gateway, new worker Jobs and the scheduled engine through runtime
wiring. Apply webhook-ingress configuration and the gateway engine configuration
together; verify all three consumers. This setting is retained across release
upgrades. Enabling it does not accept a plan, resume a flow or release a gate.
The flag defaults off: existing developer stories retain their merge-only rule,
and evaluation and gate nodes cannot acquire an `operate` executor. Evaluations
default to human acceptance; `machine` requires an accepted evidence specification.
A draft may declare operations assignments and their workflow inputs before the
flag is enabled. Saving or reading that draft does not enable dispatch. For a
future release that does not exist at acceptance time, use
`operational_release_binding` on the operate story **instead of** the three
literal revision inputs. For example, alongside
`required_inputs` for `deployment_workflow:gateway-deploy.yml` and
`node:example-flow/epic-1/wave-1/build-1`, and
`evidence_expectations: ["runtime_verified:gateway-backend"]`:

```json
{
  "operational_release_binding": {
    "predecessors": [{"address": "example-flow/epic-1/wave-1/build-1", "receipt_kind": "verified_merge"}],
    "build_prerequisites": ["example-flow/epic-1/wave-1/build-1"],
    "repository": "example/service",
    "target": "example-dev",
    "component": "gateway-backend",
    "workflows": ["gateway-deploy.yml"],
    "default_branch": "main"
  }
}
```

Every predecessor must be an accepted story and a required `node:` input in
this tenant and flow. `verified_merge` requires an engine-verified merge receipt;
`verified_operations_release` requires a completed operations agent, its accepted
runtime receipt, and the matching correlated GitHub Actions evidence. The latter
retains a deployed repair as source lineage for a subsequent operations story.
It does not establish current runtime health or replace that story's verification.
Build prerequisites must name `verified_merge` predecessors whose merges the source contains. The repository must
match the node and accepted policy; the target names an enabled private manifest
entry covered by the accepted connection and component. The workflow must also
be a required `deployment_workflow:` input. If a predecessor changes the
workflow definition, explicitly name its address as
`definition_predecessor`; otherwise the resolved workflow must be byte-identical
to the accepted definition. The engine records the exact receipts, immutable
revisions, target and workflow before dispatch and holds missing or ambiguous
evidence rather than switching to latest main. A `node:` edge alone never grants
release provenance. Keep literal pins for releases already known at acceptance.
These authoring options do not activate an unqualified target: D01 still owns
connection, transport, runtime-readback and live verification.
A plan **without** a policy is UNBOUNDED, not restricted — always include
`proposed_execution_policy`.

## 3. Validate locally

```sh
python -m venv /tmp/adp-plan-venv && /tmp/adp-plan-venv/bin/pip install pydantic 'sqlalchemy[asyncio]' PyYAML
/tmp/adp-plan-venv/bin/python .github/scripts/validate_loop_proposal.py docs/delivery-flow/plans/<slug>/proposal.authored.json
# OK   … — 16 node(s), 31 edge(s), spec revision <sha>
```

This proves graph structure only; server readiness, personas and evaluation adapters
are checked at registration.

## 4. Register as an inert draft

```sh
adp flow create --file plan.json --reason "Owner-requested inert draft for epic #N; not execution approval" --json
```

Four steps, only the last arms anything: dry-run → **register inert** → read the
effective graph back → accept. Without a terminal (or with `--json`) it stops after the
read-back and exits **4** with the flow id, gate id and `plan_hash`. Re-running with the
identical document registers no second flow. Read the preview carefully: it shows the
gates ADP inserted, which waves run concurrently (same stage = concurrent), who may mark
each node complete (human / merged PR / machine), and the bounds accepting would grant.

Commit the document next to its precedents: `docs/delivery-flow/plans/<slug>/README.md`
(sequence, bounds, unresolved bindings), `proposal.authored.json`, `policy-intent.json`,
`submission.json` (flow id, hash, gate). Record the flow id on the epic.

## 5. Revise before accepting (optional)

```sh
adp flow plans FLOW_ID                                   # current version + hash
adp flow draft preview FLOW_ID --file proposal.json --expect-plan-version 1 --expect-plan-hash BASE --json
adp flow draft save    FLOW_ID --file proposal.json --expect-plan-version 1 --expect-plan-hash BASE --expect-proposal-hash PREVIEW --json
```

Omit the server-inserted acceptance gate in revisions too. Drafts cannot be revised after
any approval or execution; accepted plans use the amendment path.

## 6. Accept — the owner's decision

```sh
adp flow plans FLOW_ID                                   # read what you are about to grant
adp flow gate approve GATE_ID --expect-plan-hash HASH --yes --reason "…"
```

Accepting the gate **grants the proposed policy** (repositories, actions, limits, expiry)
for that exact revision. "Accepted" and "running" are different claims: the CLI tells you
whether the engine has taken the work up, is WAITING on you, or has not yet admitted it —
do not re-approve, `watch` it. Needs a human account with `PLAN_APPROVE`.

## 7. While it runs

```sh
adp flow show FLOW_ID        # progress, blockers, outstanding gates, next eligible work
adp flow watch FLOW_ID       # follow; exiting detaches, hosted work continues
adp flow decisions FLOW_ID   # attributed approvals and transitions
adp flow cost FLOW_ID        # known / none_incurred / unknown — never a fake $0.00
adp flow gate approve|reject GATE_ID --expect-plan-hash HASH
```

- Code stories complete on an **observed merged PR**. Flagged operational stories
  with accepted `runtime_verified:<component>` expectations complete only after
  the gateway has attached a bounded, authenticated operations report **and** the
  engine has observed a D3 runtime receipt for that component. A successful
  correlated workflow run with a verified context artifact establishes workflow
  provenance, not runtime health. Agent prose or workflow success alone
  holds with a typed reason (`evidence_missing`, `operations_report_missing` or
  `operations_assignment_changed`); passed decisions record the report and
  observed evidence IDs. Evaluations still need human acceptance (or a machine
  specification); gates need a human answer. Neither a merged PR nor an
  operational-story result substitutes for live deployment qualification.
- An operational story without a PR can obtain runtime evidence only when its
  accepted policy permits deployment verification and the reviewed manifest
  names the exact workflow, revision, component and registered target. The
  gateway re-reads the run and release artifact and verifies actual runtime
  state through that connection; the worker receives neither the role nor its
  credentials. The Actions run response includes a receipt ID or bounded hold reason.
- Flagged operations dispatches persist the parent node and open diagnosis, repair,
  test, publication, deployment and verification tasks in the execution ledger.
  `repair_tasks` reloads earlier attempts and repair cycles after a restart;
  `repair_progress` records a bounded stage and diagnosis code. The gateway records
  observed PR heads/bases and automated-check status, merges and deployment receipt
  references beside these tasks. Task progress is not evidence of runtime success.
- **Budget exhausted** shows as `budget_reservations_pending` / "no headroom for a further
  action". Raise it through the budget revision path (platform admin); do not pour more
  budget into one node without reading its decisions first.
- **Code-only flows do not authorize deployment dispatch.** A flagged `operate`
  story can dispatch or re-run only its accepted workflows through the gateway;
  it cannot dispatch any workflow containing the human-approval input
  `adp_approved_revision`, and it receives no AWS deployment role. Code-only
  policy does not suppress push-triggered workflows. Protected components remain
  guarded by `scripts/check-assistant-deploy-boundary.sh`; deployment-boundary
  approval and live-acceptance gates remain human decisions.

### Budget visibility in the Delivery Flow UI

Open a flow's **Overview** to see its effective flow budget, recorded model spend,
outstanding reservations, remaining balance, per-run limit, concurrency and
enforcement status. While a plan is registered but not yet accepted, the same
panel shows the **proposed** limits (flow budget, concurrency, attempts per node,
wall clock, expiry) with the plan version and hash, labelled "awaiting
acceptance": nothing is in force or enforceable until a human accepts, and the
balance stays unknown rather than borrowing the proposed figure. Approved budget amendments and tighter tenant run limits are
included. Each story's expandable **Run budgets** table links to its recent agent
runs and shows their individual spend, reservations and remaining balances.

Recorded spend comes from the usage ledger. Reserved amounts are the live model
request amounts that are not yet covered by recorded cost. Remaining subtracts
the larger of the live meter and recorded spend plus reservations from the limit,
with a minimum of zero. These figures cover model usage, excluding build and
infrastructure costs. They do not include the next request's estimate or replace
the engine's admission decision; other budget scopes can still limit a request.

Missing or expired counters leave balances **Unavailable**, retaining any recorded
spend. An unavailable budget read does not prevent the graph from loading. The
Overview refreshes once a minute while visible; story balances load only when
expanded, show up to 20 recently active runs, and use the current limits. A run
appears after its first recorded model call.

The read-only API endpoints are `GET /orchestration/flows/{flow_id}/budget` and
`GET /orchestration/flows/{flow_id}/nodes/{node_id}/budget`. Both require the
flow graph's tenant-scoped `USAGE_READ` permission. Reading them never resets,
reconciles or changes budgets or execution.

## 7a. Thinking about evaluations

An evaluation is how a wave *concludes*; without one the loop delivers work and never
assesses it (hence the one-eval-per-wave rule). Guidance is spread across
[`evaluation-template.md`](../../modules/agent-factory/rules/templates/delivery-loop/evaluation-template.md),
[`issue-authoring.md`](../../modules/agent-factory/rules/agents/issue-authoring.md),
[`evaluation-evidence.md`](../architecture/evaluation-evidence.md) and
[`doc/ai-dlc-engine/README.md`](../../doc/ai-dlc-engine/README.md); the model they add up to:

```
Story AC rows  ──derive──▶  Wave evaluation issue (run-only, deterministic, one per wave)
(review-phase + live-phase)      │  command + expected output per check; defect protocol
                                 ▼
                      eval node (acceptance: human | machine)
                        human   = a person accepts recorded evidence
                        machine = pinned harness + target + connection + evidence receipt
                                 │
                      gate (live acceptance) — records authorization, never deploys
```

1. **Deterministic and derived.** Every check traces to a story's acceptance row or an
   intent invariant and is written as *command → expected output*. "Verify it works" is
   not a check. Expected values come from an independent contract, never from the
   observation itself; ask what plausible wrong result would still pass.
2. **Separate repository evidence from live evidence.** Under a code-only policy nothing
   is deployed while the flow runs, so a wave's eval can only accept *repository*
   evidence (merged PRs, CI, container runs). Live outcomes belong to a later
   qualification eval behind a readiness gate — the `adp-assistant-6929` shape, reused
   by `cos-runtime-7197` (#7214–#7216 per wave, #7217 live).
3. **Evidence is bound** to revision, target, run and attempt. A green workflow with a
   missing mandatory case cannot pass; mocks never establish a live claim.
4. **Merge ≠ deployed ≠ evaluated.** A story node passes at verified merge. The evaluation
   controller reads direct predecessors only, so put explicit edges from every story the
   eval accepts, and from the eval to the next wave's first story when the barrier matters.
5. **Partial is partial.** Skipped, blocked or not-run required checks keep the evaluation
   open; it closes only on a single all-pass run.
6. **Failures take the defect protocol** — new defect issue, native child of the epic,
   developer dispatched on the defect — never an edit to the evaluation.
7. **Machine acceptance is earned.** It needs a supported producer/adapter (today: the
   orchestration harness on EKS targets, repository scans, the knowledge CLI suite) and a
   bound evidence specification; otherwise declare `human` honestly.
8. **Give every eval node an `issue_ref`** to a run-only evaluation issue; the issue is
   where the checks and the report live, the node is where acceptance is recorded.

## 8. Checklist

1. Stories: medium or smaller, template-conformant, ≤ 8 KB, native sub-issues, `Refs`.
2. Plan: one `eval` per wave with stories, each with an `issue_ref` to a run-only evaluation
   issue; live outcomes in a separate qualification eval behind a readiness gate; no explicit
   accept gate; `spec_revision` set; `proposed_execution_policy` present with explicit `expires_at`.
3. `validate_loop_proposal.py` → OK.
4. `adp flow create --file … --json` → exit 4, flow id + hash; commit under `docs/delivery-flow/plans/<slug>/`.
5. Read `adp flow plans`; approve the gate with `--expect-plan-hash`.
6. Watch; answer gates; merge ≠ deploy.

## See also

- [DevLake POC delivery flow](../engineering-insights/devlake-poc/delivery-flow/README.md) — canonical plan alongside its Engineering Insights architecture and story designs.
- [`modules/gateway/cli/flow.md`](../../modules/gateway/cli/flow.md) — full CLI semantics, exit codes, cost model.
- [`docs/orchestration/glossary.md`](../orchestration/glossary.md) — terms (flow, wave, node, gate, lease).
- [`docs/orchestration-issue-guide.md`](../orchestration-issue-guide.md) — issue sizing and structure.
- [Engineering Insights delivery architecture](../engineering-insights/architecture.md) — project identity, producer-to-scorecard lineage and policy-gated write-back.
- [`docs/delivery-flow/plans/`](plans/) — precedents: `adp-assistant-6929`, `claude-desktop-6850`, `cos-runtime-7197`.
- `modules/agent-factory/skills/aidlc-emit-issues/SKILL.md` — the AIDLC-native, agent-authored route.

## Story checkpoint lineage

For a dispatched story, the Codex controller commits
`.adp/tasks/<issue-number>.json` to the story branch at each checkpoint. New
version-2 boards track task start and finish, originating run and attempt, a
bounded credential-masked note, and an optional pattern identifier. The header
holds the flow ID and node's graph address from dispatch metadata; without a
node dispatch those fields stay null. The reviewer can still read older
version-1 boards without changing them. A committed board is checkpoint
evidence, not proof that a PR was merged or live acceptance succeeded.

### Operations deployment repairs

An operations story can own diagnosis, repair, CI checks, merge and deployment verification in the same run. It uses the existing workspace and gateway-mediated PR operations; it does not dispatch a developer or reviewer. The accepted execution policy must permit `operate`, `repair` and `merge`. A schema-v3 policy may authorize either exact repository-relative `repair_paths` or `repair_scope: repository`. Repository scope permits needed implementation files within the already accepted repositories without making a planning file list mandatory. These alternatives cannot be combined; omitting both continues to permit no edits, including for existing accepted policies. Changing scope requires an accepted policy amendment.

Before changing a local workspace file, the worker asks the gateway to validate the edit against the current claim, accepted repository and repair scope. The gateway checks the same scope again before publication or merge. Required CI and branch rules remain enforced. Workflow edits must still name one of the story's allowed deployment workflows; approval and credential policy files are not deployment repairs. The flow's policy summary displays whether repair authority covers the repositories or a specified number of files. Optional `file_groups` remain a separate human guide and grant no authority.

The worker exposes `repair_files`, `repair_read`, `repair_write`, `repair_publish`, `repair_status` and `repair_merge`. Publication starts the repository's existing CI. The accepted schema-v3 policy must name nonempty `repair_required_checks` for a merge. The gateway checks those exact successful check-run names at the PR head, refuses a moved branch or base, and rechecks immediately before merge. Workflow repairs cannot change triggers, permissions, job authority or credential-bearing step inputs while updating an allowed step. A failed check returns to the repair loop. Successful merge receipts persist in the existing execution journal and advance the revision used for deployment evidence. Matching automatic push deployments are observed before another dispatch. Runtime evidence, rather than a merged PR, still determines story completion. Existing budget and time limits apply throughout.
